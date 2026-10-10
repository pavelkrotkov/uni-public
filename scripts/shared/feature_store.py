"""The feature-store contract: the seam between the builder and the scorer.

``outputs/current/feature_store.parquet`` is the widest interface in the repo.
``build_field_cluster_features.py`` writes it and ``score_field_clusters.py``
reads it, and until this module existed neither side declared the column names
they agreed on.

That mattered because the scorer reads defensively. ``metric_value`` turns any
absent key into ``None`` and ``safe_mean`` turns an all-``None`` component into
``50.0`` — so renaming or dropping a column did not raise. It scored that
component at the exact middle of the range for every university and wrote
``rankings_balanced.csv`` as though nothing had happened.

This module owns the names. Both sides validate against it, so a rename fails
loudly at the seam instead of quietly at rank 40.

Three groups of names live here:

* :data:`COLUMNS` — what the builder emits, in order.
* :data:`DERIVED_COLUMNS` — what the scorer computes from those before ranking.
* :data:`PERCENTILE_SPECS` — the metrics ranked within a field cluster, each
  with the direction that counts as good.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

#: Every column ``build_field_cluster_features.py`` emits, in emission order.
#: Order is part of the contract: it is the parquet column order.
COLUMNS: tuple[str, ...] = (
    # identity and cluster
    "university_id",
    "canonical_name",
    "field_cluster_slug",
    "field_cluster_label",
    "scoring_profile",
    "state_code",
    "city",
    "control",
    "country_code",
    # programme offer coverage
    "offers_any_target_cip",
    "offered_direct_cip_count",
    "offered_family_cip_count",
    "strict_target_cip_count",
    "family_target_cip_count",
    "strict_target_subfield_count",
    "family_target_subfield_count",
    "strict_unmapped_offered_cip_count",
    "family_unmapped_offered_cip_count",
    "strict_unmapped_offered_cips",
    "family_unmapped_offered_cips",
    # completions
    "recent_bachelors_completions_direct",
    "recent_bachelors_completions_family",
    "recent_total_bachelors",
    "recent_bachelors_completions_direct_by_year",
    "recent_bachelors_completions_family_by_year",
    "recent_total_bachelors_by_year",
    "target_completions_share_direct",
    "target_completions_share_family",
    "program_persistence_years_direct",
    "program_persistence_years_family",
    "min_recent_bachelors_threshold_passed",
    "completion_lookback_start",
    "completion_lookback_end",
    # research
    "research_publications_direct_weighted_5y",
    "research_publications_family_weighted_5y",
    "research_publications_primary_weighted_5y",
    "research_publications_secondary_weighted_5y",
    "research_publications_adjacent_weighted_5y",
    "research_avg_citation_impact_direct",
    "research_avg_citation_impact_family",
    "research_latest_year_publications_direct_weighted",
    "research_latest_year_publications_family_weighted",
    "research_trend_slope_direct",
    "research_trend_slope_family",
    "research_years_with_activity_direct",
    "research_years_with_activity_family",
    "research_counting_method",
    # grants
    "usaspending_total_5y",
    "nih_total_5y",
    "nsf_total_5y",
    "federal_grants_total_5y",
    "federal_grants_count_5y",
    "grant_sources_present_count",
    "grant_intensity_per_publication",
    # Scorecard field outcomes
    "scorecard_direct_earnings_1yr",
    "scorecard_direct_earnings_5yr",
    "scorecard_direct_median_debt",
    "scorecard_direct_completers",
    "scorecard_family_earnings_1yr",
    "scorecard_family_earnings_5yr",
    "scorecard_family_median_debt",
    "scorecard_family_completers",
    "effective_outcomes_source",
    "field_outcome_available",
    "using_family_field_outcomes",
    "using_institution_fallback",
    "student_outcomes_earnings_1yr",
    "student_outcomes_earnings_5yr",
    "student_outcomes_debt",
    # Scorecard institution metrics
    "scorecard_institution_completion_rate",
    "scorecard_institution_retention_rate",
    "avg_annual_cost",
    "net_price_0_30000",
    "net_price_30001_48000",
    "net_price_48001_75000",
    "net_price_75001_110000",
    "net_price_110001_plus",
    "debt_to_earnings_proxy",
    "admission_rate",
    "sat_average_overall",
    "sat_50_math",
    "sat_50_reading",
    "act_50_cumulative",
    "pell_share",
    "selectivity_band",
    # enrichment flags
    "has_undergrad_research_flag",
    "has_internship_flag",
    "has_study_abroad_flag",
    # confidence and eligibility
    "confidence_program",
    "confidence_research",
    "confidence_outcomes",
    "confidence_mapping",
    "confidence_identity",
    "missing_feature_count",
    "missing_critical_feature_count",
    "thin_completion_flag",
    "thin_research_flag",
    "missing_field_outcomes_flag",
    "sparse_mapping_flag",
    "eligible_for_ranking",
    "exclusion_reason",
    "source_openalex_run_dir",
)

COLUMN_SET: frozenset[str] = frozenset(COLUMNS)

#: Columns the scorer computes from :data:`COLUMNS` before ranking. They are not
#: in the feature store, so they are excluded from contract checks.
DERIVED_COLUMNS: frozenset[str] = frozenset(
    {
        "test_score_context_raw",
        "completion_rate_raw",
        "retention_rate_raw",
        "enrichment_raw",
    }
)

#: Suffix appended by the scorer when it ranks a metric within a field cluster.
PERCENTILE_SUFFIX = "_pct"


class FeatureStoreContractError(RuntimeError):
    """The feature store does not match the contract in this module."""


@dataclass(frozen=True)
class PercentileSpec:
    """One metric ranked within a field cluster.

    ``lower_is_better`` marks the metrics where a smaller number is the better
    outcome — cost, debt, and admission rate — so the percentile is inverted.
    """

    column: str
    lower_is_better: bool = False

    @property
    def percentile_column(self) -> str:
        return f"{self.column}{PERCENTILE_SUFFIX}"


#: Every metric the scorer ranks, with its direction. Columns not in
#: :data:`COLUMNS` must be in :data:`DERIVED_COLUMNS`; :func:`check_contract`
#: enforces that at import time.
PERCENTILE_SPECS: tuple[PercentileSpec, ...] = (
    PercentileSpec("recent_bachelors_completions_direct"),
    PercentileSpec("recent_bachelors_completions_family"),
    PercentileSpec("target_completions_share_direct"),
    PercentileSpec("program_persistence_years_family"),
    PercentileSpec("offered_direct_cip_count"),
    PercentileSpec("research_publications_direct_weighted_5y"),
    PercentileSpec("research_publications_family_weighted_5y"),
    PercentileSpec("research_avg_citation_impact_direct"),
    PercentileSpec("research_avg_citation_impact_family"),
    PercentileSpec("federal_grants_total_5y"),
    PercentileSpec("research_trend_slope_family"),
    PercentileSpec("grant_intensity_per_publication"),
    PercentileSpec("student_outcomes_earnings_5yr"),
    PercentileSpec("student_outcomes_earnings_1yr"),
    PercentileSpec("student_outcomes_debt", lower_is_better=True),
    PercentileSpec("debt_to_earnings_proxy", lower_is_better=True),
    PercentileSpec("completion_rate_raw"),
    PercentileSpec("retention_rate_raw"),
    PercentileSpec("avg_annual_cost", lower_is_better=True),
    PercentileSpec("admission_rate", lower_is_better=True),
    PercentileSpec("pell_share"),
    PercentileSpec("test_score_context_raw"),
)

#: Columns the scorer reads directly rather than through a percentile: the
#: confidence components, the penalty counters, the eligibility gate, and the
#: values quoted in the reason strings.
SCORER_DIRECT_INPUTS: frozenset[str] = frozenset(
    {
        # university_id x field_cluster_slug is the ranking grain. The scorer
        # never reads university_id by name — it copies whole rows into the
        # output — so without it here a store lacking the project's canonical
        # key would score cleanly and emit rankings nothing could join back to
        # a university.
        "university_id",
        "canonical_name",
        "field_cluster_slug",
        "scoring_profile",
        "eligible_for_ranking",
        "confidence_program",
        "confidence_research",
        "confidence_outcomes",
        "confidence_mapping",
        "confidence_identity",
        "missing_critical_feature_count",
        "thin_research_flag",
        "thin_completion_flag",
        "has_undergrad_research_flag",
        "sat_average_overall",
        "act_50_cumulative",
        "scorecard_institution_completion_rate",
        "scorecard_institution_retention_rate",
        "recent_bachelors_completions_family",
        "research_publications_family_weighted_5y",
        "student_outcomes_earnings_5yr",
        "avg_annual_cost",
        "debt_to_earnings_proxy",
        "admission_rate",
        "pell_share",
    }
)


#: How each component score is weighted over ranked metrics. Component weights
#: used to be literals buried inside ``component_scores``; keeping them beside
#: the column names means a metric can only be weighted if it is also ranked,
#: which :func:`check_contract` enforces.
COMPONENT_WEIGHTS: Mapping[str, Mapping[str, float]] = {
    "program_availability_score": {
        "recent_bachelors_completions_direct": 0.35,
        "recent_bachelors_completions_family": 0.25,
        "target_completions_share_direct": 0.20,
        "program_persistence_years_family": 0.10,
        "offered_direct_cip_count": 0.10,
    },
    "research_opportunity_score": {
        "research_publications_direct_weighted_5y": 0.35,
        "research_publications_family_weighted_5y": 0.20,
        "research_avg_citation_impact_direct": 0.15,
        "research_avg_citation_impact_family": 0.10,
        "federal_grants_total_5y": 0.10,
        "research_trend_slope_family": 0.05,
        "grant_intensity_per_publication": 0.05,
    },
    "student_outcomes_score": {
        "student_outcomes_earnings_5yr": 0.40,
        "student_outcomes_earnings_1yr": 0.20,
        "student_outcomes_debt": 0.15,
        "debt_to_earnings_proxy": 0.15,
        "completion_rate_raw": 0.05,
        "retention_rate_raw": 0.05,
    },
    "value_score": {
        "avg_annual_cost": 0.45,
        "student_outcomes_debt": 0.20,
        "debt_to_earnings_proxy": 0.20,
        "pell_share": 0.15,
    },
    "selectivity_access_score": {
        "admission_rate": 0.45,
        "test_score_context_raw": 0.25,
        "pell_share": 0.20,
        "completion_rate_raw": 0.10,
    },
}

#: The component computed straight from a derived column rather than from
#: ranked metrics.
ENRICHMENT_COMPONENT = "enrichment_score"

#: Every component name, in the order they appear in the scored output.
COMPONENT_NAMES: tuple[str, ...] = (*COMPONENT_WEIGHTS, ENRICHMENT_COMPONENT)


def scorer_required_columns() -> frozenset[str]:
    """Every feature-store column the scorer needs in order to rank.

    Derived columns are excluded — the scorer computes those itself.
    """
    ranked = {spec.column for spec in PERCENTILE_SPECS} - DERIVED_COLUMNS
    return frozenset(ranked | SCORER_DIRECT_INPUTS)


def check_contract() -> None:
    """Fail fast if this module contradicts itself.

    Runs at import so a typo in :data:`PERCENTILE_SPECS` or
    :data:`SCORER_DIRECT_INPUTS` surfaces immediately rather than as a silent
    50.0 during scoring.
    """
    if len(COLUMN_SET) != len(COLUMNS):
        duplicates = sorted({name for name in COLUMNS if COLUMNS.count(name) > 1})
        raise FeatureStoreContractError(f"COLUMNS contains duplicates: {duplicates}")

    overlap = sorted(COLUMN_SET & DERIVED_COLUMNS)
    if overlap:
        raise FeatureStoreContractError(
            f"DERIVED_COLUMNS must not name feature-store columns: {overlap}"
        )

    ranked = [spec.column for spec in PERCENTILE_SPECS]
    if len(set(ranked)) != len(ranked):
        duplicates = sorted({name for name in ranked if ranked.count(name) > 1})
        raise FeatureStoreContractError(f"PERCENTILE_SPECS ranks a column twice: {duplicates}")

    unknown_ranked = sorted(set(ranked) - COLUMN_SET - DERIVED_COLUMNS)
    if unknown_ranked:
        raise FeatureStoreContractError(
            f"PERCENTILE_SPECS names columns that are neither emitted nor derived: {unknown_ranked}"
        )

    unknown_direct = sorted(SCORER_DIRECT_INPUTS - COLUMN_SET)
    if unknown_direct:
        raise FeatureStoreContractError(
            f"SCORER_DIRECT_INPUTS names columns the builder does not emit: {unknown_direct}"
        )

    # A component may only weight a metric that is actually ranked; otherwise
    # its percentile column never exists and the component silently scores 50.0.
    ranked_set = set(ranked)
    for component, weights in COMPONENT_WEIGHTS.items():
        unranked = sorted(set(weights) - ranked_set)
        if unranked:
            raise FeatureStoreContractError(
                f"component {component!r} weights metrics that PERCENTILE_SPECS does not "
                f"rank: {unranked}"
            )
        total = round(sum(weights.values()), 6)
        if total != 1.0:
            raise FeatureStoreContractError(
                f"component {component!r} weights sum to {total}, expected 1.0"
            )


def validate_emitted_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    """Check what the builder is about to write.

    Every row comes from the same dict literal, so one row is enough to detect
    a column drift — but the row count matters on its own. ``config.yaml`` must
    declare at least one field cluster (``validate_feature_config`` enforces
    that), so an empty result means zero universities were resolved. Writing it
    would produce a parquet with no schema at all, which the scorer would then
    read as an empty store and rank into empty output without complaint.
    """
    if not rows:
        raise FeatureStoreContractError(
            "the builder produced no feature-store rows. config.yaml declares at "
            "least one field cluster, so this means no universities resolved — "
            "writing it would emit a parquet with no schema. Check the "
            "universities table and the cluster CIP rules."
        )
    validate_emitted_columns(rows[0])


def validate_emitted_columns(row: Mapping[str, Any]) -> None:
    """Check one builder row against the contract, both directions.

    Called by the builder so a column added, removed, or renamed there fails at
    the point of change rather than downstream in the scorer.
    """
    emitted = set(row)
    missing = [name for name in COLUMNS if name not in emitted]
    unexpected = sorted(emitted - COLUMN_SET)
    problems = []
    if missing:
        problems.append(f"missing {missing}")
    if unexpected:
        problems.append(f"unexpected {unexpected}")
    if problems:
        raise FeatureStoreContractError(
            "feature-store row does not match the contract in shared/feature_store.py: "
            + "; ".join(problems)
            + ". Update COLUMNS there in the same change."
        )


def validate_rows_for_scoring(rows: Sequence[Mapping[str, Any]]) -> None:
    """Check a feature store read from parquet before scoring it.

    This is the check that turns the silent-50.0 failure into an error. Only the
    columns the scorer actually consumes are required, so an older feature store
    missing a purely descriptive column still scores.

    An empty store is rejected rather than ranked into empty output: a parquet
    written from zero rows carries no schema, so there is nothing to check the
    contract against and nothing legitimate it could mean.
    """
    if not rows:
        raise FeatureStoreContractError(
            "the feature store is empty, so no contract check is possible and "
            "ranking it would write empty output. Rebuild it with "
            "scripts/build_field_cluster_features.py."
        )
    required = scorer_required_columns()
    present = set(rows[0])
    missing = sorted(required - present)
    if missing:
        raise FeatureStoreContractError(
            f"feature store is missing {len(missing)} column(s) the scorer needs: {missing}. "
            "Rebuild it with scripts/build_field_cluster_features.py, or update "
            "shared/feature_store.py if the contract changed on purpose."
        )


def missing_from(names: Iterable[str]) -> list[str]:
    """Contract columns absent from ``names``. Useful in diagnostics and tests."""
    present = set(names)
    return [name for name in COLUMNS if name not in present]


check_contract()
