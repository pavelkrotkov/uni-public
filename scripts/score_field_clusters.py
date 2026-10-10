#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from shared.config import resolve_output_dir
from shared.data_loader import ManualDataLoader
from shared.feature_store import (
    COMPONENT_WEIGHTS,
    ENRICHMENT_COMPONENT,
    PERCENTILE_SPECS,
    PERCENTILE_SUFFIX,
    PercentileSpec,
    validate_rows_for_scoring,
)
from shared.io_utils import load_parquet_rows, write_csv, write_parquet
from shared.math_utils import percentile_map


def safe_mean(pairs: list[tuple[float | None, float]], default: float = 50.0) -> float:
    numerator = 0.0
    denominator = 0.0
    for value, weight in pairs:
        if value is None or weight <= 0:
            continue
        numerator += value * weight
        denominator += weight
    if denominator == 0:
        return default
    return numerator / denominator


def metric_value(row: dict[str, Any], key: str) -> float | None:
    """Read a numeric metric, treating a null value as absent.

    A *missing column* is a different thing and must not reach here — it used to
    return None, which safe_mean turned into a 50.0 component score for every
    university. validate_rows_for_scoring() rejects that case up front, so a
    None here genuinely means the data was null for this row.
    """
    value = row.get(key)
    if value is None:
        return None
    return float(value)


def apply_percentiles(rows: list[dict[str, Any]], specs: Sequence[PercentileSpec]) -> None:
    """Rank each metric within one field cluster, writing the ``*_pct`` columns."""
    for spec in specs:
        values = [v for row in rows if (v := metric_value(row, spec.column)) is not None]
        mapping = percentile_map(values, reverse=spec.lower_is_better)
        for row in rows:
            raw = metric_value(row, spec.column)
            row[spec.percentile_column] = mapping.get(raw) if raw is not None else None


def derived_metrics(row: dict[str, Any]) -> None:
    sat_average = metric_value(row, "sat_average_overall")
    act_cumulative = metric_value(row, "act_50_cumulative")
    row["test_score_context_raw"] = (
        sat_average
        if sat_average is not None
        else (act_cumulative * 40.0 if act_cumulative is not None else None)
    )
    row["completion_rate_raw"] = metric_value(row, "scorecard_institution_completion_rate")
    row["retention_rate_raw"] = metric_value(row, "scorecard_institution_retention_rate")
    row["enrichment_raw"] = 50.0 + 25.0 * float(row["has_undergrad_research_flag"] or 0)


def component_scores(row: dict[str, Any]) -> dict[str, float]:
    """Weighted mean of ranked metrics per component.

    Weights and metric names come from COMPONENT_WEIGHTS in the feature-store
    contract, which guarantees every weighted metric is also ranked. Reading a
    percentile column that was never produced is what used to score a whole
    component at 50.0 for every university without failing.
    """
    scores = {
        component: round(
            safe_mean(
                [
                    (row.get(f"{metric}{PERCENTILE_SUFFIX}"), weight)
                    for metric, weight in weights.items()
                ]
            ),
            6,
        )
        for component, weights in COMPONENT_WEIGHTS.items()
    }
    scores[ENRICHMENT_COMPONENT] = round(float(row["enrichment_raw"]), 6)
    return scores


def overall_score(component_scores_by_name: dict[str, float], weights: dict[str, float]) -> float:
    return round(
        sum(
            component_scores_by_name[f"{metric}_score"] * float(weight)
            for metric, weight in weights.items()
        ),
        6,
    )


def confidence_score(row: dict[str, Any]) -> float:
    value = (
        0.25 * float(row["confidence_program"])
        + 0.20 * float(row["confidence_research"])
        + 0.25 * float(row["confidence_outcomes"])
        + 0.15 * float(row["confidence_mapping"])
        + 0.15 * float(row["confidence_identity"])
        - 0.03 * float(row["missing_critical_feature_count"])
        - 0.02 * float(row["thin_research_flag"])
        - 0.02 * float(row["thin_completion_flag"])
    )
    return round(max(0.0, min(1.0, value)), 6)


def build_reason_strings(
    row: dict[str, Any], component_scores_by_name: dict[str, float]
) -> list[str]:
    candidates = [
        (
            component_scores_by_name["program_availability_score"],
            f"{int(row['recent_bachelors_completions_family'])} recent completions in the target CIP family",
        ),
        (
            component_scores_by_name["research_opportunity_score"],
            f"{row['research_publications_family_weighted_5y']:.1f} weighted OpenAlex publications in mapped subfields",
        ),
        (
            component_scores_by_name["student_outcomes_score"],
            (
                f"${int(row['student_outcomes_earnings_5yr']):,} median five-year earnings"
                if row.get("student_outcomes_earnings_5yr") is not None
                else "usable student outcomes data despite suppressed field cells"
            ),
        ),
        (
            component_scores_by_name["value_score"],
            (
                f"${int(row['avg_annual_cost']):,} average net price with debt-to-earnings ratio "
                f"{row['debt_to_earnings_proxy']:.2f}"
                if row.get("avg_annual_cost") is not None
                and row.get("debt_to_earnings_proxy") is not None
                else "cost profile that holds up against observed outcomes"
            ),
        ),
        (
            component_scores_by_name["selectivity_access_score"],
            (
                f"{row['admission_rate']:.1%} admission rate and {row['pell_share']:.1%} Pell share"
                if row.get("admission_rate") is not None and row.get("pell_share") is not None
                else "strong admissions context for the cluster"
            ),
        ),
    ]
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [reason for _, reason in candidates[:3]]


def score_rows(config: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validate_rows_for_scoring(rows)
    profiles = {
        name: {
            metric.replace("_score", ""): weight
            for metric, weight in payload.get("weights", {}).items()
        }
        for name, payload in config.get("scoring_profiles", {}).items()
    }
    ranked_rows: list[dict[str, Any]] = []
    by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if int(row["eligible_for_ranking"]) != 1:
            continue
        working = dict(row)
        derived_metrics(working)
        by_cluster[working["field_cluster_slug"]].append(working)

    for _cluster_slug, cluster_rows in by_cluster.items():
        apply_percentiles(cluster_rows, PERCENTILE_SPECS)
        for row in cluster_rows:
            scores = component_scores(row)
            row.update(scores)
            for profile_name, weights in profiles.items():
                row[f"overall_{profile_name}_score"] = overall_score(scores, weights)
            row["confidence_score"] = confidence_score(row)
            reasons = build_reason_strings(row, scores)
            row["reason_1"] = reasons[0]
            row["reason_2"] = reasons[1]
            row["reason_3"] = reasons[2]
            row["score_profile_used"] = row["scoring_profile"]

        cluster_rows.sort(
            key=lambda row: (
                row["overall_balanced_score"],
                row["confidence_score"],
                row["research_opportunity_score"],
                row["recent_bachelors_completions_family"],
                row["canonical_name"],
            ),
            reverse=True,
        )
        for index, row in enumerate(cluster_rows, start=1):
            row["rank"] = index
            row["cluster_rank_size"] = len(cluster_rows)
            row["top25_low_confidence_flag"] = int(index <= 25 and row["confidence_score"] < 0.5)
            ranked_rows.append(row)

    ranked_rows.sort(key=lambda row: (row["field_cluster_slug"], row["rank"]))
    return ranked_rows


class ScoreFieldClustersLoader(ManualDataLoader):
    source_code = "SCORE_FIELD_CLUSTERS"
    description = "Score the field-cluster feature store and write balanced rankings."
    execute_help = "write rankings_balanced.parquet and rankings_balanced.csv"

    def load(
        self,
        *,
        conn,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        output_dir = resolve_output_dir(config)
        feature_store_path = output_dir / "feature_store.parquet"
        rows = load_parquet_rows(feature_store_path)
        ranked_rows = score_rows(config, rows)
        rankings_parquet_path = output_dir / "rankings_balanced.parquet"
        rankings_csv_path = output_dir / "rankings_balanced.csv"

        cluster_counts: dict[str, int] = defaultdict(int)
        for row in ranked_rows:
            cluster_counts[row["field_cluster_slug"]] += 1

        if args.execute:
            write_parquet(rankings_parquet_path, ranked_rows)
            write_csv(rankings_csv_path, ranked_rows)

        return ranked_rows, {
            "executed": args.execute,
            "feature_store_path": str(feature_store_path),
            "rankings_parquet_path": str(rankings_parquet_path),
            "rankings_csv_path": str(rankings_csv_path),
            "cluster_counts": dict(sorted(cluster_counts.items())),
        }


if __name__ == "__main__":
    raise SystemExit(ScoreFieldClustersLoader().run())
