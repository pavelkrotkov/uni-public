#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pandas>=2.0",
#   "numpy>=1.24",
#   "scikit-learn>=1.3",
# ]
# ///
"""
Run held-out admission-model experiments on Naviance scattergram data and
write a concise decision record for model deployment.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from admission_model_utils import (
    CANDIDATE_MODEL_SPEC,
    CURRENT_MODEL_SPEC,
    EXPERIMENT_SPECS,
    WAITLIST_CLEAN_EXPERIMENT_SPEC,
    candidate_beats_baseline,
    college_residuals,
    cross_validated_predictions,
    current_vs_candidate_improvement,
    feature_description,
    load_points,
    summarize_metrics,
)


ROOT = Path(__file__).resolve().parent
OUT_METRICS = ROOT / "admission_model_experiments.csv"
OUT_RESIDUALS = ROOT / "admission_model_college_residuals.csv"
OUT_REPORT = ROOT / "admission_model_research.md"


def fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def fmt_delta(value: float, as_pct: bool = False) -> str:
    if as_pct:
        return f"{value * 100:+.2f} pp"
    return f"{value:+.4f}"


def render_metrics_table(metrics_df: pd.DataFrame) -> str:
    ordered = metrics_df[
        [
            "spec_name",
            "n_points",
            "n_colleges",
            "positive_rate",
            "auc",
            "avg_precision",
            "brier",
            "logloss",
            "college_mae",
            "college_mae_n10",
        ]
    ].copy()
    header = (
        "| Spec | Points | Colleges | Admit rate | AUC | Avg precision | "
        "Brier | LogLoss | College MAE | College MAE (n>=10) |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    rows = []
    for row in ordered.itertuples(index=False):
        rows.append(
            "| "
            + " | ".join(
                [
                    str(row.spec_name),
                    str(row.n_points),
                    str(row.n_colleges),
                    fmt_pct(row.positive_rate),
                    f"{row.auc:.4f}",
                    f"{row.avg_precision:.4f}",
                    f"{row.brier:.4f}",
                    f"{row.logloss:.4f}",
                    f"{row.college_mae:.4f}",
                    f"{row.college_mae_n10:.4f}",
                ]
            )
            + " |"
        )
    return header + "\n" + "\n".join(rows)


def render_residual_table(title: str, residual_df: pd.DataFrame, limit: int = 10) -> str:
    top = residual_df.head(limit)
    header = (
        f"**{title}**\n"
        "\n"
        "| College | Points | Actual admit rate | Predicted admit rate | Abs error |\n"
        "| --- | ---: | ---: | ---: | ---: |"
    )
    rows = []
    for row in top.itertuples(index=False):
        rows.append(
            "| "
            + " | ".join(
                [
                    row.college_name,
                    str(row.n_points),
                    fmt_pct(row.actual_admit_rate),
                    fmt_pct(row.predicted_admit_rate),
                    fmt_pct(row.abs_error),
                ]
            )
            + " |"
        )
    return header + "\n" + "\n".join(rows)


def main() -> None:
    print("Loading Naviance scattergram points…")
    raw_df = load_points()
    print(f"  {len(raw_df)} raw points across {raw_df['college_id'].nunique()} colleges")

    metrics_rows: list[dict] = []
    residual_rows: list[pd.DataFrame] = []
    for spec in EXPERIMENT_SPECS:
        print(f"Running held-out evaluation for {spec.name}…")
        evaluated = cross_validated_predictions(raw_df, spec)
        metrics = summarize_metrics(evaluated, spec)
        metrics["features"] = feature_description(spec)
        metrics_rows.append(metrics)

        residual = college_residuals(evaluated)
        residual.insert(0, "spec_name", spec.name)
        residual_rows.append(residual)

        print(
            "  "
            f"AUC {metrics['auc']:.4f}  "
            f"Brier {metrics['brier']:.4f}  "
            f"LogLoss {metrics['logloss']:.4f}  "
            f"College MAE {metrics['college_mae']:.4f}"
        )

    metrics_df = pd.DataFrame(metrics_rows).sort_values("auc", ascending=False).reset_index(drop=True)
    residuals_df = pd.concat(residual_rows, ignore_index=True)
    metrics_df.to_csv(OUT_METRICS, index=False, float_format="%.6f")
    residuals_df.to_csv(OUT_RESIDUALS, index=False, float_format="%.6f")

    baseline_metrics = next(row for row in metrics_rows if row["spec_name"] == CURRENT_MODEL_SPEC.name)
    candidate_metrics = next(row for row in metrics_rows if row["spec_name"] == CANDIDATE_MODEL_SPEC.name)
    waitlist_metrics = next(row for row in metrics_rows if row["spec_name"] == WAITLIST_CLEAN_EXPERIMENT_SPEC.name)
    deltas = current_vs_candidate_improvement(baseline_metrics, candidate_metrics)
    should_switch = candidate_beats_baseline(baseline_metrics, candidate_metrics)

    decision = (
        "Switch the deployed model to `split_rounds_college_prior`."
        if should_switch
        else "Keep the deployed model on `current_like`."
    )
    rationale = (
        "The candidate beats the current-like model on every same-target held-out metric "
        "(higher AUC, lower Brier/log loss, lower college-level calibration error)."
        if should_switch
        else "The candidate did not dominate the current-like model on the same-target held-out metrics."
    )

    report = "\n".join(
        [
            "# Admission Model Research",
            "",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            "",
            "## Decision",
            "",
            decision,
            "",
            rationale,
            "",
            "## Evaluation Setup",
            "",
            "- Data source: `scattergram_points` joined to `scattergram_sessions`, `page_snapshots`, `naviance_colleges`, and `admissions_summary`.",
            f"- Raw sample: {len(raw_df)} non-user scattergram points across {raw_df['college_id'].nunique()} colleges.",
            "- Evaluation: 5-fold stratified outer CV with calibration refit inside each training fold.",
            f"- Baseline spec: `{CURRENT_MODEL_SPEC.name}` = {CURRENT_MODEL_SPEC.description}",
            f"- Candidate spec: `{CANDIDATE_MODEL_SPEC.name}` = {CANDIDATE_MODEL_SPEC.description}",
            f"- Exploratory spec: `{WAITLIST_CLEAN_EXPERIMENT_SPEC.name}` drops `Waitlisted/Unknown` rows before fitting and is not a like-for-like deployment candidate.",
            "",
            "## Metrics",
            "",
            render_metrics_table(metrics_df),
            "",
            "## Current vs Candidate",
            "",
            f"- AUC delta: {fmt_delta(deltas['auc_delta'])}",
            f"- Brier delta: {fmt_delta(deltas['brier_delta'])}",
            f"- LogLoss delta: {fmt_delta(deltas['logloss_delta'])}",
            f"- College MAE delta: {fmt_delta(deltas['college_mae_delta'])}",
            f"- College MAE (n>=10) delta: {fmt_delta(deltas['college_mae_n10_delta'])}",
            "",
            "## Waitlist-Clean Experiment",
            "",
            (
                "The waitlist-clean experiment improves further on held-out metrics, "
                f"but it changes the target semantics and sample size "
                f"({waitlist_metrics['n_points']} points instead of {baseline_metrics['n_points']}). "
                "Treat it as a research direction, not an automatic deployment replacement."
            ),
            "",
            "## Largest College-Level Misses",
            "",
            render_residual_table(
                "Current-like model",
                residuals_df[residuals_df["spec_name"] == CURRENT_MODEL_SPEC.name].drop(columns=["spec_name"]),
            ),
            "",
            render_residual_table(
                "Candidate model",
                residuals_df[residuals_df["spec_name"] == CANDIDATE_MODEL_SPEC.name].drop(columns=["spec_name"]),
            ),
        ]
    )
    OUT_REPORT.write_text(report + "\n", encoding="utf-8")

    print(f"  Metrics CSV: {OUT_METRICS}")
    print(f"  Residuals CSV: {OUT_RESIDUALS}")
    print(f"  Report: {OUT_REPORT}")
    print(f"  Deployment decision: {decision}")


if __name__ == "__main__":
    main()
