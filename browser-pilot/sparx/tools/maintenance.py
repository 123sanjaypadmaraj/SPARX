"""Storage housekeeping: report sizes, prune old audit rows, read notifications and oversized logs.

Prunes undo_log entries older than retention.undo_days. Never touches messages, the trash folder or unread notifications.
"""
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta

from .. import config, db, joblock, jobs
from . import ToolError, tool


def _num(v, default: float) -> float:
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return default


def _folder_bytes(p) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0


def _cutoff(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def _trim_log(f, limit: int) -> int:
    """Keep the newest half of an oversized log, cut on a line boundary. Returns bytes removed."""
    data = f.read_bytes()
    if len(data) <= limit:
        return 0
    cut = len(data) - len(data) // 2
    nl = data.find(b"\n", cut - 1)
    keep = data[nl + 1:] if nl != -1 else b""
    f.write_bytes(keep)
    return len(data) - len(keep)


def prune() -> dict:
    r = config.settings().get("retention") or {}
    out = {"audit_rows": 0, "notifications": 0, "undo": 0, "logs_trimmed": {}}
    days = _num(r.get("audit_days", 180), 180)
    if days:
        out["audit_rows"] = db.x("DELETE FROM audit WHERE ts < ?", (_cutoff(days),))
    days = _num(r.get("notifications_days", 60), 60)
    if days:
        out["notifications"] = db.x("DELETE FROM notifications WHERE read = 1 AND ts < ?", (_cutoff(days),))
    days = _num(r.get("undo_days", 30), 30)
    if days:
        out["undo"] = db.x("DELETE FROM undo_log WHERE ts < ?", (_cutoff(days),))
    mb = _num(r.get("log_mb", 5), 5)
    logs = config.HOME / "logs"
    if mb:
        for f in sorted(logs.glob("*.log")):
            try:
                n = _trim_log(f, int(mb * 1024 * 1024))
            except OSError:
                continue
            if n:
                out["logs_trimmed"][f.name] = n
    return out


@tool("storage_status", "Show how much space Sparx's database, logs and trash use, and how many rows the audit, notifications, messages and undo tables hold.",
      category="core")
def storage_status(ctx):
    from .. import assistant  # noqa: F401  (creates the messages table; the daemon may not have imported it yet)
    counts ={t: db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("audit", "notifications", "messages", "undo_log")}
    db_file = config.path("sparx.db")
    return {"database_bytes": db_file.stat().st_size if db_file.exists() else 0, "rows": counts,
            "logs_bytes": _folder_bytes(config.HOME / "logs"), "trash_bytes": _folder_bytes(config.HOME / "trash"),
            "retention": config.settings().get("retention"), "backups": backup_status()}


@tool("storage_prune", "Delete old audit rows, old read notifications and trim oversized logs, per the retention settings. Also deletes undo entries older than retention.undo_days. Never touches messages, trash or unread notifications.",
      category="core", risk="delete")
def storage_prune(ctx):
    return prune()


def _backups():
    """The (stamp, database file, settings copy or None) of every backup, oldest first."""
    folder = config.HOME / "backups"
    out = []
    for f in sorted(folder.glob("sparx-*.db")) if folder.is_dir() else []:
        stamp = f.stem[len("sparx-"):]
        side = folder / f"settings-{stamp}.json"
        out.append((stamp, f, side if side.exists() else None))
    return out


def backup_status() -> dict:
    rows = _backups()
    size = sum(p.stat().st_size for _, d, s in rows for p in (d, s) if p)
    return {"count": len(rows), "newest": rows[-1][0] if rows else None, "bytes": size}


def _integrity(path) -> str:
    c = sqlite3.connect(path)
    try:
        return str(c.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        c.close()


def backup(now: datetime | None = None, keep: int | None = None) -> dict:
    """Copy sparx.db (SQLite's online backup, safe while Sparx runs) and settings.json into HOME/backups, check the
    copy, and keep only the newest `keep` (default retention.backups). Raises ToolError if the copy is not sound."""
    src_file = config.path("sparx.db")
    if not src_file.exists():
        raise ToolError("There is no database to back up yet.")
    folder = config.HOME / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    final, tmp = folder / f"sparx-{stamp}.db", folder / f"sparx-{stamp}.db.tmp"
    src, dst = sqlite3.connect(src_file, timeout=30), sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    verdict = _integrity(tmp)
    if verdict != "ok":
        tmp.unlink(missing_ok=True)
        raise ToolError(f"The backup copy failed its integrity check ({verdict[:200]}), so it was discarded.")
    os.replace(tmp, final)
    settings = config.path("settings.json")
    if settings.exists():
        shutil.copy2(settings, folder / f"settings-{stamp}.json")
    if keep is None:
        keep = int(_num((config.settings().get("retention") or {}).get("backups", 7), 7))
    removed = 0
    if keep:
        for _, d, s in _backups()[:-keep]:
            for f in (d, s):
                if f:
                    f.unlink(missing_ok=True)
            removed += 1
    return {"path": str(final), "bytes": final.stat().st_size, "old_backups_removed": removed}


@tool("storage_backup", "Make a checked copy of Sparx's database and settings in the backups folder now, and delete the oldest "
      "copies beyond retention.backups. There is no restore tool: the guide explains how to restore by hand.",
      category="core", risk="write")
def storage_backup(ctx):
    return backup()


@jobs.every(86400, "backup")
def daily_backup(ctx):
    if _num((config.settings().get("retention") or {}).get("backups", 7), 7):
        backup()


@jobs.every(86400, "housekeeping")
def housekeeping(ctx):
    prune()


@tool("job_status", "Show every background job: how often it runs, whether it is enabled, when it last ran, whether that "
      "run worked, its last error and how many times in a row it has failed, plus which process is running the jobs.",
      category="core", risk="read")
def job_status(ctx):
    seen = jobs.state()
    rows = []
    for j in sorted(jobs.JOBS.values(), key=lambda j: j.name):
        s = seen.get(j.name) or {}
        rows.append({"name": j.name, "every_seconds": j.seconds, "enabled": jobs.enabled(j),
                     "last_run": s.get("last_run"), "last_ok": bool(s["last_ok"]) if s else None,
                     "last_error": (s.get("last_error") or None) if s else None, "failures": s.get("failures")})
    return json.dumps({"run_by": joblock.describe(), "jobs": rows}, indent=1)
