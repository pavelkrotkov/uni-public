"""The two-directional false-positive sweep in load_naviance (#51, PR #60 review).

A fuzzy link can become a false positive from either direction:

* a fuzzy result in *this* run lands on a university an exact or manual match
  already claims — handled inside ``match_batch``;
* a fuzzy link persisted by an *earlier* run whose university has since gained
  an exact or manual match — handled by the SQL sweep here.

The second case only shows up on an incremental replay, which is how it was
missed when the SQL pass was first replaced.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from load_naviance import run_fuzzy_match

SCHEMA = """
CREATE TABLE universities (
    university_id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL
);
CREATE TABLE naviance_colleges (
    college_id INTEGER PRIMARY KEY,
    college_name TEXT NOT NULL,
    naviance_uuid TEXT,
    university_id INTEGER,
    match_method TEXT,
    match_score REAL
);
CREATE TABLE university_identifiers (
    university_id INTEGER NOT NULL,
    system_code TEXT NOT NULL,
    external_id TEXT NOT NULL,
    match_status TEXT,
    UNIQUE (university_id, system_code, external_id)
);
INSERT INTO universities VALUES (1, 'Rice University'), (2, 'Brandeis University');
"""


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


def _college(
    conn: sqlite3.Connection,
    college_id: int,
    name: str,
    *,
    university_id: int | None = None,
    method: str | None = None,
    score: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO naviance_colleges VALUES (?, ?, ?, ?, ?, ?)",
        (college_id, name, f"uuid-{college_id}", university_id, method, score),
    )


def _state(conn: sqlite3.Connection) -> dict[int, tuple[int | None, str | None]]:
    return {
        row["college_id"]: (row["university_id"], row["match_method"])
        for row in conn.execute(
            "SELECT college_id, university_id, match_method FROM naviance_colleges"
        )
    }


def test_a_fuzzy_link_persisted_by_an_earlier_run_is_swept(conn: sqlite3.Connection) -> None:
    """The incremental-replay case: the manual match arrived after the fuzzy one."""
    _college(conn, 1, "Rice Universty", university_id=1, method="fuzzy", score=0.9)
    _college(conn, 2, "Rice University", university_id=1, method="manual", score=1.0)

    stats = run_fuzzy_match(conn)

    assert _state(conn)[1] == (None, None), "stale fuzzy link should be cleared"
    assert _state(conn)[2] == (1, "manual"), "the manual match is untouched"
    assert stats["dropped_collisions"] == 1


def test_the_swept_college_keeps_no_identifier(conn: sqlite3.Connection) -> None:
    """A cleared link must not leave its NAVIANCE identifier behind."""
    _college(conn, 1, "Rice Universty", university_id=1, method="fuzzy", score=0.9)
    _college(conn, 2, "Rice University", university_id=1, method="manual", score=1.0)

    run_fuzzy_match(conn)

    identifiers = conn.execute(
        "SELECT external_id FROM university_identifiers WHERE system_code='NAVIANCE'"
    ).fetchall()
    assert [row["external_id"] for row in identifiers] == ["uuid-2"]


def test_a_fuzzy_collision_within_one_run_is_skipped(conn: sqlite3.Connection) -> None:
    """The other direction, handled inside match_batch rather than by SQL."""
    _college(conn, 1, "Rice University", university_id=1, method="manual", score=1.0)
    _college(conn, 2, "Rice Universty")

    stats = run_fuzzy_match(conn)

    assert _state(conn)[2] == (None, None)
    assert stats["dropped_collisions"] == 1


def test_a_clean_fuzzy_link_survives_both_sweeps(conn: sqlite3.Connection) -> None:
    _college(conn, 1, "Brandies Universty")

    stats = run_fuzzy_match(conn)

    university_id, method = _state(conn)[1]
    assert (university_id, method) == (2, "fuzzy")
    assert stats["dropped_collisions"] == 0
    assert stats["fuzzy_auto"] == 1


def test_an_exact_match_is_linked_and_not_swept(conn: sqlite3.Connection) -> None:
    _college(conn, 1, "Rice University")

    stats = run_fuzzy_match(conn)

    assert _state(conn)[1] == (1, "exact")
    assert stats["exact"] == 1
    assert stats["dropped_collisions"] == 0
