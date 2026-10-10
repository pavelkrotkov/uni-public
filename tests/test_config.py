from __future__ import annotations

from pathlib import Path

import pytest
from shared.config import config_hash, load_config, resolve_output_dir


def test_load_config_round_trips_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("project:\n  output_dir: build/out\nvalues:\n  - 1\n  - 2\n")

    assert load_config(path) == {
        "project": {"output_dir": "build/out"},
        "values": [1, 2],
    }


def test_resolve_output_dir_happy_path() -> None:
    resolved = resolve_output_dir({"project": {"output_dir": "build/out"}})

    assert resolved.name == "out"
    assert resolved.parent.name == "build"
    # Resolved against the repository ROOT, so the path must be absolute.
    assert resolved.is_absolute()


def test_resolve_output_dir_missing_project_raises() -> None:
    with pytest.raises(RuntimeError, match=r"config\.project\.output_dir is required"):
        resolve_output_dir({})


def test_resolve_output_dir_missing_output_dir_raises() -> None:
    with pytest.raises(RuntimeError, match=r"config\.project\.output_dir is required"):
        resolve_output_dir({"project": {}})


def test_resolve_output_dir_empty_output_dir_raises() -> None:
    with pytest.raises(RuntimeError, match=r"config\.project\.output_dir is required"):
        resolve_output_dir({"project": {"output_dir": ""}})


def test_config_hash_is_deterministic_for_reordered_keys() -> None:
    first = {"a": 1, "b": {"x": 10, "y": 20}}
    second = {"b": {"y": 20, "x": 10}, "a": 1}

    assert config_hash(first) == config_hash(second)


def test_config_hash_differs_when_content_differs() -> None:
    assert config_hash({"a": 1}) != config_hash({"a": 2})
