#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from shared import ROOT
from shared.config import resolve_output_dir
from shared.data_loader import ManualDataLoader

SCORECARD_API_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"
HEADERS = {"User-Agent": "univ-phase2/1.0"}
BASE_PARAMS: dict[str, str | int] = {"keys_nested": "true", "all_programs_nested": "true"}
SOURCE_SYSTEM = "College Scorecard API"
FIELDS = "id,school.name,latest.programs.cip_4_digit"


@dataclass(frozen=True)
class TargetUniversity:
    university_id: int
    canonical_name: str
    scorecard_unitid: str | None
    scorecard_opeid6: str | None


def output_paths(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    output_dir = resolve_output_dir(config)
    return (
        output_dir / "scorecard_field_outcomes.parquet",
        output_dir / "scorecard_cip4_bridge.parquet",
        output_dir / "scorecard_field_outcomes_coverage.md",
    )


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def scorecard_params(params: dict[str, str | int]) -> dict[str, str | int]:
    """Build the query params for a Scorecard request, including the API key."""
    api_key = os.environ.get("SCORECARD_API_KEY")
    if not api_key:
        raise RuntimeError("SCORECARD_API_KEY is required for Scorecard loaders.")
    return {**BASE_PARAMS, "api_key": api_key, "fields": FIELDS, **params}


def load_targets(conn: sqlite3.Connection) -> list[TargetUniversity]:
    rows = conn.execute(
        """
        SELECT
            u.university_id,
            u.canonical_name,
            unitid.external_id AS scorecard_unitid,
            opeid.external_id AS scorecard_opeid6
        FROM universities u
        LEFT JOIN university_identifiers unitid
          ON unitid.university_id = u.university_id
         AND unitid.system_code = 'SCORECARD_UNITID'
        LEFT JOIN university_identifiers opeid
          ON opeid.university_id = u.university_id
         AND opeid.system_code = 'SCORECARD_OPEID6'
        ORDER BY u.university_id
        """
    ).fetchall()
    return [
        TargetUniversity(
            university_id=row["university_id"],
            canonical_name=row["canonical_name"],
            scorecard_unitid=row["scorecard_unitid"],
            scorecard_opeid6=row["scorecard_opeid6"],
        )
        for row in rows
    ]


def resolve_cluster_cips(conn: sqlite3.Connection, config: dict[str, Any]) -> set[str]:
    resolved: set[str] = set()
    for cluster in config.get("field_clusters", []):
        rules = cluster.get("cip_rules", {})
        resolved.update(rules.get("include_6digit", []))
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


def build_bridge_rows(conn: sqlite3.Connection, config: dict[str, Any]) -> list[dict[str, Any]]:
    cluster_cips = resolve_cluster_cips(conn, config)
    bridge_rows: list[dict[str, Any]] = []
    for cluster in config.get("field_clusters", []):
        rules = cluster.get("cip_rules", {})
        cluster_codes = set(rules.get("include_6digit", []))
        families = rules.get("include_2digit_families", [])
        if families:
            placeholders = ",".join("?" for _ in families)
            rows = conn.execute(
                f"""
                SELECT cip_code
                FROM cips
                WHERE cip_family_code IN ({placeholders})
                """,
                list(families),
            ).fetchall()
            cluster_codes.update(row["cip_code"] for row in rows)
        for cip_code in rules.get("exclude_6digit", []):
            cluster_codes.discard(cip_code)

        for cip_code in sorted(cluster_codes & cluster_cips):
            bridge_rows.append(
                {
                    "field_cluster_slug": cluster["slug"],
                    "field_cluster_label": cluster["label"],
                    "cip_code": cip_code,
                    "cip4_code": cip_code.replace(".", "")[:4],
                }
            )

    deduped = {(row["field_cluster_slug"], row["cip_code"]): row for row in bridge_rows}
    return sorted(deduped.values(), key=lambda row: (row["field_cluster_slug"], row["cip_code"]))


def nested_get(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if current is None or not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="snappy")


def build_outcome_row(
    *,
    target: TargetUniversity,
    program: dict[str, Any],
) -> dict[str, Any]:
    timestamp = datetime.now(UTC).isoformat()
    debt_staff_grad_plus = program.get("debt", {}).get("staff_grad_plus", {})
    return {
        "university_id": target.university_id,
        "canonical_name": target.canonical_name,
        "scorecard_unitid": target.scorecard_unitid,
        "scorecard_opeid6_expected": target.scorecard_opeid6,
        "scorecard_opeid6_program": str(program.get("ope6_id")).zfill(6)
        if program.get("ope6_id")
        else None,
        "opeid6_matches_expected": (
            1
            if target.scorecard_opeid6
            and program.get("ope6_id")
            and str(program.get("ope6_id")).zfill(6) == target.scorecard_opeid6
            else 0
        ),
        "cip4_code": program["code"],
        "cip4_title": program.get("title"),
        "credential_level": nested_get(program, "credential", "level"),
        "distance_status": program.get("distance"),
        "completers_count_year_1": nested_get(program, "counts", "ipeds_awards1"),
        "completers_count_year_2": nested_get(program, "counts", "ipeds_awards2"),
        "completers_count": nested_get(program, "counts", "ipeds_awards2"),
        "median_earnings_1yr": nested_get(program, "earnings", "1_yr", "overall_median_earnings"),
        "median_earnings_5yr": nested_get(program, "earnings", "5_yr", "overall_median_earnings"),
        "earnings_1yr_working_not_enrolled_count": nested_get(
            program, "earnings", "1_yr", "working_not_enrolled", "overall_count"
        ),
        "earnings_5yr_working_not_enrolled_count": nested_get(
            program, "earnings", "5_yr", "working_not_enrolled", "overall_count"
        ),
        "debt_staff_grad_plus_any_median": nested_get(
            debt_staff_grad_plus, "all", "all_inst", "median"
        ),
        "debt_staff_grad_plus_eval_median": nested_get(
            debt_staff_grad_plus, "all", "eval_inst", "median"
        ),
        "source_system": SOURCE_SYSTEM,
        "source_note": f"Fetched via College Scorecard API on {timestamp}.",
    }


def write_coverage_report(
    *,
    path: Path,
    bridge_rows: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    total_universities: int,
) -> None:
    bridge_by_cluster: dict[str, set[str]] = defaultdict(set)
    label_by_cluster: dict[str, str] = {}
    for row in bridge_rows:
        bridge_by_cluster[row["field_cluster_slug"]].add(row["cip4_code"])
        label_by_cluster[row["field_cluster_slug"]] = row["field_cluster_label"]

    universities_with_any_outcome: set[int] = set()
    universities_by_cluster: dict[str, set[int]] = defaultdict(set)
    rows_with_earnings_by_cluster: dict[str, int] = defaultdict(int)
    for row in outcome_rows:
        universities_with_any_outcome.add(row["university_id"])
        for cluster_slug, cip4_codes in bridge_by_cluster.items():
            if row["cip4_code"] in cip4_codes:
                universities_by_cluster[cluster_slug].add(row["university_id"])
                if row["median_earnings_1yr"] is not None or row["median_earnings_5yr"] is not None:
                    rows_with_earnings_by_cluster[cluster_slug] += 1

    lines = [
        "# Scorecard Field Outcomes Coverage",
        "",
        f"- Universities in local universe: {total_universities}",
        f"- Universities with at least one bachelor-level target CIP4 outcome row: {len(universities_with_any_outcome)}",
        f"- Raw outcome rows written: {len(outcome_rows)}",
        "",
        "| Cluster | Universities with outcome rows | Coverage | Rows with non-null earnings |",
        "| --- | ---: | ---: | ---: |",
    ]
    for cluster_slug in sorted(bridge_by_cluster):
        universities = len(universities_by_cluster.get(cluster_slug, set()))
        coverage = universities / total_universities if total_universities else 0.0
        lines.append(
            f"| {label_by_cluster[cluster_slug]} (`{cluster_slug}`) | "
            f"{universities} | {coverage:.1%} | {rows_with_earnings_by_cluster.get(cluster_slug, 0)} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


class LoadScorecardFieldOutcomesLoader(ManualDataLoader):
    requires_json_source = True
    json_source_headers = HEADERS
    source_code = "LOAD_SCORECARD_FIELD_OUTCOMES"
    description = "Export bachelor-level College Scorecard field-of-study outcomes."
    execute_help = "write parquet/coverage artifacts (default is dry-run)"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=5,
            help="number of Scorecard ids to request per API call (default: 5)",
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
        outcomes_path, bridge_path, coverage_path = output_paths(config)
        targets = load_targets(conn)
        bridge_rows = build_bridge_rows(conn, config)

        target_cip4_codes = {row["cip4_code"] for row in bridge_rows}
        records_by_id: dict[str, dict[str, Any]] = {}
        scorecard_ids = [target.scorecard_unitid for target in targets if target.scorecard_unitid]
        for id_chunk in chunked(scorecard_ids, args.chunk_size):
            payload = self.json_source.fetch(
                SCORECARD_API_URL,
                params=scorecard_params({"id": ",".join(id_chunk), "per_page": len(id_chunk)}),
            )
            for result in payload.get("results", []):
                records_by_id[str(result["id"])] = result

        deduped_rows: dict[tuple[int, str, int], dict[str, Any]] = {}
        duplicate_counts: dict[tuple[int, str, int], int] = defaultdict(int)
        for target in targets:
            record = records_by_id.get(target.scorecard_unitid or "")
            if record is None:
                continue
            programs = nested_get(record, "latest", "programs", "cip_4_digit") or []
            for program in programs:
                if nested_get(program, "credential", "level") != 3:
                    continue
                if program.get("code") not in target_cip4_codes:
                    continue

                row = build_outcome_row(target=target, program=program)
                key = (row["university_id"], row["cip4_code"], row["credential_level"])
                existing = deduped_rows.get(key)
                duplicate_counts[key] += 1
                if existing is None:
                    deduped_rows[key] = row
                    continue

                existing_count = existing.get("completers_count") or -1
                row_count = row.get("completers_count") or -1
                if row_count > existing_count:
                    deduped_rows[key] = row

        outcome_rows = []
        for key, row in sorted(deduped_rows.items()):
            row["duplicate_program_rows"] = duplicate_counts[key]
            outcome_rows.append(row)

        universities_with_rows = len({row["university_id"] for row in outcome_rows})
        if args.execute:
            write_parquet(outcomes_path, outcome_rows)
            write_parquet(bridge_path, bridge_rows)
            write_coverage_report(
                path=coverage_path,
                bridge_rows=bridge_rows,
                outcome_rows=outcome_rows,
                total_universities=len(targets),
            )
        return outcome_rows, {
            "executed": args.execute,
            "outcomes_path": str(outcomes_path.relative_to(ROOT)),
            "bridge_path": str(bridge_path.relative_to(ROOT)),
            "coverage_path": str(coverage_path.relative_to(ROOT)),
            "bridge_rows": len(bridge_rows),
            "universities_with_rows": universities_with_rows,
        }


if __name__ == "__main__":
    raise SystemExit(LoadScorecardFieldOutcomesLoader().run())
