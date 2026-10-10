#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from shared import ROOT
from shared.config import resolve_output_dir
from shared.data_loader import ManualDataLoader
from shared.http import JsonSource, JsonSourceError

API_URL = (
    "https://educationdata.urban.org/api/v1/college-university/ipeds/completions-cip-6/{year}/"
)
HEADERS = {"User-Agent": "univ-phase2/1.0"}
SOURCE_SYSTEM = "Urban Institute Education Data Portal"
BACHELORS_AWARD_LEVEL = 7


def output_paths(config: dict[str, Any]) -> tuple[Path, Path]:
    output_dir = resolve_output_dir(config)
    return (
        output_dir / "completions_by_year.parquet",
        output_dir / "completions_by_year_metadata.json",
    )


def load_targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            u.university_id,
            u.canonical_name,
            ipeds.external_id AS ipeds_unitid
        FROM universities u
        JOIN university_identifiers ipeds
          ON ipeds.university_id = u.university_id
         AND ipeds.system_code = 'IPEDS'
        ORDER BY u.university_id
        """
    ).fetchall()
    return [
        {
            "university_id": row["university_id"],
            "canonical_name": row["canonical_name"],
            "ipeds_unitid": int(row["ipeds_unitid"]),
        }
        for row in rows
    ]


def probe_year(source: JsonSource, year: int, sample_unitid: int) -> bool:
    """True when the Urban Institute has completions data for ``year``.

    A missing year answers 404, which the JsonSource raises immediately rather
    than retrying — that is the signal we want here, not an error.
    """
    params = {"unitid": sample_unitid, "race": 99, "sex": 99, "per_page": 1}
    try:
        payload = source.fetch(API_URL.format(year=year), params=params)
    except JsonSourceError:
        return False
    return bool(payload.get("results"))


def format_cip(raw_value: int | str) -> str:
    digits = f"{int(raw_value):06d}"
    return f"{digits[:2]}.{digits[2:]}"


def fetch_unitid_year(source: JsonSource, unitid: int, year: int) -> list[dict[str, Any]]:
    params = {"unitid": unitid, "race": 99, "sex": 99, "per_page": 10000}
    payload = source.fetch(API_URL.format(year=year), params=params)
    return payload.get("results", [])


def aggregate_counts(
    source: JsonSource,
    targets: list[dict[str, Any]],
    years: list[int],
    workers: int,
) -> list[dict[str, Any]]:
    totals: dict[tuple[int, int, str], int] = defaultdict(int)
    target_by_unitid = {target["ipeds_unitid"]: target for target in targets}
    target_by_university_id = {target["university_id"]: target for target in targets}
    tasks = [(target["ipeds_unitid"], year) for target in targets for year in years]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(fetch_unitid_year, source, unitid, year): (unitid, year)
            for unitid, year in tasks
        }
        for future in as_completed(future_map):
            unitid, year = future_map[future]
            rows = future.result()
            target = target_by_unitid[unitid]
            for row in rows:
                award_level = row.get("award_level")
                if award_level is None or int(award_level) != BACHELORS_AWARD_LEVEL:
                    continue
                awards = row.get("awards_6digit")
                if awards is None or int(awards) <= 0:
                    continue
                cip_code = format_cip(row["cipcode_6digit"])
                totals[(target["university_id"], year, cip_code)] += int(awards)

    timestamp = datetime.now(UTC).isoformat()
    output_rows = [
        {
            "university_id": university_id,
            "canonical_name": target_by_university_id[university_id]["canonical_name"],
            "ipeds_unitid": target_by_university_id[university_id]["ipeds_unitid"],
            "year": year,
            "cip_code": cip_code,
            "award_level": BACHELORS_AWARD_LEVEL,
            "awards_count": awards_count,
            "source_system": SOURCE_SYSTEM,
            "source_note": (
                "Aggregated from Urban Institute ipeds/completions-cip-6 with race=99, sex=99, "
                f"award_level={BACHELORS_AWARD_LEVEL} on {timestamp}."
            ),
        }
        for (university_id, year, cip_code), awards_count in sorted(totals.items())
    ]
    return output_rows


def write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="snappy")


def write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


class LoadIpedsCompletionsLoader(ManualDataLoader):
    source_code = "LOAD_IPEDS_COMPLETIONS"
    description = (
        "Export annual bachelor-level IPEDS completion counts from the Urban Institute API."
    )
    execute_help = "write parquet and metadata artifacts (default is dry-run)"
    requires_json_source = True
    json_source_headers = HEADERS

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--workers",
            type=int,
            default=8,
            help="number of concurrent API workers to use (default: 8)",
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
        completions_path, metadata_path = output_paths(config)
        start_year = int(config["years"]["completions_start"])
        end_year = int(config["years"]["completions_end"])
        if start_year > end_year:
            raise RuntimeError("config years.completions_start must be <= years.completions_end")

        targets = load_targets(conn)

        sample_unitid = targets[0]["ipeds_unitid"]
        requested_years = list(range(start_year, end_year + 1))
        available_years = [
            year for year in requested_years if probe_year(self.json_source, year, sample_unitid)
        ]
        unavailable_years = [year for year in requested_years if year not in available_years]

        rows = aggregate_counts(self.json_source, targets, available_years, args.workers)
        metadata = {
            "requested_years": requested_years,
            "available_years": available_years,
            "unavailable_years": unavailable_years,
            "bachelors_award_level": BACHELORS_AWARD_LEVEL,
            "row_count": len(rows),
            "universities_with_rows": len({row["university_id"] for row in rows}),
            "generated_at_utc": datetime.now(UTC).isoformat(),
        }

        if args.execute:
            write_parquet(completions_path, rows)
            write_metadata(metadata_path, metadata)

        return rows, {
            "executed": args.execute,
            "completions_path": str(completions_path.relative_to(ROOT)),
            "metadata_path": str(metadata_path.relative_to(ROOT)),
            "available_years": available_years,
            "unavailable_years": unavailable_years,
        }


if __name__ == "__main__":
    raise SystemExit(LoadIpedsCompletionsLoader().run())
