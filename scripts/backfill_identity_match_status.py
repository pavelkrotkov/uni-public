#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

"""
Phase 4.3 — Backfill match_status and source_note on university_identifiers.

Assigns a match_status to every identifier row based on how it was obtained:
  external_registry    — ROR or OpenAlex identifier from an authoritative registry
  ipeds_matched        — IPEDS UNITID matched from government database
  resolved             — Scorecard identifiers (already populated by resolve_scorecard_ids.py)

UEI and DUNS identifiers have been moved to the university_ueis table and are
no longer present in university_identifiers; this script no longer handles them.

Rows that already have a non-NULL match_status are left untouched.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from shared.data_loader import ManualDataLoader

SOURCE_NOTES: dict[str, str] = {
    "ROR": "Research Organization Registry persistent identifier; matched by name during OpenAlex extraction",
    "OPENALEX": "OpenAlex institution ID; matched via ROR cross-reference in OpenAlex data",
    "IPEDS": "IPEDS UNITID from NCES; matched via Fiske name and state lookups",
    "SCORECARD_UNITID": "College Scorecard UNITID; resolved via IPEDS UNITID cross-reference",
    "SCORECARD_OPEID6": "College Scorecard OPEID6; resolved via Scorecard API search",
}


def resolve_match_status(system_code: str, canonical_name: str) -> tuple[str, str]:
    """Return (match_status, source_note) for an identifier row."""
    source_note = SOURCE_NOTES.get(system_code, "")
    if system_code in ("SCORECARD_UNITID", "SCORECARD_OPEID6"):
        return "resolved", source_note
    if system_code in ("ROR", "OPENALEX"):
        return "external_registry", source_note
    if system_code == "IPEDS":
        return "ipeds_matched", source_note
    return "unknown", source_note


def backfill(conn: sqlite3.Connection) -> list[tuple[str, str, int, str]]:
    rows = conn.execute(
        """
        SELECT
            ui.university_id,
            ui.system_code,
            u.canonical_name,
            ui.match_status
        FROM university_identifiers ui
        JOIN universities u ON u.university_id = ui.university_id
        """
    ).fetchall()

    updates: list[tuple[str, str, int, str]] = []
    for row in rows:
        if row["match_status"] is not None:
            continue
        status, note = resolve_match_status(row["system_code"], row["canonical_name"])
        updates.append((status, note, int(row["university_id"]), row["system_code"]))

    conn.executemany(
        """
        UPDATE university_identifiers
        SET match_status = ?, source_note = ?
        WHERE university_id = ? AND system_code = ?
        """,
        updates,
    )
    return updates


class BackfillIdentityMatchStatusLoader(ManualDataLoader):
    source_code = "BACKFILL_IDENTITY_MATCH_STATUS"
    description = "Backfill match_status and source_note on university_identifiers."
    requires_config = False
    execute_help = "write changes to SQLite (default is dry-run)"

    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conn = self._require_conn(conn)
        updates = backfill(conn)
        status_counts: dict[str, int] = {}
        for status, _note, _uid, _code in updates:
            status_counts[status] = status_counts.get(status, 0) + 1

        if args.execute:
            conn.commit()
        else:
            conn.rollback()
        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "updated_rows": len(updates),
            "status_counts": status_counts,
        }


if __name__ == "__main__":
    raise SystemExit(BackfillIdentityMatchStatusLoader().run())
