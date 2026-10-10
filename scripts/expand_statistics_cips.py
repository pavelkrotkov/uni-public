#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

from __future__ import annotations

import sqlite3

from shared.data_loader import ManualDataLoader

STATISTICS_SUBFIELD_ID = 2613
MAPPING_SOURCE = "Phase 1 statistics cluster expansion"

STATISTICS_CIPS = [
    (
        "27.0502",
        "Mathematical Statistics and Probability",
        "primary",
        0.98,
        "Direct statistics-and-probability program alignment.",
    ),
    (
        "27.0503",
        "Mathematics and Statistics",
        "primary",
        0.9,
        "Combined mathematics/statistics programs still train directly into statistics.",
    ),
    (
        "27.0599",
        "Statistics, Other",
        "primary",
        0.85,
        "Other statistics programs remain closest to the statistics subfield.",
    ),
    (
        "27.0601",
        "Applied Statistics, General",
        "primary",
        0.98,
        "Applied statistics programs map directly to statistics and probability.",
    ),
]


def validate_schema(conn: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(cip_trains_subfield)")
    }
    required_columns = {"confidence", "rationale", "mapping_source"}
    missing = sorted(required_columns - existing_columns)
    if missing:
        raise RuntimeError(
            "Run scripts/align_schema.py first; missing cip_trains_subfield columns: "
            + ", ".join(missing)
        )


def write_statistics_cips(conn: sqlite3.Connection) -> tuple[int, int]:
    conn.executemany(
        """
        INSERT INTO cips (
            cip_code,
            cip_title,
            cip_level,
            cip_family_code,
            is_active
        )
        VALUES (?, ?, 6, '27', 1)
        ON CONFLICT(cip_code) DO UPDATE SET
            cip_title = excluded.cip_title,
            cip_level = excluded.cip_level,
            cip_family_code = excluded.cip_family_code,
            is_active = excluded.is_active
        """,
        [(cip_code, cip_title) for cip_code, cip_title, *_ in STATISTICS_CIPS],
    )
    cips_count = conn.execute("SELECT COUNT(*) FROM cips WHERE cip_code LIKE '27.%'").fetchone()[0]

    conn.executemany(
        """
        INSERT INTO cip_trains_subfield (
            cip_code,
            subfield_id,
            fit_level,
            confidence,
            rationale,
            mapping_source
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(cip_code, subfield_id) DO UPDATE SET
            fit_level = excluded.fit_level,
            confidence = excluded.confidence,
            rationale = excluded.rationale,
            mapping_source = excluded.mapping_source
        """,
        [
            (
                cip_code,
                STATISTICS_SUBFIELD_ID,
                fit_level,
                confidence,
                rationale,
                MAPPING_SOURCE,
            )
            for cip_code, _cip_title, fit_level, confidence, rationale in STATISTICS_CIPS
        ],
    )
    mapping_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM cip_trains_subfield
        WHERE cip_code LIKE '27.%'
        """
    ).fetchone()[0]

    return int(cips_count), int(mapping_count)


class ExpandStatisticsCipsLoader(ManualDataLoader):
    source_code = "EXPAND_STATISTICS_CIPS"
    description = "Expand the statistics CIP slice in the live SQLite database."
    requires_config = False
    execute_help = "write changes to the live SQLite database (default is dry-run)"

    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict,
        args,
        snapshot: str,
    ) -> tuple[list[dict], dict]:
        conn = self._require_conn(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

        validate_schema(conn)
        cips_count, mapping_count = write_statistics_cips(conn)

        if args.execute:
            conn.commit()
        else:
            conn.rollback()

        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "added_or_updated_cips": len(STATISTICS_CIPS),
            "stats_family_cips": cips_count,
            "stats_family_mappings": mapping_count,
        }


if __name__ == "__main__":
    raise SystemExit(ExpandStatisticsCipsLoader().run())
