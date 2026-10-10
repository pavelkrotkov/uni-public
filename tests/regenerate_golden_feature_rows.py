#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///
"""Regenerate tests/fixtures/golden_feature_rows.json.

Run this only when a change is *intended* to alter the feature store, and say
so in the commit message. The fixture exists so that restructuring where the
computation lives cannot silently change what it produces.

    uv run python tests/regenerate_golden_feature_rows.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import feature_row_fixture as fixture  # noqa: E402
from build_field_cluster_features import (  # noqa: E402
    build_cluster_feature_rows,
    load_feature_artifacts,
    validate_feature_config,
)


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        output_dir = Path(temp_dir)
        conn = fixture.make_conn()
        try:
            fixture.write_artifacts(output_dir)
            config = validate_feature_config(fixture.config(), conn)
            artifacts = load_feature_artifacts(config, conn, output_dir)
            rows = build_cluster_feature_rows(artifacts, config, output_dir=output_dir)
        finally:
            conn.close()

    destination = ROOT / "tests" / "fixtures" / "golden_feature_rows.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(fixture.normalise(rows), indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(rows)} rows x {len(rows[0])} columns to {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
