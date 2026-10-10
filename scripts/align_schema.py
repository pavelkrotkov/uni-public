#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

from __future__ import annotations

import sqlite3
from typing import Any

from shared.data_loader import ManualDataLoader

IDENTIFIER_SYSTEM_ROWS = [
    (
        "IPEDS",
        "Integrated Postsecondary Education Data System",
        1,
        "NCES backbone institution universe seed and completions authority.",
    ),
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
    (
        "ROR",
        "Research Organization Registry",
        1,
        "Research-side institutional anchor used to resolve OpenAlex institutions.",
    ),
    (
        "OPENALEX",
        "OpenAlex",
        0,
        "Research publication join keyed from matched ROR institutions when available.",
    ),
]

OA_DOMAIN_ROWS = [
    (1, "Life Sciences"),
    (2, "Social Sciences"),
    (3, "Physical Sciences"),
    (4, "Health Sciences"),
]

COLUMN_ADDITIONS = {
    "universities": [
        ("state_code", "TEXT"),
        ("city", "TEXT"),
        ("control", "TEXT"),
        ("is_active", "INTEGER DEFAULT 1"),
    ],
    "identifier_systems": [
        ("is_authoritative", "INTEGER"),
        ("notes", "TEXT"),
    ],
    "university_identifiers": [
        ("match_status", "TEXT"),
        ("source_note", "TEXT"),
    ],
    "cips": [
        ("cip_level", "INTEGER"),
        ("cip_family_code", "TEXT"),
        ("is_active", "INTEGER"),
    ],
    "university_cip_offers": [
        ("award_level", "TEXT"),
    ],
    "cip_trains_subfield": [
        ("confidence", "REAL"),
        ("rationale", "TEXT"),
        ("mapping_source", "TEXT"),
    ],
}

CREATE_TABLE_STATEMENTS = {
    "university_ueis": """
        CREATE TABLE IF NOT EXISTS university_ueis (
            university_id    INTEGER NOT NULL,
            uei              TEXT NOT NULL,
            entity_name      TEXT,
            entity_role      TEXT,
            is_primary       INTEGER DEFAULT 0,
            discovery_source TEXT,
            discovered_at    TEXT,
            notes            TEXT,
            PRIMARY KEY (university_id, uei),
            FOREIGN KEY (university_id) REFERENCES universities (university_id)
        )
    """,
    "oa_domains": """
        CREATE TABLE IF NOT EXISTS oa_domains (
            domain_id INTEGER PRIMARY KEY,
            domain_name TEXT NOT NULL
        )
    """,
    "university_subfield_year_metrics": """
        CREATE TABLE IF NOT EXISTS university_subfield_year_metrics (
            university_id INTEGER NOT NULL,
            subfield_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            counting_method TEXT NOT NULL,
            publications_count INTEGER NOT NULL,
            avg_citation_impact REAL,
            impact_metric_name TEXT,
            source_snapshot TEXT,
            source_note TEXT,
            PRIMARY KEY (university_id, subfield_id, year, counting_method),
            FOREIGN KEY (university_id) REFERENCES universities (university_id),
            FOREIGN KEY (subfield_id) REFERENCES oa_subfields (subfield_id)
        )
    """,
    "grant_sources": """
        CREATE TABLE IF NOT EXISTS grant_sources (
            grant_source_id INTEGER PRIMARY KEY,
            source_code TEXT NOT NULL,
            source_name TEXT NOT NULL,
            country_code TEXT,
            is_us_federal INTEGER
        )
    """,
    "university_grant_year_stats": """
        CREATE TABLE IF NOT EXISTS university_grant_year_stats (
            university_id INTEGER NOT NULL,
            grant_source_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            grant_count INTEGER,
            total_award_usd REAL,
            source_snapshot TEXT,
            PRIMARY KEY (university_id, grant_source_id, year),
            FOREIGN KEY (university_id) REFERENCES universities (university_id),
            FOREIGN KEY (grant_source_id) REFERENCES grant_sources (grant_source_id)
        )
    """,
    "university_grant_subfield_year_stats": """
        CREATE TABLE IF NOT EXISTS university_grant_subfield_year_stats (
            university_id INTEGER NOT NULL,
            grant_source_id INTEGER NOT NULL,
            subfield_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            grant_count INTEGER,
            total_award_usd REAL,
            source_snapshot TEXT,
            mapping_method TEXT,
            PRIMARY KEY (university_id, grant_source_id, subfield_id, year),
            FOREIGN KEY (university_id) REFERENCES universities (university_id),
            FOREIGN KEY (grant_source_id) REFERENCES grant_sources (grant_source_id),
            FOREIGN KEY (subfield_id) REFERENCES oa_subfields (subfield_id)
        )
    """,
}


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    actions: list[str] = []
    for table_name, columns in COLUMN_ADDITIONS.items():
        existing = table_columns(conn, table_name)
        for column_name, column_sql in columns:
            if column_name in existing:
                continue
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
            actions.append(f"added column {table_name}.{column_name}")
    return actions


def create_missing_tables(conn: sqlite3.Connection) -> list[str]:
    actions: list[str] = []
    for table_name, create_sql in CREATE_TABLE_STATEMENTS.items():
        existed = table_exists(conn, table_name)
        conn.execute(create_sql)
        if not existed:
            actions.append(f"created table {table_name}")
    return actions


def upsert_identifier_systems(conn: sqlite3.Connection) -> list[str]:
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
        IDENTIFIER_SYSTEM_ROWS,
    )
    return [f"upserted {len(IDENTIFIER_SYSTEM_ROWS)} identifier system rows"]


def upsert_oa_domains(conn: sqlite3.Connection) -> list[str]:
    conn.executemany(
        """
        INSERT INTO oa_domains (domain_id, domain_name)
        VALUES (?, ?)
        ON CONFLICT(domain_id) DO UPDATE SET domain_name = excluded.domain_name
        """,
        OA_DOMAIN_ROWS,
    )
    return [f"upserted {len(OA_DOMAIN_ROWS)} OpenAlex domain rows"]


def migrate_uei_to_dedicated_table(conn: sqlite3.Connection) -> list[str]:
    """Move UEI/DUNS out of university_identifiers into university_ueis.

    Idempotent: checks whether migration has already run by looking for rows
    in university_ueis before touching anything.
    """
    actions: list[str] = []

    already_seeded = conn.execute("SELECT COUNT(*) FROM university_ueis").fetchone()[0]
    uei_rows_in_identifiers = conn.execute(
        "SELECT COUNT(*) FROM university_identifiers WHERE system_code = 'UEI'"
    ).fetchone()[0]

    if not already_seeded and uei_rows_in_identifiers:
        from datetime import UTC, datetime

        now_iso = datetime.now(UTC).isoformat()

        # Map match_status → discovery_source
        rows = conn.execute(
            """
            SELECT ui.university_id, ui.external_id, ui.match_status
            FROM university_identifiers ui
            WHERE ui.system_code = 'UEI'
            """
        ).fetchall()

        def _source(match_status: str | None) -> str:
            if match_status in ("manual_pin", "second_pass_verified"):
                return "manual"
            return "sam_gov"

        uei_insert = [
            (
                row["university_id"],
                row["external_id"],
                None,  # entity_name — unknown at migration time
                "main",  # entity_role — assume main; harvest_ueis.py will expand
                1,  # is_primary
                _source(row["match_status"]),
                now_iso,
                f"migrated from university_identifiers (match_status={row['match_status']})",
            )
            for row in rows
        ]
        conn.executemany(
            """
            INSERT OR IGNORE INTO university_ueis
                (university_id, uei, entity_name, entity_role, is_primary,
                 discovery_source, discovered_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            uei_insert,
        )
        actions.append(f"migrated {len(uei_insert)} UEI rows to university_ueis")

    if uei_rows_in_identifiers:
        deleted = conn.execute(
            "DELETE FROM university_identifiers WHERE system_code IN ('UEI', 'DUNS')"
        ).rowcount
        if deleted:
            actions.append(f"removed {deleted} UEI/DUNS rows from university_identifiers")

    for code in ("UEI", "DUNS"):
        exists = conn.execute(
            "SELECT 1 FROM identifier_systems WHERE system_code = ?", (code,)
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM identifier_systems WHERE system_code = ?", (code,))
            actions.append(f"removed {code} from identifier_systems")

    return actions


def backfill_defaults(conn: sqlite3.Connection) -> list[str]:
    actions: list[str] = []

    universities_updated = conn.execute(
        """
        UPDATE universities
        SET is_active = 1
        WHERE is_active IS NULL
        """
    ).rowcount
    if universities_updated:
        actions.append(f"backfilled universities.is_active for {universities_updated} rows")

    cips_updated = conn.execute(
        """
        UPDATE cips
        SET cip_level = LENGTH(REPLACE(cip_code, '.', '')),
            cip_family_code = SUBSTR(cip_code, 1, 2),
            is_active = COALESCE(is_active, 1)
        WHERE cip_level IS NULL
           OR cip_family_code IS NULL
           OR is_active IS NULL
        """
    ).rowcount
    if cips_updated:
        actions.append(f"backfilled CIP metadata for {cips_updated} rows")

    return actions


def validate(conn: sqlite3.Connection) -> list[str]:
    validations: list[str] = []

    missing_columns: list[str] = []
    for table_name, columns in COLUMN_ADDITIONS.items():
        existing = table_columns(conn, table_name)
        for column_name, _ in columns:
            if column_name not in existing:
                missing_columns.append(f"{table_name}.{column_name}")
    if missing_columns:
        missing_text = ", ".join(sorted(missing_columns))
        raise RuntimeError(f"schema alignment missing columns: {missing_text}")
    validations.append("validated required columns")

    missing_tables = [
        table_name for table_name in CREATE_TABLE_STATEMENTS if not table_exists(conn, table_name)
    ]
    if missing_tables:
        raise RuntimeError(f"schema alignment missing tables: {', '.join(sorted(missing_tables))}")
    validations.append("validated required tables")

    domain_count = conn.execute("SELECT COUNT(*) FROM oa_domains").fetchone()[0]
    if domain_count != 4:
        raise RuntimeError(f"expected 4 oa_domains rows, found {domain_count}")
    validations.append("validated oa_domains row count = 4")

    system_codes = {
        row["system_code"] for row in conn.execute("SELECT system_code FROM identifier_systems")
    }
    required_systems = {row[0] for row in IDENTIFIER_SYSTEM_ROWS}
    missing_systems = sorted(required_systems - system_codes)
    if missing_systems:
        raise RuntimeError(
            f"missing identifier systems after alignment: {', '.join(missing_systems)}"
        )
    stale_systems = sorted(system_codes & {"UEI", "DUNS"})
    if stale_systems:
        raise RuntimeError(
            f"stale identifier systems still present (should be in university_ueis): {', '.join(stale_systems)}"
        )
    validations.append("validated identifier systems")

    cip_gaps = conn.execute(
        """
        SELECT COUNT(*)
        FROM cips
        WHERE cip_level IS NULL
           OR cip_family_code IS NULL
           OR is_active IS NULL
        """
    ).fetchone()[0]
    if cip_gaps:
        raise RuntimeError(f"{cip_gaps} cips rows still missing metadata backfill")
    validations.append("validated CIP metadata backfill")

    return validations


class AlignSchemaLoader(ManualDataLoader):
    source_code = "ALIGN_SCHEMA"
    description = "Align the live SQLite schema to roadmap section 7."
    requires_config = False
    execute_help = "write changes to the live SQLite database (default is dry-run)"

    def summary_extras(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "actions": manifest.get("actions", []),
            "validations": manifest.get("validations", []),
        }

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

        actions: list[str] = []
        actions.extend(add_missing_columns(conn))
        actions.extend(create_missing_tables(conn))
        actions.extend(upsert_identifier_systems(conn))
        actions.extend(migrate_uei_to_dedicated_table(conn))
        actions.extend(upsert_oa_domains(conn))
        actions.extend(backfill_defaults(conn))
        validations = validate(conn)

        if args.execute:
            conn.commit()
        else:
            conn.rollback()

        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "actions": actions,
            "validations": validations,
        }


if __name__ == "__main__":
    raise SystemExit(AlignSchemaLoader().run())
