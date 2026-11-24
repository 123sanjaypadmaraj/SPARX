from datetime import datetime, timedelta

import json

from conftest import run_tool
from sparx import automation, proactive  # noqa: F401  (their jobs register now, not into a test's empty table)
from sparx import assistant  # noqa: F401  (creates the messages table this file seeds)
from sparx import config, daemon, db, jobs, tools
from sparx.tools import maintenance

tools.load_all()


def ago(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def seed():
    for d in (200, 10):
        db.x("INSERT INTO audit (ts, tool, risk, ok) VALUES (?,?,?,?)", (ago(d), "t", "read", 1))
    for d, read in ((90, 1), (90, 0), (5, 1)):
        db.x("INSERT INTO notifications (ts, kind, title, body, read) VALUES (?,?,?,?,?)", (ago(d), "info", f"n{d}{read}", "b", read))
    db.x("INSERT INTO messages (conversation, role, content, ts) VALUES ('c','user','hi',?)", (ago(900),))
    db.x("INSERT INTO undo_log (ts, kind, description, data) VALUES (?,?,?,?)", (ago(900), "k", "d", "{}"))
    trash = config.path("trash", "old.txt")
    trash.write_text("keep", encoding="utf-8")


def titles():
    return {r["title"] for r in db.q("SELECT title FROM notifications")}


async def test_prune_removes_only_old_audit_and_read_notifications(ctx):
    seed()
    out = json.loads(await run_tool("storage_prune", ctx))
    assert out["audit_rows"] == 1 and out["notifications"] == 1 and out["undo"] == 1   # the seeded undo row is 900 days old
    assert db.one("SELECT COUNT(*) n FROM audit WHERE ts < ?", (ago(180),))["n"] == 0   # the 10-day row and this call's own row remain
    assert titles() == {"n900", "n51"}                      # unread old one and the recent read one survive
    assert db.one("SELECT COUNT(*) n FROM messages")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM undo_log")["n"] == 0      # older than retention.undo_days (30)
    assert (config.HOME / "trash" / "old.txt").read_text(encoding="utf-8") == "keep"


def test_oversized_log_is_cut_to_newest_half_on_a_line_boundary():
    config.set_key("retention.log_mb", 0.001)               # about 1 KB
    big = config.path("logs", "errors.log")
    lines = [f"line {i:04d} " + "x" * 30 for i in range(100)]
    big.write_text("\n".join(lines) + "\n", encoding="utf-8")
    small = config.path("logs", "daemon.log")
    small.write_text("tiny\n", encoding="utf-8")
    out = maintenance.prune()
    kept = big.read_text(encoding="utf-8").splitlines()
    assert "errors.log" in out["logs_trimmed"] and "daemon.log" not in out["logs_trimmed"]
    assert kept == lines[-len(kept):] and 40 <= len(kept) <= 55    # whole lines, newest ones
    assert small.read_text(encoding="utf-8") == "tiny\n"


def test_zero_disables_each_rule():
    seed()
    config.set_key("retention.audit_days", 0)
    config.set_key("retention.notifications_days", 0)
    config.set_key("retention.log_mb", 0)
    config.set_key("retention.undo_days", 0)
    log = config.path("logs", "errors.log")
    log.write_text("x\n" * 10000, encoding="utf-8")
    out = maintenance.prune()
    assert out == {"audit_rows": 0, "notifications": 0, "undo": 0, "logs_trimmed": {}}
    assert db.one("SELECT COUNT(*) n FROM audit")["n"] == 2 and len(titles()) == 3
    assert db.one("SELECT COUNT(*) n FROM undo_log")["n"] == 1
    assert len(log.read_text(encoding="utf-8")) == 20000


async def test_storage_status_reports_sizes_and_counts(ctx):
    seed()
    config.path("logs", "daemon.log").write_text("12345", encoding="utf-8")
    out = json.loads(await run_tool("storage_status", ctx))
    assert out["rows"] == {"audit": 2, "notifications": 3, "messages": 1, "undo_log": 1}
    assert out["logs_bytes"] == 5 and out["trash_bytes"] == 4 and out["database_bytes"] > 0


async def test_housekeeping_job_runs_through_daemon_tick(monkeypatch):
    job = jobs.JOBS["housekeeping"]
    monkeypatch.setattr(jobs, "JOBS", {"housekeeping": job})
    monkeypatch.setattr(daemon, "toast", lambda title, body: None)
    seed()
    out = await daemon.tick(now=100000)
    assert out["ran"] == ["housekeeping"] and not out["errors"]
    assert db.one("SELECT COUNT(*) n FROM audit WHERE ts < ?", (ago(180),))["n"] == 0
    assert "n901" not in titles() and "n900" in titles()


async def test_job_status_lists_every_job_with_its_last_result(ctx):
    jobs.record("housekeeping", "OSError: disk full")
    out = json.loads(await run_tool("job_status", ctx))
    assert out["run_by"].startswith("nobody") and [j["name"] for j in out["jobs"]] == sorted(jobs.JOBS)
    by = {j["name"]: j for j in out["jobs"]}
    assert by["housekeeping"]["every_seconds"] == 86400 and by["housekeeping"]["enabled"] is True
    assert (by["housekeeping"]["last_ok"], by["housekeeping"]["last_error"], by["housekeeping"]["failures"]) == \
        (False, "OSError: disk full", 1)
    never = next(j for j in out["jobs"] if j["name"] != "housekeeping")
    assert (never["last_run"], never["last_ok"], never["last_error"], never["failures"]) == (None, None, None, None)


# ---- backups

import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402


def _stamps(n):
    return [datetime(2026, 10, 1, 12, 0, i) for i in range(n)]


async def test_backup_contains_a_row_seeded_just_before_it_and_the_settings(ctx):
    seed()
    config.save_settings({"user": "Asha"})
    out = maintenance.backup()
    copy = sqlite3.connect(out["path"])
    try:
        assert copy.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 2
    finally:
        copy.close()
    stamp = out["path"].split("sparx-")[-1][:-3]
    assert json.loads((config.HOME / "backups" / f"settings-{stamp}.json").read_text(encoding="utf-8"))["user"] == "Asha"
    assert not list((config.HOME / "backups").glob("*.tmp"))


def test_only_the_newest_seven_backups_are_kept():
    seed()
    for t in _stamps(9):
        last = maintenance.backup(now=t)
    assert last["old_backups_removed"] == 1
    kept = sorted(p.name for p in (config.HOME / "backups").glob("sparx-*.db"))
    assert len(kept) == 7 and kept[0] == "sparx-20261001-120002.db"
    assert len(list((config.HOME / "backups").glob("settings-*.json"))) <= 7


def test_a_copy_that_fails_its_integrity_check_is_discarded(monkeypatch):
    import pytest
    seed()
    monkeypatch.setattr(maintenance, "_integrity", lambda path: "row 3 missing from index")
    with pytest.raises(tools.ToolError, match="integrity"):
        maintenance.backup()
    assert not list((config.HOME / "backups").glob("*"))


async def test_the_backup_job_runs_through_tick_and_does_nothing_at_zero(monkeypatch):
    job = jobs.JOBS["backup"]
    monkeypatch.setattr(jobs, "JOBS", {"backup": job})
    monkeypatch.setattr(daemon, "toast", lambda title, body: None)
    seed()
    config.set_key("retention.backups", 0)
    out = await daemon.tick(now=100000)
    assert out["ran"] == ["backup"] and not out["errors"] and not (config.HOME / "backups").exists()
    config.set_key("retention.backups", 7)
    out = await daemon.tick(now=300000)
    assert out["ran"] == ["backup"] and len(list((config.HOME / "backups").glob("sparx-*.db"))) == 1


async def test_the_tool_still_works_at_zero_and_prunes_nothing(ctx):
    seed()
    config.set_key("retention.backups", 0)
    for t in _stamps(3):
        out = maintenance.backup(now=t)
    assert out["old_backups_removed"] == 0 and len(list((config.HOME / "backups").glob("sparx-*.db"))) == 3
    assert "Error" not in await run_tool("storage_backup", ctx)


async def test_storage_status_reports_the_backups(ctx):
    seed()
    maintenance.backup(now=_stamps(1)[0])
    got = json.loads(await run_tool("storage_status", ctx))
    assert got["backups"]["count"] == 1 and got["backups"]["newest"] == "20261001-120000" and got["backups"]["bytes"] > 0


def test_backups_are_control_files_and_sparx_main_prints_the_path(capsys, monkeypatch):
    from sparx import cli, security
    seed()
    out = maintenance.backup(now=_stamps(1)[0])
    assert security.control_path(out["path"])
    assert security.control_path(config.HOME / "backups" / "settings-20261001-120000.json")
    assert security.control_path(config.HOME / "other" / "sparx-x.db") is None
    monkeypatch.setattr("sys.argv", ["sparx", "backup"])
    cli.main()
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    assert printed.endswith(".db") and config.HOME in Path(printed).parents


def test_old_undo_entries_are_pruned_and_zero_disables(ctx):
    from datetime import datetime, timedelta
    from sparx import undo  # noqa: F401  (creates the undo_log table)
    old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
    new = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    for ts in (old, new):
        db.x("INSERT INTO undo_log (ts, session, kind, description, data) VALUES (?,?,?,?,?)", (ts, "s", "k", "d", "{}"))
    config.set_key("retention.undo_days", 0)
    assert maintenance.prune()["undo"] == 0 and db.one("SELECT COUNT(*) AS n FROM undo_log")["n"] == 2
    config.set_key("retention.undo_days", 30)
    assert maintenance.prune()["undo"] == 1
    assert [r["ts"] for r in db.q("SELECT ts FROM undo_log")] == [new]
