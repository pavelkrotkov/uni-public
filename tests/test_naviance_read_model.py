"""The Naviance read model (#53).

The schema, both read shapes, and the rule defining *admitted* used to live in
three files that never referenced each other. These tests pin the vocabulary,
the sentinel exclusions, and the grouped read that replaced an O(colleges)
query loop.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from shared.naviance import (
    ADMITTED_OUTCOMES,
    DENIED_OUTCOMES,
    KNOWN_OUTCOMES,
    LEGEND_OUTCOME,
    SCATTERGRAM_POINTS_SQL,
    UNRESOLVED_OUTCOME,
    College,
    create_schema,
    is_admitted,
    is_resolved,
    load_colleges,
    scattergram_point_rows,
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    create_schema(connection)
    connection.execute(
        "INSERT INTO naviance_colleges "
        "(college_id, naviance_uuid, college_name, dropdown_label, app_count, "
        " profile_url, match_method) "
        "VALUES (1, 'uuid-1', 'Example College', 'Example College', 12, 'http://x', 'exact')"
    )
    connection.execute(
        "INSERT INTO high_school_contexts (high_school_id, name) VALUES (1, 'Example High School')"
    )
    connection.execute(
        "INSERT INTO page_snapshots "
        "(snapshot_id, college_id, high_school_id, fetched_at, source_url) "
        "VALUES (10, 1, 1, '2026-01-01T00:00:00Z', 'http://x')"
    )
    connection.executemany(
        "INSERT INTO quick_facts (snapshot_id, fact_key, fact_value_text, fact_value_num) "
        "VALUES (?, ?, ?, ?)",
        [
            (10, "Website", "http://x", None),
            (10, "CEEB code", "1234", None),
            (10, "Undergraduate enrollment", None, 1914.0),
            (10, "Ratio", None, 6.5),
        ],
    )
    connection.execute(
        "INSERT INTO admissions_summary (snapshot_id, acceptance_rate_pct) VALUES (10, 9.0)"
    )
    connection.executemany(
        "INSERT INTO admissions_policies (snapshot_id, policy_key, policy_value) VALUES (?, ?, ?)",
        [(10, "Test optional", "Yes"), (10, "Early decision", "No")],
    )
    connection.executemany(
        "INSERT INTO high_school_application_history "
        "(snapshot_id, year, applied, accepted, enrolled) VALUES (?, ?, ?, ?, ?)",
        [(10, 2023, 6, 2, 1), (10, 2022, 4, 1, 1)],
    )
    connection.execute(
        "INSERT INTO scattergram_sessions "
        "(session_id, snapshot_id, score_option_value, score_option_text, x_axis_kind, "
        " y_axis_kind, x_axis_label, y_axis_label, point_count) "
        "VALUES (100, 10, 'sat', 'SAT', 'score', 'gpa', 'SAT', 'GPA', 5)"
    )
    connection.executemany(
        "INSERT INTO scattergram_points "
        "(session_id, point_index, x_value, y_value, outcome_group, decision_type, "
        " is_user_point, is_reference_line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (100, 0, 1500.0, 4.0, "Accepted", "Regular Decision", 0, 0),
            (100, 1, 1300.0, 3.5, "Denied", "Regular Decision", 0, 0),
            (100, 2, 1400.0, 3.8, "Waitlisted/Accepted", "Regular Decision", 0, 0),
            (100, 3, 1450.0, 3.9, LEGEND_OUTCOME, None, 1, 0),
            (100, 4, None, None, "Accepted", None, 0, 1),
            (100, 5, None, 3.7, "Accepted", "Regular Decision", 0, 0),
        ],
    )
    yield connection
    connection.close()


# --------------------------------------------------------------------------
# the outcome vocabulary, in one place
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "admitted"),
    [
        ("Accepted", True),
        ("Waitlisted/Accepted", True),
        ("Denied", False),
        ("Waitlisted/Denied", False),
        (UNRESOLVED_OUTCOME, False),
        (LEGEND_OUTCOME, False),
        (None, False),
    ],
)
def test_is_admitted(outcome: str | None, admitted: bool) -> None:
    assert is_admitted(outcome) is admitted


def test_an_unresolved_waitlist_is_neither_admitted_nor_denied() -> None:
    """Why some model specs drop it rather than treat it as a rejection."""
    assert is_admitted(UNRESOLVED_OUTCOME) is False
    assert is_resolved(UNRESOLVED_OUTCOME) is False
    assert is_resolved("Denied") is True
    assert is_resolved("Accepted") is True


def test_admitted_and_denied_do_not_overlap() -> None:
    assert not (ADMITTED_OUTCOMES & DENIED_OUTCOMES)


def test_known_outcomes_covers_every_category() -> None:
    assert ADMITTED_OUTCOMES <= KNOWN_OUTCOMES
    assert DENIED_OUTCOMES <= KNOWN_OUTCOMES
    assert {UNRESOLVED_OUTCOME, LEGEND_OUTCOME} <= KNOWN_OUTCOMES


# --------------------------------------------------------------------------
# read shape 1: the model's points
# --------------------------------------------------------------------------


def test_scattergram_points_exclude_every_sentinel(conn: sqlite3.Connection) -> None:
    """Legend rows, the viewer's own point, reference lines and null coordinates."""
    rows = scattergram_point_rows(conn)
    assert len(rows) == 3
    assert {row["outcome_group"] for row in rows} == {
        "Accepted",
        "Denied",
        "Waitlisted/Accepted",
    }


def test_the_points_query_carries_the_college_and_acceptance_rate(
    conn: sqlite3.Connection,
) -> None:
    row = scattergram_point_rows(conn)[0]
    assert row["college_name"] == "Example College"
    assert row["acceptance_rate_pct"] == 9.0
    assert row["score_type"] == "sat"


def test_the_points_sql_is_a_single_declaration() -> None:
    """Both readers use this string; nobody restates the exclusions."""
    assert "is_user_point = 0" in SCATTERGRAM_POINTS_SQL
    assert "is_reference_line = 0" in SCATTERGRAM_POINTS_SQL
    assert "'Legend'" in SCATTERGRAM_POINTS_SQL


# --------------------------------------------------------------------------
# read shape 2: the explorer
# --------------------------------------------------------------------------


def test_load_colleges_assembles_one_college(conn: sqlite3.Connection) -> None:
    colleges = load_colleges(conn)
    assert len(colleges) == 1
    college = colleges[0]
    assert isinstance(college, College)
    assert (college.id, college.name, college.app_count) == (1, "Example College", 12)


def test_quick_facts_prefer_text_and_drop_a_bare_decimal(conn: sqlite3.Connection) -> None:
    facts = load_colleges(conn)[0].quick_facts
    assert facts["Website"] == "http://x"
    assert facts["Undergraduate enrollment"] == "1914"
    assert facts["Ratio"] == "6.5"


def test_quick_facts_are_ordered_deterministically(conn: sqlite3.Connection) -> None:
    """They render in order, and the query this replaced sorted only by accident."""
    assert list(load_colleges(conn)[0].quick_facts) == sorted(load_colleges(conn)[0].quick_facts)


def test_history_is_ordered_by_year(conn: sqlite3.Connection) -> None:
    assert [row["year"] for row in load_colleges(conn)[0].hs_history] == [2022, 2023]


def test_the_explorer_keeps_reference_lines_out_but_user_points_in(
    conn: sqlite3.Connection,
) -> None:
    """The explorer draws the viewer's own point specially, so it needs it."""
    points = load_colleges(conn)[0].scattergrams["sat"].points
    assert len(points) == 5
    assert sum(point["is_user_point"] for point in points) == 1


def test_scattergram_carries_its_axis_labels(conn: sqlite3.Connection) -> None:
    gram = load_colleges(conn)[0].scattergrams["sat"]
    assert (gram.label, gram.x_label, gram.y_label) == ("SAT", "SAT", "GPA")


def test_to_dict_matches_the_template_shape(conn: sqlite3.Connection) -> None:
    payload = load_colleges(conn)[0].to_dict()
    assert list(payload) == [
        "id",
        "name",
        "app_count",
        "profile_url",
        "match_method",
        "quick_facts",
        "admissions_summary",
        "admissions_policies",
        "hs_history",
        "scattergrams",
    ]
    assert list(payload["scattergrams"]["sat"]) == ["label", "x_label", "y_label", "points"]


def test_a_college_with_no_snapshot_is_omitted(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO naviance_colleges (college_id, naviance_uuid, college_name, dropdown_label) "
        "VALUES (2, 'uuid-2', 'Unscraped College', 'Unscraped College')"
    )
    assert [college.id for college in load_colleges(conn)] == [1]


def test_multiple_snapshots_keep_the_first_even_with_a_reverse_index(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO page_snapshots (snapshot_id, college_id, high_school_id, fetched_at) "
        "VALUES (20, 1, 1, '2026-02-01')"
    )
    conn.execute(
        "CREATE INDEX reverse_snapshots ON page_snapshots (college_id DESC, snapshot_id DESC)"
    )
    assert load_colleges(conn)[0].admissions_summary["snapshot_id"] == 10


def test_an_empty_database_yields_no_colleges() -> None:
    connection = sqlite3.connect(":memory:")
    create_schema(connection)
    try:
        assert load_colleges(connection) == []
    finally:
        connection.close()


def test_create_schema_is_idempotent(conn: sqlite3.Connection) -> None:
    create_schema(conn)
    create_schema(conn)
    assert load_colleges(conn)[0].id == 1


# --------------------------------------------------------------------------
# guards against the vocabulary or the readers drifting again
# --------------------------------------------------------------------------


def test_the_live_database_contains_no_unknown_outcome_group() -> None:
    """A re-scrape introducing a new outcome must fail here, not silently.

    Both readers classify on these values, so an unrecognised one would be
    treated as a rejection by the model and coloured as "other" by the explorer
    without anything complaining.
    """
    from shared import ROOT

    db_path = ROOT / "univ.sqlite3"
    if not db_path.exists():
        pytest.skip("live database not present")
    connection = sqlite3.connect(db_path)
    try:
        found = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT outcome_group FROM scattergram_points "
                "WHERE outcome_group IS NOT NULL"
            )
        }
    finally:
        connection.close()
    assert found <= KNOWN_OUTCOMES, (
        f"unrecognised outcome group(s): {sorted(found - KNOWN_OUTCOMES)}"
    )


def test_the_findings_scripts_contain_no_sql() -> None:
    """The read model has one owner; findings/ asks it rather than querying."""
    from shared import ROOT

    offenders = []
    for path in sorted((ROOT / "findings").glob("*.py")):
        text = path.read_text()
        for keyword in ("SELECT ", "FROM naviance_colleges", "FROM scattergram_points"):
            if keyword in text:
                offenders.append(f"{path.name} contains {keyword!r}")
    assert offenders == [], offenders
