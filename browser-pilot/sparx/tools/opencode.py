"""OpenCode CLI as a free helper: `opencode run` with a free model, used for code tasks (the opencode_task tool) and
as a plain-text backup when the main model is rate limited (llm.py). OpenCode's free tier only answers inside the
OpenCode program, so Sparx runs the CLI rather than calling its API.

Everything sent to a free model leaves this PC and may be kept by the provider. Settings: models.opencode picks the
model (default opencode/deepseek-v4.1-flash-free); models.opencode_fallback false turns the chat backup off.
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import config
from . import S, ToolError, tool
from .coding import _dir

DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def binary() -> str | None:
    """The real opencode.exe where npm put it (the .cmd shim cannot be fed a prompt on stdin reliably)."""
    exe = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    return str(exe) if exe.is_file() else shutil.which("opencode")


def model() -> str:
    return config.settings()["models"].get("opencode") or DEFAULT_MODEL


def run(prompt: str, cwd=None, agent: str = "plan", auto: bool = False, timeout: float = 300) -> str:
    """Answer from one `opencode run`. agent "plan" only reads; "build" may edit files (auto = approve its edits)."""
    exe = binary()
    if not exe:
        raise ToolError("OpenCode is not installed: npm install -g opencode-ai")
    cmd = [exe, "run", "-m", model(), "--agent", agent] + (["--auto"] if auto else [])
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=str(cwd) if cwd else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"OpenCode did not finish within {int(timeout)} s.") from None
    out = ANSI.sub("", p.stdout).strip()
    out = re.sub(r"^> .+\n", "", out).strip()          # the "> build · model" banner
    if p.returncode != 0 or not out:
        raise ToolError("OpenCode failed: " + (ANSI.sub("", p.stderr).strip() or out or f"exit {p.returncode}")[-400:])
    return out


def transcript(messages: list) -> str:
    """Chat messages as one prompt for a model that takes plain text."""
    parts = []
    for m in messages:
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        if c:
            parts.append(f"[{m.get('role', 'user')}]\n{c}")
    return "\n\n".join(parts)[-20000:] + "\n\nReply to the last message as the assistant."


_down_until = 0.0


def backup_reply(messages: list) -> str | None:
    """Plain-text answer for llm.py when every configured model is rate limited; None when this backup is off,
    missing, or failed recently (so a broken CLI is not retried on every wait)."""
    global _down_until
    if config.settings()["models"].get("opencode_fallback") is False or time.time() < _down_until:
        return None
    try:
        return run(transcript(messages), agent="plan", timeout=120)
    except ToolError:
        _down_until = time.time() + 300
        return None


@tool("opencode_task", "Hand a coding task to OpenCode running a free model: explain, review or plan (mode 'plan', "
      "read-only) or make the change (mode 'build', edits files in the folder). Use for self-contained code work; "
      "the folder's code is sent to a third-party free model.",
      {"path": {**S, "description": "project folder to work in"}, "task": {**S, "description": "what to do"},
       "mode": {**S, "description": "plan (default, read-only) or build (may edit files)"}},
      ["path", "task"], category="coding", risk="exec", timeout=330, untrusted=True)
def opencode_task(ctx, path: str, task: str, mode: str = "plan"):
    if mode not in ("plan", "build"):
        raise ToolError("mode must be 'plan' or 'build'.")
    repo = _dir(path)
    return {"model": model(), "mode": mode, "answer": run(task, cwd=repo, agent=mode, auto=mode == "build")}
