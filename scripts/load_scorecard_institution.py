#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from shared import ROOT
from shared.config import resolve_output_dir
from shared.data_loader import ManualDataLoader

SCORECARD_API_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"
SOURCE_SYSTEM = "College Scorecard API"
HEADERS = {"User-Agent": "univ-phase2/1.0"}
BASE_PARAMS: dict[str, str | int] = {"keys_nested": "true"}
FIELDS = ",".join(
    [
        "id",
        "school.name",
        "latest.admissions",
        "latest.cost",
        "latest.completion",
        "latest.earnings",
        "latest.aid",
        "latest.student.retention_rate",
    ]
)


@dataclass(frozen=True)
class TargetUniversity:
    university_id: int
    canonical_name: str
    ipeds_unitid: str | None
    scorecard_unitid: str | None


def resolve_output_path(config: dict[str, Any]) -> Path:
    output_dir = resolve_output_dir(config)
    return output_dir / "scorecard_institution.parquet"


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def scorecard_params(params: dict[str, str | int]) -> dict[str, str | int]:
    """Build the query params for a Scorecard request, including the API key."""
    api_key = os.environ.get("SCORECARD_API_KEY")
    if not api_key:
        raise RuntimeError("SCORECARD_API_KEY is required for Scorecard loaders.")
    return {**BASE_PARAMS, "api_key": api_key, "fields": FIELDS, **params}


def load_targets(conn: sqlite3.Connection) -> list[TargetUniversity]:
    rows = conn.execute(
        """
        SELECT
            u.university_id,
            u.canonical_name,
            ipeds.external_id AS ipeds_unitid,
            scorecard.external_id AS scorecard_unitid
        FROM universities u
        LEFT JOIN university_identifiers ipeds
          ON ipeds.university_id = u.university_id
         AND ipeds.system_code = 'IPEDS'
        LEFT JOIN university_identifiers scorecard
          ON scorecard.university_id = u.university_id
         AND scorecard.system_code = 'SCORECARD_UNITID'
        ORDER BY u.university_id
        """
    ).fetchall()
    return [
        TargetUniversity(
            university_id=row["university_id"],
            canonical_name=row["canonical_name"],
            ipeds_unitid=row["ipeds_unitid"],
            scorecard_unitid=row["scorecard_unitid"],
        )
        for row in rows
    ]


def nested_get(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if current is None or not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def coalesced_net_price(cost_payload: dict[str, Any], band: str) -> Any:
    consumer_band = nested_get(cost_payload, "net_price", "consumer", "by_income_level", band)
    return first_not_none(
        consumer_band,
        nested_get(cost_payload, "net_price", "public", "by_income_level", band),
        nested_get(cost_payload, "net_price", "private", "by_income_level", band),
    )


def parse_record(target: TargetUniversity, record: dict[str, Any] | None) -> dict[str, Any]:
    timestamp = datetime.now(UTC).isoformat()
    if record is None:
        return {
            "university_id": target.university_id,
            "canonical_name": target.canonical_name,
            "ipeds_unitid": target.ipeds_unitid,
            "scorecard_unitid": target.scorecard_unitid,
            "scorecard_school_name": None,
            "record_present": 0,
            "admission_rate": None,
            "sat_50_reading": None,
            "sat_50_math": None,
            "sat_average_overall": None,
            "act_50_cumulative": None,
            "avg_net_price_overall": None,
            "net_price_0_30000": None,
            "net_price_30001_48000": None,
            "net_price_48001_75000": None,
            "net_price_75001_110000": None,
            "net_price_110001_plus": None,
            "completion_rate": None,
            "completion_rate_4yr_150nt": None,
            "completion_rate_4yr_150nt_pooled": None,
            "retention_rate": None,
            "retention_rate_full_time": None,
            "retention_rate_full_time_pooled": None,
            "median_earnings_6yr_after_entry": None,
            "median_earnings_8yr_after_entry": None,
            "median_earnings_10yr_after_entry": None,
            "median_earnings_1yr_after_completion": None,
            "median_earnings_5yr_after_completion": None,
            "median_debt_completers": None,
            "pell_grant_rate": None,
            "source_system": SOURCE_SYSTEM,
            "source_note": f"No Scorecard institution record available as of {timestamp}.",
            "missing_reason": "missing_scorecard_unitid_or_api_record",
        }

    latest = record.get("latest", {})
    cost = latest.get("cost", {})
    completion = latest.get("completion", {})
    retention = nested_get(latest, "student", "retention_rate") or {}
    earnings = latest.get("earnings", {})
    aid = latest.get("aid", {})

    completion_rate = first_not_none(
        completion.get("completion_rate_4yr_150nt_pooled"),
        completion.get("completion_rate_4yr_150nt"),
    )
    retention_rate = first_not_none(
        nested_get(retention, "four_year", "full_time_pooled"),
        nested_get(retention, "overall", "full_time"),
        nested_get(retention, "four_year", "full_time"),
    )

    scorecard_record_id = record.get("id")

    return {
        "university_id": target.university_id,
        "canonical_name": target.canonical_name,
        "ipeds_unitid": target.ipeds_unitid,
        "scorecard_unitid": str(scorecard_record_id)
        if scorecard_record_id is not None
        else target.scorecard_unitid,
        "scorecard_school_name": nested_get(record, "school", "name"),
        "record_present": 1,
        "admission_rate": nested_get(latest, "admissions", "admission_rate", "overall"),
        "sat_50_reading": nested_get(
            latest, "admissions", "sat_scores", "50th_percentile", "critical_reading"
        ),
        "sat_50_math": nested_get(latest, "admissions", "sat_scores", "50th_percentile", "math"),
        "sat_average_overall": nested_get(latest, "admissions", "sat_scores", "average", "overall"),
        "act_50_cumulative": nested_get(
            latest, "admissions", "act_scores", "50th_percentile", "cumulative"
        ),
        "avg_net_price_overall": nested_get(cost, "avg_net_price", "overall"),
        "net_price_0_30000": coalesced_net_price(cost, "0-30000"),
        "net_price_30001_48000": coalesced_net_price(cost, "30001-48000"),
        "net_price_48001_75000": coalesced_net_price(cost, "48001-75000"),
        "net_price_75001_110000": coalesced_net_price(cost, "75001-110000"),
        "net_price_110001_plus": coalesced_net_price(cost, "110001-plus"),
        "completion_rate": completion_rate,
        "completion_rate_4yr_150nt": completion.get("completion_rate_4yr_150nt"),
        "completion_rate_4yr_150nt_pooled": completion.get("completion_rate_4yr_150nt_pooled"),
        "retention_rate": retention_rate,
        "retention_rate_full_time": first_not_none(
            nested_get(retention, "overall", "full_time"),
            nested_get(retention, "four_year", "full_time"),
        ),
        "retention_rate_full_time_pooled": nested_get(retention, "four_year", "full_time_pooled"),
        "median_earnings_6yr_after_entry": nested_get(earnings, "6_yrs_after_entry", "median"),
        "median_earnings_8yr_after_entry": first_not_none(
            nested_get(earnings, "8_yrs_after_entry", "median_earnings"),
            nested_get(earnings, "8_yrs_after_entry", "median"),
        ),
        "median_earnings_10yr_after_entry": nested_get(earnings, "10_yrs_after_entry", "median"),
        "median_earnings_1yr_after_completion": nested_get(
            earnings, "1_yr_after_completion", "median"
        ),
        "median_earnings_5yr_after_completion": nested_get(
            earnings, "5_yrs_after_completion", "median"
        ),
        "median_debt_completers": nested_get(aid, "median_debt", "completers", "overall"),
        "pell_grant_rate": aid.get("pell_grant_rate"),
        "source_system": SOURCE_SYSTEM,
        "source_note": f"Fetched via College Scorecard API on {timestamp}.",
        "missing_reason": None,
    }


def write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="snappy")


class LoadScorecardInstitutionLoader(ManualDataLoader):
    source_code = "LOAD_SCORECARD_INSTITUTION"
    description = "Export institution-level College Scorecard metrics for the local universe."
    execute_help = "write the parquet artifact (default is dry-run)"
    requires_json_source = True
    json_source_headers = HEADERS

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=10,
            help="number of Scorecard ids to request per API call (default: 10)",
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
        output_path = resolve_output_path(config)
        targets = load_targets(conn)

        records_by_id: dict[str, dict[str, Any]] = {}
        scorecard_ids = [target.scorecard_unitid for target in targets if target.scorecard_unitid]
        for id_chunk in chunked(scorecard_ids, args.chunk_size):
            payload = self.json_source.fetch(
                SCORECARD_API_URL,
                params=scorecard_params({"id": ",".join(id_chunk), "per_page": len(id_chunk)}),
            )
            for result in payload.get("results", []):
                records_by_id[str(result["id"])] = result

        rows = [
            parse_record(target, records_by_id.get(target.scorecard_unitid or ""))
            for target in targets
        ]
        rows.sort(key=lambda row: row["university_id"])
        matched = sum(row["record_present"] for row in rows)
        missing = len(rows) - matched

        if args.execute:
            write_parquet(output_path, rows)
        return rows, {
            "executed": args.execute,
            "output_path": str(output_path.relative_to(ROOT)),
            "matched": matched,
            "missing": missing,
        }


if __name__ == "__main__":
    raise SystemExit(LoadScorecardInstitutionLoader().run())
