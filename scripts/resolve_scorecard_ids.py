#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from shared.data_loader import ManualDataLoader
from shared.http import JsonSource

SCORECARD_API_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"
HEADERS = {"User-Agent": "univ-phase1/1.0"}

SCORECARD_SYSTEM_ROWS = [
    (
        "SCORECARD_UNITID",
        "College Scorecard UNITID",
        1,
        "Department of Education institution-level join key; usually matches IPEDS UNITID.",
    ),
    (
        "SCORECARD_OPEID6",
        "College Scorecard OPEID6",
        1,
        "Department of Education / Federal Student Aid field-of-study join key.",
    ),
]


@dataclass(frozen=True)
class TargetUniversity:
    university_id: int
    canonical_name: str
    ipeds_unitid: int | None


@dataclass(frozen=True)
class IdentifierRow:
    university_id: int
    system_code: str
    external_id: str
    external_url: str | None
    match_status: str
    source_note: str


@dataclass(frozen=True)
class ReviewIssue:
    university_id: int
    canonical_name: str
    issue: str
    ipeds_unitid: int | None
    scorecard_unitid: str | None
    scorecard_opeid6: str | None


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def load_targets(conn: sqlite3.Connection) -> list[TargetUniversity]:
    rows = conn.execute(
        """
        SELECT
            u.university_id,
            u.canonical_name,
            ipeds.external_id AS ipeds_unitid
        FROM universities u
        LEFT JOIN university_identifiers ipeds
          ON ipeds.university_id = u.university_id
         AND ipeds.system_code = 'IPEDS'
        ORDER BY u.university_id
        """
    ).fetchall()
    return [
        TargetUniversity(
            university_id=row["university_id"],
            canonical_name=row["canonical_name"],
            ipeds_unitid=int(row["ipeds_unitid"]) if row["ipeds_unitid"] is not None else None,
        )
        for row in rows
    ]


def fetch_scorecard_records(source: JsonSource, unitids: list[int]) -> dict[int, dict[str, object]]:
    api_key = os.environ.get("SCORECARD_API_KEY")
    if not api_key:
        raise RuntimeError("SCORECARD_API_KEY is required for this resolver.")

    records: dict[int, dict[str, object]] = {}
    for id_chunk in chunked([str(unitid) for unitid in unitids], size=20):
        payload = source.fetch(
            SCORECARD_API_URL,
            params={
                "api_key": api_key,
                "fields": "id,school.name,ope6_id,ope8_id",
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


def normalized_opeid6(result: dict[str, object]) -> str | None:
    ope6_id = str(result.get("ope6_id", "")).strip()
    if ope6_id:
        return ope6_id.zfill(6)

    ope8_id = str(result.get("ope8_id", "")).strip()
    digits = "".join(character for character in ope8_id if character.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return None


def build_identifier_rows(
    targets: list[TargetUniversity],
    scorecard_records: dict[int, dict[str, object]],
) -> tuple[list[IdentifierRow], list[ReviewIssue]]:
    today = date.today().isoformat()
    source_note = f"Resolved via College Scorecard API lookup by IPEDS UNITID on {today}."

    unitid_rows: list[IdentifierRow] = []
    opeid_candidates: list[tuple[TargetUniversity, str, str | None]] = []
    review_issues: list[ReviewIssue] = []

    for target in targets:
        if target.ipeds_unitid is None:
            review_issues.append(
                ReviewIssue(
                    university_id=target.university_id,
                    canonical_name=target.canonical_name,
                    issue="missing_ipeds_unitid",
                    ipeds_unitid=None,
                    scorecard_unitid=None,
                    scorecard_opeid6=None,
                )
            )
            continue

        record = scorecard_records[target.ipeds_unitid]
        scorecard_unitid = str(record["id"]).strip()
        unitid_rows.append(
            IdentifierRow(
                university_id=target.university_id,
                system_code="SCORECARD_UNITID",
                external_id=scorecard_unitid,
                external_url=None,
                match_status="exact",
                source_note=source_note,
            )
        )
        opeid_candidates.append((target, scorecard_unitid, normalized_opeid6(record)))

    targets_by_opeid6: dict[str, list[tuple[TargetUniversity, str]]] = defaultdict(list)
    for target, scorecard_unitid, opeid6 in opeid_candidates:
        if opeid6 is None:
            review_issues.append(
                ReviewIssue(
                    university_id=target.university_id,
                    canonical_name=target.canonical_name,
                    issue="missing_opeid6",
                    ipeds_unitid=target.ipeds_unitid,
                    scorecard_unitid=scorecard_unitid,
                    scorecard_opeid6=None,
                )
            )
            continue
        targets_by_opeid6[opeid6].append((target, scorecard_unitid))

    identifier_rows = list(unitid_rows)
    for opeid6, grouped_targets in sorted(targets_by_opeid6.items()):
        if len(grouped_targets) > 1:
            for target, scorecard_unitid in grouped_targets:
                review_issues.append(
                    ReviewIssue(
                        university_id=target.university_id,
                        canonical_name=target.canonical_name,
                        issue="duplicate_opeid6",
                        ipeds_unitid=target.ipeds_unitid,
                        scorecard_unitid=scorecard_unitid,
                        scorecard_opeid6=opeid6,
                    )
                )
            continue

        target, scorecard_unitid = grouped_targets[0]
        identifier_rows.append(
            IdentifierRow(
                university_id=target.university_id,
                system_code="SCORECARD_OPEID6",
                external_id=opeid6,
                external_url=None,
                match_status="exact",
                source_note=source_note,
            )
        )

    return identifier_rows, sorted(
        review_issues,
        key=lambda issue: (issue.issue, issue.canonical_name, issue.university_id),
    )


def upsert_identifier_systems(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """
        INSERT INTO identifier_systems (
            system_code,
            system_name,
            is_authoritative,
            notes
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(system_code) DO UPDATE SET
            system_name = excluded.system_name,
            is_authoritative = excluded.is_authoritative,
            notes = excluded.notes
        """,
        SCORECARD_SYSTEM_ROWS,
    )


def write_identifiers(conn: sqlite3.Connection, rows: list[IdentifierRow]) -> None:
    conn.execute(
        """
        DELETE FROM university_identifiers
        WHERE system_code IN ('SCORECARD_UNITID', 'SCORECARD_OPEID6')
        """
    )
    conn.executemany(
        """
        INSERT INTO university_identifiers (
            university_id,
            system_code,
            external_id,
            external_url,
            match_status,
            source_note
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row.university_id,
                row.system_code,
                row.external_id,
                row.external_url,
                row.match_status,
                row.source_note,
            )
            for row in rows
        ],
    )


def validate(
    conn: sqlite3.Connection, expected_unitid_rows: int, expected_opeid6_rows: int
) -> None:
    scorecard_unitids = conn.execute(
        """
        SELECT COUNT(*)
        FROM university_identifiers
        WHERE system_code = 'SCORECARD_UNITID'
        """
    ).fetchone()[0]
    if scorecard_unitids != expected_unitid_rows:
        raise RuntimeError(
            f"expected {expected_unitid_rows} SCORECARD_UNITID rows, found {scorecard_unitids}"
        )

    scorecard_opeid6 = conn.execute(
        """
        SELECT COUNT(*)
        FROM university_identifiers
        WHERE system_code = 'SCORECARD_OPEID6'
        """
    ).fetchone()[0]
    if scorecard_opeid6 != expected_opeid6_rows:
        raise RuntimeError(
            f"expected {expected_opeid6_rows} SCORECARD_OPEID6 rows, found {scorecard_opeid6}"
        )


class ResolveScorecardIdsLoader(ManualDataLoader):
    requires_json_source = True
    json_source_headers = HEADERS
    default_max_attempts = 4
    source_code = "RESOLVE_SCORECARD_IDS"
    description = "Resolve Scorecard identifiers into university_identifiers."
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

        targets = load_targets(conn)
        scorecard_targets = [target.ipeds_unitid for target in targets if target.ipeds_unitid]
        scorecard_records = fetch_scorecard_records(self.json_source, scorecard_targets)
        identifier_rows, review_issues = build_identifier_rows(targets, scorecard_records)
        scorecard_opeid6_rows = sum(
            1 for row in identifier_rows if row.system_code == "SCORECARD_OPEID6"
        )

        upsert_identifier_systems(conn)
        write_identifiers(conn, identifier_rows)
        validate(
            conn,
            expected_unitid_rows=len(scorecard_targets),
            expected_opeid6_rows=scorecard_opeid6_rows,
        )

        if args.execute:
            conn.commit()
        else:
            conn.rollback()

        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "scorecard_unitid_rows": len(scorecard_targets),
            "scorecard_opeid6_rows": scorecard_opeid6_rows,
            "review_issue_count": len(review_issues),
            "review_issues": [issue.__dict__ for issue in review_issues],
        }


if __name__ == "__main__":
    raise SystemExit(ResolveScorecardIdsLoader().run())
