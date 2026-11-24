"""User hooks: shell commands from settings.json that run around tool calls and at session start/stop.

    "hooks": {"pre_tool": [{"match": "code_edit|code_write", "command": "python check.py", "timeout": 10}],
              "post_tool": [...], "session_start": [...], "stop": [...],
              "user_prompt": [...], "pre_compact": [...], "subagent_stop": [...], "notification": [...]}

The hook gets JSON on stdin ({"event", "tool", "args" (redacted), "result" for post_tool, "session"}). A pre_tool hook
that exits 2 blocks the call and its stderr goes to the model as the reason; any other non-zero exit is a warning.
A user_prompt hook (payload has "prompt") that exits 2 stops the prompt before the model sees it; its stdout, when it
exits 0, is added to the prompt as extra context. pre_compact, subagent_stop and notification are informational.
Hooks come only from settings.json, never from model output, and run without credentials in their environment."""
import asyncio
import json
import os
import re
import subprocess

from . import config, security

EVENTS = ("pre_tool", "post_tool", "session_start", "stop", "user_prompt", "pre_compact", "subagent_stop", "notification")
_SECRET_ENV = re.compile(r"key|token|secret|pass(word)?|credential|auth|cookie|session", re.I)


def clean_env() -> dict:
    """The environment without anything that looks like a credential, or a key Sparx's providers read."""
    keys = {k for _b, k, _d in config.bp.PROVIDERS.values() if k}
    return {k: v for k, v in os.environ.items() if k not in keys and not _SECRET_ENV.search(k)}


def configured(event: str, tool: str = "") -> list[dict]:
    raw = config.settings().get("hooks", {}).get(event, [])
    out = []
    for h in raw if isinstance(raw, list) else []:
        if not isinstance(h, dict) or not isinstance(h.get("command"), str) or not h["command"].strip():
            continue
        m = h.get("match") or "*"
        if event in ("pre_tool", "post_tool") and m != "*":
            try:
                if not re.fullmatch(m, tool):
                    continue
            except re.error:
                continue
        out.append(h)
    return out


def _run(h: dict, payload: dict) -> tuple[str, str]:
    """(status, text): status is ok | block | warn | timeout | error."""
    try:
        timeout = float(h.get("timeout") or config.settings().get("hooks", {}).get("timeout", 10))
    except (TypeError, ValueError):
        timeout = 10.0
    try:
        p = subprocess.run(h["command"], shell=True, input=json.dumps(payload, default=str), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, env=clean_env(),
                           cwd=str(config.HOME) if config.HOME.is_dir() else None)
    except subprocess.TimeoutExpired:
        return "timeout", f"hook timed out after {timeout:g}s"
    except Exception as e:
        return "error", f"{type(e).__name__}: {str(e)[:200]}"
    err = (p.stderr or "").strip()[:1000]
    if p.returncode == 0:
        return "ok", (p.stdout or "").strip()[:2000]
    if p.returncode == 2:
        return "block", err or "blocked by a hook"
    return "warn", f"exit {p.returncode}: {err or (p.stdout or '').strip()[:300]}"


async def fire(event: str, ctx, tool: str = "", args: dict | None = None, result: str | None = None) -> str | None:
    """Run the hooks for an event. Returns the blocking reason when a pre_tool hook blocks, else None.
    Every hook run is written to the audit log."""
    hooks = configured(event, tool)
    if not hooks:
        return None
    payload = {"event": event, "tool": tool, "args": security.redact(args or {}), "session": ctx.session,
               "agent": ctx.agent}
    if result is not None:
        payload["result"] = security.redact(result)[:4000]
    for h in hooks:
        status, text = await asyncio.to_thread(_run, h, payload)
        security.audit(ctx.session, ctx.agent, f"hook:{event}", "exec", {"tool": tool, "command": h["command"][:200]},
                       status in ("ok", "warn"), f"{status}: {text}", 0)
        if status != "ok":
            ctx.event(type="hook", hook=event, tool=tool, status=status, text=text)
        if status == "block" and event == "pre_tool":
            return text
    return None


async def fire_prompt(ctx, prompt: str) -> tuple[str | None, str]:
    """Run user_prompt hooks. Returns (blocking reason or None, extra context from the hooks' stdout)."""
    extra = []
    for h in configured("user_prompt"):
        status, text = await asyncio.to_thread(_run, h, {"event": "user_prompt", "prompt": security.redact(prompt)[:8000],
                                                           "session": ctx.session, "agent": ctx.agent})
        security.audit(ctx.session, ctx.agent, "hook:user_prompt", "exec", {"command": h["command"][:200]},
                       status in ("ok", "warn"), f"{status}: {text[:300]}", 0)
        if status == "block":
            return text, ""
        if status == "ok" and text:
            extra.append(text)
    return None, "\n".join(extra)


async def fire_info(event: str, ctx, **payload):
    """Informational events (pre_compact, subagent_stop, notification): run the hooks, ignore what they say."""
    for h in configured(event):
        status, text = await asyncio.to_thread(_run, h, {"event": event, "session": ctx.session, "agent": ctx.agent,
                                                           **security.redact(payload)})
        security.audit(ctx.session, ctx.agent, f"hook:{event}", "exec", {"command": h["command"][:200]},
                       status in ("ok", "warn"), f"{status}: {text[:300]}", 0)


class _SessionCtx:
    def __init__(self):
        import uuid
        self.session, self.agent = uuid.uuid4().hex[:12], "sparx"


def _run_all(event: str, ctx):
    for h in configured(event):
        status, text = _run(h, {"event": event, "session": ctx.session})
        security.audit(ctx.session, ctx.agent, f"hook:{event}", "exec", {"command": h["command"][:200]},
                       status in ("ok", "warn"), f"{status}: {text}", 0)


def session_start_sync():
    """Run session_start hooks at the start of a chat and register the `stop` hooks to run at exit."""
    import atexit
    ctx = _SessionCtx()
    _run_all("session_start", ctx)
    atexit.register(_run_all, "stop", ctx)


def notify_sync(n: dict):
    """Run `notification` hooks for a stored notification, in a background thread so a slow hook never blocks the job."""
    if not configured("notification"):
        return
    import threading
    ctx = _SessionCtx()
    payload = {"event": "notification", "session": ctx.session, "title": n.get("title"), "body": n.get("body"),
               "kind": n.get("kind")}
    threading.Thread(target=lambda: [_run(h, payload) for h in configured("notification")], daemon=True).start()
