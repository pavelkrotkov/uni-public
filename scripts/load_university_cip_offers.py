#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

from __future__ import annotations

import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from shared.data_loader import ManualDataLoader
from shared.http import JsonSource

API_URL = (
    "https://educationdata.urban.org/api/v1/college-university/ipeds/completions-cip-6/{year}/"
)
SOURCE_SYSTEM = "IPEDS"
SOURCE_NOTE = (
    "Derived from Urban Institute Education Data Portal ipeds/completions-cip-6 "
    "(1983-2022), filtered to race=99 and sex=99; positive awards_6digit indicates "
    "a university offered that CIP in the given year."
)

HEADERS = {"User-Agent": "univ-loader/1.0"}
PRINT_LOCK = threading.Lock()


def format_cip(raw: int | str) -> str:
    digits = f"{int(raw):06d}"
    return f"{digits[:2]}.{digits[2:]}"


def load_targets(conn: sqlite3.Connection) -> tuple[list[tuple[int, str, int]], set[str]]:
    universities = [
        (row["university_id"], row["canonical_name"], int(row["external_id"]))
        for row in conn.execute(
            """
            SELECT u.university_id, u.canonical_name, ui.external_id
            FROM universities u
            JOIN university_identifiers ui
              ON ui.university_id = u.university_id
             AND ui.system_code = 'IPEDS'
            ORDER BY u.university_id
            """
        )
    ]
    cips = {row["cip_code"] for row in conn.execute("SELECT cip_code FROM cips")}
    return universities, cips


def fetch_unitid_year(source: JsonSource, unitid: int, year: int) -> list[dict]:
    params = {
        "unitid": unitid,
        "race": 99,
        "sex": 99,
        "awlevel": 5,  # IPEDS award level 5 = Bachelor's degree
        "per_page": 10000,
    }
    data = source.fetch(API_URL.format(year=year), params=params)
    results = data.get("results", [])
    count = data.get("count", len(results))
    if count > len(results):
        raise RuntimeError(
            f"Unexpected pagination for unitid={unitid} year={year}: count={count} results={len(results)}"
        )
    return results


def collect_offers(
    source: JsonSource,
    universities: list[tuple[int, str, int]],
    target_cips: set[str],
    *,
    year_start: int,
    year_end: int,
    workers: int,
) -> dict[tuple[int, str], tuple[int, int]]:
    offers: dict[tuple[int, str], tuple[int, int]] = {}
    tasks = [
        (university_id, canonical_name, unitid, year)
        for university_id, canonical_name, unitid in universities
        for year in range(year_start, year_end + 1)
    ]

    completed = 0
    total = len(tasks)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(fetch_unitid_year, source, unitid, year): (
                university_id,
                canonical_name,
                unitid,
                year,
            )
            for university_id, canonical_name, unitid, year in tasks
        }
        for future in as_completed(future_map):
            university_id, _canonical_name, _unitid, year = future_map[future]
            rows = future.result()
            seen_for_year: set[str] = set()
            for row in rows:
                awards = row.get("awards_6digit")
                if awards is None or int(awards) <= 0:
                    continue
                cip_code = format_cip(row["cipcode_6digit"])
                if cip_code not in target_cips:
                    continue
                if cip_code in seen_for_year:
                    continue
                seen_for_year.add(cip_code)
                pair = (university_id, cip_code)
                current = offers.get(pair)
                if current is None:
                    offers[pair] = (year, year)
                else:
                    offers[pair] = (min(current[0], year), max(current[1], year))

            completed += 1
            if completed % 250 == 0 or completed == total:
                with PRINT_LOCK:
                    print(f"Fetched {completed}/{total} unitid-year slices", file=sys.stderr)

    return offers


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS university_cip_offers (
            university_id INTEGER NOT NULL,
            cip_code TEXT NOT NULL,
            first_year INTEGER NOT NULL,
            last_year INTEGER NOT NULL,
            source_system TEXT NOT NULL,
            source_note TEXT,
            PRIMARY KEY (university_id, cip_code),
            FOREIGN KEY (university_id) REFERENCES universities (university_id),
            FOREIGN KEY (cip_code) REFERENCES cips (cip_code)
        )
        """
    )


def write_offers(conn: sqlite3.Connection, offers: dict[tuple[int, str], tuple[int, int]]) -> None:
    create_table(conn)
    conn.execute("DELETE FROM university_cip_offers")
    conn.executemany(
        """
        INSERT INTO university_cip_offers (
            university_id,
            cip_code,
            first_year,
            last_year,
            award_level,
            source_system,
            source_note
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                university_id,
                cip_code,
                first_year,
                last_year,
                "bachelors",
                SOURCE_SYSTEM,
                SOURCE_NOTE,
            )
            for (university_id, cip_code), (first_year, last_year) in sorted(offers.items())
        ],
    )


class LoadUniversityCipOffersLoader(ManualDataLoader):
    source_code = "LOAD_UNIVERSITY_CIP_OFFERS"
    description = "Load university_cip_offers from IPEDS completions-cip-6 data."
    requires_config = False
    execute_help = "write changes to the live SQLite database (default is dry-run)"
    requires_json_source = True
    json_source_headers = HEADERS
    default_max_attempts = 4

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--workers",
            type=int,
            default=8,
            help="number of concurrent API workers to use (default: 8)",
        )
        parser.add_argument(
            "--year-start",
            type=int,
            default=1983,
            help="first IPEDS year to pull (default: 1983)",
        )
        parser.add_argument(
            "--year-end",
            type=int,
            default=2022,
            help="last IPEDS year to pull, inclusive (default: 2022)",
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
        if args.year_start > args.year_end:
            raise RuntimeError("--year-start must be <= --year-end")
        universities, target_cips = load_targets(conn)
        if not universities:
            raise RuntimeError("No universities with IPEDS identifiers found.")
        if not target_cips:
            raise RuntimeError("No CIP codes found in cips table.")

        offers = collect_offers(
            self.json_source,
            universities,
            target_cips,
            year_start=args.year_start,
            year_end=args.year_end,
            workers=args.workers,
        )

        write_offers(conn, offers)
        row_count = len(offers)
        university_count = len({university_id for university_id, _ in offers})
        cip_count = len({cip_code for _, cip_code in offers})

        if args.execute:
            conn.commit()
        else:
            conn.rollback()
        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "year_start": args.year_start,
            "year_end": args.year_end,
            "rows": row_count,
            "universities": university_count,
            "cips": cip_count,
        }


if __name__ == "__main__":
    raise SystemExit(LoadUniversityCipOffersLoader().run())
