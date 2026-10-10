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
import math
import sqlite3
import time
from collections import defaultdict
from typing import Any

from identity_overrides import alias_variants
from shared.data_loader import (
    GrantYearDataLoader,
    filter_targets_by_ids,
    grant_years_from_config,
    load_grant_targets,
)
from shared.http import JsonSource
from shared.institution_match import MatchCandidate, UniversityRef, matches

NSF_AWARDS_URL = "https://api.nsf.gov/services/v1/awards.json"
HEADERS = {"User-Agent": "univ-phase2-5/1.0"}


def load_targets(conn: sqlite3.Connection) -> list[UniversityRef]:
    return [
        UniversityRef(
            university_id=row["university_id"],
            canonical_name=row["canonical_name"],
            state_code=row["state_code"],
            ueis=frozenset(row["ueis_csv"].split(",")) if row["ueis_csv"] else frozenset(),
            aliases=tuple(dict.fromkeys(alias_variants(row["canonical_name"]))),
        )
        for row in load_grant_targets(conn)
    ]


def matches_target(target: UniversityRef, award: dict[str, Any]) -> bool:
    """Does an NSF award's awardee refer to ``target``?

    Only the field mapping is NSF-specific; the rule lives in
    shared.institution_match and is shared with the NIH loader.
    """
    candidate = MatchCandidate.build(
        award.get("awardeeName"),
        state_code=award.get("awardeeStateCode"),
        ueis=[award.get("ueiNumber"), award.get("parentUeiNumber")],
    )
    return matches(candidate, target).is_match


def fetch_awards_for_alias(
    source: JsonSource,
    target: UniversityRef,
    alias: str,
    *,
    start_year: int,
    end_year: int,
    page_size: int,
    sleep_seconds: float,
) -> tuple[list[dict[str, Any]], int]:
    if not target.state_code:
        return [], 0

    offset = 0
    requests = 0
    awards: list[dict[str, Any]] = []
    while True:
        params = {
            "awardeeName": f'"{alias}"',
            "awardeeStateCode": target.state_code,
            "dateStart": f"01/01/{start_year}",
            "dateEnd": f"12/31/{end_year}",
            "offset": offset,
            "rpp": page_size,
            "sortKey": "awardNumber",
        }
        response = source.fetch(NSF_AWARDS_URL, params=params)
        requests += 1
        chunk = response.get("response", {}).get("award", []) or []
        awards.extend(chunk)
        offset += len(chunk)
        if len(chunk) < page_size:
            break
        time.sleep(max(sleep_seconds, 0))
    return awards, requests


def matched_awards_for_target(
    source: JsonSource, target: UniversityRef, **kwargs: Any
) -> tuple[dict[str, dict[str, Any]], int]:
    deduped: dict[str, dict[str, Any]] = {}
    request_count = 0
    for alias in target.aliases:
        awards, requests = fetch_awards_for_alias(source, target, alias, **kwargs)
        request_count += requests
        deduped.update(
            (award_id, award)
            for award in awards
            if (award_id := str(award.get("id") or "").strip()) and matches_target(target, award)
        )
    return deduped, request_count


def yearly_rows_for_target(
    target: UniversityRef,
    awards: dict[str, dict[str, Any]],
    *,
    start_year: int,
    end_year: int,
    snapshot: str,
) -> list[dict[str, Any]]:
    yearly_awards: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for award_id in sorted(awards):
        award = awards[award_id]
        if not award.get("date"):
            continue
        year = int(str(award["date"])[-4:])
        if start_year <= year <= end_year:
            yearly_awards[year].append(award)
    return [
        {
            "university_id": target.university_id,
            "year": year,
            "grant_count": len(yearly_awards[year]),
            "total_award_usd": round(
                math.fsum(
                    float(award.get("fundsObligatedAmt") or 0) for award in yearly_awards[year]
                ),
                2,
            ),
            "source_snapshot": snapshot,
        }
        for year in sorted(yearly_awards)
    ]


def aggregate_yearly_totals(
    source: JsonSource,
    targets: list[UniversityRef],
    *,
    start_year: int,
    end_year: int,
    page_size: int,
    sleep_seconds: float,
    timeout: float,
    max_attempts: int,
    snapshot: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``timeout`` and ``max_attempts`` are recorded in the manifest only; the
    JsonSource owns the actual transport policy."""
    rows: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    request_count = 0
    matched_awards_total = 0

    for index, target in enumerate(targets, start=1):
        print(
            f"[nsf] {index}/{len(targets)} {target.university_id} {target.canonical_name}",
            flush=True,
        )
        deduped_awards, requests = matched_awards_for_target(
            source,
            target,
            start_year=start_year,
            end_year=end_year,
            page_size=page_size,
            sleep_seconds=sleep_seconds,
        )
        request_count += requests
        target_rows = yearly_rows_for_target(
            target,
            deduped_awards,
            start_year=start_year,
            end_year=end_year,
            snapshot=snapshot,
        )
        if not target_rows:
            unmatched.append(
                {
                    "university_id": target.university_id,
                    "canonical_name": target.canonical_name,
                    "state_code": target.state_code,
                    "ueis": sorted(target.ueis),
                }
            )
            continue

        matched_awards_total += len(deduped_awards)
        rows.extend(target_rows)

    manifest = {
        "source_code": NSFAwardsLoader.source_code,
        "source_system": "NSF Awards API",
        "pulled_at": snapshot,
        "year_start": start_year,
        "year_end": end_year,
        "page_size": page_size,
        "request_timeout": timeout,
        "max_attempts": max_attempts,
        "request_count": request_count,
        "matched_awards": matched_awards_total,
        "rows_prepared": len(rows),
        "unmatched_universities": unmatched,
    }
    return rows, manifest


class NSFAwardsLoader(GrantYearDataLoader):
    source_code = "NSF"
    source_name = "NSF Awards API"
    manifest_filename = "nsf_awards_manifest.json"
    staging_filename = "nsf_awards.parquet"
    description = "Load NSF top-line yearly award totals into grant tables."
    requires_json_source = True
    json_source_headers = HEADERS
    default_request_timeout = 30

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--page-size",
            type=int,
            choices=range(1, 26),
            default=25,
            help="NSF page size, 1-25 (default: 25)",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=float,
            default=0.05,
            help="sleep between API page requests (default: 0.05)",
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

        targets = filter_targets_by_ids(load_targets(conn), args.university_id)
        if not targets:
            raise RuntimeError("No target universities resolved for NSF loader.")

        return aggregate_yearly_totals(
            self.json_source,
            targets,
            start_year=start_year,
            end_year=end_year,
            page_size=args.page_size,
            sleep_seconds=args.sleep_seconds,
            timeout=args.request_timeout,
            max_attempts=args.max_attempts,
            snapshot=snapshot,
        )


if __name__ == "__main__":
    raise SystemExit(NSFAwardsLoader().run())
