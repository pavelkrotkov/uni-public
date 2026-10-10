#!/usr/bin/env -S uv run
# /// script
# dependencies = ["PyYAML>=6.0", "rapidfuzz>=3.0"]
# ///

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from identity_overrides import alias_variants
from shared import ROOT
from shared.data_loader import ManualDataLoader
from shared.institution_match import (
    FUZZY_AUTO_THRESHOLD,
    FUZZY_REVIEW_THRESHOLD,
    MatchCandidate,
    UniversityRef,
    match_batch,
    normalize,
    similarity,
)
from shared.naviance import DDL

NAVIANCE_DIR = ROOT / "naviance" / "out"


# Naviance name → canonical universities.canonical_name
# Used for cases where fuzzy score falls just below the auto-link threshold
# due to formatting differences (hyphens, suffixes, full official names).
MANUAL_MATCHES: dict[str, str] = {
    "Binghamton University, State University of New York": "Binghamton University",
    "Columbia University": "Columbia University in the City of New York",
    "Georgia Institute of Technology": "Georgia Institute of Technology-Main Campus",
    "Indiana University Bloomington": "Indiana University-Bloomington",
    "Olin College of Engineering": "Franklin W Olin College of Engineering",
    "Penn State University Park": "Pennsylvania State University-Main Campus",
    "Rutgers University - New Brunswick": "Rutgers University-New Brunswick",
    "St. John's College, Annapolis": "St. John's College",
    "Stony Brook University, State University of New York": "Stony Brook University",
    "Texas A&M University": "Texas A&M University-College Station",
    "University at Albany, State University of New York": "University at Albany",
    "University at Buffalo, the State University of New York": "University at Buffalo",
    "University of California, Berkeley": "University of California-Berkeley",
    "University of California, Santa Cruz": "University of California-Santa Cruz",
    "University of Michigan": "University of Michigan-Ann Arbor",
    "University of Minnesota, Twin Cities Campus": "University of Minnesota-Twin Cities",
    "University of Virginia": "University of Virginia-Main Campus",
    "University of Washington": "University of Washington-Seattle Campus",
}


# ── helpers ───────────────────────────────────────────────────────────────────


def parse_pct(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


def parse_fee(text: str) -> tuple[int | None, str | None]:
    m = re.search(r"\$(\d+)", text)
    fee = int(m.group(1)) if m else None
    note = text.split("|", 1)[1].strip() if "|" in text else None
    return fee, note


def yes_no(value: str | None) -> int | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v == "yes":
        return 1
    if v == "no":
        return 0
    return None


def parse_int(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(re.sub(r"[,\s]", "", s))
    except ValueError:
        return None


def extract_num_unit(text: str) -> tuple[float | None, str | None]:
    m = re.match(r"^([\d,]+(?:\.\d+)?)\s*(.*)", text.strip())
    if m:
        try:
            num = float(m.group(1).replace(",", ""))
        except ValueError:
            return None, None
        unit = m.group(2).strip() or None
        return num, unit
    return None, None


# Identifier systems this loader populates from the Naviance quick facts.
IDENTIFIER_SYSTEMS = [
    ("NAVIANCE", "Naviance Student Profile UUID", 0, "From scattergram manifest harvest"),
    ("CEEB", "College Board CEEB Code", 1, "From Naviance quick facts"),
    ("ACT_CODE", "ACT Institution Code", 1, "From Naviance quick facts"),
]


def execute_sql_script_transactionally(conn: sqlite3.Connection, script: str) -> None:
    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        statement = "\n".join(pending).strip()
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            pending = []
    if "\n".join(pending).strip():
        raise RuntimeError("Incomplete SQL statement in DDL script")


def setup_schema(conn: sqlite3.Connection) -> None:
    execute_sql_script_transactionally(conn, DDL)
    for code, name, auth, notes in IDENTIFIER_SYSTEMS:
        conn.execute(
            "INSERT OR IGNORE INTO identifier_systems (system_code, system_name, is_authoritative, notes) VALUES (?, ?, ?, ?)",
            (code, name, auth, notes),
        )


# ── manifest ──────────────────────────────────────────────────────────────────


def load_manifest(conn: sqlite3.Connection, manifest_path: Path) -> int:
    data = json.loads(manifest_path.read_text())
    count = 0
    for row in data.get("rows", []):
        if not row.get("resolved"):
            continue
        conn.execute(
            """
            INSERT INTO naviance_colleges
                (naviance_uuid, college_name, dropdown_label, app_count, profile_url, harvested_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(naviance_uuid) DO UPDATE SET
                college_name   = excluded.college_name,
                dropdown_label = excluded.dropdown_label,
                app_count      = excluded.app_count,
                profile_url    = excluded.profile_url,
                harvested_at   = excluded.harvested_at
            """,
            (
                row["profileUuid"],
                row["collegeName"],
                row["label"],
                row.get("appCount"),
                row.get("profileUrl"),
                data.get("generatedAt"),
            ),
        )
        count += 1
    return count


# ── manual overrides ─────────────────────────────────────────────────────────


def run_manual_overrides(conn: sqlite3.Connection) -> int:
    uni_by_name = {
        name: uid
        for uid, name in conn.execute("SELECT university_id, canonical_name FROM universities")
    }
    count = 0
    for naviance_name, canonical_name in MANUAL_MATCHES.items():
        university_id = uni_by_name.get(canonical_name)
        if university_id is None:
            print(f"  WARN: manual override target not found: {canonical_name!r}", file=sys.stderr)
            continue
        cur = conn.execute(
            """
            UPDATE naviance_colleges
            SET university_id=?, match_method='manual', match_score=1.0
            WHERE college_name=? AND university_id IS NULL
            """,
            (university_id, naviance_name),
        )
        count += cur.rowcount
    return count


# ── fuzzy matching ────────────────────────────────────────────────────────────


def run_fuzzy_match(conn: sqlite3.Connection) -> dict[str, int]:
    """Link unmatched Naviance colleges to canonical universities.

    Scoring, thresholds and the false-positive sweep all live in
    shared.institution_match; this function reads the two tables, applies the
    decisions, and reports. Aliases come from data/identity_overrides.json,
    which the old Naviance-specific normalizer could not reach.
    """
    universe = [
        UniversityRef(
            university_id=row["university_id"],
            canonical_name=row["canonical_name"],
            aliases=tuple(alias_variants(row["canonical_name"])),
        )
        for row in conn.execute("SELECT university_id, canonical_name FROM universities").fetchall()
    ]
    candidates = [
        (row["college_id"], MatchCandidate(name=row["college_name"]))
        for row in conn.execute(
            "SELECT college_id, college_name FROM naviance_colleges WHERE university_id IS NULL"
        ).fetchall()
    ]
    # Universities already linked by an exact or manual pass. A fuzzy result
    # landing on one of these is a false positive: the real match is recorded.
    claimed = {
        row["university_id"]
        for row in conn.execute(
            "SELECT university_id FROM naviance_colleges "
            "WHERE match_method IN ('exact', 'manual') AND university_id IS NOT NULL"
        ).fetchall()
    }

    outcome = match_batch(candidates, universe, claimed_university_ids=claimed)

    for assignment in outcome.matched:
        result = assignment.result
        method = "fuzzy" if result.method == "fuzzy" else "exact"
        conn.execute(
            "UPDATE naviance_colleges SET university_id=?, match_method=?, match_score=? "
            "WHERE college_id=?",
            (result.university_id, method, result.score, assignment.key),
        )

    # Two sweeps are needed, because a false positive can arrive from either
    # direction. match_batch already skipped fuzzy results in *this* run that
    # landed on a claimed university; this clears fuzzy links persisted by an
    # *earlier* run whose university has since gained an exact or manual match.
    # Dropping this second sweep would leave a stale link — and its NAVIANCE
    # identifier — in place on an incremental replay.
    swept = conn.execute(
        """
        UPDATE naviance_colleges
        SET university_id=NULL, match_method=NULL, match_score=NULL
        WHERE match_method='fuzzy'
          AND university_id IN (
              SELECT university_id FROM naviance_colleges
              WHERE match_method IN ('exact', 'manual')
          )
        """
    ).rowcount
    dropped = len(outcome.dropped_collisions) + swept
    if dropped:
        print(
            f"  deduped: removed {dropped} false-positive fuzzy links "
            f"({len(outcome.dropped_collisions)} this run, {swept} previously persisted) "
            "- canonical already had exact/manual match",
            file=sys.stderr,
        )

    # Insert NAVIANCE UUIDs into university_identifiers for all colleges that
    # are still linked. Runs after both sweeps so a cleared link cannot leave
    # its identifier behind.
    matched = conn.execute(
        "SELECT university_id, naviance_uuid FROM naviance_colleges WHERE university_id IS NOT NULL"
    ).fetchall()
    for university_id, naviance_uuid in matched:
        conn.execute(
            "INSERT OR IGNORE INTO university_identifiers "
            "(university_id, system_code, external_id, match_status) "
            "VALUES (?, 'NAVIANCE', ?, 'matched')",
            (university_id, naviance_uuid),
        )

    if outcome.review:
        print(
            f"\nFuzzy review candidates ({int(FUZZY_REVIEW_THRESHOLD * 100)}-"
            f"{int(FUZZY_AUTO_THRESHOLD * 100) - 1}% match - not auto-linked):",
            file=sys.stderr,
        )
        for assignment in sorted(outcome.review, key=lambda a: -a.result.score):
            print(
                f"  {assignment.result.score * 100:5.1f}%  {assignment.candidate.name!r}"
                f"  →  {assignment.result.matched_name!r}",
                file=sys.stderr,
            )
        print(file=sys.stderr)

    counts = outcome.counts()
    counts["dropped_collisions"] = dropped
    return counts


# ── scrape loading ────────────────────────────────────────────────────────────


def ensure_high_school(conn: sqlite3.Connection, name: str | None, slug: str | None) -> int:
    if not name or not slug or not name.strip() or not slug.strip():
        raise ValueError("Supply --high-school-name and --high-school-slug for scrapes")
    name, slug = name.strip(), slug.strip()
    row = conn.execute(
        "SELECT high_school_id FROM high_school_contexts WHERE naviance_slug = ?",
        (slug,),
    ).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO high_school_contexts (name, naviance_slug) VALUES (?, ?)",
        (name, slug),
    )
    high_school_id = cur.lastrowid
    if high_school_id is None:
        raise RuntimeError("Failed to insert high_school_contexts row: lastrowid is None")
    return high_school_id


def find_college_id(conn: sqlite3.Connection, page_title: str) -> int | None:
    # exact
    row = conn.execute(
        "SELECT college_id FROM naviance_colleges WHERE lower(college_name) = lower(?)",
        (page_title,),
    ).fetchone()
    if row:
        return row[0]
    # fuzzy fallback (≥85%)
    colleges = conn.execute("SELECT college_id, college_name FROM naviance_colleges").fetchall()
    norm_title = normalize(page_title)
    best_score, best_id = 0.0, None
    for cid, cname in colleges:
        score = similarity(norm_title, normalize(cname))
        if score > best_score:
            best_score, best_id = score, cid
    return best_id if best_score >= FUZZY_AUTO_THRESHOLD else None


def load_scrape(
    conn: sqlite3.Connection,
    json_path: Path,
    high_school_id: int,
    college_id_override: int | None = None,
) -> int | None:
    data = json.loads(json_path.read_text())
    page_title = data.get("pageTitle", "").strip()
    if not page_title:
        return None

    college_id = college_id_override or find_college_id(conn, page_title)
    if college_id is None:
        print(
            f"  WARN: no manifest entry for {page_title!r} in {json_path.name}, skipping",
            file=sys.stderr,
        )
        return None

    # page_snapshot
    cur = conn.execute(
        "INSERT INTO page_snapshots (college_id, high_school_id, fetched_at, page_title, raw_json_path) VALUES (?, ?, ?, ?, ?)",
        (
            college_id,
            high_school_id,
            datetime.now(UTC).isoformat(),
            page_title,
            str(json_path.relative_to(ROOT)),
        ),
    )
    snapshot_id = cur.lastrowid
    if snapshot_id is None:
        raise RuntimeError("Failed to insert page_snapshots row: lastrowid is None")

    # quick_facts — also capture CEEB / ACT codes
    ceeb: str | None = None
    act_code: str | None = None
    qf_rows = []
    for fact in data.get("quickFacts", []):
        key = fact.get("key", "").strip()
        val = str(fact.get("value", "")).strip()
        num, unit = extract_num_unit(val)
        qf_rows.append((snapshot_id, key, val, num, unit))
        if key == "CEEB code":
            ceeb = val
        elif key == "ACT code":
            act_code = val
    if qf_rows:
        conn.executemany("INSERT OR REPLACE INTO quick_facts VALUES (?, ?, ?, ?, ?)", qf_rows)

    # admissions_summary
    ah = {item["key"]: item.get("value", "") for item in data.get("admissionsHighlights", [])}
    ip = {item["key"]: item.get("value", "") for item in data.get("importantPolicies", [])}
    next_deadline = ah.get("Next deadline") or None
    if next_deadline and "no data" in next_deadline.lower():
        next_deadline = None
    fee, fee_note = parse_fee(ah.get("Application fees", ""))
    conn.execute(
        "INSERT OR REPLACE INTO admissions_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            parse_pct(ah.get("Acceptance rate", "")),
            fee,
            fee_note,
            next_deadline,
            yes_no(ip.get("Test optional")),
            yes_no(ip.get("Accepts Common App")),
            yes_no(ip.get("Accepts Coalition App")),
            yes_no(ip.get("Early decision")),
            yes_no(ip.get("Wait List")),
        ),
    )

    # admissions_policies — all importantPolicies key/value pairs
    if ip:
        conn.executemany(
            "INSERT OR REPLACE INTO admissions_policies VALUES (?, ?, ?)",
            [(snapshot_id, k, v) for k, v in ip.items()],
        )

    # application_factors — each bucket has a list of factor names
    af_rows = []
    sort_order = 0
    for section in data.get("applicationFactors", []):
        bucket = section.get("importance", "").strip()
        for factor_name in section.get("factors", []):
            if bucket and factor_name:
                af_rows.append((snapshot_id, bucket, factor_name.strip(), sort_order))
                sort_order += 1
    if af_rows:
        conn.executemany("INSERT OR IGNORE INTO application_factors VALUES (?, ?, ?, ?)", af_rows)

    # high_school_application_history
    hs_rows = []
    for row in data.get("highSchoolHistory", []):
        year = parse_int(row.get("Year"))
        if year is None:
            continue
        hs_rows.append(
            (
                snapshot_id,
                year,
                parse_int(row.get("Applied")),
                parse_int(row.get("Accepted")),
                parse_int(row.get("Enrolled")),
            )
        )
    if hs_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO high_school_application_history VALUES (?, ?, ?, ?, ?)", hs_rows
        )

    # scattergram sessions + points
    total_points = 0
    for view in data.get("scatter", {}).get("views", []):
        score_val = view.get("scoreOptionValue")
        x_kind = view.get("xAxisKind", "")
        if not score_val or not x_kind:
            continue

        points = view.get("points", [])
        conn.execute(
            """
            INSERT OR IGNORE INTO scattergram_sessions
                (snapshot_id, score_option_value, score_option_text,
                 x_axis_kind, y_axis_kind, x_axis_label, y_axis_label, point_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                score_val,
                view.get("scoreOptionText"),
                x_kind,
                view.get("yAxisKind", ""),
                view.get("xAxisLabel"),
                view.get("yAxisLabel"),
                len(points),
            ),
        )
        session_id = conn.execute(
            "SELECT session_id FROM scattergram_sessions WHERE snapshot_id=? AND score_option_value=?",
            (snapshot_id, score_val),
        ).fetchone()[0]

        pt_rows = [
            (
                session_id,
                idx,
                pt.get("estimatedXValue"),
                pt.get("estimatedYValue"),
                pt.get("centerX"),
                pt.get("centerY"),
                pt.get("outcomeGroup"),
                pt.get("decisionType"),
                pt.get("signature"),
                int(bool(pt.get("isUserPoint"))),
                int(bool(pt.get("isReferenceLine"))),
            )
            for idx, pt in enumerate(points)
        ]
        if pt_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO scattergram_points VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                pt_rows,
            )
        total_points += len(pt_rows)

    # CEEB / ACT_CODE → university_identifiers (only if college is linked to canonical university)
    uid_row = conn.execute(
        "SELECT university_id FROM naviance_colleges WHERE college_id=?", (college_id,)
    ).fetchone()
    if uid_row and uid_row[0]:
        uid = uid_row[0]
        for sys_code, ext_id in [("CEEB", ceeb), ("ACT_CODE", act_code)]:
            if ext_id:
                conn.execute(
                    "INSERT OR IGNORE INTO university_identifiers (university_id, system_code, external_id, match_status) VALUES (?, ?, ?, 'scraped')",
                    (uid, sys_code, ext_id),
                )

    return total_points


# ── CLI ───────────────────────────────────────────────────────────────────────


class LoadNavianceLoader(ManualDataLoader):
    source_code = "LOAD_NAVIANCE"
    description = "Load Naviance manifest and scrape data into SQLite."
    requires_config = False
    execute_help = "write Naviance manifest and scrape data to SQLite (default is dry-run)"

    def summary_extras(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "fuzzy_match": manifest.get("fuzzy_match", {}),
            "scrape_progress": manifest.get("scrape_progress", []),
        }

    def add_arguments(self, parser) -> None:
        parser.add_argument("--naviance-dir", type=Path, default=NAVIANCE_DIR)
        parser.add_argument("--high-school-name", help="School owning the scraped data")
        parser.add_argument("--high-school-slug", help="Stable slug for that school")
        parser.add_argument(
            "--manifest-only",
            action="store_true",
            help="Load manifest + run fuzzy match only; skip per-college scrapes",
        )
        parser.add_argument(
            "--college",
            metavar="SLUG",
            help="Load only the scrape matching this filename stem (e.g. cornell_university)",
        )

    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conn = self._require_conn(conn)
        conn.execute("BEGIN IMMEDIATE")

        setup_schema(conn)

        manifest_path = args.naviance_dir / "scattergram_manifest.json"
        print(f"[naviance] load manifest {manifest_path}", file=sys.stderr)
        manifest_count = load_manifest(conn, manifest_path)

        manual_count = run_manual_overrides(conn)

        stats = run_fuzzy_match(conn)
        print(
            "[naviance] fuzzy "
            f"exact={stats['exact']} auto={stats['fuzzy_auto']} "
            f"review={stats['fuzzy_review']} unmatched={stats['unmatched']}",
            file=sys.stderr,
        )
        total_linked = conn.execute(
            "SELECT count(*) FROM naviance_colleges WHERE university_id IS NOT NULL"
        ).fetchone()[0]
        distinct_linked = conn.execute(
            "SELECT count(DISTINCT university_id) FROM naviance_colleges WHERE university_id IS NOT NULL"
        ).fetchone()[0]

        if args.manifest_only:
            snapshots_loaded = 0
            total_points = 0
            scrape_progress: list[dict[str, Any]] = []
        else:
            high_school_id = ensure_high_school(conn, args.high_school_name, args.high_school_slug)

            scrape_files = sorted(args.naviance_dir.glob("*.structured.json"))
            if args.college:
                scrape_files = [f for f in scrape_files if args.college in f.stem]

            snapshots_loaded = 0
            total_points = 0
            scrape_progress = []
            for path in scrape_files:
                pts = load_scrape(conn, path, high_school_id)
                loaded = pts is not None
                if pts is not None:
                    snapshots_loaded += 1
                    total_points += pts
                scrape_progress.append(
                    {
                        "file": path.name,
                        "loaded": loaded,
                        "points": pts or 0,
                    }
                )
                print(
                    f"[naviance] scrape {path.name} "
                    f"{'loaded' if loaded else 'skipped'} points={pts or 0}",
                    file=sys.stderr,
                )

        if args.execute:
            conn.commit()
        else:
            conn.rollback()

        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "naviance_dir": str(args.naviance_dir),
            "manifest_count": manifest_count,
            "manual_overrides": manual_count,
            "fuzzy_match": stats,
            "total_linked": total_linked,
            "distinct_linked": distinct_linked,
            "snapshots_loaded": snapshots_loaded,
            "total_scatter_points": total_points,
            "scrape_progress": scrape_progress,
        }


if __name__ == "__main__":
    raise SystemExit(LoadNavianceLoader().run())
