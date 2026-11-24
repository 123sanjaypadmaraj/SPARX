import asyncio
import json
import time

import pytest

from sparx import automation, proactive  # noqa: F401  (their jobs register now, not into a test's empty table)
from sparx import config, daemon, jobs, security


@pytest.fixture
def jobset(monkeypatch):
    """An empty job table for the test, and no real Windows toast."""
    table = {}
    monkeypatch.setattr(jobs, "JOBS", table)
    monkeypatch.setattr(daemon, "toast", lambda title, body: None)
    return table


def add(table, name, fn, seconds=60, flag=""):
    table[name] = jobs.Job(name, seconds, fn, flag or name)
    return table[name]


async def test_tick_runs_due_jobs_and_survives_one_that_raises(jobset):
    seen = []

    async def good(ctx):
        seen.append(ctx)

    def boom(ctx):
        raise RuntimeError("kaput")

    add(jobset, "boom", boom)
    add(jobset, "good", good)
    add(jobset, "sync", lambda ctx: seen.append(ctx))
    out = await daemon.tick(now=1000)
    assert out["ran"] == ["good", "sync"] and out["errors"] == {"boom": "RuntimeError: kaput"} and not out["stopped"]
    assert len(seen) == 2 and seen[0] is not seen[1]                     # a fresh Context per job
    assert all(c.agent == "daemon" and c.approve is None and c.data["automated"] for c in seen)
    log = config.path("logs", "daemon.log").read_text(encoding="utf-8")
    assert "boom failed" in log and "RuntimeError: kaput" in log


async def test_tick_only_runs_what_is_due_and_enabled(jobset):
    ran = []
    add(jobset, "fast", lambda ctx: ran.append("fast"), seconds=10)
    add(jobset, "slow", lambda ctx: ran.append("slow"), seconds=100)
    add(jobset, "off", lambda ctx: ran.append("off"), flag="deadline_risk")
    config.set_key("proactive.deadline_risk", False)
    assert (await daemon.tick(now=1000))["ran"] == ["fast", "slow"]
    assert (await daemon.tick(now=1005))["ran"] == []
    assert (await daemon.tick(now=1011))["ran"] == ["fast"]
    assert (await daemon.tick(now=1100))["ran"] == ["fast", "slow"]
    config.set_key("proactive.enabled", False)                           # the master switch
    assert (await daemon.tick(now=5000))["ran"] == []
    assert "off" not in ran


async def test_tick_does_nothing_while_emergency_stop_is_set(jobset):
    ran = []
    add(jobset, "j", lambda ctx: ran.append(1))
    security.request_stop("test")
    assert await daemon.tick(now=1000) == {"ran": [], "errors": {}, "stopped": True}
    security.clear_stop()
    assert (await daemon.tick(now=1000))["ran"] == ["j"] and ran == [1]  # the stop did not use up the job's turn


async def test_job_hitting_a_budget_stop_does_not_end_the_pass(jobset):
    def limited(ctx):
        raise security.Stopped("Tool-call limit for this run reached.")
    add(jobset, "limited", limited)
    add(jobset, "after", lambda ctx: None)
    out = await daemon.tick(now=1000)
    assert out["ran"] == ["after"] and out["stopped"]


def test_heartbeat_file_and_status(jobset):
    assert daemon.status() == {"alive": False}
    daemon.heartbeat({"last": {"ran": ["x"]}})
    f = config.HOME / "daemon.heartbeat"                                 # the exact path sparx/tools/sparx.py reads
    beat = json.loads(f.read_text(encoding="utf-8"))
    assert beat["last"] == {"ran": ["x"]} and abs(beat["time"] - time.time()) < 5 and time.time() - f.stat().st_mtime < 5
    assert daemon.status()["alive"] and not daemon.status(max_age=-1)["alive"]


def test_deliver_prints_the_reason_and_never_raises(jobset, monkeypatch, capsys):
    def broken(title, body):
        raise OSError("no powershell")
    monkeypatch.setattr(daemon, "toast", broken)
    jobs.LISTENERS.append(daemon.deliver)
    try:
        jobs.notify("Deadline at risk", "Report is due at 5", kind="suggestion", why="It no longer fits today")
    finally:
        jobs.LISTENERS.remove(daemon.deliver)
    out = capsys.readouterr().out
    assert "suggestion: Deadline at risk" in out and "why: It no longer fits today" in out
    assert "toast failed" in config.path("logs", "daemon.log").read_text(encoding="utf-8")


async def test_main_loops_ticks_and_touches_the_heartbeat(jobset, monkeypatch):
    class Quit(Exception):
        pass
    ran, beats, sleeps = [], [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        beats.append(daemon.status())
        assert daemon.deliver in jobs.LISTENERS
        if len(sleeps) == 2:
            security.request_stop("test")
        if len(sleeps) == 3:
            raise Quit

    monkeypatch.setattr(daemon.tools, "load_all", lambda: None)
    monkeypatch.setattr(daemon.asyncio, "sleep", fake_sleep)
    add(jobset, "j", lambda ctx: ran.append(1), seconds=0)
    with pytest.raises(Quit):
        await daemon.main(interval=5)
    assert ran == [1, 1]                                                 # the third pass was under the emergency stop
    assert all(b["alive"] for b in beats) and beats[0]["last"]["ran"] == ["j"] and beats[2]["last"]["stopped"]
    assert sleeps == [5, 5, 15]                                          # idles longer while stopped
    assert daemon.deliver not in jobs.LISTENERS and not (config.HOME / "daemon.heartbeat").exists()


def alerts():
    return [n for n in jobs.unread() if n["kind"] == "system"]


async def test_a_job_that_keeps_failing_is_reported_once(jobset):
    mood = {"fail": True}

    def flaky(ctx):
        if mood["fail"]:
            raise RuntimeError("no network")

    add(jobset, "flaky", flaky, seconds=0)
    for n in (1, 2):
        await daemon.tick(now=n)
    assert alerts() == [] and jobs.state()["flaky"]["failures"] == 2
    await daemon.tick(now=3)
    (note,) = alerts()
    assert note["title"] == "Background job 'flaky' keeps failing" and note["body"] == "RuntimeError: no network"
    assert "3 times in a row" in note["why"] and "job_status" in note["why"]
    await daemon.tick(now=4)
    assert len(alerts()) == 1 and jobs.state()["flaky"]["failures"] == 4            # one notice per streak

    mood["fail"] = False
    await daemon.tick(now=5)
    row = jobs.state()["flaky"]
    assert (row["failures"], row["last_ok"], row["last_error"], row["runs"]) == (0, 1, "", 5)
    mood["fail"] = True
    for n in (6, 7, 8):
        await daemon.tick(now=n)
    assert len(alerts()) == 2                                                       # a new streak, a new notice


async def test_a_timeout_and_a_stop_are_recorded_honestly(jobset, monkeypatch):
    async def slow(ctx):
        await asyncio.sleep(5)

    def halted(ctx):
        raise security.Stopped("budget used up")

    monkeypatch.setattr(daemon, "JOB_TIMEOUT", 0.01)
    add(jobset, "slow", slow, seconds=0)
    add(jobset, "halted", halted, seconds=0)
    await daemon.tick(now=1)
    state = jobs.state()
    assert state["slow"]["last_error"] == "timed out after 0.01s" and "halted" not in state


async def test_a_bookkeeping_failure_does_not_break_the_tick(jobset, monkeypatch):
    ran = []
    add(jobset, "a", lambda ctx: ran.append("a"), seconds=0)
    add(jobset, "b", lambda ctx: 1 / 0, seconds=0)

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(jobs.db, "x", broken)
    out = await daemon.tick(now=1)
    assert out["ran"] == ["a"] and list(out["errors"]) == ["b"] and ran == ["a"]
