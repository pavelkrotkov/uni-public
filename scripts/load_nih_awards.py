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
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from identity_overrides import alias_variants
from shared.data_loader import (
    GrantYearDataLoader,
    filter_targets_by_ids,
    grant_years_from_config,
    load_grant_targets,
)
from shared.http import JsonSource, JsonSourceHTTPError
from shared.institution_match import MatchCandidate, UniversityRef, matches

NIH_PROJECTS_URL = "https://api.reporter.nih.gov/v2/projects/search"
HEADERS = {"User-Agent": "univ-phase2-5/1.0", "Content-Type": "application/json"}


@dataclass(frozen=True)
class TargetUniversity:
    university_id: int
    canonical_name: str
    state_code: str | None
    ueis: tuple[str, ...]
    aliases: tuple[str, ...]

    def as_reference(self) -> UniversityRef:
        return UniversityRef(
            university_id=self.university_id,
            canonical_name=self.canonical_name,
            state_code=self.state_code,
            ueis=frozenset(self.ueis),
            aliases=self.aliases,
        )


def load_targets(conn: sqlite3.Connection) -> list[TargetUniversity]:
    return [
        TargetUniversity(
            university_id=row["university_id"],
            canonical_name=row["canonical_name"],
            state_code=row["state_code"],
            ueis=tuple(row["ueis_csv"].split(",")) if row["ueis_csv"] else (),
            aliases=tuple(dict.fromkeys(alias_variants(row["canonical_name"]))),
        )
        for row in load_grant_targets(conn)
    ]


def matches_target(target: TargetUniversity, organization: dict[str, Any]) -> bool:
    """Does an NIH project's organization refer to ``target``?

    Only the field mapping is NIH-specific; the rule lives in
    shared.institution_match and is shared with the NSF loader.
    """
    candidate = MatchCandidate.build(
        organization.get("org_name"),
        state_code=organization.get("org_state"),
        ueis=[organization.get("primary_uei"), *(organization.get("org_ueis") or [])],
    )
    return matches(candidate, target.as_reference()).is_match


def _fetch_pages(
    source: JsonSource,
    criteria: dict[str, Any],
    *,
    page_size: int,
    sleep_seconds: float,
) -> tuple[list[dict[str, Any]], int]:
    """Paginate through NIH RePORTER results for a given criteria dict."""
    offset = 0
    requests = 0
    projects: list[dict[str, Any]] = []
    while True:
        payload = {
            "criteria": criteria,
            "include_fields": ["ProjectNum", "AwardAmount", "FiscalYear", "Organization"],
            "offset": offset,
            "limit": page_size,
        }
        response = source.fetch(NIH_PROJECTS_URL, payload=payload)
        requests += 1
        results = response.get("results", [])
        projects.extend(results)
        offset += len(results)
        total = int(response.get("meta", {}).get("total") or 0)
        if offset >= total or not results:
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return projects, requests


def fetch_projects(
    source: JsonSource,
    target: TargetUniversity,
    *,
    years: list[int],
    page_size: int,
    sleep_seconds: float,
) -> tuple[list[dict[str, Any]], int]:
    alias_list = list(target.aliases[:8])
    if target.canonical_name not in alias_list:
        alias_list.insert(0, target.canonical_name)
    alias_list = list(dict.fromkeys(alias_list))[:8]

    return _fetch_pages(
        source,
        {"org_names": alias_list, "fiscal_years": years},
        page_size=page_size,
        sleep_seconds=sleep_seconds,
    )


def fetch_projects_by_uei(
    source: JsonSource,
    uei: str,
    *,
    years: list[int],
    page_size: int,
    sleep_seconds: float,
) -> tuple[list[dict[str, Any]], int]:
    """Query NIH by UEI directly — used as fallback when name-based search yields no matches."""
    return _fetch_pages(
        source,
        {"org_ueis": [uei], "fiscal_years": years},
        page_size=page_size,
        sleep_seconds=sleep_seconds,
    )


def aggregate_yearly_totals(
    source: JsonSource,
    targets: list[TargetUniversity],
    *,
    years: list[int],
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
    matched_projects_total = 0

    for target in targets:
        projects, requests = fetch_projects(
            source,
            target,
            years=years,
            page_size=page_size,
            sleep_seconds=sleep_seconds,
        )
        request_count += requests

        yearly_amounts: dict[int, float] = defaultdict(float)
        yearly_projects: dict[int, set[str]] = defaultdict(set)
        seen_project_years: set[tuple[int, str]] = set()

        for project in projects:
            organization = project.get("organization") or {}
            if not matches_target(target, organization):
                continue
            fiscal_year = project.get("fiscal_year")
            project_num = (project.get("project_num") or "").strip()
            if fiscal_year not in years or not project_num:
                continue
            dedupe_key = (int(fiscal_year), project_num)
            if dedupe_key in seen_project_years:
                continue
            seen_project_years.add(dedupe_key)
            amount = float(project.get("award_amount") or 0.0)
            yearly_amounts[int(fiscal_year)] += amount
            yearly_projects[int(fiscal_year)].add(project_num)

        if not yearly_amounts and target.ueis:
            # Name-based search returned nothing — retry by UEI directly.
            # This handles schools whose NIH org_name doesn't match any alias
            # (e.g. grants held by a foundation or SUNY system entity).
            # The org_ueis criterion was added to the NIH API in 2024; wrap in
            # a try-except so older API versions fail silently rather than crash.
            try:
                uei_projects, uei_requests = fetch_projects_by_uei(
                    source,
                    target.ueis[0],
                    years=years,
                    page_size=page_size,
                    sleep_seconds=sleep_seconds,
                )
                request_count += uei_requests
                for project in uei_projects:
                    organization = project.get("organization") or {}
                    if not matches_target(target, organization):
                        continue
                    fiscal_year = project.get("fiscal_year")
                    project_num = (project.get("project_num") or "").strip()
                    if fiscal_year not in years or not project_num:
                        continue
                    dedupe_key = (int(fiscal_year), project_num)
                    if dedupe_key in seen_project_years:
                        continue
                    seen_project_years.add(dedupe_key)
                    amount = float(project.get("award_amount") or 0.0)
                    yearly_amounts[int(fiscal_year)] += amount
                    yearly_projects[int(fiscal_year)].add(project_num)
            except JsonSourceHTTPError as exc:
                if exc.status == 400:
                    pass  # org_ueis criterion not supported by this API version
                else:
                    raise

        if not yearly_amounts:
            unmatched.append(
                {
                    "university_id": target.university_id,
                    "canonical_name": target.canonical_name,
                    "state_code": target.state_code,
                    "ueis": list(target.ueis),
                }
            )
            continue

        matched_projects_total += len(seen_project_years)
        for year in years:
            if year not in yearly_amounts:
                continue
            rows.append(
                {
                    "university_id": target.university_id,
                    "year": year,
                    "grant_count": len(yearly_projects[year]) or None,
                    "total_award_usd": round(yearly_amounts[year], 2),
                    "source_snapshot": snapshot,
                }
            )

    manifest = {
        "source_code": NIHAwardsLoader.source_code,
        "source_system": "NIH RePORTER API",
        "pulled_at": snapshot,
        "years": years,
        "year_start": years[0],
        "year_end": years[-1],
        "page_size": page_size,
        "request_timeout": timeout,
        "max_attempts": max_attempts,
        "request_count": request_count,
        "matched_project_years": matched_projects_total,
        "rows_prepared": len(rows),
        "unmatched_universities": unmatched,
    }
    return rows, manifest


class NIHAwardsLoader(GrantYearDataLoader):
    source_code = "NIH"
    source_name = "NIH RePORTER"
    manifest_filename = "nih_awards_manifest.json"
    staging_filename = "nih_awards.parquet"
    description = "Load NIH top-line yearly award totals into grant tables."
    requires_json_source = True
    json_source_headers = HEADERS
    default_request_timeout = 45

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--page-size", type=int, default=500, help="NIH page size (default: 500)"
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
        years = list(range(start_year, end_year + 1))

        targets = filter_targets_by_ids(load_targets(conn), args.university_id)
        if not targets:
            raise RuntimeError("No target universities resolved for NIH loader.")

        return aggregate_yearly_totals(
            self.json_source,
            targets,
            years=years,
            page_size=args.page_size,
            sleep_seconds=args.sleep_seconds,
            timeout=args.request_timeout,
            max_attempts=args.max_attempts,
            snapshot=snapshot,
        )


if __name__ == "__main__":
    raise SystemExit(NIHAwardsLoader().run())
