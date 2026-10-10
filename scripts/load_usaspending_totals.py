#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///

from __future__ import annotations

import argparse
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from shared.data_loader import (
    GrantYearDataLoader,
    filter_targets_by_ids,
    grant_years_from_config,
    load_grant_targets,
)
from shared.http import JsonSource

USASPENDING_AWARD_COUNT_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award_count/"
USASPENDING_CATEGORY_URL = (
    "https://api.usaspending.gov/api/v2/search/spending_by_category/awarding_agency/"
)
USASPENDING_AWARD_TYPES_URL = "https://api.usaspending.gov/api/v2/references/award_types/"
HEADERS = {"User-Agent": "univ-phase2-5/1.0", "Content-Type": "application/json"}
FALLBACK_AWARD_TYPE_CODES = (
    "02",
    "03",
    "04",
    "05",
    "06",
    "07",
    "08",
    "09",
    "10",
    "11",
    "A",
    "B",
    "C",
    "D",
    "F001",
    "F002",
    "F003",
    "F004",
    "F005",
    "F006",
    "F007",
    "F008",
    "F009",
    "F010",
    "IDV_A",
    "IDV_B",
    "IDV_B_A",
    "IDV_B_B",
    "IDV_B_C",
    "IDV_C",
    "IDV_D",
    "IDV_E",
    "-1",
)


@dataclass(frozen=True)
class TargetUniversity:
    university_id: int
    canonical_name: str
    ueis: tuple[str, ...]


def fetch_award_type_codes(source: JsonSource) -> tuple[str, ...]:
    try:
        payload = source.fetch(USASPENDING_AWARD_TYPES_URL)
    except Exception:
        return FALLBACK_AWARD_TYPE_CODES

    codes: list[str] = []
    for category in payload.values():
        if isinstance(category, dict):
            codes.extend(code for code in category if code)
    resolved = tuple(dict.fromkeys(codes))
    return resolved or FALLBACK_AWARD_TYPE_CODES


def load_targets(conn: sqlite3.Connection) -> list[TargetUniversity]:
    return [
        TargetUniversity(
            university_id=row["university_id"],
            canonical_name=row["canonical_name"],
            ueis=tuple(row["ueis_csv"].split(",")) if row["ueis_csv"] else (),
        )
        for row in load_grant_targets(conn, require_uei=True)
    ]


def build_filters(
    *, ueis: tuple[str, ...], year: int, award_type_codes: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "award_type_codes": list(award_type_codes),
        "recipient_search_text": list(ueis),
        "time_period": [
            {
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
            }
        ],
    }


def fetch_award_count(source: JsonSource, filters: dict[str, Any]) -> int:
    response = source.fetch(
        USASPENDING_AWARD_COUNT_URL,
        payload={"filters": filters},
    )
    results = response.get("results", {})
    return int(sum(int(value or 0) for value in results.values()))


def fetch_total_amount(
    source: JsonSource,
    filters: dict[str, Any],
    *,
    page_size: int,
    sleep_seconds: float,
) -> tuple[float, int]:
    page = 1
    requests = 0
    total_amount = 0.0
    while True:
        response = source.fetch(
            USASPENDING_CATEGORY_URL,
            payload={"filters": filters, "page": page, "limit": page_size},
        )
        requests += 1
        results = response.get("results", [])
        total_amount += sum(float(row.get("amount") or 0.0) for row in results)
        metadata = response.get("page_metadata", {})
        if not metadata.get("hasNext"):
            break
        page += 1
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return total_amount, requests


def aggregate_yearly_totals(
    source: JsonSource,
    targets: list[TargetUniversity],
    *,
    start_year: int,
    end_year: int,
    page_size: int,
    sleep_seconds: float,
    award_type_codes: tuple[str, ...],
    request_timeout: float,
    max_attempts: int,
    snapshot: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``request_timeout`` and ``max_attempts`` are recorded in the manifest
    only; the JsonSource owns the actual transport policy."""
    rows: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    errored: list[dict[str, Any]] = []
    request_count = 0

    for index, target in enumerate(targets, start=1):
        print(
            f"[usaspending] {index}/{len(targets)} {target.university_id} {target.canonical_name}",
            flush=True,
        )
        if not target.ueis:
            unmatched.append(
                {
                    "university_id": target.university_id,
                    "canonical_name": target.canonical_name,
                    "ueis": [],
                }
            )
            continue
        matched_years = 0
        for year in range(start_year, end_year + 1):
            filters = build_filters(ueis=target.ueis, year=year, award_type_codes=award_type_codes)
            try:
                award_count = fetch_award_count(source, filters)
                request_count += 1
                total_amount, amount_requests = fetch_total_amount(
                    source,
                    filters,
                    page_size=page_size,
                    sleep_seconds=sleep_seconds,
                )
                request_count += amount_requests
            except Exception as exc:
                errored.append(
                    {
                        "university_id": target.university_id,
                        "canonical_name": target.canonical_name,
                        "ueis": list(target.ueis),
                        "year": year,
                        "error": str(exc),
                    }
                )
                continue

            if award_count == 0 and total_amount == 0:
                continue
            matched_years += 1
            rows.append(
                {
                    "university_id": target.university_id,
                    "year": year,
                    "grant_count": award_count or None,
                    "total_award_usd": round(total_amount, 2),
                    "source_snapshot": snapshot,
                }
            )

        if matched_years == 0:
            unmatched.append(
                {
                    "university_id": target.university_id,
                    "canonical_name": target.canonical_name,
                    "ueis": list(target.ueis),
                }
            )

    manifest = {
        "source_code": USASpendingTotalsLoader.source_code,
        "source_system": "USAspending API",
        "pulled_at": snapshot,
        "year_start": start_year,
        "year_end": end_year,
        "award_type_codes": list(award_type_codes),
        "page_size": page_size,
        "request_timeout": request_timeout,
        "max_attempts": max_attempts,
        "request_count": request_count,
        "rows_prepared": len(rows),
        "unmatched_universities": unmatched,
        "errored_universities": errored,
    }
    return rows, manifest


class USASpendingTotalsLoader(GrantYearDataLoader):
    source_code = "USASPENDING_ALL_FEDERAL"
    source_name = "USAspending all federal awards"
    manifest_filename = "usaspending_totals_manifest.json"
    staging_filename = "usaspending_totals.parquet"
    description = "Load USAspending top-line yearly federal award totals into grant tables."
    requires_json_source = True
    json_source_headers = HEADERS
    default_request_timeout = 45

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--page-size",
            type=int,
            default=100,
            help="USAspending page size (default: 100)",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=0.02,
            help="sleep between API page requests (default: 0.02)",
        )

    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict[str, Any],
        args: argparse.Namespace,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conn = self._require_conn(conn)
        start_year, end_year = grant_years_from_config(config)

        targets = load_targets(conn)
        if not targets:
            raise RuntimeError("No universities with UEI records in university_ueis found.")
        targets = filter_targets_by_ids(targets, args.university_id)

        award_type_codes = fetch_award_type_codes(self.json_source)
        return aggregate_yearly_totals(
            self.json_source,
            targets,
            start_year=start_year,
            end_year=end_year,
            page_size=args.page_size,
            sleep_seconds=args.sleep_seconds,
            award_type_codes=award_type_codes,
            request_timeout=args.request_timeout,
            max_attempts=args.max_attempts,
            snapshot=snapshot,
        )


if __name__ == "__main__":
    raise SystemExit(USASpendingTotalsLoader().run())
