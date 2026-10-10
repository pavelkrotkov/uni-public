"""Base classes for repo data-loading scripts.

Concrete loaders subclass ``DataLoader``, set class-level metadata, optionally
override ``add_arguments()`` for custom CLI flags, and implement ``load()``.
The base class owns argument parsing, config loading, the SQLite connection
lifecycle, the dry-run / execute toggle, summary printing, and connection
cleanup on error.

Summary envelopes have two common shapes:

* Manual loaders report ``source_code``, ``row_count``, ``executed``, optional
  ``covered_universities``, plus manifest keys flattened to counts for lists.
* Grant-year loaders report ``source_code``, ``yearly_rows``,
  ``covered_universities``, ``unmatched_universities``, ``request_count``, plus
  manifest keys flattened to counts for lists.

Loaders can override ``summary_extras()`` to preserve full manifest values in
the JSON output when count-flattening would hide operator detail.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from . import ROOT
from .config import load_config, resolve_output_dir
from .db import DEFAULT_DB_PATH, connect
from .http import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT,
    HttpJsonSource,
    JsonSource,
)

DEFAULT_CONFIG_PATH = ROOT / "config.yaml"
DEFAULT_STAGING_DIR = ROOT / "data" / "staging"
STAGING_MANIFEST_KEY = b"manifest"


class DataLoader(ABC):
    """Abstract base for repo data-loading scripts.

    Subclasses MUST set ``source_code`` and ``description`` and implement
    ``load()``. ``load()`` returns ``(rows, manifest)`` so every script can
    produce the same JSON summary envelope.
    """

    source_code: ClassVar[str] = ""
    source_name: ClassVar[str] = ""
    staging_filename: ClassVar[str] = ""
    description: ClassVar[str] = ""
    execute_help: ClassVar[str] = "write outputs (default is dry-run)"
    requires_db: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    supports_university_id: ClassVar[bool] = False

    # Loaders that call an external API set this. It registers the shared
    # --request-timeout / --max-attempts flags and makes ``self.json_source``
    # available inside ``load()``.
    requires_json_source: ClassVar[bool] = False
    json_source_headers: ClassVar[dict[str, str]] = {}
    default_request_timeout: ClassVar[float] = DEFAULT_TIMEOUT
    default_max_attempts: ClassVar[int] = DEFAULT_MAX_ATTEMPTS

    def __init__(self, json_source: JsonSource | None = None) -> None:
        """``json_source`` overrides the HTTP adapter. Tests pass a fake here."""
        self._json_source = json_source

    @property
    def json_source(self) -> JsonSource:
        """The JSON seam for this run. Available once ``run()`` has parsed args."""
        if self._json_source is None:
            raise RuntimeError(
                f"{type(self).__name__} has no json_source. Set "
                "requires_json_source = True, or pass one to the constructor."
            )
        return self._json_source

    def build_json_source(self, args: argparse.Namespace) -> JsonSource:
        """Hook: construct the production adapter. Override to change transport."""
        return HttpJsonSource(
            headers=self.json_source_headers,
            timeout=args.request_timeout,
            max_attempts=args.max_attempts,
        )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:  # noqa: B027
        """Hook: register source-specific CLI flags. Default: no-op."""

    @abstractmethod
    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict[str, Any],
        args: argparse.Namespace,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch data and return ``(rows, manifest)``.

        ``rows`` should be the primary records prepared by the loader when a
        row-shaped result exists. ``manifest`` keys are included in the JSON
        summary, after large list values are converted to counts.
        """

    def summary_extras(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Hook: extra fields to add to the dry-run summary. Default: none."""
        return {}

    def _summary_extras_without_reserved_keys(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
        reserved_keys: set[str],
    ) -> dict[str, Any]:
        extras = self.summary_extras(rows, manifest)
        overlap = sorted(reserved_keys & extras.keys())
        if overlap:
            keys = ", ".join(overlap)
            raise RuntimeError(f"summary_extras may not override summary keys: {keys}")
        return extras

    def run(self, argv: Sequence[str] | None = None) -> int:
        args = self._parse_args(argv)
        config = load_config(args.config) if self.requires_config else {}
        snapshot = datetime.now(UTC).isoformat()

        # An injected source wins, so tests never reach the network.
        if self.requires_json_source and self._json_source is None:
            self._json_source = self.build_json_source(args)

        conn = connect(args.db) if self.requires_db else None
        try:
            if args.from_staging:
                rows, manifest = self._read_staging(args.staging_dir / self.staging_filename)
                if args.university_id:
                    rows = self._filter_staged_rows_by_university_id(rows, args.university_id)
            else:
                rows, manifest = self.load(conn=conn, config=config, args=args, snapshot=snapshot)
                if self.staging_filename:
                    self._write_staging(args.staging_dir / self.staging_filename, rows, manifest)
            self._print_summary(rows, manifest)
            if args.execute:
                self._execute(conn, rows, manifest, config, args)
        finally:
            if conn is not None:
                conn.close()
        return 0

    def _parse_args(self, argv: Sequence[str] | None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            description=self.description or self.source_name or self.source_code
        )
        parser.add_argument(
            "--db",
            "--db-path",
            type=Path,
            dest="db",
            default=DEFAULT_DB_PATH,
            help=f"SQLite database path (default: {DEFAULT_DB_PATH.name})",
        )
        parser.add_argument(
            "--config",
            type=Path,
            default=DEFAULT_CONFIG_PATH,
            help=f"pipeline config path (default: {DEFAULT_CONFIG_PATH.name})",
        )
        if self.supports_university_id:
            parser.add_argument(
                "--university-id",
                type=int,
                action="append",
                default=[],
                help="limit execution to one or more university_id values",
            )
        else:
            parser.set_defaults(university_id=[])
        if self.staging_filename:
            parser.add_argument(
                "--from-staging",
                action="store_true",
                help="read rows from the staging parquet instead of calling the API",
            )
            parser.add_argument(
                "--staging-dir",
                type=Path,
                default=DEFAULT_STAGING_DIR,
                help=(
                    "directory holding staging parquet files "
                    f"(default: {DEFAULT_STAGING_DIR.relative_to(ROOT)})"
                ),
            )
        else:
            parser.set_defaults(from_staging=False, staging_dir=DEFAULT_STAGING_DIR)
        parser.add_argument(
            "--execute",
            action="store_true",
            help=self.execute_help,
        )
        parser.add_argument(
            "--dry-run",
            action="store_false",
            dest="execute",
            help=argparse.SUPPRESS,
        )
        if self.requires_json_source:
            parser.add_argument(
                "--request-timeout",
                type=float,
                default=self.default_request_timeout,
                help=(
                    f"per-request timeout in seconds (default: {self.default_request_timeout:g})"
                ),
            )
            parser.add_argument(
                "--max-attempts",
                type=int,
                default=self.default_max_attempts,
                help=(
                    "max attempts per API request before failing "
                    f"(default: {self.default_max_attempts})"
                ),
            )
        else:
            parser.set_defaults(
                request_timeout=self.default_request_timeout,
                max_attempts=self.default_max_attempts,
            )
        self.add_arguments(parser)
        return parser.parse_args(list(argv) if argv is not None else None)

    def build_summary(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {"source_code": self.source_code}
        summary.update(self._summary_extras_without_reserved_keys(rows, manifest, set(summary)))
        return self._merge_manifest_summary(summary, manifest)

    @staticmethod
    def _merge_manifest_summary(
        summary: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        for key, value in manifest.items():
            # Summary keys win over manifest keys; this lets summary_extras() preserve full lists.
            if key in summary:
                continue
            if isinstance(value, list):
                summary[key] = len(value)
            else:
                summary[key] = value
        return summary

    def _print_summary(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> None:
        print(json.dumps(self.build_summary(rows, manifest), indent=2, sort_keys=True))

    @staticmethod
    def _write_staging(
        path: Path,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> None:
        """Write ``rows`` to ``path`` as Parquet, with ``manifest`` in schema metadata.

        Imported lazily so loaders that don't stage don't need pyarrow installed.
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows) if rows else pa.table({})
        table = table.replace_schema_metadata(
            {STAGING_MANIFEST_KEY: json.dumps(manifest, sort_keys=True).encode("utf-8")}
        )
        # Stage to .tmp and atomically rename so a crash mid-write never leaves
        # a half-written staging file that --from-staging would later trust.
        # The cleanup wraps the write too, so a failure mid-write also leaves
        # no .tmp behind to confuse a future run.
        staged = path.with_name(path.name + ".tmp")
        try:
            pq.write_table(table, staged, compression="snappy")
            staged.replace(path)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    @staticmethod
    def _read_staging(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        import pyarrow.parquet as pq

        if not path.exists():
            raise FileNotFoundError(
                f"Staging file not found: {path}. Run without --from-staging first."
            )
        table = pq.read_table(path)
        rows = table.to_pylist()
        metadata = table.schema.metadata or {}
        manifest_blob = metadata.get(STAGING_MANIFEST_KEY)
        if manifest_blob is None:
            raise RuntimeError(f"Staging file missing manifest metadata: {path}")
        manifest = json.loads(manifest_blob.decode("utf-8"))
        return rows, manifest

    @staticmethod
    def _filter_staged_rows_by_university_id(
        rows: list[dict[str, Any]],
        requested_ids: list[int],
    ) -> list[dict[str, Any]]:
        """Filter ``rows`` to the requested university IDs.

        Without this, ``--from-staging --execute --university-id X`` would delete
        only X's rows from the DB but re-insert every row in the staging file —
        either polluting the DB with unrelated universities or hitting PK conflicts
        on rows that survived the targeted delete.
        """
        requested = set(requested_ids)
        present = {row["university_id"] for row in rows if "university_id" in row}
        missing = sorted(requested - present)
        if missing:
            raise RuntimeError(
                f"university_id values {missing} are not present in the staging file; "
                "re-fetch from the API or pick IDs that the staging file already covers"
            )
        return [row for row in rows if row.get("university_id") in requested]

    def _execute(
        self,
        conn: sqlite3.Connection | None,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
        config: dict[str, Any],
        args: argparse.Namespace,
    ) -> None:
        return None

    @staticmethod
    def _require_conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
        if conn is None:
            raise RuntimeError("Database connection is required")
        return conn


class ManualDataLoader(DataLoader):
    """Base for scripts that perform their own execute/dry-run side effects in load()."""

    def build_summary(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "source_code": self.source_code,
            "row_count": len(rows),
            "executed": bool(manifest.get("executed", False)),
        }
        if rows and all("university_id" in row for row in rows):
            summary["covered_universities"] = len({row["university_id"] for row in rows})
        summary.update(self._summary_extras_without_reserved_keys(rows, manifest, set(summary)))
        return self._merge_manifest_summary(summary, manifest)


class GrantYearDataLoader(DataLoader):
    """Base for scripts that load yearly aggregated grant data."""

    execute_help = "write results into SQLite and manifest output (default is dry-run)"
    supports_university_id = True
    manifest_filename: ClassVar[str] = ""
    country_code: ClassVar[str] = "US"
    is_us_federal: ClassVar[int] = 1
    GRANT_STATS_TABLE: ClassVar[str] = "university_grant_year_stats"

    def build_summary(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        unmatched = manifest.get("unmatched_universities", [])
        summary: dict[str, Any] = {
            "source_code": self.source_code,
            "yearly_rows": len(rows),
            "covered_universities": len(
                {row["university_id"] for row in rows if "university_id" in row}
            ),
            "unmatched_universities": len(unmatched),
            "request_count": int(manifest.get("request_count", 0)),
        }
        summary.update(self._summary_extras_without_reserved_keys(rows, manifest, set(summary)))
        return self._merge_manifest_summary(summary, manifest)

    def _execute(
        self,
        conn: sqlite3.Connection | None,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
        config: dict[str, Any],
        args: argparse.Namespace,
    ) -> None:
        conn = self._require_conn(conn)
        year_start = int(manifest["year_start"])
        year_end = int(manifest["year_end"])
        grant_source_id = self._ensure_grant_source(conn)
        self._write_rows(
            conn,
            grant_source_id=grant_source_id,
            rows=rows,
            year_start=year_start,
            year_end=year_end,
            university_ids=args.university_id or None,
        )
        # Stage the manifest to a temp file BEFORE committing so disk /
        # output-dir problems surface before the DB write is finalized.
        # Then commit. Then atomically promote the staged file. This
        # guarantees we never leave a committed run without a manifest
        # and never leave a manifest claiming a run that didn't commit.
        output_dir = resolve_output_dir(config)
        output_dir.mkdir(parents=True, exist_ok=True)
        final_path = output_dir / self.manifest_filename
        staged_path = final_path.with_name(final_path.name + ".tmp")
        staged_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        try:
            conn.commit()
        except BaseException:
            staged_path.unlink(missing_ok=True)
            raise
        try:
            staged_path.replace(final_path)
        except BaseException:
            staged_path.unlink(missing_ok=True)
            raise

    def _ensure_grant_source(self, conn: sqlite3.Connection) -> int:
        existing = conn.execute(
            "SELECT grant_source_id FROM grant_sources WHERE source_code = ?",
            (self.source_code,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE grant_sources
                SET source_name = ?, country_code = ?, is_us_federal = ?
                WHERE grant_source_id = ?
                """,
                (
                    self.source_name,
                    self.country_code,
                    self.is_us_federal,
                    existing["grant_source_id"],
                ),
            )
            return int(existing["grant_source_id"])

        # grant_source_id is declared INTEGER PRIMARY KEY (rowid alias), so
        # SQLite assigns it automatically when we omit the column.
        cursor = conn.execute(
            """
            INSERT INTO grant_sources (
                source_code,
                source_name,
                country_code,
                is_us_federal
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                self.source_code,
                self.source_name,
                self.country_code,
                self.is_us_federal,
            ),
        )
        new_id = cursor.lastrowid
        if new_id is None:
            raise RuntimeError("Failed to insert grant_sources row: lastrowid is None")
        return int(new_id)

    def _write_rows(
        self,
        conn: sqlite3.Connection,
        *,
        grant_source_id: int,
        rows: list[dict[str, Any]],
        year_start: int,
        year_end: int,
        university_ids: list[int] | None,
    ) -> None:
        if university_ids:
            placeholders = ",".join("?" for _ in university_ids)
            conn.execute(
                f"""
                DELETE FROM {self.GRANT_STATS_TABLE}
                WHERE grant_source_id = ?
                  AND year BETWEEN ? AND ?
                  AND university_id IN ({placeholders})
                """,
                [grant_source_id, year_start, year_end, *university_ids],
            )
        else:
            conn.execute(
                f"""
                DELETE FROM {self.GRANT_STATS_TABLE}
                WHERE grant_source_id = ?
                  AND year BETWEEN ? AND ?
                """,
                (grant_source_id, year_start, year_end),
            )
        conn.executemany(
            f"""
            INSERT INTO {self.GRANT_STATS_TABLE} (
                university_id,
                grant_source_id,
                year,
                grant_count,
                total_award_usd,
                source_snapshot
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["university_id"],
                    grant_source_id,
                    row["year"],
                    row["grant_count"],
                    row["total_award_usd"],
                    row["source_snapshot"],
                )
                for row in rows
            ],
        )


def grant_years_from_config(config: dict[str, Any]) -> tuple[int, int]:
    """Read ``years.grants_start`` and ``years.grants_end`` with validation."""
    years = config.get("years", {})
    start_year = int(years["grants_start"])
    end_year = int(years["grants_end"])
    if start_year > end_year:
        raise RuntimeError("config years.grants_start must be <= years.grants_end")
    return start_year, end_year


def load_grant_targets(
    conn: sqlite3.Connection,
    *,
    require_uei: bool = False,
) -> list[sqlite3.Row]:
    """Return rows of (university_id, canonical_name, state_code, ueis_csv).

    When ``require_uei`` is True, universities without any UEI row in
    ``university_ueis`` are excluded (INNER JOIN instead of LEFT JOIN).
    """
    join = "JOIN" if require_uei else "LEFT JOIN"
    return conn.execute(
        f"""
        SELECT
            u.university_id,
            u.canonical_name,
            u.state_code,
            GROUP_CONCAT(uu.uei) AS ueis_csv
        FROM universities u
        {join} university_ueis uu ON uu.university_id = u.university_id
        GROUP BY u.university_id, u.canonical_name, u.state_code
        ORDER BY u.university_id
        """
    ).fetchall()


def filter_targets_by_ids(
    targets: list[Any],
    requested_ids: list[int],
    *,
    id_attr: str = "university_id",
) -> list[Any]:
    """Filter a list of dataclass-like targets by university_id values."""
    if not requested_ids:
        return targets
    requested = set(requested_ids)
    filtered = [target for target in targets if getattr(target, id_attr) in requested]
    missing = sorted(requested - {getattr(target, id_attr) for target in filtered})
    if missing:
        raise RuntimeError(f"Unknown university_id values: {missing}")
    return filtered
