#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

from __future__ import annotations

import os
import sqlite3
from typing import Any, cast

from shared.data_loader import ManualDataLoader
from shared.http import JsonSource

SCORECARD_API_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"
HEADERS = {"User-Agent": "univ-phase1/1.0"}

CONTROL_BY_OWNERSHIP = {
    1: "public",
    2: "private-nonprofit",
    3: "private-forprofit",
}

MANUAL_METADATA = {
    "Deep Springs College": {
        "state_code": "CA",
        "city": "Big Pine",
        "control": "private-nonprofit",
    }
}


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def fetch_scorecard_records(source: JsonSource, unitids: list[int]) -> dict[int, dict[str, object]]:
    api_key = os.environ.get("SCORECARD_API_KEY")
    if not api_key:
        raise RuntimeError("SCORECARD_API_KEY is required for this backfill.")

    records: dict[int, dict[str, object]] = {}
    for id_chunk in chunked([str(unitid) for unitid in unitids], size=50):
        payload = source.fetch(
            SCORECARD_API_URL,
            params={
                "api_key": api_key,
                "fields": "id,school.name,school.state,school.city,school.ownership",
                "id": ",".join(id_chunk),
                "per_page": len(id_chunk),
            },
        )
        for result in payload.get("results", []):
            records[int(result["id"])] = result

    missing = sorted(set(unitids) - set(records))
    if missing:
        raise RuntimeError(f"College Scorecard missing records for unitids: {missing}")
    return records


def load_targets(
    conn: sqlite3.Connection,
) -> tuple[list[sqlite3.Row], list[int]]:
    rows = conn.execute(
        """
        SELECT
            u.university_id,
            u.canonical_name,
            u.state_code,
            u.city,
            u.control,
            ipeds.external_id AS ipeds_unitid
        FROM universities u
        LEFT JOIN university_identifiers ipeds
          ON ipeds.university_id = u.university_id
         AND ipeds.system_code = 'IPEDS'
        ORDER BY u.university_id
        """
    ).fetchall()
    unitids = [int(row["ipeds_unitid"]) for row in rows if row["ipeds_unitid"] is not None]
    return rows, unitids


def resolve_metadata(
    rows: list[sqlite3.Row], scorecard_records: dict[int, dict[str, object]]
) -> tuple[list[tuple[str, str, str, int]], list[str]]:
    resolved: list[tuple[str, str, str, int]] = []
    manual_names: list[str] = []

    for row in rows:
        ipeds_unitid = row["ipeds_unitid"]
        canonical_name = row["canonical_name"]
        if ipeds_unitid is None:
            manual = MANUAL_METADATA.get(canonical_name)
            if manual is None:
                raise RuntimeError(f"No manual metadata fallback configured for {canonical_name!r}")
            manual_names.append(canonical_name)
            resolved.append(
                (
                    manual["state_code"],
                    manual["city"],
                    manual["control"],
                    row["university_id"],
                )
            )
            continue

        scorecard = scorecard_records[int(ipeds_unitid)]
        state_code = str(scorecard.get("school.state", "")).strip()
        city = str(scorecard.get("school.city", "")).strip()
        ownership = scorecard.get("school.ownership")
        if not state_code or not city or ownership is None:
            raise RuntimeError(
                f"Incomplete Scorecard metadata for {canonical_name} (unitid={ipeds_unitid})"
            )

        control = CONTROL_BY_OWNERSHIP.get(cast(int, ownership))
        if control is None:
            raise RuntimeError(f"Unexpected school.ownership={ownership!r} for {canonical_name}")

        resolved.append((state_code, city, control, row["university_id"]))

    return resolved, manual_names


def validate_coverage(conn: sqlite3.Connection) -> tuple[int, int, int]:
    state_missing, city_missing, control_missing = conn.execute(
        """
        SELECT
            SUM(CASE WHEN state_code IS NULL OR TRIM(state_code) = '' THEN 1 ELSE 0 END),
            SUM(CASE WHEN city IS NULL OR TRIM(city) = '' THEN 1 ELSE 0 END),
            SUM(CASE WHEN control IS NULL OR TRIM(control) = '' THEN 1 ELSE 0 END)
        FROM universities
        """
    ).fetchone()
    missing_counts = (
        int(state_missing or 0),
        int(city_missing or 0),
        int(control_missing or 0),
    )
    if any(missing_counts):
        raise RuntimeError(
            "Metadata backfill incomplete: "
            f"state_missing={missing_counts[0]}, "
            f"city_missing={missing_counts[1]}, "
            f"control_missing={missing_counts[2]}"
        )
    return missing_counts


class BackfillUniversityMetadataLoader(ManualDataLoader):
    requires_json_source = True
    json_source_headers = HEADERS
    source_code = "BACKFILL_UNIVERSITY_METADATA"
    description = "Backfill universities.state_code, city, and control."
    requires_config = False
    execute_help = "write changes to the live SQLite database (default is dry-run)"

    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conn = self._require_conn(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

        rows, unitids = load_targets(conn)
        scorecard_records = fetch_scorecard_records(self.json_source, unitids)
        resolved, manual_names = resolve_metadata(rows, scorecard_records)

        conn.executemany(
            """
            UPDATE universities
            SET state_code = ?,
                city = ?,
                control = ?
            WHERE university_id = ?
            """,
            resolved,
        )

        validate_coverage(conn)

        if args.execute:
            conn.commit()
        else:
            conn.rollback()

        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "updated_rows": len(resolved),
            "scorecard_rows": len(unitids),
            "manual_fallback": sorted(manual_names),
            "validated_full_coverage": True,
        }


if __name__ == "__main__":
    raise SystemExit(BackfillUniversityMetadataLoader().run())
