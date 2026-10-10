from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from . import ROOT

DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


def load_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def resolve_output_dir(config: dict[str, Any]) -> Path:
    configured = config.get("project", {}).get("output_dir")
    if not configured:
        raise RuntimeError("config.project.output_dir is required")
    return ROOT / configured


def config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
