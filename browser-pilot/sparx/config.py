"""Paths and settings. Everything Sparx stores lives under HOME (~/.sparx).

Always go through `config.path(...)` / `config.settings()` rather than importing HOME,
so tests (and SPARX_HOME) can point Sparx at another directory.
"""
import copy
import json
import os
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import browserpilot.config as bp  # importing it also loads ~/.browserpilot/.env

HOME = Path(os.environ.get("SPARX_HOME") or Path.home() / ".sparx")

# What each risk level does by default: allow | ask | deny
DEFAULTS = {
    "name": "Sparx",
    "user": "",
    "language": "en",            # en | hi | kn | ... (reply language; voice uses it too)
    "personality": "sparx",     # sparx | casual | professional | technical
    "address": "sir",            # how the sparx personality addresses you: sir, ma'am, boss, your name...
    "models": {"default": "", "fast": "", "smart": "", "vision": "", "embed": ""},
    "permissions": {
        "read": "allow",      # look at data Sparx already has access to
        "write": "allow",     # create or change Sparx's own data and files in its workspace
        "exec": "ask",        # run commands / code
        "control": "ask",     # drive the desktop, browser, devices
        "delete": "ask",      # remove things
        "send": "ask",        # email, messages, posts: anything that reaches other people
        "spend": "ask",       # purchases, transfers, trades
    },
    "tool_permissions": {},       # per-tool override: {"email_send": "deny"}
    "permission_mode": "default", # default | accept_edits | plan | bypass (see sparx/permissions.py)
    "permission_rules": {"allow": [], "ask": [], "deny": []},   # "tool" or "tool(glob on the command/path/url)"
    "output_style": "default",    # default | concise | explanatory | learning | a file in ~/.sparx/output-styles
    "statusline": {"command": ""},  # a shell command whose first output line replaces the chat toolbar text
    "limits": {"max_rounds": 14, "max_tool_calls": 40, "max_seconds": 900, "max_agents": 4, "pending_minutes": 60, "max_history_chars": 24000, "loop_guard": True, "loop_repeat_nudge": 3, "loop_repeat_stop": 6, "loop_fail_nudge": 3, "loop_fail_stop": 6, "loop_alternate_nudge": 6, "loop_alternate_stop": 10, "subagent_rounds": 8, "subagent_report_chars": 2000, "automation_runs_per_hour": 120},
    "retention": {"audit_days": 180, "notifications_days": 60, "log_mb": 5, "backups": 7, "undo_days": 30},   # housekeeping; 0 disables a rule
    "always_on": ["core", "memory", "productivity", "sparx"],
    "proactive": {
        "enabled": True, "daily_briefing": "08:00", "reminders": True, "deadline_risk": True,
        "follow_ups": True, "routine_detection": True, "monitors": True, "anomalies": True,
        # flags read by jobs and behaviours (a missing flag counts as on)
        "device_suggestions": True, "toasts": True, "goal_intervention": True, "opportunities": True,
        "analytics": True, "research": True,
    },
    "voice": {"wake_word": "hey sparx", "voice": "en-IN-NeerjaNeural", "stt_model": "base", "speak_replies": True},
    "server": {"host": "127.0.0.1", "port": 8765},
    "hooks": {"timeout": 10},     # shell commands around tools: {"pre_tool": [{"match": "code_.*", "command": "..."}], "post_tool": [], "session_start": [], "stop": [], "user_prompt": [], "pre_compact": [], "subagent_stop": [], "notification": []}
    "plan_mode": False,           # start every session in plan mode (read-only tools until a plan is approved)
    "allowed_roots": [],          # extra folders file tools may touch (home folder is always allowed)
    "integrations": {},           # email / telegram / home assistant / mqtt settings (secrets go in .env)
}


def _load_env():
    f = HOME / ".env"
    if not f.is_file():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_env()
for _name, _var in (("custom", "BROWSERPILOT_BASE_URL"), ("ollama", "OLLAMA_BASE_URL"), ("lmstudio", "LMSTUDIO_BASE_URL")):
    if os.environ.get(_var):    # browserpilot read these before ~/.sparx/.env was loaded
        bp.PROVIDERS[_name] = (os.environ[_var], *bp.PROVIDERS[_name][1:])


def path(*parts, mkdir: bool = True) -> Path:
    """A path under HOME. Parent folders are created."""
    p = HOME.joinpath(*map(str, parts))
    if mkdir:
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


_SHAPES = {dict: "a group of settings", list: "a list"}


def _shape(v) -> str:
    return _SHAPES.get(type(v) if isinstance(v, (dict, list)) else None, "a single value")


def _overlay(base: dict, over: dict, default, bad: list, at: str = "") -> dict:
    """_merge, except that an override whose shape differs from the default at the same path is skipped and named
    in `bad`: a group stays a group, a list a list, a single value a single value. Paths with no default pass."""
    for k, v in over.items():
        known = isinstance(default, dict) and k in default
        if known and _shape(default[k]) != _shape(v):
            bad.append(f"{at}{k}: expected {_shape(default[k])}, found {json.dumps(v, ensure_ascii=False)[:60]}")
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _overlay(base[k], v, default[k] if known else None, bad, f"{at}{k}.")
        else:
            base[k] = v
    return base


def _load(bad: list) -> dict:
    out = copy.deepcopy(DEFAULTS)
    try:
        text = (HOME / "settings.json").read_text(encoding="utf-8")
        saved = json.loads(text) if text.strip() else {}
    except OSError:
        return out
    except ValueError as e:
        bad.append(f"settings.json: not valid JSON ({e}); every setting falls back to its default until it is fixed or removed")
        return out
    if not isinstance(saved, dict):
        bad.append(f"settings.json: expected a group of settings, found {json.dumps(saved, ensure_ascii=False)[:60]}")
        return out
    return _overlay(out, saved, DEFAULTS, bad)


def settings() -> dict:
    """Defaults with settings.json on top. A wrong-shaped entry is ignored, so no caller ever gets a string where
    a group belongs; problems() names what was ignored."""
    return _load([])


def problems() -> list[str]:
    """Entries in settings.json that are ignored because their shape is wrong."""
    bad = []
    _load(bad)
    return bad


class SettingsDamaged(ValueError):
    """settings.json exists but is not a JSON object. Saving would throw the user's other settings away."""


@contextmanager
def _locked(f: Path, wait: float = 10.0, stale: float = 5.0):
    """Cross-process lock: a file created with O_EXCL beside `f`. A lock older than `stale` seconds is broken."""
    lock = f.with_name(f.name + ".lock")
    end = time.monotonic() + wait
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):      # Windows: a lock that is being deleted answers PermissionError
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink()
                    continue
            except OSError:
                continue
            if time.monotonic() > end:
                raise OSError(f"{lock} is held by another Sparx process; remove it if no Sparx is running")
            time.sleep(0.02)
    try:
        yield
    finally:
        with suppress(OSError):
            lock.unlink()


def _current(f: Path) -> dict:
    try:
        text = f.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        cur = json.loads(text) if text.strip() else {}
    except ValueError as e:
        raise SettingsDamaged(f"{f} is not valid JSON ({e}). Nothing was saved; fix or remove that file.") from e
    if not isinstance(cur, dict):
        raise SettingsDamaged(f"{f} is not a JSON object. Nothing was saved; fix or remove that file.")
    return cur


def _write_atomic(f: Path, data: str):
    tmp = f.with_name(f"{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as h:
            h.write(data)
            h.flush()
            os.fsync(h.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, f)
                return
            except PermissionError:             # Windows: another process has the file open for a moment
                if attempt == 9:
                    raise
                time.sleep(0.05)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def save_settings(patch: dict) -> dict:
    """Deep-merge patch into settings.json and return the effective settings. The merge is applied to the latest file
    under a lock and written atomically; a damaged file is never overwritten (SettingsDamaged, a ValueError)."""
    f = path("settings.json")
    with _locked(f):
        cur = _merge(_current(f), patch)
        _write_atomic(f, json.dumps(cur, indent=2, ensure_ascii=False))
    return settings()


def set_key(key: str, value):
    """Set a dotted key ('permissions.send', 'proactive.daily_briefing'). Strings are parsed as JSON when possible."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    patch = cur = {}
    parts = key.split(".")
    if not all(parts):
        raise ValueError(f"'{key}' is not a setting name. Use a dotted key such as proactive.daily_briefing.")
    for p in parts[:-1]:
        cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value
    bad = []
    _overlay(copy.deepcopy(DEFAULTS), patch, DEFAULTS, bad)
    if bad:
        raise ValueError(bad[0])
    save_settings(patch)
    return value


def secret(name: str, default: str = "") -> str:
    """Credentials come from the environment / .env files only, never from settings.json."""
    return os.environ.get(name, default)


def default_model() -> str:
    """Configured model, else browser-pilot's default if its key exists, else local Ollama."""
    spec = settings()["models"]["default"] or os.environ.get("SPARX_MODEL") or bp.DEFAULT_MODEL
    provider = spec.partition("/")[0]
    key_env = bp.PROVIDERS.get(provider, (None, None, None))[1]
    if key_env and not os.environ.get(key_env) and provider != "custom":
        return "ollama/gpt-oss:latest"
    return spec
