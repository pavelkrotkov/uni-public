"""The feature-store contract, and the silent-50.0 defect it closes (#50).

The defect: ``score_field_clusters.py`` read the feature store with
``row.get(key)``, so a renamed or dropped column became ``None``, and
``safe_mean`` turned an all-``None`` component into ``50.0``. Rankings were
written as though nothing was wrong. These tests pin that it now raises.
"""

from __future__ import annotations

import ast
from typing import Any

import pytest
from score_field_clusters import component_scores, derived_metrics, score_rows
from shared import ROOT
from shared.feature_store import (
    COLUMN_SET,
    COLUMNS,
    COMPONENT_NAMES,
    COMPONENT_WEIGHTS,
    DERIVED_COLUMNS,
    ENRICHMENT_COMPONENT,
    PERCENTILE_SPECS,
    FeatureStoreContractError,
    check_contract,
    missing_from,
    scorer_required_columns,
    validate_emitted_columns,
    validate_emitted_rows,
    validate_rows_for_scoring,
)

CONFIG: dict[str, Any] = {
    "scoring_profiles": {"balanced": {"weights": dict.fromkeys(COMPONENT_NAMES, 1 / 6)}}
}


def _feature_row(**overrides: Any) -> dict[str, Any]:
    """A complete, contract-valid feature-store row."""
    row: dict[str, Any] = dict.fromkeys(COLUMNS, 0)
    row.update(
        {
            "university_id": 1,
            "canonical_name": "Example University",
            "field_cluster_slug": "molbio",
            "field_cluster_label": "Molecular Biology",
            "scoring_profile": "balanced",
            "eligible_for_ranking": 1,
            "admission_rate": 0.2,
            "pell_share": 0.3,
            "avg_annual_cost": 20000.0,
            "debt_to_earnings_proxy": 0.4,
            "student_outcomes_earnings_5yr": 70000.0,
            "sat_average_overall": 1400.0,
            "scorecard_institution_completion_rate": 0.9,
            "scorecard_institution_retention_rate": 0.95,
            "research_publications_family_weighted_5y": 12.5,
            "recent_bachelors_completions_family": 40,
        }
    )
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# the contract is internally consistent
# --------------------------------------------------------------------------


def test_contract_is_self_consistent() -> None:
    check_contract()


def test_contract_has_no_duplicate_columns() -> None:
    assert len(COLUMN_SET) == len(COLUMNS)


def test_every_ranked_metric_is_emitted_or_derived() -> None:
    for spec in PERCENTILE_SPECS:
        assert spec.column in COLUMN_SET or spec.column in DERIVED_COLUMNS, spec.column


def test_every_weighted_metric_is_ranked() -> None:
    """A weighted-but-unranked metric is exactly the silent-50.0 shape."""
    ranked = {spec.column for spec in PERCENTILE_SPECS}
    for component, weights in COMPONENT_WEIGHTS.items():
        assert set(weights) <= ranked, f"{component} weights unranked metrics"


def test_component_weights_each_sum_to_one() -> None:
    for component, weights in COMPONENT_WEIGHTS.items():
        assert round(sum(weights.values()), 6) == 1.0, component


def test_scorer_required_columns_excludes_derived_ones() -> None:
    assert not (scorer_required_columns() & DERIVED_COLUMNS)
    assert scorer_required_columns() <= COLUMN_SET


# --------------------------------------------------------------------------
# the defect: a missing column must raise, not score 50.0
# --------------------------------------------------------------------------


@pytest.mark.parametrize("column", sorted(scorer_required_columns()))
def test_scoring_raises_when_any_required_column_is_missing(column: str) -> None:
    row = _feature_row()
    del row[column]
    with pytest.raises(FeatureStoreContractError, match=column):
        score_rows(CONFIG, [row])


def test_the_error_names_every_missing_column_and_says_how_to_fix_it() -> None:
    row = _feature_row()
    del row["admission_rate"]
    del row["pell_share"]
    with pytest.raises(FeatureStoreContractError) as excinfo:
        score_rows(CONFIG, [row])
    message = str(excinfo.value)
    assert "admission_rate" in message
    assert "pell_share" in message
    assert "build_field_cluster_features.py" in message


def test_a_renamed_column_is_rejected_rather_than_ignored() -> None:
    """The regression this issue is about: a rename used to score 50.0."""
    row = _feature_row()
    row["admission_rate_v2"] = row.pop("admission_rate")
    with pytest.raises(FeatureStoreContractError, match="admission_rate"):
        score_rows(CONFIG, [row])


def test_scoring_a_complete_row_succeeds() -> None:
    ranked = score_rows(CONFIG, [_feature_row()])
    assert len(ranked) == 1
    assert ranked[0]["rank"] == 1
    for component in COMPONENT_NAMES:
        assert component in ranked[0]


def test_a_genuinely_null_value_is_still_tolerated() -> None:
    """Null data is normal and must not raise — only an absent column does."""
    ranked = score_rows(CONFIG, [_feature_row(admission_rate=None, pell_share=None)])
    assert len(ranked) == 1


def test_an_empty_feature_store_is_rejected_rather_than_ranked() -> None:
    """A parquet written from zero rows has no schema, so nothing can be checked."""
    with pytest.raises(FeatureStoreContractError, match="empty"):
        validate_rows_for_scoring([])


def test_the_builder_refuses_to_write_an_empty_feature_store() -> None:
    with pytest.raises(FeatureStoreContractError, match="no feature-store rows"):
        validate_emitted_rows([])


def test_validate_emitted_rows_accepts_a_contract_row() -> None:
    validate_emitted_rows([_feature_row(), _feature_row(university_id=2)])


def test_university_id_is_required_before_scoring() -> None:
    """The ranking grain is university_id x field_cluster_slug.

    The scorer never reads university_id by name, so without it in the contract
    a store missing the canonical key scored cleanly and produced rankings that
    could not be joined back to a university.
    """
    assert "university_id" in scorer_required_columns()
    row = _feature_row()
    del row["university_id"]
    with pytest.raises(FeatureStoreContractError, match="university_id"):
        score_rows(CONFIG, [row])


def test_the_whole_ranking_grain_is_required() -> None:
    assert {"university_id", "field_cluster_slug"} <= scorer_required_columns()


def test_a_purely_descriptive_column_is_not_required_by_the_scorer() -> None:
    """Only what the scorer consumes is required, so older stores still score."""
    row = _feature_row()
    del row["city"]
    assert score_rows(CONFIG, [row])


# --------------------------------------------------------------------------
# component scores read only percentile columns that exist
# --------------------------------------------------------------------------


def test_component_scores_returns_every_component() -> None:
    row = _feature_row()
    derived_metrics(row)
    scores = component_scores(row)
    assert set(scores) == set(COMPONENT_NAMES)


def test_enrichment_tracks_the_undergrad_research_flag() -> None:
    for flag, expected in ((0, 50.0), (1, 75.0)):
        row = _feature_row(has_undergrad_research_flag=flag)
        derived_metrics(row)
        assert component_scores(row)[ENRICHMENT_COMPONENT] == expected


# --------------------------------------------------------------------------
# the builder side of the seam
# --------------------------------------------------------------------------


def test_validate_emitted_columns_accepts_a_contract_row() -> None:
    validate_emitted_columns(_feature_row())


def test_validate_emitted_columns_rejects_a_dropped_column() -> None:
    row = _feature_row()
    del row["admission_rate"]
    with pytest.raises(FeatureStoreContractError, match=r"missing.*admission_rate"):
        validate_emitted_columns(row)


def test_validate_emitted_columns_rejects_an_undeclared_column() -> None:
    """Adding a column to the builder without the contract fails at the builder."""
    with pytest.raises(FeatureStoreContractError, match=r"unexpected.*brand_new_metric"):
        validate_emitted_columns(_feature_row(brand_new_metric=1))


def test_missing_from_reports_in_contract_order() -> None:
    assert missing_from(COLUMNS) == []
    assert missing_from(set(COLUMNS) - {"city", "university_id"}) == ["university_id", "city"]


def test_builder_row_literal_matches_the_contract_exactly() -> None:
    """Read the real emission site, not a fixture.

    ``build_cluster_feature_rows`` builds every row from one dict literal. Comparing
    that literal's keys to COLUMNS catches a column added or reordered in the
    builder without the contract being updated in the same change — including
    the ordering, which is the parquet column order.
    """
    tree = ast.parse((ROOT / "scripts" / "build_field_cluster_features.py").read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_cluster_feature_rows"
    )
    literal = next(
        call.args[0]
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "append"
        and call.args
        and isinstance(call.args[0], ast.Dict)
    )
    emitted = [key.value for key in literal.keys if isinstance(key, ast.Constant)]
    assert emitted == list(COLUMNS)
