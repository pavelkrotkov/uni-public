#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pandas>=2.0",
#   "numpy>=1.24",
#   "scikit-learn>=1.3",
#   "plotly>=5.18",
# ]
# ///
"""
Build the Naviance admission-probability explorer and deploy the stronger
same-target model only if it beats the current-like baseline on held-out data.
"""

from __future__ import annotations

from pathlib import Path

from report_renderer import render_report

import numpy as np
import pandas as pd

from admission_model_utils import (
    CANDIDATE_MODEL_SPEC,
    CURRENT_MODEL_SPEC,
    FittedAdmissionModel,
    candidate_beats_baseline,
    cross_validated_predictions,
    current_vs_candidate_improvement,
    feature_description,
    fit_final_model,
    load_points,
    summarize_metrics,
)


OUT_HTML = Path(__file__).resolve().parent / "admission_model.html"
OUT_CSV = Path(__file__).resolve().parent / "admission_probabilities.csv"
TEMPLATE_PATH = Path(__file__).resolve().parent / "admission_model.template.html"


def deployment_note(baseline_metrics: dict, candidate_metrics: dict, deployed_spec) -> str:
    deltas = current_vs_candidate_improvement(baseline_metrics, candidate_metrics)
    if deployed_spec.name == CANDIDATE_MODEL_SPEC.name:
        return (
            "Deployment gate passed: the candidate beat the current-like baseline on the same target "
            f"(AUC {deltas['auc_delta']:+.4f}, LogLoss {deltas['logloss_delta']:+.4f}, "
            f"College MAE {deltas['college_mae_delta']:+.4f}), so this HTML uses the candidate model."
        )
    return (
        "Deployment gate failed: the candidate did not dominate the current-like baseline on the same target, "
        "so this HTML stayed on the current-like model."
    )


def round_note(spec) -> str:
    if spec.split_early_rounds:
        return "This deployed model learns separate surfaces for Regular Decision, Early Action, and Early Decision."
    return "This deployed model pools Early Action and Early Decision into one early-round effect, so the EA and ED surfaces are identical."


def evaluate_specs(raw_df: pd.DataFrame) -> tuple:
    baseline_eval = cross_validated_predictions(raw_df, CURRENT_MODEL_SPEC)
    candidate_eval = cross_validated_predictions(raw_df, CANDIDATE_MODEL_SPEC)

    baseline_metrics = summarize_metrics(baseline_eval, CURRENT_MODEL_SPEC)
    candidate_metrics = summarize_metrics(candidate_eval, CANDIDATE_MODEL_SPEC)

    if candidate_beats_baseline(baseline_metrics, candidate_metrics):
        return CANDIDATE_MODEL_SPEC, candidate_eval, baseline_metrics, candidate_metrics
    return CURRENT_MODEL_SPEC, baseline_eval, baseline_metrics, candidate_metrics


def prediction_grid(model: FittedAdmissionModel, evaluated_df: pd.DataFrame) -> list[dict]:
    colleges: list[dict] = []
    deployed_df = evaluated_df.copy()

    for (college_id, college_name, score_type), grp in deployed_df.groupby(
        ["college_id", "college_name", "score_type"]
    ):
        if len(grp) < 5:
            continue

        accept_rate_pct = float(grp["acceptance_rate_pct"].iloc[0])
        if score_type == "SAT":
            scores = np.arange(1200, 1610, 10)
        else:
            scores = np.arange(25, 37, 1)
        gpas = np.arange(3.0, 5.01, 0.05)

        base_rows = [
            {
                "college_id": int(college_id),
                "college_name": college_name,
                "score_type": score_type,
                "acceptance_rate_pct": accept_rate_pct,
                "test_score": float(score),
                "gpa": float(gpa),
            }
            for score in scores
            for gpa in gpas
        ]

        round_probs: dict[str, list[list[float]]] = {}
        for round_code, decision_type in [
            ("rd", "Regular Decision"),
            ("ea", "Early Action"),
            ("ed", "Early Decision"),
        ]:
            grid_df = pd.DataFrame(base_rows)
            grid_df["decision_type"] = decision_type
            probs = model.predict_proba(grid_df).reshape(len(scores), len(gpas))
            round_probs[round_code] = [
                [round(float(prob), 3) for prob in row]
                for row in probs
            ]

        points = []
        for row in grp.itertuples(index=False):
            points.append(
                {
                    "score": round(float(row.test_score), 1),
                    "gpa": round(float(row.gpa), 3),
                    "admitted": int(row.admitted),
                    "early": int(row.decision_type in {"Early Decision", "Early Action"}),
                    "outcome": row.outcome_group,
                    "decision": row.decision_type,
                    "p": round(float(row.p_oof), 3),
                }
            )

        colleges.append(
            {
                "college_id": int(college_id),
                "name": college_name,
                "score_type": score_type,
                "accept_rate": round(accept_rate_pct, 1),
                "n_points": len(grp),
                "scores": [round(float(score), 1) for score in scores],
                "gpas": [round(float(gpa), 2) for gpa in gpas],
                "probs_rd": round_probs["rd"],
                "probs_ea": round_probs["ea"],
                "probs_ed": round_probs["ed"],
                "points": points,
            }
        )

    return colleges


def build_view_model(
    grid_data: list[dict],
    deployed_eval: pd.DataFrame,
    deployed_spec,
    deployed_metrics: dict,
    baseline_metrics: dict,
    candidate_metrics: dict,
) -> dict[str, object]:
    return {
        "DATA": grid_data,
        "N_POINTS": len(deployed_eval),
        "N_COLLEGES": int(deployed_eval["college_id"].nunique()),
        "MODEL_NAME": deployed_spec.name,
        "MODEL_DESC": deployed_spec.description,
        "FEATURES": feature_description(deployed_spec),
        "ROUND_NOTE": round_note(deployed_spec),
        "AUC": f"{deployed_metrics['auc']:.3f}",
        "BRIER": f"{deployed_metrics['brier']:.4f}",
        "LOGLOSS": f"{deployed_metrics['logloss']:.4f}",
        "DEPLOYMENT_NOTE": deployment_note(baseline_metrics, candidate_metrics, deployed_spec),
    }


def main() -> None:
    print("Loading scattergram data…")
    raw_df = load_points()
    print(f"  {len(raw_df)} raw points, {raw_df['college_id'].nunique()} colleges")

    print("Evaluating current vs candidate models…")
    deployed_spec, deployed_eval, baseline_metrics, candidate_metrics = evaluate_specs(raw_df)
    deployed_metrics = candidate_metrics if deployed_spec.name == CANDIDATE_MODEL_SPEC.name else baseline_metrics

    print(
        "  Baseline "
        f"AUC {baseline_metrics['auc']:.4f}  "
        f"Brier {baseline_metrics['brier']:.4f}  "
        f"LogLoss {baseline_metrics['logloss']:.4f}  "
        f"College MAE {baseline_metrics['college_mae']:.4f}"
    )
    print(
        "  Candidate "
        f"AUC {candidate_metrics['auc']:.4f}  "
        f"Brier {candidate_metrics['brier']:.4f}  "
        f"LogLoss {candidate_metrics['logloss']:.4f}  "
        f"College MAE {candidate_metrics['college_mae']:.4f}"
    )
    print(f"  Deployed spec: {deployed_spec.name}")

    print("Fitting deployed model on full data…")
    fitted_model, _ = fit_final_model(raw_df, deployed_spec)
    deployed_eval = deployed_eval.copy()
    deployed_eval["p_admit_final"] = fitted_model.predict_proba(deployed_eval)

    print("Generating prediction grids…")
    grid_data = prediction_grid(fitted_model, deployed_eval)
    print(f"  {len(grid_data)} college×score_type grids")

    out_cols = [
        "college_name",
        "score_type",
        "test_score",
        "gpa",
        "decision_type",
        "outcome_group",
        "admitted",
        "p_oof",
        "p_admit_final",
    ]
    deployed_eval[out_cols].rename(columns={"p_oof": "p_admit_oof"}).to_csv(
        OUT_CSV,
        index=False,
        float_format="%.3f",
    )
    print(f"  Predictions CSV: {OUT_CSV}")

    html = render_report(
        TEMPLATE_PATH,
        build_view_model(
            grid_data,
            deployed_eval,
            deployed_spec,
            deployed_metrics,
            baseline_metrics,
            candidate_metrics,
        ),
    )

    OUT_HTML.write_text(html, encoding="utf-8")
    size_mb = OUT_HTML.stat().st_size / (1024 * 1024)
    print(f"  HTML: {OUT_HTML} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
