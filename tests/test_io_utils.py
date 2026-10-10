from __future__ import annotations

import csv
from pathlib import Path

import pytest
from shared.io_utils import (
    latest_openalex_run_dir,
    load_parquet_rows,
    read_json,
    write_csv,
    write_json,
    write_parquet,
)


def test_read_json_and_write_json_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "payload.json"
    payload = {"alpha": 1, "beta": ["x", "y"], "gamma": {"nested": True}}

    write_json(path, payload)

    assert read_json(path) == payload


def test_write_json_writes_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "payload.json"
    write_json(path, {"a": 1})

    assert path.read_text().endswith("\n")


def test_write_csv_round_trips_rows_with_auto_fieldnames(tmp_path: Path) -> None:
    path = tmp_path / "rows.csv"
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]

    write_csv(path, rows)

    with path.open() as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["a", "b"]
        assert list(reader) == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_write_csv_auto_fieldnames_preserves_first_seen_order(tmp_path: Path) -> None:
    path = tmp_path / "rows.csv"
    rows = [
        {"first": 1, "second": 2},
        {"second": 4, "third": 5},
    ]

    write_csv(path, rows)

    with path.open() as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["first", "second", "third"]


def test_write_csv_respects_explicit_fieldnames_ordering(tmp_path: Path) -> None:
    path = tmp_path / "rows.csv"
    rows = [{"a": 1, "b": 2, "c": 3}]

    write_csv(path, rows, fieldnames=["c", "a", "b"])

    with path.open() as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["c", "a", "b"]
        assert list(reader) == [{"c": "3", "a": "1", "b": "2"}]


def test_write_parquet_and_load_parquet_rows_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "rows.parquet"
    rows = [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]

    write_parquet(path, rows)

    assert load_parquet_rows(path) == rows


def test_latest_openalex_run_dir_picks_lexicographically_latest(tmp_path: Path) -> None:
    for stamp in ("2025-01-01", "2025-06-15", "2024-12-31"):
        run = tmp_path / stamp
        run.mkdir()
        (run / "openalex_metrics.parquet").touch()

    # Directory without the marker file must be ignored, even if its name sorts later.
    decoy = tmp_path / "2026-01-01-empty"
    decoy.mkdir()

    assert latest_openalex_run_dir(tmp_path) == tmp_path / "2025-06-15"


def test_latest_openalex_run_dir_raises_when_no_candidates(tmp_path: Path) -> None:
    (tmp_path / "lonely-dir").mkdir()

    with pytest.raises(RuntimeError, match="No OpenAlex parquet runs found"):
        latest_openalex_run_dir(tmp_path)
