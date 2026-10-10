#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from shared import ROOT
from shared.data_loader import ManualDataLoader
from shared.http import JsonSource

OPENALEX_DATASET = "subugoe-collaborative.openalex"
DEFAULT_WORK_TYPES = ("article", "review", "preprint")


@dataclass(frozen=True)
class CandidateInstitution:
    university_id: int
    canonical_name: str
    openalex_short_id: str

    @property
    def openalex_uri(self) -> str:
        return normalize_openalex_uri(self.openalex_short_id, "I")


def normalize_openalex_uri(raw_value: str, expected_prefix: str) -> str:
    value = raw_value.strip()
    if value.startswith("https://openalex.org/"):
        return value
    if not value.startswith(expected_prefix):
        raise ValueError(f"Unexpected OpenAlex identifier {raw_value!r}")
    return f"https://openalex.org/{value}"


def short_openalex_id(uri: str, expected_prefix: str) -> str:
    value = uri.strip().rstrip("/")
    if value.startswith("https://openalex.org/"):
        value = value.removeprefix("https://openalex.org/")
    if expected_prefix == "I":
        value = value.rsplit("/", 1)[-1]
        if not value.startswith("I"):
            raise ValueError(f"Unexpected OpenAlex institution URI {uri!r}")
        return value
    if expected_prefix == "subfields":
        if not value.startswith("subfields/"):
            raise ValueError(f"Unexpected OpenAlex subfield URI {uri!r}")
        return value
    raise ValueError(f"Unsupported OpenAlex prefix {expected_prefix!r}")


def config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return result.stdout.strip() or None


def run_command(command: list[str]) -> str:
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def resolve_gcp_project(config: dict[str, Any], override: str | None) -> str:
    if override:
        return override

    configured = (
        config.get("sources", {}).get("openalex", {}).get("bigquery", {}).get("gcp_project")
    )
    if configured and configured != "your-gcp-project":
        return configured

    detected = run_command(["gcloud", "config", "get-value", "project"])
    if detected and detected != "(unset)":
        return detected

    raise RuntimeError(
        "No GCP billing project resolved. Pass --gcp-project or set the current gcloud project."
    )


def resolve_run_output_dir(config: dict[str, Any], override: Path | None, run_id: str) -> Path:
    base = override
    if base is None:
        configured = config.get("project", {}).get("output_dir")
        if not configured:
            raise RuntimeError("config.project.output_dir is required")
        base = ROOT / configured
    return base / run_id


def load_candidate_institutions(conn: sqlite3.Connection) -> list[CandidateInstitution]:
    rows = conn.execute(
        """
        SELECT
            u.university_id,
            u.canonical_name,
            ui.external_id AS openalex_id
        FROM universities u
        JOIN university_identifiers ui
          ON ui.university_id = u.university_id
         AND ui.system_code = 'OPENALEX'
        ORDER BY u.university_id
        """
    ).fetchall()
    return [
        CandidateInstitution(
            university_id=row["university_id"],
            canonical_name=row["canonical_name"],
            openalex_short_id=short_openalex_id(row["openalex_id"], "I"),
        )
        for row in rows
    ]


def resolve_cluster_cips(conn: sqlite3.Connection, config: dict[str, Any]) -> set[str]:
    resolved: set[str] = set()
    for cluster in config.get("field_clusters", []):
        rules = cluster.get("cip_rules", {})
        for cip_code in rules.get("include_6digit", []):
            resolved.add(cip_code)
        families = rules.get("include_2digit_families", [])
        if families:
            placeholders = ",".join("?" for _ in families)
            family_rows = conn.execute(
                f"""
                SELECT cip_code
                FROM cips
                WHERE cip_family_code IN ({placeholders})
                """,
                list(families),
            ).fetchall()
            resolved.update(row["cip_code"] for row in family_rows)
        for cip_code in rules.get("exclude_6digit", []):
            resolved.discard(cip_code)
    if not resolved:
        raise RuntimeError("No config-driven CIP targets resolved from field_clusters.")
    return resolved


def resolve_target_subfields(conn: sqlite3.Connection, config: dict[str, Any]) -> set[str]:
    cluster_cips = resolve_cluster_cips(conn, config)
    placeholders = ",".join("?" for _ in cluster_cips)
    mapping_rows = conn.execute(
        f"""
        SELECT DISTINCT subfield_id
        FROM cip_trains_subfield
        WHERE cip_code IN ({placeholders})
        """,
        sorted(cluster_cips),
    ).fetchall()
    if not mapping_rows:
        raise RuntimeError("No cip_trains_subfield mappings found for configured CIPs.")

    target_subfields = {
        normalize_openalex_uri(f"subfields/{row['subfield_id']}", "subfields")
        for row in mapping_rows
    }

    for cluster in config.get("field_clusters", []):
        oa_rules = cluster.get("oa_rules", {})
        for bucket in ("primary_subfields", "secondary_subfields", "adjacent_subfields"):
            for subfield_id in oa_rules.get(bucket, []):
                numeric_id = int(subfield_id)
                target_subfields.add(normalize_openalex_uri(f"subfields/{numeric_id}", "subfields"))

    return target_subfields


def sql_array_literal(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"


def build_query(
    *,
    candidate_ids: list[str],
    subfield_ids: list[str],
    start_year: int,
    end_year: int,
    counting_method: str,
) -> str:
    if counting_method not in {"primary_subfield", "topic_deduped_subfield"}:
        raise RuntimeError(f"Unsupported OpenAlex counting method in config: {counting_method}")

    script_parts = [
        f"DECLARE candidate_institution_ids ARRAY<STRING> DEFAULT {sql_array_literal(candidate_ids)};",
        f"DECLARE target_subfield_ids ARRAY<STRING> DEFAULT {sql_array_literal(subfield_ids)};",
        f"DECLARE work_types ARRAY<STRING> DEFAULT {sql_array_literal(list(DEFAULT_WORK_TYPES))};",
        f"DECLARE start_year INT64 DEFAULT {start_year};",
        f"DECLARE end_year INT64 DEFAULT {end_year};",
    ]

    if counting_method == "primary_subfield":
        script_parts.extend(
            [
                f"""
CREATE TEMP TABLE filtered_works AS
SELECT
  id,
  publication_year,
  citation_normalized_percentile.value AS citation_normalized_percentile_value,
  primary_topic.subfield.id AS subfield_id,
  authorships
FROM `{OPENALEX_DATASET}.works`
WHERE publication_year BETWEEN start_year AND end_year
  AND is_paratext = FALSE
  AND is_retracted = FALSE
  AND type IN UNNEST(work_types)
  AND primary_topic.subfield.id IN UNNEST(target_subfield_ids)
  AND EXISTS (
    SELECT 1
    FROM UNNEST(authorships) AS au
    CROSS JOIN UNNEST(au.institutions) AS inst
    WHERE inst.id IN UNNEST(candidate_institution_ids)
  );
""".strip(),
                """
CREATE TEMP TABLE primary_subfield_metrics AS
SELECT
  institution_openalex_id,
  subfield_id,
  year,
  'primary_subfield' AS counting_method,
  COUNT(DISTINCT work_id) AS publications_count,
  AVG(citation_normalized_percentile_value) AS avg_citation_impact
FROM (
  SELECT DISTINCT
    fw.id AS work_id,
    fw.publication_year AS year,
    inst.id AS institution_openalex_id,
    fw.subfield_id AS subfield_id,
    fw.citation_normalized_percentile_value
  FROM filtered_works fw
  CROSS JOIN UNNEST(fw.authorships) AS au
  CROSS JOIN UNNEST(au.institutions) AS inst
  WHERE inst.id IN UNNEST(candidate_institution_ids)
)
GROUP BY institution_openalex_id, subfield_id, year;
""".strip(),
            ]
        )
    else:
        script_parts.extend(
            [
                f"""
CREATE TEMP TABLE filtered_works AS
SELECT
  id,
  publication_year,
  citation_normalized_percentile.value AS citation_normalized_percentile_value,
  topics,
  authorships
FROM `{OPENALEX_DATASET}.works`
WHERE publication_year BETWEEN start_year AND end_year
  AND is_paratext = FALSE
  AND is_retracted = FALSE
  AND type IN UNNEST(work_types)
  AND EXISTS (
    SELECT 1
    FROM UNNEST(topics) AS topic
    WHERE topic.subfield.id IN UNNEST(target_subfield_ids)
  )
  AND EXISTS (
    SELECT 1
    FROM UNNEST(authorships) AS au
    CROSS JOIN UNNEST(au.institutions) AS inst
    WHERE inst.id IN UNNEST(candidate_institution_ids)
  );
""".strip(),
                """
CREATE TEMP TABLE topic_deduped_subfield_metrics AS
SELECT
  institution_openalex_id,
  subfield_id,
  year,
  'topic_deduped_subfield' AS counting_method,
  COUNT(DISTINCT work_id) AS publications_count,
  AVG(citation_normalized_percentile_value) AS avg_citation_impact
FROM (
  SELECT DISTINCT
    fw.id AS work_id,
    fw.publication_year AS year,
    inst.id AS institution_openalex_id,
    topic.subfield.id AS subfield_id,
    fw.citation_normalized_percentile_value
  FROM filtered_works fw
  CROSS JOIN UNNEST(fw.authorships) AS au
  CROSS JOIN UNNEST(au.institutions) AS inst
  CROSS JOIN UNNEST(fw.topics) AS topic
  WHERE inst.id IN UNNEST(candidate_institution_ids)
    AND topic.subfield.id IN UNNEST(target_subfield_ids)
)
GROUP BY institution_openalex_id, subfield_id, year;
""".strip(),
            ]
        )

    final_table = (
        "primary_subfield_metrics"
        if counting_method == "primary_subfield"
        else "topic_deduped_subfield_metrics"
    )
    script_parts.append(
        f"""
SELECT
  institution_openalex_id,
  subfield_id,
  year,
  counting_method,
  publications_count,
  avg_citation_impact
FROM {final_table};
""".strip()
    )
    return "\n\n".join(script_parts)


def gcloud_access_token() -> str:
    token = run_command(["gcloud", "auth", "print-access-token"])
    if not token:
        raise RuntimeError("Unable to obtain a gcloud access token.")
    return token


def bigquery_request(
    *,
    source: JsonSource,
    token: str,
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    retry: bool = True,
) -> dict[str, Any]:
    """One BigQuery REST call.

    The bearer token is per-request, so it goes in ``headers`` rather than on
    the source. ``JsonSourceHTTPError.body`` carries BigQuery's own explanation
    of a failure, which the message includes.

    Timeout comes from the adapter (``--request-timeout``, default 180 for this
    loader) rather than a literal here, so the flag actually takes effect.

    ``retry=False`` marks a call that must not be replayed — see
    ``submit_query_job``.
    """
    return source.fetch(
        url,
        payload=payload,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
        retry=retry,
    )


def submit_query_job(
    *,
    source: JsonSource,
    token: str,
    project_id: str,
    query: str,
    max_bytes_billed: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Submit a query job.

    A real (non-dry-run) jobs.insert is not idempotent: the request omits
    jobReference.jobId, so if BigQuery accepts the insert but the response is
    lost, a replay creates a second independent job. With useQueryCache off,
    that job scans and bills the full query while the manifest records only the
    job whose response came back. AGENTS.md 5 makes cost discipline mandatory,
    so this call gets a single attempt — matching the behaviour before the
    JsonSource seam existed. Dry runs are free and create no persisted job, so
    they keep the normal retry policy.
    """
    return bigquery_request(
        source=source,
        token=token,
        url=f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/jobs",
        method="POST",
        retry=dry_run,
        payload={
            "configuration": {
                "dryRun": dry_run,
                "query": {
                    "query": query,
                    "useLegacySql": False,
                    "maximumBytesBilled": str(max_bytes_billed),
                    "useQueryCache": False,
                },
            }
        },
    )


def wait_for_query_results(
    *,
    source: JsonSource,
    token: str,
    project_id: str,
    job_id: str,
    location: str,
) -> list[dict[str, Any]]:
    page_token: str | None = None
    schema_fields: list[dict[str, Any]] | None = None
    rows: list[dict[str, Any]] = []

    while True:
        url = (
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries/{job_id}"
            f"?location={location}&maxResults=10000"
        )
        if page_token:
            url += f"&pageToken={page_token}"

        payload = bigquery_request(source=source, token=token, url=url)
        if not payload.get("jobComplete", False):
            time.sleep(2)
            continue

        schema_fields = schema_fields or payload.get("schema", {}).get("fields", [])
        for row in payload.get("rows", []):
            rows.append(bigquery_row_to_dict(schema_fields, row["f"]))

        page_token = payload.get("pageToken")
        if not page_token:
            return rows


def fetch_job_metadata(
    *,
    source: JsonSource,
    token: str,
    project_id: str,
    job_id: str,
    location: str,
) -> dict[str, Any]:
    return bigquery_request(
        source=source,
        token=token,
        url=(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/jobs/{job_id}"
            f"?location={location}"
        ),
    )


def bigquery_row_to_dict(
    schema_fields: list[dict[str, Any]], values: list[dict[str, Any]]
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for field, wrapper in zip(schema_fields, values, strict=True):
        value = wrapper.get("v")
        field_type = field["type"]
        if value is None:
            row[field["name"]] = None
        elif field_type in {"INTEGER", "INT64"}:
            row[field["name"]] = int(value)
        elif field_type in {"FLOAT", "FLOAT64", "NUMERIC"}:
            row[field["name"]] = float(value)
        else:
            row[field["name"]] = value
    return row


def row_to_output(
    row: dict[str, Any],
    institution_lookup: dict[str, CandidateInstitution],
) -> dict[str, Any]:
    institution_uri = row["institution_openalex_id"]
    subfield_uri = row["subfield_id"]
    institution = institution_lookup[short_openalex_id(institution_uri, "I")]
    subfield_short = short_openalex_id(subfield_uri, "subfields")
    return {
        "university_id": institution.university_id,
        "canonical_name": institution.canonical_name,
        "institution_openalex_id": institution.openalex_short_id,
        "subfield_id": int(subfield_short.removeprefix("subfields/")),
        "year": int(row["year"]),
        "counting_method": row["counting_method"],
        "publications_count": int(row["publications_count"]),
        "avg_citation_impact": (
            float(row["avg_citation_impact"]) if row["avg_citation_impact"] is not None else None
        ),
        "impact_metric_name": "citation_normalized_percentile",
    }


def write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="snappy")


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def upsert_metrics(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    start_year: int,
    end_year: int,
    counting_methods: list[str],
    target_subfield_ids: set[int],
) -> None:
    placeholders_methods = ",".join("?" for _ in counting_methods)
    placeholders_subfields = ",".join("?" for _ in target_subfield_ids)
    conn.execute(
        f"""
        DELETE FROM university_subfield_year_metrics
        WHERE year BETWEEN ? AND ?
          AND counting_method IN ({placeholders_methods})
          AND subfield_id IN ({placeholders_subfields})
        """,
        [start_year, end_year, *counting_methods, *sorted(target_subfield_ids)],
    )
    conn.executemany(
        """
        INSERT INTO university_subfield_year_metrics (
            university_id,
            subfield_id,
            year,
            counting_method,
            publications_count,
            avg_citation_impact,
            impact_metric_name,
            source_snapshot,
            source_note
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["university_id"],
                row["subfield_id"],
                row["year"],
                row["counting_method"],
                row["publications_count"],
                row["avg_citation_impact"],
                row["impact_metric_name"],
                row["source_snapshot"],
                row["source_note"],
            )
            for row in rows
        ],
    )


class ExtractOpenAlexMetricsLoader(ManualDataLoader):
    source_code = "EXTRACT_OPENALEX_METRICS"
    description = (
        "Extract OpenAlex research metrics from BigQuery, write parquet, and optionally upsert."
    )
    execute_help = "write parquet artifacts and upsert into SQLite (default is dry-run)"
    requires_json_source = True
    default_request_timeout = 180

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--output-dir",
            type=Path,
            default=None,
            help="override the configured output_dir",
        )
        parser.add_argument(
            "--run-id",
            default=None,
            help="explicit run identifier (default: utc timestamp)",
        )
        parser.add_argument(
            "--gcp-project",
            default=None,
            help="billing project for BigQuery; defaults to config or current gcloud project",
        )
        parser.add_argument(
            "--max-bytes-billed",
            type=int,
            default=None,
            help="override config.sources.openalex.bigquery.max_bytes_billed",
        )

    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conn = self._require_conn(conn)
        run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_dir = resolve_run_output_dir(config, args.output_dir, run_id)
        max_bytes_billed = args.max_bytes_billed or config.get("sources", {}).get(
            "openalex", {}
        ).get("bigquery", {}).get("max_bytes_billed")
        if not max_bytes_billed:
            raise RuntimeError("openalex.bigquery.max_bytes_billed must be configured.")

        counting_methods = config.get("sources", {}).get("openalex", {}).get("counting_methods", [])
        start_year = int(config["years"]["research_start"])
        end_year = int(config["years"]["research_end"])
        if start_year > end_year:
            raise RuntimeError("config years.research_start must be <= years.research_end")

        institutions = load_candidate_institutions(conn)
        if not institutions:
            raise RuntimeError("No OPENALEX identifiers found in university_identifiers.")
        target_subfield_uris = resolve_target_subfields(conn, config)
        institution_lookup = {row.openalex_short_id: row for row in institutions}
        candidate_uris = [row.openalex_uri for row in institutions]
        gcp_project = resolve_gcp_project(config, args.gcp_project)
        token = gcloud_access_token()
        dry_run_jobs: list[dict[str, Any]] = []
        actual_jobs: list[dict[str, Any]] = []
        extracted_rows: list[dict[str, Any]] = []

        for counting_method in counting_methods:
            query = build_query(
                candidate_ids=candidate_uris,
                subfield_ids=sorted(target_subfield_uris),
                start_year=start_year,
                end_year=end_year,
                counting_method=counting_method,
            )

            dry_run_response = submit_query_job(
                source=self.json_source,
                token=token,
                project_id=gcp_project,
                query=query,
                max_bytes_billed=int(max_bytes_billed),
                dry_run=True,
            )
            # BQ dry-run responses put totalBytesProcessed at statistics top-level,
            # not nested under statistics.query (which is where executed jobs put it).
            dry_run_stats = dry_run_response.get("statistics", {})
            dry_run_query_stats = dry_run_stats.get("query", {})
            dry_run_bytes = int(
                dry_run_stats.get("totalBytesProcessed")
                or dry_run_query_stats.get("totalBytesProcessed")
                or "0"
            )
            dry_run_jobs.append(
                {
                    "counting_method": counting_method,
                    "dry_run_job_id": dry_run_response.get("jobReference", {}).get("jobId"),
                    "total_bytes_processed": dry_run_bytes,
                    "total_bytes_billed": int(
                        dry_run_stats.get("totalBytesBilled")
                        or dry_run_query_stats.get("totalBytesBilled")
                        or "0"
                    ),
                }
            )
            print(
                "Dry run: "
                f"counting_method={counting_method} "
                f"bytes_processed={dry_run_bytes} "
                f"max_bytes_billed={int(max_bytes_billed)} "
                f"institutions={len(candidate_uris)} "
                f"subfields={len(target_subfield_uris)}"
            )

            if not args.execute:
                continue

            actual_job = submit_query_job(
                source=self.json_source,
                token=token,
                project_id=gcp_project,
                query=query,
                max_bytes_billed=int(max_bytes_billed),
                dry_run=False,
            )
            job_reference = actual_job["jobReference"]
            job_id = job_reference["jobId"]
            location = job_reference["location"]

            method_rows = wait_for_query_results(
                source=self.json_source,
                token=token,
                project_id=gcp_project,
                job_id=job_id,
                location=location,
            )
            job_metadata = fetch_job_metadata(
                source=self.json_source,
                token=token,
                project_id=gcp_project,
                job_id=job_id,
                location=location,
            )
            method_output_rows = [row_to_output(row, institution_lookup) for row in method_rows]
            extracted_rows.extend(method_output_rows)

            query_stats = job_metadata.get("statistics", {}).get("query", {})
            actual_jobs.append(
                {
                    "counting_method": counting_method,
                    "query_job_id": job_id,
                    "location": location,
                    "total_bytes_processed": int(query_stats.get("totalBytesProcessed", "0")),
                    "total_bytes_billed": int(query_stats.get("totalBytesBilled", "0")),
                }
            )

        if not args.execute:
            return [], {
                "executed": False,
                "run_id": run_id,
                "output_dir": str(output_dir),
                "counting_methods": counting_methods,
                "institutions": len(candidate_uris),
                "subfields": len(target_subfield_uris),
                "dry_run_jobs": dry_run_jobs,
                "max_bytes_billed": int(max_bytes_billed),
            }

        extracted_rows.sort(
            key=lambda row: (
                row["university_id"],
                row["subfield_id"],
                row["year"],
                row["counting_method"],
            )
        )
        if not extracted_rows:
            raise RuntimeError("BigQuery extraction returned zero rows.")

        source_snapshot = (
            f"bigquery:{OPENALEX_DATASET}.works:{datetime.now(UTC).date().isoformat()}"
        )
        source_note = (
            "Aggregated from the public OpenAlex BigQuery works table after year/type/"
            "paratext/retraction filters, then restricted to configured institutions and subfields."
        )
        rows_for_db = [
            {
                **row,
                "source_snapshot": source_snapshot,
                "source_note": source_note,
            }
            for row in extracted_rows
        ]

        parquet_path = output_dir / "openalex_metrics.parquet"
        manifest_path = output_dir / "run_manifest.json"
        write_parquet(parquet_path, rows_for_db)

        upsert_metrics(
            conn,
            rows_for_db,
            start_year=start_year,
            end_year=end_year,
            counting_methods=counting_methods,
            target_subfield_ids={int(row["subfield_id"]) for row in rows_for_db},
        )
        conn.commit()

        manifest = {
            "config_hash": config_hash(config),
            "code_revision": git_revision(),
            "extracted_at_utc": datetime.now(UTC).isoformat(),
            "openalex_backend": "bigquery_primary",
            "parquet_artifacts": [str(parquet_path.relative_to(ROOT))],
            "bigquery": {
                "billing_project": gcp_project,
                "dataset": OPENALEX_DATASET,
                "maximum_bytes_billed": int(max_bytes_billed),
                "dry_run_jobs": dry_run_jobs,
                "query_jobs": actual_jobs,
                "total_bytes_processed": sum(job["total_bytes_processed"] for job in actual_jobs),
                "total_bytes_billed": sum(job["total_bytes_billed"] for job in actual_jobs),
            },
            "row_counts": {
                "openalex_metrics_parquet_rows": len(rows_for_db),
                "university_subfield_year_metrics_rows_upserted": len(rows_for_db),
                "institutions": len({row["university_id"] for row in rows_for_db}),
                "subfields": len({row["subfield_id"] for row in rows_for_db}),
            },
            "years": {
                "research_start": start_year,
                "research_end": end_year,
            },
        }
        write_manifest(manifest_path, manifest)

        return rows_for_db, {
            "executed": True,
            "run_id": run_id,
            "output_dir": str(output_dir),
            "rows": len(rows_for_db),
            "institutions": manifest["row_counts"]["institutions"],
            "subfields": manifest["row_counts"]["subfields"],
            "query_jobs": [job["query_job_id"] for job in actual_jobs],
            "dry_run_jobs": dry_run_jobs,
            "total_bytes_processed": manifest["bigquery"]["total_bytes_processed"],
            "total_bytes_billed": manifest["bigquery"]["total_bytes_billed"],
        }


if __name__ == "__main__":
    raise SystemExit(ExtractOpenAlexMetricsLoader().run())
