"""Naviance schema, outcome vocabulary, and shared read shapes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

DDL = """\
CREATE TABLE IF NOT EXISTS naviance_colleges (
    college_id     INTEGER PRIMARY KEY,
    university_id  INTEGER REFERENCES universities,
    naviance_uuid  TEXT NOT NULL UNIQUE,
    college_name   TEXT NOT NULL,
    dropdown_label TEXT NOT NULL,
    app_count      INTEGER,
    profile_url    TEXT,
    match_method   TEXT,
    match_score    REAL,
    harvested_at   TEXT
);

CREATE TABLE IF NOT EXISTS high_school_contexts (
    high_school_id INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    naviance_slug  TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS page_snapshots (
    snapshot_id    INTEGER PRIMARY KEY,
    college_id     INTEGER NOT NULL REFERENCES naviance_colleges,
    high_school_id INTEGER NOT NULL REFERENCES high_school_contexts,
    fetched_at     TEXT NOT NULL,
    source_url     TEXT,
    page_title     TEXT,
    mode           TEXT,
    raw_json_path  TEXT
);

CREATE TABLE IF NOT EXISTS quick_facts (
    snapshot_id     INTEGER NOT NULL REFERENCES page_snapshots,
    fact_key        TEXT NOT NULL,
    fact_value_text TEXT,
    fact_value_num  REAL,
    unit            TEXT,
    PRIMARY KEY (snapshot_id, fact_key)
);

CREATE TABLE IF NOT EXISTS admissions_summary (
    snapshot_id           INTEGER PRIMARY KEY REFERENCES page_snapshots,
    acceptance_rate_pct   REAL,
    application_fee       INTEGER,
    application_fee_note  TEXT,
    next_deadline_text    TEXT,
    test_optional         INTEGER,
    accepts_common_app    INTEGER,
    accepts_coalition_app INTEGER,
    early_decision        INTEGER,
    wait_list             INTEGER
);

CREATE TABLE IF NOT EXISTS admissions_policies (
    snapshot_id  INTEGER NOT NULL REFERENCES page_snapshots,
    policy_key   TEXT NOT NULL,
    policy_value TEXT,
    PRIMARY KEY (snapshot_id, policy_key)
);

CREATE TABLE IF NOT EXISTS application_factors (
    snapshot_id       INTEGER NOT NULL REFERENCES page_snapshots,
    importance_bucket TEXT NOT NULL,
    factor_name       TEXT NOT NULL,
    sort_order        INTEGER,
    PRIMARY KEY (snapshot_id, importance_bucket, factor_name)
);

CREATE TABLE IF NOT EXISTS high_school_application_history (
    snapshot_id INTEGER NOT NULL REFERENCES page_snapshots,
    year        INTEGER NOT NULL,
    applied     INTEGER,
    accepted    INTEGER,
    enrolled    INTEGER,
    PRIMARY KEY (snapshot_id, year)
);

CREATE TABLE IF NOT EXISTS scattergram_sessions (
    session_id         INTEGER PRIMARY KEY,
    snapshot_id        INTEGER NOT NULL REFERENCES page_snapshots,
    score_option_value TEXT NOT NULL,
    score_option_text  TEXT,
    x_axis_kind        TEXT NOT NULL,
    y_axis_kind        TEXT NOT NULL,
    x_axis_label       TEXT,
    y_axis_label       TEXT,
    point_count        INTEGER,
    UNIQUE (snapshot_id, score_option_value)
);

CREATE TABLE IF NOT EXISTS scattergram_points (
    session_id        INTEGER NOT NULL REFERENCES scattergram_sessions,
    point_index       INTEGER NOT NULL,
    x_value           REAL,
    y_value           REAL,
    center_x          REAL,
    center_y          REAL,
    outcome_group     TEXT,
    decision_type     TEXT,
    marker_signature  TEXT,
    is_user_point     INTEGER DEFAULT 0,
    is_reference_line INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, point_index)
);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every Naviance table if absent. Idempotent."""
    conn.executescript(DDL)


# ---------------------------------------------------------------------------
# The outcome vocabulary
# ---------------------------------------------------------------------------

#: Outcome groups that count as an admission. "Waitlisted/Accepted" means the
#: student came off the waitlist, so it is an admit.
ADMITTED_OUTCOMES: frozenset[str] = frozenset({"Accepted", "Waitlisted/Accepted"})

#: Outcome groups that count as a rejection.
DENIED_OUTCOMES: frozenset[str] = frozenset({"Denied", "Waitlisted/Denied"})

#: A waitlist result Naviance never resolved. Neither an admit nor a denial, so
#: some model specs drop it rather than treat it as either.
UNRESOLVED_OUTCOME = "Waitlisted/Unknown"

#: Not a student at all — the chart legend, scraped as points.
LEGEND_OUTCOME = "Legend"

#: Every value the scrape actually produces, checked by the tests against the
#: live database so a new one cannot appear unnoticed.
KNOWN_OUTCOMES: frozenset[str] = (
    ADMITTED_OUTCOMES | DENIED_OUTCOMES | {UNRESOLVED_OUTCOME, LEGEND_OUTCOME}
)

#: The predicate that keeps only real, plottable student outcomes. Legend rows,
#: the viewing student's own point, reference lines and null coordinates are all
#: sentinels rather than data.
REAL_POINT_PREDICATE = """
    sp.is_user_point = 0
    AND sp.is_reference_line = 0
    AND sp.outcome_group <> 'Legend'
    AND sp.x_value IS NOT NULL
    AND sp.y_value IS NOT NULL
"""


def is_admitted(outcome_group: str | None) -> bool:
    """Did this outcome end in an offer?"""
    return outcome_group in ADMITTED_OUTCOMES


def is_resolved(outcome_group: str | None) -> bool:
    """Did this outcome resolve either way? False for an unresolved waitlist."""
    return outcome_group in ADMITTED_OUTCOMES or outcome_group in DENIED_OUTCOMES


# ---------------------------------------------------------------------------
# Read shape 1: the admission model
# ---------------------------------------------------------------------------

#: One row per plottable scattergram point, for model fitting. Callers wrap this
#: in their own DataFrame reader so this module stays pandas-free.
SCATTERGRAM_POINTS_SQL = f"""
SELECT
    nc.college_id,
    nc.college_name,
    ads.acceptance_rate_pct,
    ss.score_option_value AS score_type,
    sp.x_value            AS test_score,
    sp.y_value            AS gpa,
    sp.outcome_group,
    sp.decision_type
FROM scattergram_points sp
JOIN scattergram_sessions ss ON ss.session_id = sp.session_id
JOIN page_snapshots ps       ON ps.snapshot_id = ss.snapshot_id
JOIN naviance_colleges nc    ON nc.college_id  = ps.college_id
JOIN admissions_summary ads  ON ads.snapshot_id = ps.snapshot_id
WHERE {REAL_POINT_PREDICATE}
"""


def scattergram_point_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Plottable scattergram points as plain dicts, for callers without pandas."""
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(SCATTERGRAM_POINTS_SQL)]


# ---------------------------------------------------------------------------
# Read shape 2: the explorer
# ---------------------------------------------------------------------------


@dataclass
class Scattergram:
    """One score-type view of a college's scattergram."""

    label: str
    x_label: str | None
    y_label: str | None
    points: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class College:
    """Everything the explorer shows for one scraped college."""

    id: int
    name: str
    app_count: int | None
    profile_url: str | None
    match_method: str | None
    quick_facts: dict[str, str] = field(default_factory=dict)
    admissions_summary: dict[str, Any] = field(default_factory=dict)
    admissions_policies: dict[str, Any] = field(default_factory=dict)
    hs_history: list[dict[str, Any]] = field(default_factory=list)
    scattergrams: dict[str, Scattergram] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape the explorer template expects."""
        return {
            "id": self.id,
            "name": self.name,
            "app_count": self.app_count,
            "profile_url": self.profile_url,
            "match_method": self.match_method,
            "quick_facts": self.quick_facts,
            "admissions_summary": self.admissions_summary,
            "admissions_policies": self.admissions_policies,
            "hs_history": self.hs_history,
            "scattergrams": {
                key: {
                    "label": gram.label,
                    "x_label": gram.x_label,
                    "y_label": gram.y_label,
                    "points": gram.points,
                }
                for key, gram in self.scattergrams.items()
            },
        }


def _format_fact(row: sqlite3.Row) -> str:
    """Prefer the text value; fall back to the number, dropping a bare .0."""
    text = row["fact_value_text"]
    if text:
        return text
    number = row["fact_value_num"]
    if number is None:
        return ""
    return str(int(number)) if number == int(number) else str(number)


def _group_by(rows: list[sqlite3.Row], key: str) -> dict[Any, list[sqlite3.Row]]:
    grouped: dict[Any, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


def _scattergrams(
    sessions: list[sqlite3.Row], points: dict[Any, list[sqlite3.Row]]
) -> dict[str, Scattergram]:
    result: dict[str, Scattergram] = {}
    for session in sessions:
        result[session["score_option_value"]] = Scattergram(
            label=session["score_option_text"],
            x_label=session["x_axis_label"],
            y_label=session["y_axis_label"],
            points=[
                {
                    "x_value": point["x_value"],
                    "y_value": point["y_value"],
                    "outcome_group": point["outcome_group"],
                    "decision_type": point["decision_type"],
                    "is_user_point": point["is_user_point"],
                }
                for point in points.get(session["session_id"], [])
            ],
        )
    return result


def load_colleges(conn: sqlite3.Connection) -> list[College]:
    """Every scraped college with its facts, policies, history and scattergrams.

    Issues a fixed number of queries regardless of how many colleges there are.
    The previous version ran six per college plus one per scattergram session.
    """
    conn.row_factory = sqlite3.Row

    snapshots = conn.execute(
        """
        SELECT MIN(ps.snapshot_id) AS snapshot_id, ps.college_id, nc.college_name, nc.app_count,
               nc.profile_url, nc.match_method
        FROM page_snapshots ps
        JOIN naviance_colleges nc ON nc.college_id = ps.college_id
        GROUP BY ps.college_id
        ORDER BY nc.college_name
        """
    ).fetchall()
    if not snapshots:
        return []

    # Every grouped read is explicitly ordered. The per-college queries this
    # replaced were ordered only incidentally, by whichever primary-key index
    # the planner happened to use, and quick_facts render in order — so a
    # full-table scan without ORDER BY silently reshuffled the page.
    facts = _group_by(
        conn.execute(
            """SELECT snapshot_id, fact_key, fact_value_text, fact_value_num
               FROM quick_facts ORDER BY snapshot_id, fact_key"""
        ).fetchall(),
        "snapshot_id",
    )
    summaries = {
        row["snapshot_id"]: dict(row)
        for row in conn.execute("SELECT * FROM admissions_summary").fetchall()
    }
    policies = _group_by(
        conn.execute(
            """SELECT snapshot_id, policy_key, policy_value
               FROM admissions_policies ORDER BY snapshot_id, policy_key"""
        ).fetchall(),
        "snapshot_id",
    )
    history = _group_by(
        conn.execute(
            """SELECT snapshot_id, year, applied, accepted, enrolled
               FROM high_school_application_history ORDER BY snapshot_id, year"""
        ).fetchall(),
        "snapshot_id",
    )
    sessions = _group_by(
        conn.execute(
            """SELECT session_id, snapshot_id, score_option_value, score_option_text,
                      x_axis_label, y_axis_label, point_count
               FROM scattergram_sessions ORDER BY snapshot_id, score_option_value"""
        ).fetchall(),
        "snapshot_id",
    )
    points = _group_by(
        conn.execute(
            """SELECT session_id, x_value, y_value, outcome_group, decision_type,
                      is_user_point
               FROM scattergram_points sp
               WHERE sp.is_reference_line = 0
               ORDER BY sp.session_id, sp.point_index"""
        ).fetchall(),
        "session_id",
    )

    colleges: list[College] = []
    for snapshot in snapshots:
        snapshot_id = snapshot["snapshot_id"]
        college = College(
            id=snapshot["college_id"],
            name=snapshot["college_name"],
            app_count=snapshot["app_count"],
            profile_url=snapshot["profile_url"],
            match_method=snapshot["match_method"],
            quick_facts={row["fact_key"]: _format_fact(row) for row in facts.get(snapshot_id, [])},
            admissions_summary=summaries.get(snapshot_id, {}),
            admissions_policies={
                row["policy_key"]: row["policy_value"] for row in policies.get(snapshot_id, [])
            },
            hs_history=[
                {
                    "year": row["year"],
                    "applied": row["applied"],
                    "accepted": row["accepted"],
                    "enrolled": row["enrolled"],
                }
                for row in history.get(snapshot_id, [])
            ],
            scattergrams=_scattergrams(sessions.get(snapshot_id, []), points),
        )
        colleges.append(college)
    return colleges
