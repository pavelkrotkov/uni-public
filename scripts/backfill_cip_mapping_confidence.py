#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

"""
Phase 4.5 — Backfill confidence, rationale, and mapping_source on cip_trains_subfield.

All 140 hand-curated mappings are assigned:
  confidence     = 0.9   (high quality; manually reviewed against OpenAlex native taxonomy)
  mapping_source = 'hand_curated'
  rationale      = 'Manually curated from cip_to_openalex_subfields_openalex_native.md'

The 4 statistics-expansion rows that already have explicit confidence values are left untouched.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from shared.data_loader import ManualDataLoader

DEFAULT_CONFIDENCE = 0.9
DEFAULT_MAPPING_SOURCE = "hand_curated"
DEFAULT_RATIONALE = (
    "Manually curated from cip_to_openalex_subfields_openalex_native.md "
    "cross-referenced against OpenAlex native subfield taxonomy"
)


def backfill(conn: sqlite3.Connection) -> int:
    result = conn.execute(
        """
        UPDATE cip_trains_subfield
        SET
            confidence     = CASE WHEN confidence     IS NULL THEN ? ELSE confidence     END,
            mapping_source = CASE WHEN mapping_source IS NULL THEN ? ELSE mapping_source END,
            rationale      = CASE WHEN rationale      IS NULL THEN ? ELSE rationale      END
        WHERE confidence IS NULL OR mapping_source IS NULL OR rationale IS NULL
        """,
        (DEFAULT_CONFIDENCE, DEFAULT_MAPPING_SOURCE, DEFAULT_RATIONALE),
    )
    return result.rowcount


class BackfillCipMappingConfidenceLoader(ManualDataLoader):
    source_code = "BACKFILL_CIP_MAPPING_CONFIDENCE"
    description = "Backfill confidence metadata on cip_trains_subfield."
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
        count = backfill(conn)
        if args.execute:
            conn.commit()
        else:
            conn.rollback()
        return [], {"executed": args.execute, "db": str(args.db), "updated_rows": count}


if __name__ == "__main__":
    raise SystemExit(BackfillCipMappingConfidenceLoader().run())
