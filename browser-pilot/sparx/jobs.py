"""Background jobs and notifications.

Any module can register a periodic job; `sparx daemon` runs them.

    from sparx import jobs

    @jobs.every(60, "reminders")                 # seconds between runs
    async def due_reminders(ctx):
        for r in ...:
            jobs.notify("Reminder", r["text"], kind="reminder")

A job is skipped when settings["proactive"][<flag>] is false (flag defaults to the job name).
"""
import inspect
from dataclasses import dataclass
from typing import Callable

from . import config, db

db.schema("""
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL, why TEXT NOT NULL DEFAULT '', read INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_state (
    name TEXT PRIMARY KEY, last_run TEXT NOT NULL, last_ok INTEGER NOT NULL, last_error TEXT NOT NULL DEFAULT '',
    failures INTEGER NOT NULL DEFAULT 0, runs INTEGER NOT NULL DEFAULT 0
);
""")

FAIL_ALERT = 3     # consecutive failures before the user is told


@dataclass
class Job:
    name: str
    seconds: float
    fn: Callable
    flag: str
    last: float = 0.0


JOBS: dict[str, Job] = {}
LISTENERS: list[Callable] = []   # sync (notification: dict) -> None; the daemon / web UI add theirs


def every(seconds: float, name: str, flag: str = ""):
    def deco(fn):
        JOBS[name] = Job(name, seconds, fn, flag or name)
        return fn
    return deco


def enabled(job: Job) -> bool:
    p = config.settings()["proactive"]
    return bool(p.get("enabled", True)) and bool(p.get(job.flag, True))


async def run(job: Job, ctx):
    r = job.fn(ctx)
    if inspect.isawaitable(r):
        await r


def notify(title: str, body: str, kind: str = "info", why: str = "") -> int:
    """Store a notification and hand it to every listener (desktop toast, voice, web UI).

    `why` explains the reason for a proactive suggestion; always fill it for those.
    """
    n = {"ts": db.now(), "kind": kind, "title": title, "body": body, "why": why}
    n["id"] = db.x("INSERT INTO notifications (ts, kind, title, body, why) VALUES (?,?,?,?,?)",
                   (n["ts"], kind, title, body, why))
    for fn in list(LISTENERS):
        try:
            fn(n)
        except Exception:
            pass
    try:
        from . import hooks
        hooks.notify_sync(n)
    except Exception:
        pass
    return n["id"]


def unread(limit: int = 50) -> list[dict]:
    return db.q("SELECT * FROM notifications WHERE read = 0 ORDER BY id DESC LIMIT ?", (limit,))


def mark_read(ids=None):
    if ids:
        db.many("UPDATE notifications SET read = 1 WHERE id = ?", [(i,) for i in ids])
    else:
        db.x("UPDATE notifications SET read = 1 WHERE read = 0")


def record(name: str, error: str | None = None):
    """Note that a job ran. After FAIL_ALERT failures in a row the user is told once; a success resets the count.
    Never raises: bookkeeping must not stop the jobs."""
    try:
        if error is None:
            db.x("INSERT INTO job_state (name, last_run, last_ok, runs) VALUES (?,?,1,1) ON CONFLICT(name) DO UPDATE "
                 "SET last_run = excluded.last_run, last_ok = 1, last_error = '', failures = 0, runs = runs + 1",
                 (name, db.now()))
            return
        error = str(error)[:1000]
        db.x("INSERT INTO job_state (name, last_run, last_ok, last_error, failures, runs) VALUES (?,?,0,?,1,1) "
             "ON CONFLICT(name) DO UPDATE SET last_run = excluded.last_run, last_ok = 0, "
             "last_error = excluded.last_error, failures = failures + 1, runs = runs + 1", (name, db.now(), error))
        row = db.one("SELECT failures FROM job_state WHERE name = ?", (name,))
        if row and row["failures"] == FAIL_ALERT:
            notify(f"Background job '{name}' keeps failing", error[:300], kind="system",
                   why=f"It failed {FAIL_ALERT} times in a row. Details are in logs/daemon.log; job_status shows every job.")
    except Exception:
        pass


def state() -> dict[str, dict]:
    return {r["name"]: r for r in db.q("SELECT * FROM job_state")}
