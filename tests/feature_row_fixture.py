"""A feature-builder fixture broad enough to characterise row assembly.

``write_feature_store`` computes roughly half the feature row inline — outcomes
aggregation, four of the five confidence components, the grant derivations, the
cost ratios, the missing-feature counters. Pinning its output needs inputs that
actually reach those branches, so this fixture defines three universities that
differ in the ways the function branches on:

* **1 Fullbright University** — complete data. Direct field outcomes, grants
  from two sources, research activity, eligible for ranking.
* **2 Fallback College** — no field-level Scorecard outcomes, so the
  institution-level fallback path runs and the missing-field-outcomes flag
  trips. No grants, so grant intensity stays null.
* **3 Nooffer Institute** — offers nothing in either cluster, so it is excluded
  and carries an exclusion reason.

Two clusters exercise the strict/family split: ``molbio`` has a family wider
than its strict set, ``genetics`` is narrow.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

RUN_DIR_PLACEHOLDER = "<run_dir>"

SCHEMA = """
CREATE TABLE cips (
    cip_code TEXT PRIMARY KEY,
    cip_family_code TEXT NOT NULL
);
CREATE TABLE cip_trains_subfield (
    cip_code TEXT NOT NULL,
    fit_level TEXT NOT NULL,
    subfield_id INTEGER NOT NULL,
    confidence REAL
);
CREATE TABLE universities (
    university_id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    state_code TEXT,
    city TEXT,
    control TEXT,
    country_code TEXT
);
CREATE TABLE university_identifiers (
    university_id INTEGER NOT NULL,
    system_code TEXT NOT NULL,
    external_id TEXT NOT NULL,
    match_status TEXT
);
CREATE TABLE grant_sources (
    grant_source_id INTEGER PRIMARY KEY,
    source_code TEXT NOT NULL UNIQUE
);
CREATE TABLE university_grant_year_stats (
    university_id INTEGER NOT NULL,
    grant_source_id INTEGER NOT NULL,
    year INTEGER NOT NULL,
    grant_count INTEGER,
    total_award_usd REAL
);
CREATE TABLE university_cip_offers (
    university_id INTEGER NOT NULL,
    cip_code TEXT NOT NULL,
    first_year INTEGER NOT NULL,
    last_year INTEGER NOT NULL
);

INSERT INTO cips VALUES
    ('26.0202', '26'), ('26.0203', '26'), ('26.0801', '26'), ('27.0501', '27');
INSERT INTO cip_trains_subfield VALUES
    ('26.0202', 'primary',   1103, 0.9),
    ('26.0202', 'secondary', 1110, 0.6),
    ('26.0203', 'adjacent',  1310, 0.4),
    ('26.0801', 'primary',   1110, 0.8);

INSERT INTO universities VALUES
    (1, 'Fullbright University', 'CA', 'Pasadena', 'private', 'US'),
    (2, 'Fallback College',      'NY', 'Ithaca',   'public',  'US'),
    (3, 'Nooffer Institute',     'TX', 'Austin',   'public',  'US');

INSERT INTO university_identifiers VALUES
    (1, 'OPENALEX', 'I123', 'verified'),
    (1, 'SCORECARD_UNITID', '999', 'verified'),
    (2, 'OPENALEX', 'I456', 'verified'),
    (3, 'SCORECARD_UNITID', '777', 'unverified');

INSERT INTO grant_sources VALUES (1, 'NIH'), (2, 'USASPENDING_ALL_FEDERAL'), (3, 'NSF');
INSERT INTO university_grant_year_stats VALUES
    (1, 1, 2023, 4, 1000000.0),
    (1, 2, 2023, 10, 5000000.0),
    (1, 3, 2023, 2, 250000.0);

INSERT INTO university_cip_offers VALUES
    (1, '26.0202', 2018, 2024),
    (1, '26.0203', 2020, 2024),
    (1, '26.0801', 2019, 2024),
    (2, '26.0202', 2021, 2024);
"""


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def config() -> dict[str, Any]:
    return {
        "project": {"output_dir": "outputs/current"},
        "years": {"research_start": 2019, "research_end": 2023},
        "institution_filters": {
            "min_recent_bachelors_in_target_family": 5,
        },
        "feature_flags": {"allow_institution_level_outcome_fallback": True},
        "scoring_profiles": {"balanced": {"weights": {}}},
        "field_clusters": [
            {
                "slug": "molbio",
                "label": "Molecular Biology",
                "scoring_profile": "balanced",
                "cip_rules": {
                    "include_6digit": ["26.0202", "26.0203"],
                    "include_2digit_families": ["26"],
                    "exclude_6digit": [],
                },
                "oa_rules": {
                    "primary_subfields": [1103],
                    "secondary_subfields": [1110],
                    "adjacent_subfields": [1310],
                },
                "fit_weights": {"primary": 1.0, "secondary": 0.6, "adjacent": 0.25},
            },
            {
                "slug": "genetics",
                "label": "Genetics",
                "scoring_profile": "balanced",
                "cip_rules": {
                    "include_6digit": ["26.0801"],
                    "include_2digit_families": [],
                    "exclude_6digit": [],
                },
                "oa_rules": {
                    "primary_subfields": [1110],
                    "secondary_subfields": [],
                    "adjacent_subfields": [],
                },
                "fit_weights": {"primary": 1.0, "secondary": 0.6, "adjacent": 0.25},
            },
        ],
    }


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def write_artifacts(output_dir: Path) -> None:
    """Write the parquet artifacts ``load_feature_artifacts`` expects."""
    _write(
        output_dir / "scorecard_institution.parquet",
        [
            {
                "university_id": 1,
                "record_present": 1,
                "admission_rate": 0.1,
                "pell_grant_rate": 0.2,
                "avg_net_price_overall": 30000.0,
                "net_price_0_30000": 5000.0,
                "completion_rate": 0.95,
                "retention_rate": 0.98,
                "sat_average_overall": 1500.0,
                "sat_50_math": 780.0,
                "sat_50_reading": 750.0,
                "act_50_cumulative": None,
                # Key names must match load_scorecard_institution.py exactly:
                # effective_outcomes() reads these and silently ignores anything
                # else, so a near-miss here would make the fallback look empty.
                "median_earnings_1yr_after_completion": 50000.0,
                "median_earnings_5yr_after_completion": 80000.0,
                "median_earnings_10yr_after_entry": 85000.0,
                "median_debt_completers": 15000.0,
            },
            {
                # No field-level outcomes for this one: exercises the fallback,
                # which is why its earnings and debt values matter.
                "university_id": 2,
                "record_present": 1,
                "admission_rate": 0.55,
                "pell_grant_rate": 0.44,
                "avg_net_price_overall": 18000.0,
                "net_price_0_30000": 9000.0,
                "completion_rate": 0.7,
                "retention_rate": 0.85,
                "sat_average_overall": None,
                "act_50_cumulative": 28.0,
                "median_earnings_1yr_after_completion": 42000.0,
                "median_earnings_5yr_after_completion": 61000.0,
                "median_earnings_10yr_after_entry": 64000.0,
                "median_debt_completers": 21000.0,
            },
        ],
    )
    _write(
        output_dir / "scorecard_field_outcomes.parquet",
        [
            {
                "university_id": 1,
                "cip4_code": "2602",
                "completers_count": 25,
                "median_earnings_1yr": 60000.0,
                "median_earnings_5yr": 90000.0,
                "debt_staff_grad_plus_eval_median": 18000.0,
            },
            {
                "university_id": 1,
                "cip4_code": "2608",
                "completers_count": 8,
                "median_earnings_1yr": 55000.0,
                "median_earnings_5yr": 78000.0,
                "debt_staff_grad_plus_eval_median": 16000.0,
            },
        ],
    )
    _write(
        output_dir / "completions_by_year.parquet",
        [
            {"university_id": 1, "cip_code": "26.0202", "year": 2022, "awards_count": 10},
            {"university_id": 1, "cip_code": "26.0202", "year": 2023, "awards_count": 12},
            {"university_id": 1, "cip_code": "26.0203", "year": 2023, "awards_count": 6},
            {"university_id": 1, "cip_code": "26.0801", "year": 2023, "awards_count": 9},
            {"university_id": 1, "cip_code": "00.0099", "year": 2023, "awards_count": 200},
            {"university_id": 2, "cip_code": "26.0202", "year": 2023, "awards_count": 7},
            {"university_id": 2, "cip_code": "00.0099", "year": 2023, "awards_count": 150},
            {"university_id": 3, "cip_code": "00.0099", "year": 2023, "awards_count": 90},
        ],
    )
    (output_dir / "completions_by_year_metadata.json").write_text(
        json.dumps({"available_years": [2019, 2020, 2021, 2022, 2023]})
    )
    _write(
        output_dir / "openalex-2024-01-01" / "openalex_metrics.parquet",
        [
            {
                "university_id": 1,
                "subfield_id": 1103,
                "year": 2022,
                "counting_method": "primary_subfield",
                "publications_count": 4.0,
                "avg_citation_impact": 1.2,
            },
            {
                "university_id": 1,
                "subfield_id": 1103,
                "year": 2023,
                "counting_method": "primary_subfield",
                "publications_count": 5.0,
                "avg_citation_impact": 1.5,
            },
            {
                "university_id": 1,
                "subfield_id": 1110,
                "year": 2023,
                "counting_method": "primary_subfield",
                "publications_count": 3.0,
                "avg_citation_impact": 0.9,
            },
            {
                "university_id": 2,
                "subfield_id": 1103,
                "year": 2023,
                "counting_method": "primary_subfield",
                "publications_count": 1.0,
                "avg_citation_impact": 0.5,
            },
        ],
    )


def normalise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the run-dir path, which embeds a per-test temp directory."""
    normalised = []
    for row in rows:
        copy = dict(row)
        copy["source_openalex_run_dir"] = RUN_DIR_PLACEHOLDER
        normalised.append(copy)
    return normalised
