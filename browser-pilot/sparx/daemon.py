"""`sparx daemon`: runs every registered job when it is due, delivers notifications, honours the emergency stop.

`tick()` is one pass over the jobs; the web server can call it to run jobs in-process instead of a second process.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import traceback

from . import config, joblock, jobs, security, tools
from .tools import Context

JOB_TIMEOUT = 600      # a hung job must not block the others forever
_TOAST = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
          "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
          "$n.Visible = $true; $n.ShowBalloonTip(8000, $env:SPARX_TOAST_TITLE, $env:SPARX_TOAST_BODY, 'Info'); "
          "Start-Sleep -Seconds 9; $n.Dispose()")


def _log(text: str):
    with config.path("logs", "daemon.log").open("a", encoding="utf-8") as f:
        f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def heartbeat(info: dict | None = None):
    """Touch the heartbeat file; the web UI reads it to show whether the daemon is alive."""
    config.path("daemon.heartbeat").write_text(
        json.dumps({"pid": os.getpid(), "time": time.time(), "jobs": len(jobs.JOBS), **(info or {})}), encoding="utf-8")


def status(max_age: float = 60) -> dict:
    """{"alive": bool, ...last heartbeat}. Alive means a heartbeat was written in the last `max_age` seconds."""
    try:
        beat = json.loads(config.path("daemon.heartbeat", mkdir=False).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"alive": False}
    return {"alive": time.time() - beat.get("time", 0) <= max_age, **beat}


def toast(title: str, body: str):
    """Windows balloon notification through PowerShell. Best effort and non-blocking; text goes in by environment
    variable, never into the command line."""
    if sys.platform != "win32" or not config.settings()["proactive"].get("toasts", True):
        return
    env = {**os.environ, "SPARX_TOAST_TITLE": title[:60] or "Sparx", "SPARX_TOAST_BODY": body[:240] or " "}
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _TOAST],
                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def deliver(n: dict):
    """jobs.LISTENERS callback: print the notification and show a toast."""
    why = f"\n  why: {n['why']}" if n.get("why") else ""
    print(f"[{n['ts'][11:16]}] {n['kind']}: {n['title']}\n  {n['body']}{why}", flush=True)
    try:
        toast(n["title"], n["body"])
    except Exception as e:
        _log(f"toast failed: {type(e).__name__}: {e}")


async def tick(now: float | None = None) -> dict:
    """One pass: run each enabled job that is due, each with a fresh Context nobody can approve for.
    A job that raises is logged and the rest still run. Does nothing while the emergency stop is set."""
    now = time.time() if now is None else now
    out = {"ran": [], "errors": {}, "stopped": security.stop_requested()}
    if out["stopped"]:
        return out
    for job in list(jobs.JOBS.values()):
        if not jobs.enabled(job) or now - job.last < job.seconds:
            continue
        job.last = now
        ctx = Context(agent="daemon", approve=None)
        ctx.data["automated"] = True
        try:
            await asyncio.wait_for(jobs.run(job, ctx), JOB_TIMEOUT)
            out["ran"].append(job.name)
            jobs.record(job.name)
        except security.Stopped as e:
            out["stopped"] = True
            _log(f"{job.name}: stopped: {e}")
            if security.stop_requested():
                break
        except Exception as e:
            out["errors"][job.name] = f"{type(e).__name__}: {e}"
            jobs.record(job.name, f"timed out after {JOB_TIMEOUT}s" if isinstance(e, asyncio.TimeoutError)
                        else out["errors"][job.name])
            _log(f"{job.name} failed\n{traceback.format_exc()}")
    return out


async def main(interval: float = 5):
    tools.load_all()
    from . import automation, proactive  # noqa: F401  (their jobs register on import)
    jobs.LISTENERS.append(deliver)
    print(f"Sparx daemon running: {len(jobs.JOBS)} jobs ({', '.join(sorted(jobs.JOBS))}). Ctrl+C to quit.", flush=True)
    for mod, err in tools.LOAD_ERRORS.items():
        _log(f"tool module '{mod}' failed to load: {err}")
    lock = joblock.JobLock("daemon", log=lambda m: (print(m, flush=True), _log(m)))
    try:
        while True:
            try:
                result = await tick() if lock.hold(interval) else {"skipped": "jobs are run by another process"}
            except Exception:
                result = {"error": "tick failed, see logs/daemon.log"}
                _log(f"tick failed\n{traceback.format_exc()}")
            heartbeat({"last": result})
            await asyncio.sleep(interval * 3 if result.get("stopped") else interval)   # idle while stopped
    finally:
        lock.release()
        jobs.LISTENERS.remove(deliver)
        config.path("daemon.heartbeat").unlink(missing_ok=True)
