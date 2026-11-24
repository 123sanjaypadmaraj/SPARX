"""One background job loop per Sparx home.

`sparx serve` and `sparx daemon` both can run the job loop; running both would fire a reminder twice. The process that
holds this lock (`jobs.lock` under the Sparx home: pid, start time, heartbeat) runs jobs; the other one skips them and
re-tries. A lock is free when it is missing, its pid is dead, or its heartbeat is older than the stale limit.
"""
import atexit
import json
import os
import sys
import time

from . import config

NAME = "jobs.lock"


def pid_alive(pid: int) -> bool:
    """Is this process running? os.kill(pid, 0) is not a probe on Windows (signal 0 is not special-cased there and it
    can terminate or mis-report), so on Windows ask the OS through OpenProcess + GetExitCodeProcess."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5             # access denied: it exists, we may not look
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def read() -> dict | None:
    try:
        d = json.loads(config.path(NAME, mkdir=False).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def stale_limit(interval: float) -> float:
    return max(120.0, 3 * interval)


def is_stale(d: dict | None, limit: float) -> bool:
    return not d or not pid_alive(d.get("pid")) or time.time() - float(d.get("heartbeat") or 0) > limit


def describe() -> str:
    """One honest line for `sparx doctor`: who is running the jobs."""
    d = read()
    if not d:
        return "nobody (start `sparx daemon` or `sparx serve`)"
    who = f"{d.get('role') or 'process'} (pid {d.get('pid')})"
    if not pid_alive(d.get("pid")):
        return f"nobody ({who} left a lock but is not running)"
    age = int(time.time() - float(d.get("heartbeat") or 0))
    return who + (f", heartbeat {age}s ago" if age < 120 else f", but its heartbeat is {age}s old (stale)")


class JobLock:
    def __init__(self, role: str = "process", log=print):
        self.role, self.pid, self.log = role, os.getpid(), log
        self.held = False
        self._started = time.time()
        self._told = None            # last state we logged, so a waiting process logs once
        atexit.register(self.release)

    def _body(self) -> str:
        return json.dumps({"pid": self.pid, "role": self.role, "started": self._started, "heartbeat": time.time()})

    def _create(self) -> bool:
        try:
            fd = os.open(config.path(NAME), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self._body())
        return True

    def acquire(self, stale: float = 120.0) -> tuple[bool, dict | None]:
        """(True, None) when we now hold the lock, else (False, holder). Never takes a live, fresh lock."""
        if self.held and (read() or {}).get("pid") == self.pid:
            return True, None
        self.held = False
        for _ in range(3):
            if self._create():
                self.held = True
                return True, None
            cur = read()
            if cur and not is_stale(cur, stale):
                return False, cur
            # Stale (or unreadable): claim it by renaming it away; only one racing process can win the rename.
            p = config.path(NAME)
            mine = p.with_name(f"{NAME}.{self.pid}.old")
            try:
                os.replace(p, mine)
            except OSError:
                continue
            try:
                taken = json.loads(mine.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                taken = None
            if taken is not None and not is_stale(taken, stale) and not os.path.exists(p):
                os.replace(mine, p)                    # we grabbed a lock that was refreshed meanwhile: put it back
                return False, taken
            mine.unlink(missing_ok=True)
        return False, read()

    def touch(self) -> bool:
        """Refresh the heartbeat. False (and not held any more) if someone else owns the file now."""
        if not self.held:
            return False
        if (read() or {}).get("pid") != self.pid:
            self.held = False
            return False
        p = config.path(NAME)
        tmp = p.with_name(f"{NAME}.{self.pid}.tmp")
        try:
            tmp.write_text(self._body(), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            tmp.unlink(missing_ok=True)                # a reader had it open for a moment; the next tick will do
        return True

    def release(self):
        """Delete the lock, only if we own it."""
        self.held = False
        d = read()
        if d and d.get("pid") == self.pid:
            config.path(NAME, mkdir=False).unlink(missing_ok=True)

    def hold(self, interval: float) -> bool:
        """Call once per loop tick: refresh if held, else try to take over. True means this process runs the jobs."""
        if self.held and self.touch():
            return True
        ok, holder = self.acquire(stale_limit(interval))
        state = "run" if ok else f"wait:{(holder or {}).get('pid')}"
        if state != self._told:
            self._told = state
            self.log(f"[{self.role}] running the background jobs" if ok else
                     f"[{self.role}] background jobs are already run by {(holder or {}).get('role', 'another process')} "
                     f"(pid {(holder or {}).get('pid')}); not running them here, will re-check")
        return ok
