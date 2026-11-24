"""Background processes: dev servers, watchers and builds that keep running while Sparx does other things."""
import atexit
import os
import re
import subprocess
import threading
import time
import uuid

from .. import config, db, security
from . import I, S, ToolError, category, tool
from .files import safe_path
from .system import WINDOWS, _log, clean_env, denied_command

category("background", "Start long-running commands (servers, builds) in the background, read their output, stop them")

db.schema("""
CREATE TABLE IF NOT EXISTS bg_procs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', command TEXT NOT NULL, cwd TEXT NOT NULL DEFAULT '',
    pid INTEGER NOT NULL, log TEXT NOT NULL, started TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
    exit_code INTEGER, read_offset INTEGER NOT NULL DEFAULT 0, owner INTEGER NOT NULL DEFAULT 0
);
""")

_LIVE: dict[str, subprocess.Popen] = {}       # processes started by this Sparx process (we can read their exit code)
_LOCK = threading.RLock()
_WATCH: threading.Thread | None = None
_CHUNK = 20000


def _cfg(key: str, default):
    return (config.settings().get("background") or {}).get(key, default)


def _alive(pid: int) -> bool:
    if WINDOWS:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k.CloseHandle(ctypes.c_void_p(h))
        return bool(ok) and code.value == 259             # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _kill_tree(pid: int):
    if WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
    import signal
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _refresh(row: dict) -> dict:
    """Update status and exit code of a row from reality; enforces the output cap."""
    if row["status"] != "running":
        return row
    p = _LIVE.get(row["id"])
    code = p.poll() if p else None
    running = (code is None) if p else _alive(row["pid"])
    status = "running"
    if running:
        cap = int(_cfg("max_output_kb", 5120)) * 1024
        try:
            if os.path.getsize(row["log"]) > cap:
                _kill_tree(row["pid"])
                running, status = False, "stopped (output cap reached)"
        except OSError:
            pass
    if not running:
        if p:
            try:
                code = p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = p.poll()
            _LIVE.pop(row["id"], None)
        status = status if status != "running" else "exited"
        db.x("UPDATE bg_procs SET status=?, exit_code=? WHERE id=?", (status, code, row["id"]))
        row = {**row, "status": status, "exit_code": code}
    return row


def stop_all(only_owned: bool = False) -> int:
    """Terminate every running background process (emergency stop, exit). Returns how many were running."""
    n = 0
    with _LOCK:
        for row in db.q("SELECT * FROM bg_procs WHERE status='running'"):
            if only_owned and row["owner"] != os.getpid():
                continue
            _kill_tree(row["pid"])
            n += 1
            p = _LIVE.pop(row["id"], None)
            code = None
            if p:
                try:
                    code = p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            db.x("UPDATE bg_procs SET status='stopped', exit_code=? WHERE id=?", (code, row["id"]))
    return n


def _on_exit():
    try:
        if not _cfg("keep_on_exit", False):
            stop_all(only_owned=True)
    except Exception:
        pass


atexit.register(_on_exit)


def _watchdog():
    """Polls the emergency-stop flag (it may be set by another process) and enforces the output cap."""
    while True:
        time.sleep(2)
        try:
            if security.stop_requested():
                stop_all()
            for row in db.q("SELECT * FROM bg_procs WHERE status='running' AND owner=?", (os.getpid(),)):
                _refresh(row)
        except Exception:
            pass


def _ensure_watchdog():
    global _WATCH
    if _WATCH is None or not _WATCH.is_alive():
        _WATCH = threading.Thread(target=_watchdog, daemon=True, name="sparx-bg-watchdog")
        _WATCH.start()


def _row(id: str) -> dict:
    row = db.one("SELECT * FROM bg_procs WHERE id=?", (id,))
    if not row:
        raise ToolError(f"No background process '{id}'. bg_list shows the known ones.")
    return row


def _view(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "command": row["command"], "pid": row["pid"],
            "status": row["status"], "exit_code": row["exit_code"], "started": row["started"], "log": row["log"]}


@tool("bg_start", "Start a long-running command (dev server, file watcher, build) in the background and return at once with an id. "
      "Output goes to a log file; use bg_read to see it and bg_stop to end it. Same refusals and credential scrubbing as shell_run.",
      {"command": S, "cwd": {**S, "description": "working folder; default is the home folder"},
       "name": {**S, "description": "short label, e.g. 'web server'"}},
      ["command"], category="background", risk="exec", timeout=60)
def bg_start(ctx, command: str, cwd: str = "", name: str = ""):
    why = denied_command(command)
    if why:
        raise ToolError(f"Refused: this command {why}. Sparx never runs it, even with approval; "
                        "the user can run it themselves if they really mean it.")
    folder = safe_path(cwd or "~", exists=True)
    if not folder.is_dir():
        raise ToolError(f"{folder} is not a folder.")
    with _LOCK:
        for row in db.q("SELECT * FROM bg_procs WHERE status='running'"):
            _refresh(row)
        running = db.one("SELECT COUNT(*) AS n FROM bg_procs WHERE status='running'")["n"]
        cap = int(_cfg("max_processes", 5))
        if running >= cap:
            raise ToolError(f"{running} background processes are already running (limit {cap}). Stop one with bg_stop first.")
        bid = uuid.uuid4().hex[:8]
        log = config.path("background", f"{bid}.log")
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)} if WINDOWS \
            else {"start_new_session": True}
        env = clean_env({"PYTHONUNBUFFERED": "1"})
        try:
            with open(log, "wb") as out:
                p = subprocess.Popen(command, cwd=str(folder), shell=True, env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=subprocess.STDOUT, **kw)
        except OSError as e:
            raise ToolError(f"Could not start the command: {e}")
        _LIVE[bid] = p
        db.x("INSERT INTO bg_procs (id, name, command, cwd, pid, log, started, owner) VALUES (?,?,?,?,?,?,?,?)",
             (bid, name, str(security.redact(command)), str(folder), p.pid, str(log), db.now(), os.getpid()))
    _ensure_watchdog()
    _log(ctx, "bg_start", command)
    time.sleep(0.3)
    return _view(_refresh(_row(bid)))


@tool("bg_read", "Read the output a background process has produced since the last bg_read (or the last N lines), "
      "plus whether it is still running and its exit code.",
      {"id": S, "tail_lines": {**I, "description": "return only the last N lines instead of new output"}},
      ["id"], category="background", risk="read", untrusted=True)
def bg_read(ctx, id: str, tail_lines: int = 0):
    row = _refresh(_row(id))
    try:
        size = os.path.getsize(row["log"])
        with open(row["log"], "rb") as f:
            if tail_lines and tail_lines > 0:
                f.seek(max(0, size - _CHUNK * 4))
                text = f.read().decode("utf-8", "replace")
                text = "\n".join(text.splitlines()[-int(tail_lines):])
                off = size
            else:
                f.seek(min(row["read_offset"], size))
                text = f.read(_CHUNK).decode("utf-8", "replace")
                off = f.tell()
    except OSError:
        text, off = "", row["read_offset"]
    if not tail_lines:
        db.x("UPDATE bg_procs SET read_offset=? WHERE id=?", (off, id))
    more = size - off
    return {**_view(row), "output": str(security.redact(text)), "more_bytes_unread": max(0, more)}


@tool("bg_stop", "Stop a background process and everything it started (the whole process tree).",
      {"id": S}, ["id"], category="background", risk="exec")
def bg_stop(ctx, id: str):
    with _LOCK:
        row = _row(id)
        if row["status"] == "running":
            _kill_tree(row["pid"])
            p = _LIVE.pop(id, None)
            code = None
            if p:
                try:
                    code = p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            db.x("UPDATE bg_procs SET status='stopped', exit_code=? WHERE id=?", (code, id))
    _log(ctx, "bg_stop", id)
    return _view(_row(id))


@tool("bg_list", "List background processes Sparx has started (including ones from an earlier session) and their status.",
      {}, [], category="background", risk="read")
def bg_list(ctx):
    return [_view(_refresh(r)) for r in db.q("SELECT * FROM bg_procs ORDER BY started DESC LIMIT 50")]
