#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///

from __future__ import annotations

import csv
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from shared.config import config_hash, resolve_output_dir
from shared.data_loader import ManualDataLoader
from shared.io_utils import git_revision, latest_openalex_run_dir, load_parquet_rows, read_json

TIMESTAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+\d{2}:\d{2})")


def extract_timestamp(source_note: str | None) -> str | None:
    if not source_note:
        return None
    match = TIMESTAMP_PATTERN.search(source_note)
    if not match:
        return None
    return match.group(1)


def csv_row_count(path: Path) -> int:
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        row_count = sum(1 for _ in reader)
    return max(0, row_count - 1)


def review_queue_summary(path: Path) -> dict[str, int]:
    summary: dict[str, int] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            key = row.get("issue_type") or row.get("review_type", "unknown")
            summary[key] = summary.get(key, 0) + 1
    return summary


def build_manifest(config: dict[str, Any]) -> dict[str, Any]:
    output_dir = resolve_output_dir(config)
    feature_store_rows = load_parquet_rows(output_dir / "feature_store.parquet")
    rankings_rows = load_parquet_rows(output_dir / "rankings_balanced.parquet")
    scorecard_institution_rows = load_parquet_rows(output_dir / "scorecard_institution.parquet")
    scorecard_field_rows = load_parquet_rows(output_dir / "scorecard_field_outcomes.parquet")
    completions_metadata = read_json(output_dir / "completions_by_year_metadata.json")
    openalex_run_dir = latest_openalex_run_dir(output_dir)
    openalex_manifest = read_json(openalex_run_dir / "run_manifest.json")
    usaspending_manifest = read_json(output_dir / "usaspending_totals_manifest.json")
    nih_manifest = read_json(output_dir / "nih_awards_manifest.json")
    nsf_manifest = read_json(output_dir / "nsf_awards_manifest.json")

    sensitivity_path = output_dir / "sensitivity_report.csv"
    review_queue_path = output_dir / "review_queue.csv"
    qa_report_path = output_dir / "qa_report.md"
    rankings_csv_path = output_dir / "rankings_balanced.csv"

    cluster_counts: dict[str, int] = {}
    for row in rankings_rows:
        cluster_counts[row["field_cluster_slug"]] = (
            cluster_counts.get(row["field_cluster_slug"], 0) + 1
        )

    feature_cluster_counts: dict[str, int] = {}
    for row in feature_store_rows:
        if int(row["eligible_for_ranking"]) != 1:
            continue
        feature_cluster_counts[row["field_cluster_slug"]] = (
            feature_cluster_counts.get(row["field_cluster_slug"], 0) + 1
        )

    first_institution_note = (
        scorecard_institution_rows[0]["source_note"] if scorecard_institution_rows else None
    )
    first_field_note = scorecard_field_rows[0]["source_note"] if scorecard_field_rows else None

    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "project_slug": config["project"]["slug"],
        "output_dir": str(output_dir.relative_to(output_dir.parent.parent)),
        "config_hash": config_hash(config),
        "code_revision": git_revision(),
        "years": {
            "research": {
                "start": config["years"]["research_start"],
                "end": config["years"]["research_end"],
            },
            "grants": {
                "start": config["years"]["grants_start"],
                "end": config["years"]["grants_end"],
            },
            "completions_requested": {
                "start": config["years"]["completions_start"],
                "end": config["years"]["completions_end"],
            },
            "completions_available": completions_metadata["available_years"],
            "completions_unavailable": completions_metadata["unavailable_years"],
            "feature_store_lookback_window": {
                "start": min(int(row["completion_lookback_start"]) for row in feature_store_rows),
                "end": max(int(row["completion_lookback_end"]) for row in feature_store_rows),
            },
        },
        "sources": {
            "openalex": {
                "backend": openalex_manifest.get("openalex_backend"),
                "extracted_at": openalex_manifest.get("extracted_at_utc"),
                "billing_project": openalex_manifest.get("bigquery", {}).get("billing_project"),
                "dataset": openalex_manifest.get("bigquery", {}).get("dataset"),
                "maximum_bytes_billed": openalex_manifest.get("bigquery", {}).get(
                    "maximum_bytes_billed"
                ),
                "query_jobs": openalex_manifest.get("bigquery", {}).get("query_jobs"),
                "dry_run_jobs": openalex_manifest.get("bigquery", {}).get("dry_run_jobs"),
                "total_bytes_processed": openalex_manifest.get("bigquery", {}).get(
                    "total_bytes_processed"
                ),
                "total_bytes_billed": openalex_manifest.get("bigquery", {}).get(
                    "total_bytes_billed"
                ),
                "run_dir": str(openalex_run_dir.relative_to(output_dir.parent.parent)),
                "parquet_artifacts": openalex_manifest.get("parquet_artifacts"),
            },
            "scorecard_institution": {
                "path": str(
                    (output_dir / "scorecard_institution.parquet").relative_to(
                        output_dir.parent.parent
                    )
                ),
                "row_count": len(scorecard_institution_rows),
                "extracted_at": extract_timestamp(first_institution_note),
            },
            "scorecard_field_outcomes": {
                "path": str(
                    (output_dir / "scorecard_field_outcomes.parquet").relative_to(
                        output_dir.parent.parent
                    )
                ),
                "row_count": len(scorecard_field_rows),
                "extracted_at": extract_timestamp(first_field_note),
            },
            "ipeds_completions": {
                "path": str(
                    (output_dir / "completions_by_year.parquet").relative_to(
                        output_dir.parent.parent
                    )
                ),
                "row_count": int(completions_metadata["row_count"]),
                "generated_at": completions_metadata.get("generated_at_utc"),
                "available_years": completions_metadata["available_years"],
                "unavailable_years": completions_metadata["unavailable_years"],
                "universities_with_rows": completions_metadata["universities_with_rows"],
            },
            "grant_context": {
                "usaspending": {
                    "pulled_at": usaspending_manifest.get("pulled_at"),
                    "rows_prepared": usaspending_manifest.get("rows_prepared"),
                    "unmatched_universities": len(
                        usaspending_manifest.get("unmatched_universities", [])
                    ),
                },
                "nih": {
                    "pulled_at": nih_manifest.get("pulled_at"),
                    "rows_prepared": nih_manifest.get("rows_prepared"),
                    "unmatched_universities": len(nih_manifest.get("unmatched_universities", [])),
                },
                "nsf": {
                    "pulled_at": nsf_manifest.get("pulled_at"),
                    "rows_prepared": nsf_manifest.get("rows_prepared"),
                    "unmatched_universities": len(nsf_manifest.get("unmatched_universities", [])),
                },
            },
        },
        "artifacts": {
            "feature_store": {
                "path": str(
                    (output_dir / "feature_store.parquet").relative_to(output_dir.parent.parent)
                ),
                "row_count": len(feature_store_rows),
                "eligible_rows": sum(
                    int(row["eligible_for_ranking"]) for row in feature_store_rows
                ),
                "eligible_rows_by_cluster": feature_cluster_counts,
            },
            "rankings_balanced": {
                "parquet_path": str(
                    (output_dir / "rankings_balanced.parquet").relative_to(output_dir.parent.parent)
                ),
                "csv_path": str(rankings_csv_path.relative_to(output_dir.parent.parent)),
                "row_count": len(rankings_rows),
                "rows_by_cluster": cluster_counts,
            },
            "qa_report": {
                "path": str(qa_report_path.relative_to(output_dir.parent.parent)),
            },
            "sensitivity_report": {
                "path": str(sensitivity_path.relative_to(output_dir.parent.parent)),
                "row_count": csv_row_count(sensitivity_path),
            },
            "review_queue": {
                "path": str(review_queue_path.relative_to(output_dir.parent.parent)),
                "row_count": csv_row_count(review_queue_path),
                "by_type": review_queue_summary(review_queue_path),
            },
        },
    }


class WritePhase3ManifestLoader(ManualDataLoader):
    source_code = "WRITE_PHASE3_MANIFEST"
    description = "Write the Phase 3 run_manifest.yaml artifact."
    execute_help = "write outputs/current/run_manifest.yaml"

    def load(
        self,
        *,
        conn,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        output_dir = resolve_output_dir(config)
        manifest_path = output_dir / "run_manifest.yaml"
        manifest = build_manifest(config)

        if args.execute:
            manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

        return [], {
            "executed": args.execute,
            "manifest_path": str(manifest_path),
            "code_revision": manifest["code_revision"],
            "feature_store_rows": manifest["artifacts"]["feature_store"]["row_count"],
            "ranking_rows": manifest["artifacts"]["rankings_balanced"]["row_count"],
            "review_queue_rows": manifest["artifacts"]["review_queue"]["row_count"],
        }


if __name__ == "__main__":
    raise SystemExit(WritePhase3ManifestLoader().run())
