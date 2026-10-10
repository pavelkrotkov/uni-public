from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
DB = ROOT / "univ.sqlite3"

from shared.naviance import ADMITTED_OUTCOMES, SCATTERGRAM_POINTS_SQL, UNRESOLVED_OUTCOME  # noqa: E402


@dataclass(frozen=True)
class AdmissionModelSpec:
    name: str
    description: str
    drop_waitlisted_unknown: bool = False
    split_early_rounds: bool = False
    include_college_prior: bool = False
    include_score_type_indicator: bool = False
    college_prior_alpha: float = 25.0
    calibration_method: str = "sigmoid"
    calibration_cv: int = 3
    logistic_c: float = 1.0


CURRENT_MODEL_SPEC = AdmissionModelSpec(
    name="current_like",
    description="Current target + pooled logistic with one early flag, acceptance rate, and selectivity interactions.",
    split_early_rounds=False,
    include_college_prior=False,
    include_score_type_indicator=False,
    college_prior_alpha=25.0,
    calibration_method="sigmoid",
    calibration_cv=3,
    logistic_c=1.0,
)


CANDIDATE_MODEL_SPEC = AdmissionModelSpec(
    name="split_rounds_college_prior",
    description="Current target + separate EA/ED effects + smoothed college prior + selectivity interactions.",
    split_early_rounds=True,
    include_college_prior=True,
    include_score_type_indicator=False,
    college_prior_alpha=1.0,
    calibration_method="sigmoid",
    calibration_cv=3,
    logistic_c=1.0,
)


WAITLIST_CLEAN_EXPERIMENT_SPEC = AdmissionModelSpec(
    name="waitlist_clean_split_rounds_college_prior",
    description="Exploratory variant that drops Waitlisted/Unknown rows before fitting.",
    drop_waitlisted_unknown=True,
    split_early_rounds=True,
    include_college_prior=True,
    include_score_type_indicator=False,
    college_prior_alpha=1.0,
    calibration_method="sigmoid",
    calibration_cv=3,
    logistic_c=1.0,
)


EXPERIMENT_SPECS = [
    CURRENT_MODEL_SPEC,
    AdmissionModelSpec(
        name="split_rounds",
        description="Current target + separate EA/ED effects, no college prior.",
        split_early_rounds=True,
        include_college_prior=False,
        include_score_type_indicator=False,
        calibration_method="sigmoid",
        calibration_cv=3,
        logistic_c=1.0,
    ),
    CANDIDATE_MODEL_SPEC,
    WAITLIST_CLEAN_EXPERIMENT_SPEC,
]


@dataclass
class FeatureContext:
    score_stats: dict[str, tuple[float, float]]
    global_rate: float
    college_prior_map: dict[int, float]


class FittedAdmissionModel:
    def __init__(self, spec: AdmissionModelSpec, context: FeatureContext, estimator, feature_columns: list[str]):
        self.spec = spec
        self.context = context
        self.estimator = estimator
        self.feature_columns = feature_columns

    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        transformed, _ = build_feature_frame(frame, self.spec, self.context)
        return transformed

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        transformed = self.transform(frame)
        return self.estimator.predict_proba(transformed[self.feature_columns].values)[:, 1]


def load_points(db_path: Path = DB) -> pd.DataFrame:
    """Plottable scattergram points.

    The query and the sentinel-exclusion rule live in shared.naviance, so this
    and findings/naviance_explorer.py cannot drift apart on what counts as a
    real student outcome.
    """
    con = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(SCATTERGRAM_POINTS_SQL, con)
    finally:
        con.close()


def apply_target_policy(frame: pd.DataFrame, spec: AdmissionModelSpec) -> pd.DataFrame:
    df = frame.copy()
    if spec.drop_waitlisted_unknown:
        df = df[df["outcome_group"] != UNRESOLVED_OUTCOME].copy()
    df["admitted"] = df["outcome_group"].isin(ADMITTED_OUTCOMES).astype(int)
    return df.reset_index(drop=True)


def _safe_std(value: float) -> float:
    if value is None or np.isnan(value) or value == 0:
        return 1.0
    return float(value)


def fit_feature_context(train_df: pd.DataFrame, spec: AdmissionModelSpec) -> FeatureContext:
    score_stats_df = train_df.groupby("score_type")["test_score"].agg(["mean", "std"])
    score_stats = {
        str(score_type): (float(row["mean"]), _safe_std(float(row["std"])))
        for score_type, row in score_stats_df.iterrows()
    }
    global_rate = float(train_df["admitted"].mean())
    college_prior_map: dict[int, float] = {}
    if spec.include_college_prior:
        college_stats = train_df.groupby("college_id")["admitted"].agg(["mean", "size"])
        alpha = spec.college_prior_alpha
        college_stats["prior"] = (
            college_stats["mean"] * college_stats["size"] + global_rate * alpha
        ) / (college_stats["size"] + alpha)
        college_prior_map = {int(college_id): float(prior) for college_id, prior in college_stats["prior"].items()}
    return FeatureContext(
        score_stats=score_stats,
        global_rate=global_rate,
        college_prior_map=college_prior_map,
    )


def build_feature_columns(spec: AdmissionModelSpec) -> list[str]:
    if spec.split_early_rounds:
        columns = [
            "test_score_z",
            "gpa",
            "round_ea",
            "round_ed",
            "accept_rate",
            "score_x_selectivity",
            "gpa_x_selectivity",
            "ea_x_selectivity",
            "ed_x_selectivity",
        ]
    else:
        columns = [
            "test_score_z",
            "gpa",
            "is_early",
            "accept_rate",
            "score_x_selectivity",
            "gpa_x_selectivity",
            "early_x_selectivity",
        ]
    if spec.include_college_prior:
        columns.extend(["college_prior", "college_prior_logit"])
    if spec.include_score_type_indicator:
        columns.append("is_sat")
    return columns


def build_feature_frame(frame: pd.DataFrame, spec: AdmissionModelSpec, context: FeatureContext) -> tuple[pd.DataFrame, list[str]]:
    df = frame.copy()

    def zscore_row(row) -> float:
        score_type = str(row["score_type"])
        mean, std = context.score_stats.get(score_type, (float(row["test_score"]), 1.0))
        return (float(row["test_score"]) - mean) / std

    df["test_score_z"] = df.apply(zscore_row, axis=1)
    df["accept_rate"] = df["acceptance_rate_pct"] / 100.0

    if spec.split_early_rounds:
        df["round_ea"] = df["decision_type"].eq("Early Action").astype(int)
        df["round_ed"] = df["decision_type"].eq("Early Decision").astype(int)
        df["score_x_selectivity"] = df["test_score_z"] * (1 - df["accept_rate"])
        df["gpa_x_selectivity"] = df["gpa"] * (1 - df["accept_rate"])
        df["ea_x_selectivity"] = df["round_ea"] * (1 - df["accept_rate"])
        df["ed_x_selectivity"] = df["round_ed"] * (1 - df["accept_rate"])
    else:
        df["is_early"] = df["decision_type"].isin(["Early Decision", "Early Action"]).astype(int)
        df["score_x_selectivity"] = df["test_score_z"] * (1 - df["accept_rate"])
        df["gpa_x_selectivity"] = df["gpa"] * (1 - df["accept_rate"])
        df["early_x_selectivity"] = df["is_early"] * (1 - df["accept_rate"])

    if spec.include_college_prior:
        df["college_prior"] = (
            df["college_id"].astype(int).map(context.college_prior_map).fillna(context.global_rate)
        )
        clipped = np.clip(df["college_prior"], 1e-4, 1 - 1e-4)
        df["college_prior_logit"] = np.log(clipped / (1 - clipped))

    if spec.include_score_type_indicator:
        df["is_sat"] = df["score_type"].eq("SAT").astype(int)

    return df, build_feature_columns(spec)


def build_estimator(spec: AdmissionModelSpec):
    base = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("lr", LogisticRegression(C=spec.logistic_c, max_iter=1000, solver="lbfgs")),
        ]
    )
    return CalibratedClassifierCV(
        base,
        cv=spec.calibration_cv,
        method=spec.calibration_method,
    )


def cross_validated_predictions(
    raw_df: pd.DataFrame,
    spec: AdmissionModelSpec,
    n_splits: int = 5,
    random_state: int = 0,
) -> pd.DataFrame:
    df = apply_target_policy(raw_df, spec)
    splitter = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state)
    probs = np.zeros(len(df))

    for train_idx, test_idx in splitter.split(df, df["admitted"]):
        train = df.iloc[train_idx].copy()
        test = df.iloc[test_idx].copy()
        context = fit_feature_context(train, spec)
        train_features, feature_columns = build_feature_frame(train, spec, context)
        test_features, _ = build_feature_frame(test, spec, context)
        estimator = build_estimator(spec)
        estimator.fit(train_features[feature_columns].values, train_features["admitted"].values)
        probs[test_idx] = estimator.predict_proba(test_features[feature_columns].values)[:, 1]

    evaluated = df.copy()
    evaluated["p_oof"] = probs
    return evaluated


def fit_final_model(raw_df: pd.DataFrame, spec: AdmissionModelSpec) -> tuple[FittedAdmissionModel, pd.DataFrame]:
    df = apply_target_policy(raw_df, spec)
    context = fit_feature_context(df, spec)
    feature_frame, feature_columns = build_feature_frame(df, spec, context)
    estimator = build_estimator(spec)
    estimator.fit(feature_frame[feature_columns].values, feature_frame["admitted"].values)
    return FittedAdmissionModel(spec, context, estimator, feature_columns), df


def college_residuals(evaluated_df: pd.DataFrame, prob_col: str = "p_oof") -> pd.DataFrame:
    college = (
        evaluated_df.groupby(["college_id", "college_name"])
        .agg(
            n_points=("admitted", "size"),
            actual_admit_rate=("admitted", "mean"),
            predicted_admit_rate=(prob_col, "mean"),
        )
        .reset_index()
    )
    college["abs_error"] = (college["actual_admit_rate"] - college["predicted_admit_rate"]).abs()
    return college.sort_values(["abs_error", "n_points"], ascending=[False, False]).reset_index(drop=True)


def summarize_metrics(evaluated_df: pd.DataFrame, spec: AdmissionModelSpec, prob_col: str = "p_oof") -> dict:
    y = evaluated_df["admitted"].to_numpy()
    probs = evaluated_df[prob_col].to_numpy()
    college = college_residuals(evaluated_df, prob_col=prob_col)
    college_n10 = college[college["n_points"] >= 10]
    return {
        "spec_name": spec.name,
        "description": spec.description,
        "n_points": int(len(evaluated_df)),
        "n_colleges": int(evaluated_df["college_id"].nunique()),
        "positive_rate": float(y.mean()),
        "auc": float(roc_auc_score(y, probs)),
        "avg_precision": float(average_precision_score(y, probs)),
        "brier": float(brier_score_loss(y, probs)),
        "logloss": float(log_loss(y, probs)),
        "college_mae": float(college["abs_error"].mean()),
        "college_mae_n10": float(college_n10["abs_error"].mean()) if len(college_n10) else np.nan,
        "target_policy": "drop_waitlisted_unknown" if spec.drop_waitlisted_unknown else "current",
        "split_early_rounds": spec.split_early_rounds,
        "include_college_prior": spec.include_college_prior,
        "college_prior_alpha": spec.college_prior_alpha if spec.include_college_prior else np.nan,
        "include_score_type_indicator": spec.include_score_type_indicator,
        "calibration_method": spec.calibration_method,
        "calibration_cv": spec.calibration_cv,
    }


def current_vs_candidate_improvement(baseline_metrics: dict, candidate_metrics: dict) -> dict:
    return {
        "auc_delta": candidate_metrics["auc"] - baseline_metrics["auc"],
        "brier_delta": candidate_metrics["brier"] - baseline_metrics["brier"],
        "logloss_delta": candidate_metrics["logloss"] - baseline_metrics["logloss"],
        "college_mae_delta": candidate_metrics["college_mae"] - baseline_metrics["college_mae"],
        "college_mae_n10_delta": candidate_metrics["college_mae_n10"] - baseline_metrics["college_mae_n10"],
    }


def candidate_beats_baseline(baseline_metrics: dict, candidate_metrics: dict) -> bool:
    if baseline_metrics["n_points"] != candidate_metrics["n_points"]:
        return False
    return (
        candidate_metrics["auc"] > baseline_metrics["auc"]
        and candidate_metrics["brier"] < baseline_metrics["brier"]
        and candidate_metrics["logloss"] < baseline_metrics["logloss"]
        and candidate_metrics["college_mae"] < baseline_metrics["college_mae"]
        and candidate_metrics["college_mae_n10"] < baseline_metrics["college_mae_n10"]
    )


def feature_description(spec: AdmissionModelSpec) -> str:
    base_features = [
        "test_score_z",
        "gpa",
        "accept_rate",
        "score_x_selectivity",
        "gpa_x_selectivity",
    ]
    if spec.split_early_rounds:
        base_features.extend(["round_ea", "round_ed", "ea_x_selectivity", "ed_x_selectivity"])
    else:
        base_features.extend(["is_early", "early_x_selectivity"])
    if spec.include_college_prior:
        base_features.extend(["college_prior", "college_prior_logit"])
    if spec.include_score_type_indicator:
        base_features.append("is_sat")
    return ", ".join(base_features)
