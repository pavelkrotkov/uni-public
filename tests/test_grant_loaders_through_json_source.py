"""End-to-end tests for the grant loaders, driven through the JsonSource seam.

Before the seam existed these loaders could not be tested at all: every path
below ``load()`` went through a hand-rolled ``fetch_json`` that called urllib
directly. These tests run the real ``run()`` — argument parsing, config,
targets query, pagination, matching, aggregation, staging, summary — with a
``RecordedJsonSource`` standing in for the network.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
import yaml
from load_nih_awards import NIHAwardsLoader
from load_nsf_awards import NSFAwardsLoader
from load_usaspending_totals import USASpendingTotalsLoader
from shared.http import FetchCall, HttpJsonSource, JsonSourceHTTPError, RecordedJsonSource

SCHEMA = """
CREATE TABLE universities (
    university_id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    state_code TEXT
);
CREATE TABLE university_ueis (
    university_id INTEGER NOT NULL,
    uei TEXT NOT NULL,
    is_primary INTEGER DEFAULT 0
);
CREATE TABLE grant_sources (
    grant_source_id INTEGER PRIMARY KEY,
    source_code TEXT NOT NULL UNIQUE,
    source_name TEXT,
    country_code TEXT,
    is_us_federal INTEGER
);
CREATE TABLE university_grant_year_stats (
    university_id INTEGER NOT NULL,
    grant_source_id INTEGER NOT NULL,
    year INTEGER NOT NULL,
    grant_count INTEGER,
    total_award_usd REAL,
    source_snapshot TEXT,
    PRIMARY KEY (university_id, grant_source_id, year)
);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "univ.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO universities VALUES (1, 'Example State University', 'CA')",
    )
    conn.execute("INSERT INTO university_ueis VALUES (1, 'UEI000000001', 1)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "project": {"output_dir": "outputs/test"},
                "years": {"grants_start": 2023, "grants_end": 2023},
            }
        )
    )
    return path


def _argv(db_path: Path, config_path: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--db",
        str(db_path),
        "--config",
        str(config_path),
        "--staging-dir",
        str(tmp_path / "staging"),
        *extra,
    ]


def _payload(call: FetchCall) -> dict[str, Any]:
    """The JSON body of a recorded call, asserted present."""
    assert call.payload is not None, f"expected a JSON body on {call.url}"
    return call.payload


def _params(call: FetchCall) -> Mapping[str, Any]:
    """The query params of a recorded call, asserted present."""
    assert call.params is not None, f"expected query params on {call.url}"
    return call.params


def _summary(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """Parse the JSON summary envelope, which is printed last and pretty-printed."""
    lines = capsys.readouterr().out.strip().split("\n")
    start = max(index for index, line in enumerate(lines) if line == "{")
    return json.loads("\n".join(lines[start:]))


# --------------------------------------------------------------------------
# NIH
# --------------------------------------------------------------------------


def _nih_project(year: int, project_num: str, amount: float) -> dict[str, Any]:
    return {
        "project_num": project_num,
        "fiscal_year": year,
        "award_amount": amount,
        "organization": {
            "org_name": "EXAMPLE STATE UNIVERSITY",
            "org_state": "CA",
            "primary_uei": "UEI000000001",
        },
    }


def test_nih_loader_aggregates_a_matched_project(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedJsonSource(
        [
            {
                "results": [_nih_project(2023, "R01AA", 500_000.0)],
                "meta": {"total": 1},
            }
        ]
    )
    NIHAwardsLoader(json_source=source).run(_argv(db_path, config_path, tmp_path))

    summary = _summary(capsys)
    assert summary["yearly_rows"] == 1
    assert summary["covered_universities"] == 1
    assert summary["unmatched_universities"] == 0
    assert summary["request_count"] == 1
    assert _payload(source.calls[0])["criteria"]["fiscal_years"] == [2023]


def test_nih_loader_paginates_until_the_reported_total_is_reached(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedJsonSource(
        [
            {"results": [_nih_project(2023, "R01AA", 100.0)], "meta": {"total": 2}},
            {"results": [_nih_project(2023, "R01BB", 200.0)], "meta": {"total": 2}},
        ]
    )
    NIHAwardsLoader(json_source=source).run(
        _argv(db_path, config_path, tmp_path, "--sleep-seconds", "0")
    )

    summary = _summary(capsys)
    assert summary["request_count"] == 2
    assert summary["matched_project_years"] == 2
    assert _payload(source.calls[1])["offset"] == 1


def test_nih_loader_deduplicates_a_project_repeated_within_a_year(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedJsonSource(
        [
            {
                "results": [
                    _nih_project(2023, "R01AA", 500_000.0),
                    _nih_project(2023, "R01AA", 500_000.0),
                ],
                "meta": {"total": 2},
            }
        ]
    )
    NIHAwardsLoader(json_source=source).run(_argv(db_path, config_path, tmp_path))
    assert _summary(capsys)["matched_project_years"] == 1


def test_nih_loader_falls_back_to_uei_search_when_names_match_nothing(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def responder(call: FetchCall) -> dict[str, Any]:
        criteria = _payload(call)["criteria"]
        if "org_ueis" in criteria:
            return {"results": [_nih_project(2023, "R01CC", 750.0)], "meta": {"total": 1}}
        return {"results": [], "meta": {"total": 0}}

    source = RecordedJsonSource(responder)
    NIHAwardsLoader(json_source=source).run(_argv(db_path, config_path, tmp_path))

    summary = _summary(capsys)
    assert summary["yearly_rows"] == 1
    assert summary["unmatched_universities"] == 0
    assert _payload(source.calls[-1])["criteria"]["org_ueis"] == ["UEI000000001"]


def test_nih_loader_tolerates_an_api_that_rejects_the_uei_criterion(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """org_ueis was added in 2024; a 400 means the API predates it, not a failure."""

    def responder(call: FetchCall) -> Any:
        if "org_ueis" in _payload(call)["criteria"]:
            return JsonSourceHTTPError("unsupported criterion", status=400)
        return {"results": [], "meta": {"total": 0}}

    NIHAwardsLoader(json_source=RecordedJsonSource(responder)).run(
        _argv(db_path, config_path, tmp_path)
    )
    assert _summary(capsys)["unmatched_universities"] == 1


def test_nih_loader_propagates_a_non_400_error_from_the_uei_fallback(
    db_path: Path, config_path: Path, tmp_path: Path
) -> None:
    def responder(call: FetchCall) -> Any:
        if "org_ueis" in _payload(call)["criteria"]:
            return JsonSourceHTTPError("server is down", status=503)
        return {"results": [], "meta": {"total": 0}}

    with pytest.raises(JsonSourceHTTPError):
        NIHAwardsLoader(json_source=RecordedJsonSource(responder)).run(
            _argv(db_path, config_path, tmp_path)
        )


def test_nih_loader_ignores_a_project_from_a_different_state(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _nih_project(2023, "R01AA", 500_000.0)
    project["organization"]["org_state"] = "NY"
    project["organization"]["primary_uei"] = "OTHERUEI0001"

    def responder(call: FetchCall) -> dict[str, Any]:
        if "org_ueis" in _payload(call)["criteria"]:
            return {"results": [], "meta": {"total": 0}}
        return {"results": [project], "meta": {"total": 1}}

    NIHAwardsLoader(json_source=RecordedJsonSource(responder)).run(
        _argv(db_path, config_path, tmp_path)
    )
    assert _summary(capsys)["unmatched_universities"] == 1


def test_nih_loader_writes_rows_on_execute(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedJsonSource(
        [{"results": [_nih_project(2023, "R01AA", 500_000.0)], "meta": {"total": 1}}]
    )
    NIHAwardsLoader(json_source=source).run(_argv(db_path, config_path, tmp_path, "--execute"))
    capsys.readouterr()

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT u.university_id, u.year, u.grant_count, u.total_award_usd, s.source_code
        FROM university_grant_year_stats u
        JOIN grant_sources s ON s.grant_source_id = u.grant_source_id
        """
    ).fetchall()
    conn.close()
    assert rows == [(1, 2023, 1, 500000.0, "NIH")]


# --------------------------------------------------------------------------
# NSF
# --------------------------------------------------------------------------


def _nsf_award(award_id: str, amount: str, date: str = "01/15/2023") -> dict[str, Any]:
    return {
        "id": award_id,
        "awardeeName": "Example State University",
        "awardeeStateCode": "CA",
        "fundsObligatedAmt": amount,
        "date": date,
        "ueiNumber": "UEI000000001",
    }


def test_nsf_loader_aggregates_a_matched_award(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedJsonSource(
        [
            {
                "response": {
                    "award": [_nsf_award("2300001", "250000")],
                    "metadata": {"totalCount": 1},
                }
            }
        ]
    )
    NSFAwardsLoader(json_source=source).run(_argv(db_path, config_path, tmp_path))

    summary = _summary(capsys)
    assert summary["yearly_rows"] == 1
    assert summary["covered_universities"] == 1
    assert summary["request_count"] == 1
    assert _params(source.calls[0])["awardeeStateCode"] == "CA"
    assert _params(source.calls[0])["dateStart"] == "01/01/2023"


@pytest.mark.parametrize("reverse", [False, True])
def test_nsf_loader_unions_aliases_and_out_of_order_pages(
    db_path: Path,
    config_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    reverse: bool,
) -> None:
    aliases = ["Example State University", "Example University"]
    monkeypatch.setattr(
        "load_nsf_awards.alias_variants", lambda _name: aliases[::-1] if reverse else aliases
    )
    pages = [
        [_nsf_award("2300001", "0.1"), _nsf_award("2300002", "0.2")],
        [_nsf_award("2300003", "0.3"), _nsf_award("2300004", "0.4")],
    ]
    if reverse:
        pages.reverse()

    def responder(call: FetchCall) -> dict[str, Any]:
        params = _params(call)
        offset = int(params["offset"])
        assert params["sortKey"] == "awardNumber"
        assert params["rpp"] == 2
        if params["awardeeName"] == '"Example University"':
            chunk = (
                [_nsf_award("2300002", "0.2"), _nsf_award("2300005", "0.5")] if offset == 0 else []
            )
        else:
            chunk = pages[offset // 2] if offset < 4 else []
        return {
            "response": {
                "award": chunk,
                "metadata": {"totalCount": 1 if offset == 0 else 0},
            }
        }

    source = RecordedJsonSource(responder)
    NSFAwardsLoader(json_source=source).run(
        _argv(db_path, config_path, tmp_path, "--page-size", "2", "--sleep-seconds", "0")
    )

    summary = _summary(capsys)
    assert summary["matched_awards"] == 5
    assert summary["request_count"] == 5
    rows = pq.read_table(tmp_path / "staging" / "nsf_awards.parquet").to_pylist()
    assert [(row["grant_count"], row["total_award_usd"]) for row in rows] == [(5, 1.5)]


def test_nsf_loader_reports_a_university_with_no_matching_awards(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedJsonSource(
        lambda _call: {"response": {"award": [], "metadata": {"totalCount": 0}}}
    )
    NSFAwardsLoader(json_source=source).run(_argv(db_path, config_path, tmp_path))

    summary = _summary(capsys)
    assert summary["yearly_rows"] == 0
    assert summary["unmatched_universities"] == 1


# --------------------------------------------------------------------------
# USAspending
# --------------------------------------------------------------------------


def _usaspending_responder(*, award_count: int = 3, amount: float = 1_000_000.0) -> Any:
    def responder(call: FetchCall) -> dict[str, Any]:
        if "award_types" in call.url:
            return {"grants": {"02": "Block Grant", "03": "Formula Grant"}}
        if "spending_by_award_count" in call.url:
            return {"results": {"grants": award_count}}
        return {
            "results": [{"amount": amount}],
            "page_metadata": {"hasNext": False},
        }

    return responder


def test_usaspending_loader_aggregates_a_year(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedJsonSource(_usaspending_responder())
    USASpendingTotalsLoader(json_source=source).run(_argv(db_path, config_path, tmp_path))

    summary = _summary(capsys)
    assert summary["yearly_rows"] == 1
    assert summary["covered_universities"] == 1
    assert summary["award_type_codes"] == 2
    assert _payload(source.calls[1])["filters"]["recipient_search_text"] == ["UEI000000001"]


def test_usaspending_loader_falls_back_when_award_types_cannot_be_fetched(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed award-types lookup must not abort the run — it has a static fallback."""

    def responder(call: FetchCall) -> Any:
        if "award_types" in call.url:
            return JsonSourceHTTPError("gone", status=404)
        return _usaspending_responder()(call)

    USASpendingTotalsLoader(json_source=RecordedJsonSource(responder)).run(
        _argv(db_path, config_path, tmp_path)
    )
    summary = _summary(capsys)
    assert summary["yearly_rows"] == 1
    assert summary["award_type_codes"] > 2


def test_usaspending_loader_records_a_year_that_errored_without_failing_the_run(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def responder(call: FetchCall) -> Any:
        if "award_types" in call.url:
            return {"grants": {"02": "Block Grant"}}
        return JsonSourceHTTPError("server is down", status=503)

    USASpendingTotalsLoader(json_source=RecordedJsonSource(responder)).run(
        _argv(db_path, config_path, tmp_path)
    )
    summary = _summary(capsys)
    assert summary["yearly_rows"] == 0
    assert summary["errored_universities"] == 1
    assert summary["unmatched_universities"] == 1


def test_usaspending_loader_skips_a_year_with_no_spending(
    db_path: Path, config_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RecordedJsonSource(_usaspending_responder(award_count=0, amount=0.0))
    USASpendingTotalsLoader(json_source=source).run(_argv(db_path, config_path, tmp_path))

    summary = _summary(capsys)
    assert summary["yearly_rows"] == 0
    assert summary["unmatched_universities"] == 1


# --------------------------------------------------------------------------
# the seam itself
# --------------------------------------------------------------------------


def test_a_loader_that_needs_no_json_source_raises_when_one_is_requested(
    db_path: Path, config_path: Path, tmp_path: Path
) -> None:
    loader = NIHAwardsLoader()
    with pytest.raises(RuntimeError, match="has no json_source"):
        _ = loader.json_source


def test_request_policy_flags_reach_the_http_adapter() -> None:
    loader = NIHAwardsLoader()
    args = loader._parse_args(["--request-timeout", "12", "--max-attempts", "2"])
    adapter = loader.build_json_source(args)
    assert isinstance(adapter, HttpJsonSource)
    assert adapter.timeout == 12
    assert adapter.max_attempts == 2
    assert adapter.headers["User-Agent"] == "univ-phase2-5/1.0"
