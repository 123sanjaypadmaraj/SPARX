"""Undo log. A tool that changes something reversible records how to reverse it.

    from sparx import undo

    @undo.handler("file_move")
    def _undo_move(data):               # data is what was passed to record()
        shutil.move(data["to"], data["from"])

    undo.record(ctx, "file_move", f"Moved {src} to {dst}", {"from": str(src), "to": str(dst)})
"""
import json

from . import db

db.schema("""
CREATE TABLE IF NOT EXISTS undo_log (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, session TEXT, kind TEXT NOT NULL,
    description TEXT NOT NULL, data TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0
);
""")

HANDLERS: dict = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def record(ctx, kind: str, description: str, data: dict) -> int:
    return db.x("INSERT INTO undo_log (ts, session, kind, description, data) VALUES (?,?,?,?,?)",
                (db.now(), getattr(ctx, "session", ""), kind, description, json.dumps(data, default=str)))


def recent(limit: int = 20) -> list[dict]:
    return db.q("SELECT id, ts, kind, description, undone FROM undo_log ORDER BY id DESC LIMIT ?", (limit,))


def run(undo_id: int | None = None) -> str:
    """Reverse one logged action (the most recent not-yet-undone one by default)."""
    row = (db.one("SELECT * FROM undo_log WHERE id = ?", (undo_id,)) if undo_id
           else db.one("SELECT * FROM undo_log WHERE undone = 0 ORDER BY id DESC LIMIT 1"))
    if not row:
        return "Nothing to undo."
    if row["undone"]:
        return f"Already undone: {row['description']}"
    fn = HANDLERS.get(row["kind"])
    if not fn:
        return f"Cannot undo '{row['kind']}' automatically: {row['description']}"
    fn(json.loads(row["data"]))
    db.x("UPDATE undo_log SET undone = 1 WHERE id = ?", (row["id"],))
    return f"Undone: {row['description']}"
