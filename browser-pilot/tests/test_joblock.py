import json
import os
import subprocess
import sys
import time

from sparx import automation, proactive  # noqa: F401  (their jobs register now, as in test_daemon)
from sparx import config, daemon, joblock, jobs


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _write(d: dict):
    config.path(joblock.NAME).write_text(json.dumps(d), encoding="utf-8")


def test_pid_alive_knows_live_and_dead():
    assert joblock.pid_alive(os.getpid())
    assert not joblock.pid_alive(_dead_pid())
    assert not joblock.pid_alive(0) and not joblock.pid_alive(-5)


def test_acquire_release():
    a = joblock.JobLock("serve", log=lambda m: None)
    assert a.acquire() == (True, None)
    d = joblock.read()
    assert d["pid"] == os.getpid() and d["role"] == "serve" and d["started"] and d["heartbeat"]
    assert "serve" in joblock.describe()
    a.release()
    assert joblock.read() is None and not a.held
    assert joblock.describe().startswith("nobody")


def test_second_acquire_refused_while_live(monkeypatch):
    a = joblock.JobLock("daemon", log=lambda m: None)
    assert a.acquire()[0]
    b = joblock.JobLock("serve", log=lambda m: None)
    b.pid = os.getpid() + 1_000_000                      # another process, as far as the file is concerned
    monkeypatch.setattr(joblock, "pid_alive", lambda pid: True)
    ok, holder = b.acquire()
    assert not ok and holder["pid"] == os.getpid() and holder["role"] == "daemon"
    assert not b.hold(20) and joblock.read()["pid"] == os.getpid()
    a.release()


def test_takeover_when_pid_dead():
    _write({"pid": _dead_pid(), "role": "daemon", "started": time.time(), "heartbeat": time.time()})
    b = joblock.JobLock("serve", log=lambda m: None)
    assert b.acquire() == (True, None)
    assert joblock.read()["pid"] == os.getpid()
    b.release()


def test_takeover_when_heartbeat_stale(monkeypatch):
    old = time.time() - 600
    _write({"pid": os.getpid() + 1_000_000, "role": "daemon", "started": old, "heartbeat": old})
    monkeypatch.setattr(joblock, "pid_alive", lambda pid: True)       # alive, but silent for 10 minutes
    b = joblock.JobLock("serve", log=lambda m: None)
    assert b.acquire(stale=120) == (True, None)
    assert joblock.read()["role"] == "serve"
    b.release()


def test_non_owner_cannot_release_and_touch_notices_loss():
    a = joblock.JobLock("daemon", log=lambda m: None)
    a.acquire()
    other = joblock.JobLock("serve", log=lambda m: None)
    other.pid = a.pid + 1_000_000
    other.release()
    assert joblock.read()["pid"] == a.pid
    _write({"pid": other.pid, "role": "serve", "started": time.time(), "heartbeat": time.time()})
    assert not a.touch() and not a.held
    a.release()                                           # not the owner any more: file stays
    assert joblock.read()["pid"] == other.pid


def test_hold_refreshes_heartbeat_and_logs_once(monkeypatch):
    lines = []
    a = joblock.JobLock("serve", log=lines.append)
    assert a.hold(20)
    first = joblock.read()["heartbeat"]
    time.sleep(0.02)
    assert a.hold(20) and joblock.read()["heartbeat"] > first
    assert len(lines) == 1
    a.release()


def test_stale_limit_minimum():
    assert joblock.stale_limit(5) == 120 and joblock.stale_limit(100) == 300


async def test_daemon_tick_still_works_without_lock(monkeypatch):
    monkeypatch.setattr(jobs, "JOBS", {})
    monkeypatch.setattr(daemon, "toast", lambda t, b: None)
    out = await daemon.tick(now=1000)
    assert out["ran"] == [] and joblock.read() is None    # tick itself is lock-free
