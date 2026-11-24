"""Actions a background job could not run because nobody was there to approve them.

`tools.call` records them here; the user approves or dismisses them later (pending_approve / pending_dismiss).
Queueing never loosens a permission: approving re-runs the stored call through `tools.call`, which asks as usual.
"""
import json
from datetime import datetime, timedelta

from . import config, db, security

db.schema("""
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, expires TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL DEFAULT '{}',
    agent TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', result TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS pending_actions_status ON pending_actions (status);
""")

RISKS = ("control", "send")                       # the only risks worth keeping for later
NEVER = ("home_security_set", "robot_command",    # never operated unattended, so never left waiting either
         # these act on a screen position or on whatever has the focus: run up to an hour later they would hit
         # whatever is on the screen then, not what the job saw
         "phone_tap", "phone_tap_confirm", "phone_swipe", "phone_type", "phone_key", "phone_task",
         "mouse_click", "key_type", "key_press")


def expire() -> int:
    """Mark overdue entries expired: a stale 'turn the fan on' must not run hours later."""
    return db.x("UPDATE pending_actions SET status = 'expired' WHERE status = 'pending' AND expires <= ?", (db.now(),))


def queue(tool, args: dict, agent: str = "") -> int | None:
    """Record a call that needed approval. Returns its id (the existing one when the same call is already
    pending), or None when it is not queued. Never raises: a call that cannot be stored is simply not queued."""
    try:
        minutes = float(config.settings()["limits"].get("pending_minutes", 60))
        if tool.risk not in RISKS or tool.name in NEVER or minutes <= 0 or security.redact(args) != args:
            return None                           # redact() changed something: credentials are never stored
        blob = json.dumps(args, ensure_ascii=False, sort_keys=True)
        expire()
        same = db.one("SELECT id FROM pending_actions WHERE status = 'pending' AND tool = ? AND args = ?", (tool.name, blob))
        if same:
            return same["id"]
        expires = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        return db.x("INSERT INTO pending_actions (ts, expires, tool, args, agent) VALUES (?,?,?,?,?)",
                    (db.now(), expires, tool.name, blob, agent))
    except Exception:
        return None


def hand_over(tool: str, args, result: str, after: int | None = None) -> int:
    """Dismiss the pending entry for this call because something else now asks the user for the same approval (an
    automation run that paused on it); `result` says where. `args` is the call's arguments, as a dict or JSON text.
    If the JSON text cannot be read (the audit log cuts long arguments) and `after` is given, every entry for the
    tool queued after that id goes instead. Returns how many entries were dismissed. Never raises."""
    try:
        try:
            args = json.loads(args) if isinstance(args, str) else args
            where, params = "args = ?", (json.dumps(args, ensure_ascii=False, sort_keys=True),)
        except ValueError:
            if after is None:
                return 0
            where, params = "id > ?", (after,)
        return db.x(f"UPDATE pending_actions SET status = 'dismissed', result = ? WHERE status = 'pending' AND tool = ? "
                    f"AND {where}", (security.redact(result)[:2000], tool, *params))
    except Exception:
        return 0


def _row(r: dict | None) -> dict | None:
    return r and {**r, "args": json.loads(r["args"])}


def rows(status: str = "pending", limit: int = 50) -> list[dict]:
    """Entries with this status ('' = every status), newest first."""
    expire()
    where, params = (" WHERE status = ?", (status,)) if status else ("", ())
    return [_row(r) for r in db.q(f"SELECT * FROM pending_actions{where} ORDER BY id DESC LIMIT ?", (*params, limit))]


def get(id: int) -> dict | None:
    expire()
    return _row(db.one("SELECT * FROM pending_actions WHERE id = ?", (id,)))


def close(id: int, status: str, result: str = "") -> None:
    """Set the status of an entry and store what happened (credential-looking text masked)."""
    db.x("UPDATE pending_actions SET status = ?, result = ? WHERE id = ?", (status, security.redact(result)[:2000], id))
