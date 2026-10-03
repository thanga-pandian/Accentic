"""
Database connection helper.

DATABASE_URL examples:
  sqlite:///data/accentic.db                  — local SQLite (default, no server needed)
  postgresql://assent:assent@localhost:5433/assent_compliance  — Postgres (prod)
"""

import os
import sqlite3
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///data/accentic.db")


def get_connection():
    if DATABASE_URL.startswith("sqlite:///"):
        return _sqlite_connection()
    import psycopg
    return psycopg.connect(DATABASE_URL)


# ---------------------------------------------------------------------------
# SQLite compatibility shim — makes sqlite3 look like psycopg3 for our queries
# ---------------------------------------------------------------------------

class _Cursor:
    """Wraps sqlite3.Cursor, converts %s → ? and emulates .description."""

    def __init__(self, raw_cursor):
        self._cur = raw_cursor

    def execute(self, sql, params=()):
        sql = _to_sqlite(sql)
        self._cur.execute(sql, params)
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return tuple(row)

    def fetchall(self):
        return [tuple(r) for r in self._cur.fetchall()]

    @property
    def description(self):
        return self._cur.description

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._cur.close()

    def close(self):
        self._cur.close()


class _Connection:
    """Wraps sqlite3.Connection to match the psycopg3 interface."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def cursor(self):
        return _Cursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


def _to_sqlite(sql: str) -> str:
    """Convert psycopg %s placeholders to sqlite3 ? placeholders."""
    return re.sub(r"%s", "?", sql)


def _sqlite_connection() -> _Connection:
    db_path = DATABASE_URL[len("sqlite:///"):]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return _Connection(conn)
