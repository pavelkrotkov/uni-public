#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Generate findings/naviance_explorer.html — standalone Naviance data explorer."""

import sqlite3
import sys
from pathlib import Path

from report_renderer import render_report

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from shared.naviance import load_colleges  # noqa: E402
DB_PATH = ROOT / "univ.sqlite3"
OUTPUT_PATH = Path(__file__).resolve().parent / "naviance_explorer.html"
TEMPLATE_PATH = Path(__file__).resolve().parent / "naviance_explorer.template.html"


# ── data extraction ───────────────────────────────────────────────────────────

def load_data(db_path: Path) -> list[dict]:
    """Every scraped college, in the JSON shape the template expects.

    The schema, the queries and the outcome vocabulary all live in
    shared.naviance, so this script and findings/admission_model_utils.py read
    the same model instead of each restating it in SQL.
    """
    conn = sqlite3.connect(db_path)
    try:
        return [college.to_dict() for college in load_colleges(conn)]
    finally:
        conn.close()


# ── main ──────────────────────────────────────────────────────────────────────

def build_view_model(colleges: list[dict]) -> dict[str, object]:
    return {"DATA_PLACEHOLDER": colleges}


def main():
    print("Loading data from SQLite…")
    colleges = load_data(DB_PATH)
    print(f"  {len(colleges)} scraped colleges")

    html = render_report(TEMPLATE_PATH, build_view_model(colleges))

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Written: {OUTPUT_PATH}  ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
