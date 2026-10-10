from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from shared.db import connect


def test_connect_returns_sqlite_connection_with_row_factory(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    with closing(connect(db_path)) as conn:
        assert isinstance(conn, sqlite3.Connection)
        assert conn.row_factory is sqlite3.Row

        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (id, name) VALUES (1, 'first')")
        conn.commit()

        row = conn.execute("SELECT id, name FROM items").fetchone()
        assert row["id"] == 1
        assert row["name"] == "first"


def test_connect_enables_foreign_key_enforcement(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    with closing(connect(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        conn.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE children ("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES parents(id))"
        )
        conn.execute("INSERT INTO parents (id) VALUES (1)")
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO children (id, parent_id) VALUES (1, 999)")
