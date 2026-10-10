import json
import re
from pathlib import Path

import admission_model
import naviance_explorer
import pandas as pd
from admission_model_utils import CURRENT_MODEL_SPEC


def test_report_view_models_are_json_serializable():
    colleges = [{"name": "Example", "scattergrams": {}}]
    naviance = naviance_explorer.build_view_model(colleges)
    assert naviance["DATA_PLACEHOLDER"] == colleges

    metrics = {
        "auc": 0.8,
        "brier": 0.1,
        "logloss": 0.2,
        "college_mae": 0.03,
        "college_mae_n10": 0.03,
    }
    evaluated = pd.DataFrame({"college_id": [1, 1, 2]})
    admission = admission_model.build_view_model(
        [], evaluated, CURRENT_MODEL_SPEC, metrics, metrics, metrics
    )
    assert admission["N_POINTS"] == 3
    assert admission["N_COLLEGES"] == 2
    features = admission["FEATURES"]
    assert isinstance(features, str) and "<" not in features

    findings = Path(__file__).resolve().parents[1] / "findings"
    for template, view_model in [
        ("naviance_explorer.template.html", naviance),
        ("admission_model.template.html", admission),
    ]:
        placeholders = set(re.findall(r"__([A-Z_]+)__", (findings / template).read_text()))
        assert placeholders == view_model.keys()
        json.dumps(view_model)
