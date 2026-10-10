"""Characterisation of feature-row assembly (#49).

``write_feature_store`` computed roughly half of each feature row inline and
then wrote parquet, so the assembly logic was only reachable through a file
write and the tested pure functions covered only the other half.

These tests pin the assembled rows themselves against a golden fixture captured
from the pre-refactor code, so restructuring where the computation lives cannot
change what it produces. The fixture covers the branches that live in the
assembly step: direct field outcomes, the institution-level fallback, missing
outcomes entirely, eligible and excluded rows, and a university with grants
versus one without.

Regenerate the fixture only when a change is *intended* to alter the feature
store, and say so in the commit message:

    uv run python tests/regenerate_golden_feature_rows.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import feature_row_fixture as fixture
import pytest
from build_field_cluster_features import (
    build_cluster_feature_rows,
    load_feature_artifacts,
    validate_feature_config,
)
from shared import ROOT
from shared.feature_store import COLUMNS

GOLDEN_PATH = ROOT / "tests" / "fixtures" / "golden_feature_rows.json"


@pytest.fixture
def rows(tmp_path: Path) -> list[dict[str, Any]]:
    conn = fixture.make_conn()
    try:
        fixture.write_artifacts(tmp_path)
        config = validate_feature_config(fixture.config(), conn)
        artifacts = load_feature_artifacts(config, conn, tmp_path)
        built = build_cluster_feature_rows(artifacts, config, output_dir=tmp_path)
    finally:
        conn.close()
    return fixture.normalise(built)


@pytest.fixture
def golden() -> list[dict[str, Any]]:
    return json.loads(GOLDEN_PATH.read_text())


def test_assembled_rows_match_the_golden_fixture(
    rows: list[dict[str, Any]], golden: list[dict[str, Any]]
) -> None:
    """The whole point: restructuring must not change a single value."""
    assert rows == golden


def test_every_row_carries_exactly_the_contract_columns(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        assert list(row) == list(COLUMNS)


def test_grain_is_one_row_per_university_and_cluster(rows: list[dict[str, Any]]) -> None:
    grain = [(row["university_id"], row["field_cluster_slug"]) for row in rows]
    assert len(set(grain)) == len(grain)
    assert len(grain) == 3 * 2


def test_rows_are_ordered_by_university_then_cluster(rows: list[dict[str, Any]]) -> None:
    assert [(r["university_id"], r["field_cluster_slug"]) for r in rows] == [
        (1, "molbio"),
        (1, "genetics"),
        (2, "molbio"),
        (2, "genetics"),
        (3, "molbio"),
        (3, "genetics"),
    ]


def _row(rows: list[dict[str, Any]], university_id: int, slug: str) -> dict[str, Any]:
    return next(
        r for r in rows if r["university_id"] == university_id and r["field_cluster_slug"] == slug
    )


# --------------------------------------------------------------------------
# the branches that live in the assembly step
# --------------------------------------------------------------------------


def test_direct_field_outcomes_are_preferred_when_present(rows: list[dict[str, Any]]) -> None:
    row = _row(rows, 1, "molbio")
    assert row["effective_outcomes_source"] == "field_direct"
    assert row["field_outcome_available"] == 1
    assert row["using_institution_fallback"] == 0
    assert row["missing_field_outcomes_flag"] == 0
    assert row["student_outcomes_earnings_5yr"] == 90000.0


def test_institution_fallback_runs_when_field_outcomes_are_absent(
    rows: list[dict[str, Any]],
) -> None:
    row = _row(rows, 2, "molbio")
    assert row["effective_outcomes_source"] == "institution_fallback"
    assert row["using_institution_fallback"] == 1
    assert row["using_family_field_outcomes"] == 0
    assert row["field_outcome_available"] == 0
    assert row["missing_field_outcomes_flag"] == 1


def test_institution_fallback_propagates_the_institution_level_values(
    rows: list[dict[str, Any]],
) -> None:
    """The fallback must carry real numbers, not just set its flags.

    effective_outcomes() reads median_earnings_*_after_completion and
    median_debt_completers, the exact keys load_scorecard_institution.py emits.
    An earlier version of this fixture used near-miss names, which those reads
    silently ignored — so the flags above passed while every fallback value was
    null and a regression in propagation would not have been caught.
    """
    row = _row(rows, 2, "molbio")
    assert row["student_outcomes_earnings_1yr"] == 42000.0
    assert row["student_outcomes_earnings_5yr"] == 61000.0
    assert row["student_outcomes_debt"] == 21000.0
    assert row["debt_to_earnings_proxy"] == pytest.approx(21000.0 / 61000.0)


def test_both_clusters_share_one_institution_level_fallback(
    rows: list[dict[str, Any]],
) -> None:
    """The fallback is institution-wide, so it cannot differ by cluster."""
    molbio = _row(rows, 2, "molbio")
    genetics = _row(rows, 2, "genetics")
    for column in (
        "student_outcomes_earnings_1yr",
        "student_outcomes_earnings_5yr",
        "student_outcomes_debt",
    ):
        assert molbio[column] == genetics[column], column


def test_field_outcomes_differ_per_cluster_when_they_are_available(
    rows: list[dict[str, Any]],
) -> None:
    """The contrast with the fallback: direct outcomes are per CIP family."""
    molbio = _row(rows, 1, "molbio")
    genetics = _row(rows, 1, "genetics")
    assert molbio["student_outcomes_earnings_5yr"] == 90000.0
    assert genetics["student_outcomes_earnings_5yr"] == 78000.0


def test_outcomes_are_missing_when_there_is_no_scorecard_record(
    rows: list[dict[str, Any]],
) -> None:
    row = _row(rows, 3, "molbio")
    assert row["effective_outcomes_source"] == "missing"
    assert row["student_outcomes_earnings_5yr"] is None
    assert row["admission_rate"] is None


def test_a_university_offering_nothing_is_excluded_with_a_reason(
    rows: list[dict[str, Any]],
) -> None:
    row = _row(rows, 3, "genetics")
    assert row["offers_any_target_cip"] == 0
    assert row["eligible_for_ranking"] == 0
    assert row["exclusion_reason"] == "no_target_offer"


def test_grant_totals_come_from_usaspending_not_the_sum_of_sources(
    rows: list[dict[str, Any]],
) -> None:
    """NIH and NSF are sub-components already inside the USAspending total."""
    row = _row(rows, 1, "molbio")
    assert row["usaspending_total_5y"] == 5000000.0
    assert row["nih_total_5y"] == 1000000.0
    assert row["nsf_total_5y"] == 250000.0
    assert row["federal_grants_total_5y"] == 5000000.0
    assert row["grant_sources_present_count"] == 3


def test_grant_intensity_is_null_when_a_university_has_no_grants(
    rows: list[dict[str, Any]],
) -> None:
    assert _row(rows, 2, "molbio")["federal_grants_total_5y"] == 0.0
    assert _row(rows, 3, "molbio")["grant_intensity_per_publication"] is None


def test_debt_to_earnings_is_derived_from_effective_outcomes(
    rows: list[dict[str, Any]],
) -> None:
    row = _row(rows, 1, "molbio")
    assert row["debt_to_earnings_proxy"] == pytest.approx(
        row["student_outcomes_debt"] / row["student_outcomes_earnings_5yr"]
    )


def test_missing_feature_counters_reflect_absent_values(rows: list[dict[str, Any]]) -> None:
    complete = _row(rows, 1, "molbio")
    empty = _row(rows, 3, "molbio")
    assert complete["missing_critical_feature_count"] == 0
    assert empty["missing_critical_feature_count"] == 3
    assert empty["missing_feature_count"] >= empty["missing_critical_feature_count"]


def test_all_five_confidence_components_are_populated(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        for component in (
            "confidence_program",
            "confidence_research",
            "confidence_outcomes",
            "confidence_mapping",
            "confidence_identity",
        ):
            assert isinstance(row[component], float), component
            assert 0.0 <= row[component] <= 1.0, component


def test_selectivity_band_follows_admission_rate(rows: list[dict[str, Any]]) -> None:
    assert (
        _row(rows, 1, "molbio")["selectivity_band"] != _row(rows, 2, "molbio")["selectivity_band"]
    )
    assert _row(rows, 3, "molbio")["selectivity_band"] == "unknown"
