"""One SQLite file (HOME/sparx.db) shared by every module.

    from sparx import db
    db.schema("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    new_id = db.x("INSERT INTO tasks (title) VALUES (?)", ("Buy milk",))     # 0 when an INSERT changed no row
    rows = db.q("SELECT * FROM tasks WHERE id = ?", (new_id,))     # list of dicts

Call db.schema(...) at import time. Schemas are (re)applied to every new connection,
which is what lets tests swap HOME and call db.reset().
"""
import sqlite3
import threading
from datetime import datetime

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_schemas: list[str] = []


def schema(sql: str):
    """Register CREATE TABLE/INDEX IF NOT EXISTS statements (one or many, ';' separated)."""
    with _lock:
        if sql not in _schemas:
            _schemas.append(sql)
            if _conn is not None:
                _conn.executescript(sql)
                _conn.commit()


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            c = sqlite3.connect(config.path("sparx.db"), check_same_thread=False, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            for s in _schemas:
                c.executescript(s)
            c.commit()
            _conn = c
        return _conn


def reset():
    """Close the connection; the next call reopens at the current config.HOME."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def q(sql: str, params=()) -> list[dict]:
    with _lock:
        c = connect()
        was_open = c.in_transaction
        try:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        finally:
            if c.in_transaction and not was_open:   # a write sent through q: never keep the write lock for it
                c.rollback()


def one(sql: str, params=()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql: str, params=()) -> int:
    """Execute a write. Returns lastrowid for an INSERT that added a row (0 if it changed none), rowcount otherwise.

    A failed statement or commit is rolled back before re-raising: an open transaction would hold
    the write lock against every other Sparx process until this one happened to write again."""
    with _lock:
        c = connect()
        try:
            cur = c.execute(sql, params)
            c.commit()
        except BaseException:
            c.rollback()
            raise
        return cur.lastrowid if cur.rowcount and sql.lstrip()[:6].upper() == "INSERT" else cur.rowcount


def many(sql: str, rows) -> int:
    with _lock:
        c = connect()
        try:
            cur = c.executemany(sql, rows)
            c.commit()
        except BaseException:
            c.rollback()
            raise
        return cur.rowcount


def now() -> str:
    """Local time as ISO 'YYYY-MM-DDTHH:MM:SS', the format every table stores."""
    return datetime.now().isoformat(timespec="seconds")
