from __future__ import annotations

import json
import sqlite3
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from load_naviance import (
    LoadNavianceLoader,
    ensure_high_school,
    execute_sql_script_transactionally,
)
from shared.data_loader import (
    DataLoader,
    GrantYearDataLoader,
    ManualDataLoader,
    filter_targets_by_ids,
    grant_years_from_config,
)


class _FakeLoader(GrantYearDataLoader):
    source_code = "FAKE"
    source_name = "Fake Source"
    manifest_filename = "fake_manifest.json"
    description = "Fake loader for tests."

    def __init__(self, rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
        self._rows = rows
        self._manifest = manifest

    def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
        return self._rows, self._manifest


class _FakeManualLoader(ManualDataLoader):
    source_code = "MANUAL"
    description = "Manual loader for tests."
    requires_db = False
    requires_config = False

    def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
        return [{"university_id": 1}, {"university_id": 1}], {
            "executed": args.execute,
            "review_items": [{"id": 1}, {"id": 2}],
        }


class _NoDbManualLoader(ManualDataLoader):
    source_code = "NO_DB"
    description = "Manual loader that wrongly requires DB work."
    requires_db = False
    requires_config = False

    def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
        self._require_conn(conn)
        return [], {"executed": args.execute}


class _ExtrasLoader(DataLoader):
    source_code = "EXTRAS"
    description = "Generic loader for summary precedence tests."
    requires_db = False
    requires_config = False

    def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
        return [], {"items": [{"id": 1}, {"id": 2}], "flattened": ["a", "b"]}

    def summary_extras(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        return {"items": manifest["items"]}


class _ConflictingExtrasLoader(ManualDataLoader):
    source_code = "CONFLICT"
    description = "Loader with invalid summary extras."
    requires_db = False
    requires_config = False

    def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
        return [], {"executed": args.execute}

    def summary_extras(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        return {"row_count": 99}


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE grant_sources (
            grant_source_id INTEGER PRIMARY KEY,
            source_code TEXT UNIQUE NOT NULL,
            source_name TEXT NOT NULL,
            country_code TEXT NOT NULL,
            is_us_federal INTEGER NOT NULL
        );
        CREATE TABLE university_grant_year_stats (
            university_id INTEGER NOT NULL,
            grant_source_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            grant_count INTEGER,
            total_award_usd REAL,
            source_snapshot TEXT NOT NULL,
            PRIMARY KEY (university_id, grant_source_id, year)
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _make_naviance_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "naviance.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE universities (
            university_id INTEGER PRIMARY KEY,
            canonical_name TEXT NOT NULL
        );
        CREATE TABLE university_identifiers (
            university_id INTEGER NOT NULL,
            system_code TEXT NOT NULL,
            external_id TEXT NOT NULL,
            match_status TEXT
        );
        CREATE TABLE identifier_systems (
            system_code TEXT PRIMARY KEY,
            system_name TEXT NOT NULL,
            is_authoritative INTEGER,
            notes TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _make_naviance_dir(tmp_path: Path) -> Path:
    naviance_dir = tmp_path / "naviance"
    naviance_dir.mkdir()
    (naviance_dir / "scattergram_manifest.json").write_text(
        json.dumps(
            {
                "generatedAt": "2026-01-01T00:00:00+00:00",
                "rows": [
                    {
                        "resolved": True,
                        "profileUuid": "nav-1",
                        "collegeName": "Sample College",
                        "label": "Sample College",
                        "appCount": 3,
                        "profileUrl": "https://example.test/sample",
                    }
                ],
            }
        )
    )
    return naviance_dir


def _make_config(tmp_path: Path, output_subdir: str = "out") -> Path:
    config_path = tmp_path / "config.yaml"
    output_dir = tmp_path / output_subdir
    config_path.write_text(
        f"project:\n  output_dir: {output_dir}\nyears:\n  grants_start: 2020\n  grants_end: 2021\n"
    )
    return config_path


def test_build_summary_has_identical_fields() -> None:
    loader = _FakeLoader(rows=[], manifest={})
    summary = loader.build_summary(
        rows=[
            {"university_id": 1, "year": 2020},
            {"university_id": 2, "year": 2021},
            {"university_id": 2, "year": 2020},
        ],
        manifest={"unmatched_universities": [{"university_id": 7}], "request_count": 42},
    )
    assert set(summary.keys()) == {
        "source_code",
        "yearly_rows",
        "covered_universities",
        "unmatched_universities",
        "request_count",
    }
    assert summary["source_code"] == "FAKE"
    assert summary["yearly_rows"] == 3
    assert summary["covered_universities"] == 2
    assert summary["unmatched_universities"] == 1
    assert summary["request_count"] == 42


def test_dry_run_skips_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    rows = [
        {
            "university_id": 11,
            "year": 2020,
            "grant_count": 3,
            "total_award_usd": 100.0,
            "source_snapshot": "snap",
        }
    ]
    manifest = {
        "year_start": 2020,
        "year_end": 2021,
        "unmatched_universities": [],
        "request_count": 5,
    }
    loader = _FakeLoader(rows=rows, manifest=manifest)

    exit_code = loader.run(["--db", str(db_path), "--config", str(config_path)])
    assert exit_code == 0

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["source_code"] == "FAKE"
    assert summary["yearly_rows"] == 1

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM grant_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM university_grant_year_stats").fetchone()[0] == 0
    finally:
        conn.close()
    assert not (tmp_path / "out").exists()


def test_manual_loader_summary_is_consistent(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = _FakeManualLoader().run(["--dry-run"])
    assert exit_code == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "covered_universities": 1,
        "executed": False,
        "review_items": 2,
        "row_count": 2,
        "source_code": "MANUAL",
    }


def test_require_conn_raises_when_missing() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        assert DataLoader._require_conn(conn) is conn
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="Database connection is required"):
        DataLoader._require_conn(None)


def test_hidden_dry_run_alias_sets_execute_false(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = _FakeManualLoader().run(["--execute", "--dry-run"])
    assert exit_code == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["executed"] is False


def test_summary_extras_keys_override_manifest_lists(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = _ExtrasLoader().run([])
    assert exit_code == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["items"] == [{"id": 1}, {"id": 2}]
    assert summary["flattened"] == 2


def test_summary_extras_cannot_override_summary_keys() -> None:
    with pytest.raises(RuntimeError, match="summary_extras may not override summary keys"):
        _ConflictingExtrasLoader().run([])


def test_manual_loader_without_db_raises_when_db_work_requested() -> None:
    with pytest.raises(RuntimeError, match="Database connection is required"):
        _NoDbManualLoader().run(["--execute"])


def test_execute_writes_grant_source_rows_and_manifest(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    rows = [
        {
            "university_id": 11,
            "year": 2020,
            "grant_count": 3,
            "total_award_usd": 100.0,
            "source_snapshot": "snap",
        },
        {
            "university_id": 11,
            "year": 2021,
            "grant_count": 5,
            "total_award_usd": 250.0,
            "source_snapshot": "snap",
        },
    ]
    manifest = {
        "year_start": 2020,
        "year_end": 2021,
        "unmatched_universities": [],
        "request_count": 5,
    }
    loader = _FakeLoader(rows=rows, manifest=manifest)

    loader.run(["--db", str(db_path), "--config", str(config_path), "--execute"])

    conn = sqlite3.connect(db_path)
    try:
        gs = conn.execute(
            "SELECT source_code, source_name, country_code, is_us_federal FROM grant_sources"
        ).fetchall()
        assert gs == [("FAKE", "Fake Source", "US", 1)]
        stats = conn.execute(
            "SELECT university_id, year, grant_count, total_award_usd, source_snapshot "
            "FROM university_grant_year_stats ORDER BY year"
        ).fetchall()
        assert stats == [
            (11, 2020, 3, 100.0, "snap"),
            (11, 2021, 5, 250.0, "snap"),
        ]
    finally:
        conn.close()

    manifest_path = tmp_path / "out" / "fake_manifest.json"
    assert manifest_path.exists(), f"manifest missing at {manifest_path}"
    written = json.loads(manifest_path.read_text())
    assert written["year_start"] == 2020
    assert written["year_end"] == 2021


def test_execute_filters_by_university_ids(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO grant_sources VALUES (1, 'FAKE', 'Fake Source', 'US', 1)")
    conn.executemany(
        "INSERT INTO university_grant_year_stats VALUES (?, 1, ?, ?, ?, 'old')",
        [
            (11, 2020, 1, 1.0),
            (11, 2021, 1, 1.0),
            (22, 2020, 1, 1.0),  # this should survive — not in --university-id filter
        ],
    )
    conn.commit()
    conn.close()

    rows = [
        {
            "university_id": 11,
            "year": 2020,
            "grant_count": 9,
            "total_award_usd": 999.0,
            "source_snapshot": "new",
        }
    ]
    manifest = {
        "year_start": 2020,
        "year_end": 2021,
        "unmatched_universities": [],
        "request_count": 1,
    }
    loader = _FakeLoader(rows=rows, manifest=manifest)
    loader.run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--execute",
            "--university-id",
            "11",
        ]
    )

    conn = sqlite3.connect(db_path)
    try:
        stats = conn.execute(
            "SELECT university_id, year, source_snapshot FROM university_grant_year_stats "
            "ORDER BY university_id, year"
        ).fetchall()
    finally:
        conn.close()
    assert stats == [(11, 2020, "new"), (22, 2020, "old")]


def test_connection_closed_on_load_error(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)

    class _Boom(_FakeLoader):
        def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
            self._captured_conn = conn
            raise RuntimeError("boom")

    loader = _Boom(rows=[], manifest={})
    with patch("sys.stdout", new=StringIO()), pytest.raises(RuntimeError, match="boom"):
        loader.run(["--db", str(db_path), "--config", str(config_path)])

    # SQLite raises ProgrammingError when used after close.
    with pytest.raises(sqlite3.ProgrammingError):
        loader._captured_conn.execute("SELECT 1")


def test_manifest_not_written_when_commit_fails(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    rows = [
        {
            "university_id": 11,
            "year": 2020,
            "grant_count": 1,
            "total_award_usd": 1.0,
            "source_snapshot": "snap",
        }
    ]
    manifest = {
        "year_start": 2020,
        "year_end": 2021,
        "unmatched_universities": [],
        "request_count": 1,
    }
    loader = _FakeLoader(rows=rows, manifest=manifest)

    original_connect = __import__("shared.db", fromlist=["connect"]).connect

    class _NoCommitConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def commit(self) -> None:
            raise RuntimeError("commit failed")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    def _patched_connect(db: Path) -> Any:
        return _NoCommitConn(original_connect(db))

    with (
        patch("shared.data_loader.connect", new=_patched_connect),
        pytest.raises(RuntimeError, match="commit failed"),
    ):
        loader.run(["--db", str(db_path), "--config", str(config_path), "--execute"])

    manifest_path = tmp_path / "out" / "fake_manifest.json"
    assert not manifest_path.exists()
    # Staged temp file must also be cleaned up.
    assert not (tmp_path / "out" / "fake_manifest.json.tmp").exists()


def test_db_not_committed_when_manifest_staging_fails(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    # Point output_dir at an existing regular file so mkdir() fails.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a dir")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"project:\n  output_dir: {blocked}\nyears:\n  grants_start: 2020\n  grants_end: 2021\n"
    )

    rows = [
        {
            "university_id": 11,
            "year": 2020,
            "grant_count": 1,
            "total_award_usd": 1.0,
            "source_snapshot": "snap",
        }
    ]
    manifest = {
        "year_start": 2020,
        "year_end": 2021,
        "unmatched_universities": [],
        "request_count": 1,
    }
    loader = _FakeLoader(rows=rows, manifest=manifest)

    with pytest.raises((FileExistsError, NotADirectoryError, OSError)):
        loader.run(["--db", str(db_path), "--config", str(config_path), "--execute"])

    # DB must be untouched: no grant_sources row and no stat rows.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM grant_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM university_grant_year_stats").fetchone()[0] == 0
    finally:
        conn.close()


def test_ensure_grant_source_uses_autoassigned_id(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO grant_sources VALUES (5, 'EXISTING', 'Other', 'US', 1)")
    conn.commit()
    conn.close()

    rows = [
        {
            "university_id": 1,
            "year": 2020,
            "grant_count": 1,
            "total_award_usd": 1.0,
            "source_snapshot": "snap",
        }
    ]
    manifest = {
        "year_start": 2020,
        "year_end": 2021,
        "unmatched_universities": [],
        "request_count": 1,
    }
    _FakeLoader(rows=rows, manifest=manifest).run(
        ["--db", str(db_path), "--config", str(config_path), "--execute"]
    )

    conn = sqlite3.connect(db_path)
    try:
        sources = conn.execute(
            "SELECT grant_source_id, source_code FROM grant_sources ORDER BY grant_source_id"
        ).fetchall()
    finally:
        conn.close()
    # FAKE should be auto-assigned a fresh id distinct from the pre-existing row.
    assert ("EXISTING",) in [(row[1],) for row in sources]
    fake_rows = [row for row in sources if row[1] == "FAKE"]
    assert len(fake_rows) == 1
    assert fake_rows[0][0] != 5


def test_grant_years_from_config_validates_order() -> None:
    assert grant_years_from_config({"years": {"grants_start": 2020, "grants_end": 2024}}) == (
        2020,
        2024,
    )
    with pytest.raises(RuntimeError):
        grant_years_from_config({"years": {"grants_start": 2025, "grants_end": 2024}})


def test_filter_targets_by_ids_rejects_unknown() -> None:
    from dataclasses import dataclass

    @dataclass
    class T:
        university_id: int

    targets = [T(1), T(2), T(3)]
    assert filter_targets_by_ids(targets, []) == targets
    assert filter_targets_by_ids(targets, [2]) == [T(2)]
    with pytest.raises(RuntimeError, match="Unknown university_id"):
        filter_targets_by_ids(targets, [99])


class _StagingLoader(_FakeLoader):
    staging_filename = "fake_staging.parquet"
    load_call_count = 0

    def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
        type(self).load_call_count += 1
        return self._rows, self._manifest


def _staging_rows_and_manifest() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [
        {
            "university_id": 11,
            "year": 2020,
            "grant_count": 3,
            "total_award_usd": 100.0,
            "source_snapshot": "snap",
        },
        {
            "university_id": 12,
            "year": 2021,
            "grant_count": 5,
            "total_award_usd": 250.5,
            "source_snapshot": "snap",
        },
    ]
    manifest = {
        "year_start": 2020,
        "year_end": 2021,
        "unmatched_universities": [{"university_id": 99}],
        "request_count": 7,
    }
    return rows, manifest


def test_staging_writes_parquet_with_manifest_metadata(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    rows, manifest = _staging_rows_and_manifest()
    staging_dir = tmp_path / "staging"

    _StagingLoader.load_call_count = 0
    loader = _StagingLoader(rows=rows, manifest=manifest)
    loader.run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
        ]
    )

    assert _StagingLoader.load_call_count == 1
    staging_path = staging_dir / "fake_staging.parquet"
    assert staging_path.exists(), "staging parquet was not written"

    import pyarrow.parquet as pq

    table = pq.read_table(staging_path)
    assert table.to_pylist() == rows
    manifest_blob = (table.schema.metadata or {}).get(b"manifest")
    assert manifest_blob is not None
    assert json.loads(manifest_blob.decode("utf-8")) == manifest


def test_from_staging_skips_api_and_executes(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    rows, manifest = _staging_rows_and_manifest()
    staging_dir = tmp_path / "staging"

    _StagingLoader.load_call_count = 0
    _StagingLoader(rows=rows, manifest=manifest).run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
        ]
    )
    assert _StagingLoader.load_call_count == 1

    # Re-run with --from-staging --execute and a loader whose load() would blow up
    # if called — proving the API path is bypassed.
    class _ExplodingLoader(_StagingLoader):
        def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
            raise AssertionError("load() must not be called when --from-staging is set")

    _ExplodingLoader.load_call_count = 0
    _ExplodingLoader(rows=[], manifest={}).run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
            "--from-staging",
            "--execute",
        ]
    )
    assert _ExplodingLoader.load_call_count == 0

    conn = sqlite3.connect(db_path)
    try:
        stats = conn.execute(
            "SELECT university_id, year, grant_count, total_award_usd, source_snapshot "
            "FROM university_grant_year_stats ORDER BY university_id, year"
        ).fetchall()
    finally:
        conn.close()
    assert stats == [
        (11, 2020, 3, 100.0, "snap"),
        (12, 2021, 5, 250.5, "snap"),
    ]

    manifest_path = tmp_path / "out" / "fake_manifest.json"
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text())["year_start"] == 2020


def test_from_staging_dry_run_does_not_touch_db(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    rows, manifest = _staging_rows_and_manifest()
    staging_dir = tmp_path / "staging"

    _StagingLoader(rows=rows, manifest=manifest).run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
        ]
    )

    class _ExplodingLoader(_StagingLoader):
        def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
            raise AssertionError("load() must not be called when --from-staging is set")

    _ExplodingLoader(rows=[], manifest={}).run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
            "--from-staging",
        ]
    )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM grant_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM university_grant_year_stats").fetchone()[0] == 0
    finally:
        conn.close()
    assert not (tmp_path / "out").exists()


def test_from_staging_missing_file_raises(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    staging_dir = tmp_path / "no_staging_here"

    loader = _StagingLoader(rows=[], manifest={})
    with pytest.raises(FileNotFoundError, match="Staging file not found"):
        loader.run(
            [
                "--db",
                str(db_path),
                "--config",
                str(config_path),
                "--staging-dir",
                str(staging_dir),
                "--from-staging",
            ]
        )


def test_staging_round_trips_empty_rows(tmp_path: Path) -> None:
    """A loader that returns zero rows must still write a readable staging file."""
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    manifest = {
        "year_start": 2020,
        "year_end": 2021,
        "unmatched_universities": [],
        "request_count": 0,
    }
    staging_dir = tmp_path / "staging"

    _StagingLoader(rows=[], manifest=manifest).run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
        ]
    )

    # Re-read via --from-staging --execute. The DB should stay empty, but no
    # exception should propagate.
    class _ExplodingLoader(_StagingLoader):
        def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
            raise AssertionError("load() must not be called when --from-staging is set")

    _ExplodingLoader(rows=[], manifest={}).run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
            "--from-staging",
            "--execute",
        ]
    )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM university_grant_year_stats").fetchone()[0] == 0
        # grant_sources is still upserted with the source descriptor.
        assert conn.execute("SELECT source_code FROM grant_sources").fetchone() == ("FAKE",)
    finally:
        conn.close()


def test_staging_cleans_tmp_when_write_fails(tmp_path: Path) -> None:
    """A failure during pq.write_table must not leak a half-written .tmp file."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    target = staging_dir / "fake_staging.parquet"

    rows = [{"university_id": 1, "year": 2020}]
    manifest = {"year_start": 2020, "year_end": 2020}

    def _boom(*args: Any, **kwargs: Any) -> None:
        # Simulate disk-full / schema error after the .tmp is opened.
        staged = target.with_name(target.name + ".tmp")
        staged.write_text("partial garbage")
        raise OSError("simulated write failure")

    with patch("pyarrow.parquet.write_table", new=_boom), pytest.raises(OSError, match="simulated"):
        _StagingLoader._write_staging(target, rows, manifest)

    assert not target.exists()
    assert not target.with_name(target.name + ".tmp").exists(), ".tmp file leaked on write failure"


def test_from_staging_with_university_id_filters_rows(tmp_path: Path) -> None:
    """Replaying a full-run staging with --university-id must only upsert that ID."""
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    staging_dir = tmp_path / "staging"

    # Seed staging with two universities, mimicking a full run.
    rows = [
        {
            "university_id": 11,
            "year": 2020,
            "grant_count": 3,
            "total_award_usd": 100.0,
            "source_snapshot": "snap",
        },
        {
            "university_id": 22,
            "year": 2020,
            "grant_count": 7,
            "total_award_usd": 700.0,
            "source_snapshot": "snap",
        },
    ]
    manifest = {
        "year_start": 2020,
        "year_end": 2020,
        "unmatched_universities": [],
        "request_count": 1,
    }
    _StagingLoader(rows=rows, manifest=manifest).run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
        ]
    )

    # Pre-populate the DB with both universities — so a buggy replay would
    # either PK-conflict on uni 22 or silently re-insert it under a fresh snapshot.
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO grant_sources VALUES (1, 'FAKE', 'Fake Source', 'US', 1)")
    conn.executemany(
        "INSERT INTO university_grant_year_stats VALUES (?, 1, ?, ?, ?, 'pre-existing')",
        [(11, 2020, 1, 1.0), (22, 2020, 1, 1.0)],
    )
    conn.commit()
    conn.close()

    class _ExplodingLoader(_StagingLoader):
        def load(self, *, conn, config, args, snapshot):  # type: ignore[no-untyped-def]
            raise AssertionError("load() must not be called when --from-staging is set")

    _ExplodingLoader(rows=[], manifest={}).run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
            "--from-staging",
            "--execute",
            "--university-id",
            "11",
        ]
    )

    conn = sqlite3.connect(db_path)
    try:
        stats = conn.execute(
            "SELECT university_id, source_snapshot FROM university_grant_year_stats "
            "ORDER BY university_id"
        ).fetchall()
    finally:
        conn.close()
    # uni 11 was overwritten by staged data; uni 22 was left alone.
    assert stats == [(11, "snap"), (22, "pre-existing")]


def test_from_staging_with_unknown_university_id_raises(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)
    staging_dir = tmp_path / "staging"

    rows = [
        {
            "university_id": 11,
            "year": 2020,
            "grant_count": 1,
            "total_award_usd": 1.0,
            "source_snapshot": "snap",
        }
    ]
    manifest = {
        "year_start": 2020,
        "year_end": 2020,
        "unmatched_universities": [],
        "request_count": 1,
    }
    _StagingLoader(rows=rows, manifest=manifest).run(
        [
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--staging-dir",
            str(staging_dir),
        ]
    )

    with pytest.raises(RuntimeError, match=r"\[99\] are not present in the staging file"):
        _StagingLoader(rows=[], manifest={}).run(
            [
                "--db",
                str(db_path),
                "--config",
                str(config_path),
                "--staging-dir",
                str(staging_dir),
                "--from-staging",
                "--university-id",
                "99",
            ]
        )


def test_from_staging_flag_not_available_without_staging_filename(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    config_path = _make_config(tmp_path)

    # _FakeLoader has no staging_filename — passing --from-staging should be rejected.
    loader = _FakeLoader(rows=[], manifest={})
    with pytest.raises(SystemExit):
        loader.run(
            [
                "--db",
                str(db_path),
                "--config",
                str(config_path),
                "--from-staging",
            ]
        )


@pytest.mark.parametrize(
    ("name", "slug"),
    [(None, "example"), ("Example High School", " ")],
)
def test_naviance_requires_school_identity(name: str | None, slug: str | None) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError, match="--high-school-name and --high-school-slug"):
            ensure_high_school(conn, name, slug)
    finally:
        conn.close()


def test_load_naviance_dry_run_rolls_back_source_db(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _make_naviance_db(tmp_path)
    naviance_dir = _make_naviance_dir(tmp_path)

    exit_code = LoadNavianceLoader().run(
        [
            "--db",
            str(db_path),
            "--naviance-dir",
            str(naviance_dir),
            "--manifest-only",
        ]
    )
    assert exit_code == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["executed"] is False
    assert summary["fuzzy_match"] == {
        "exact": 0,
        "fuzzy_auto": 0,
        "fuzzy_review": 0,
        "unmatched": 1,
        # Previously this count was subtracted from fuzzy_auto, hiding it.
        "dropped_collisions": 0,
    }
    assert summary["scrape_progress"] == []

    conn = sqlite3.connect(db_path)
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'naviance_colleges'"
        ).fetchone()
        identifier_count = conn.execute("SELECT COUNT(*) FROM identifier_systems").fetchone()[0]
    finally:
        conn.close()
    assert table_exists is None
    assert identifier_count == 0


def test_transactional_sql_script_handles_semicolon_in_string(tmp_path: Path) -> None:
    db_path = tmp_path / "script.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        execute_sql_script_transactionally(
            conn,
            """
            CREATE TABLE demo (
                value TEXT DEFAULT 'a;b'
            );
            INSERT INTO demo DEFAULT VALUES;
            CREATE TRIGGER demo_upper
            AFTER INSERT ON demo
            BEGIN
                UPDATE demo SET value = upper(NEW.value) WHERE rowid = NEW.rowid;
            END;
            """,
        )
        conn.execute("INSERT INTO demo (value) VALUES ('c;d')")
        rows = conn.execute("SELECT value FROM demo ORDER BY rowid").fetchall()
        assert rows == [("a;b",), ("C;D",)]
        conn.rollback()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'demo'").fetchone() is None
    finally:
        conn.close()
