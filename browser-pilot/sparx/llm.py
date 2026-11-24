"""Model access by role, on top of browser-pilot's provider-agnostic client.

Roles let cheap work go to a small model and hard work to a big one:
    default  everyday chat and tool use        fast   summaries, classification, extraction
    smart    planning, verification            vision image understanding
Unset roles fall back to `default`. `models.fallbacks` is a list of specs that take over, in order, while the
model in use is rate limited or overloaded: sparx config models.fallbacks '["groq/openai/gpt-oss-20b"]'
"""
import asyncio
import os
from asyncio import to_thread
import time
import traceback
from datetime import date, timedelta
from types import SimpleNamespace

import openai
from browserpilot.llm import LLM, extract_json  # noqa: F401  (re-exported)

from . import config, db

db.schema("""CREATE TABLE IF NOT EXISTS llm_usage (
    day TEXT NOT NULL, model TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
    unreported_calls INTEGER NOT NULL DEFAULT 0, ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model))""")

USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "ms": 0, "by_model": {}}
_cache: dict[str, "SparxLLM"] = {}


class FirstRunError(Exception):
    """A failure a first-time user meets (no key, model server down, model missing). str() is the whole instruction."""


OLLAMA_DOWN = "Ollama is not running: start it, or set a key for a hosted model"


def not_pulled(model: str) -> str:
    return f"model {model} is not pulled: ollama pull {model}"


def missing_key(key_env: str) -> str:
    return f"{key_env} is not set: put {key_env}=<your key> in {config.HOME / '.env'}"


BUSY = (openai.RateLimitError, openai.InternalServerError, openai.APITimeoutError)
MAX_WAIT = 120      # seconds one call may spend waiting for a rate limit to clear when every model is busy


def _retry_after(e: Exception) -> float:
    """Seconds the provider asked us to stay away (a rate limit says; an overload or timeout does not)."""
    try:
        return min(max(float(e.response.headers["retry-after"]), 1), 3600)
    except (AttributeError, KeyError, ValueError):
        return 5 if isinstance(e, openai.APITimeoutError) else 20


def _too_big(e: Exception) -> bool:
    """The request is larger than this model accepts (Groq: more tokens than a whole minute's allowance)."""
    return isinstance(e, openai.APIStatusError) and e.status_code == 413


def _down(e: Exception) -> bool:
    """The server is unreachable or erroring (a remote notebook whose session ended answers 5xx or refuses)."""
    return isinstance(e, FirstRunError) or (isinstance(e, openai.APIStatusError) and e.status_code >= 500)


def _opencode_backup(kw: dict) -> str | None:
    """OpenCode's free model answering a plain chat while every API model is rate limited (tools/opencode.py)."""
    from .tools import opencode
    return opencode.backup_reply(kw.get("messages") or [])


UNSIGNED = {"google": {"thought_signature": "skip_thought_signature_validator"}}


def _signed(messages: list) -> list:
    """Copy of `messages` Gemini accepts: it refuses a tool call without its own signature, so calls another
    model made are given the value Google documents for exactly this case."""
    out = []
    for m in messages:
        if m.get("tool_calls") and not all((c.get("extra_content") or {}).get("google") for c in m["tool_calls"]):
            m = {**m, "tool_calls": [c if (c.get("extra_content") or {}).get("google") else
                                     {**c, "extra_content": UNSIGNED} for c in m["tool_calls"]]}
        out.append(m)
    return out


class SparxLLM(LLM):
    """LLM that records token usage and latency, and hands a call to a backup model (settings models.fallbacks,
    tried in order) while this one is rate limited, overloaded, or too small for the request."""

    def __init__(self, spec: str):
        super().__init__(spec)
        self.client = self.client.with_options(max_retries=0)    # _create does the waiting, so a backup can step in
        self.busy_until = 0.0

    async def _create(self, **kw):
        deadline, err = time.time() + MAX_WAIT, None
        while True:
            chain, waiting = [self, *backups(self.spec)], False
            for m in chain:
                if m.busy_until > time.time():
                    waiting = True
                    continue
                try:
                    return await m._once(**kw)
                except BUSY as e:
                    m.busy_until, err, waiting = time.time() + _retry_after(e), e, True
                except (openai.APIStatusError, FirstRunError) as e:
                    if m is self and not (len(chain) > 1 and (_too_big(e) or _down(e))):
                        raise
                    err = e     # a model that cannot take this request is skipped; so is a misconfigured backup
            if waiting and not kw.get("tools") and (text := await to_thread(_opencode_backup, kw)):
                msg = SimpleNamespace(content=text, tool_calls=None, role="assistant")
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
            wake = min((m.busy_until for m in chain if m.busy_until > time.time()), default=time.time() + 1)
            if err and (not waiting or wake > deadline):    # not waiting: nobody is merely busy, so a wait changes nothing
                raise err
            await asyncio.sleep(max(wake - time.time(), 1))

    async def _once(self, **kw):
        start = time.time()
        if self.provider == "gemini" and kw.get("messages"):
            kw["messages"] = _signed(kw["messages"])
        try:
            r = await LLM._create(self, **kw)
        except openai.NotFoundError:
            raise FirstRunError(not_pulled(self.model) if self.provider == "ollama" else
                                f"model {self.model} was not found at {self.provider}: check the name, or /model to pick another") from None
        except openai.APITimeoutError:
            raise
        except openai.APIConnectionError:
            raise FirstRunError(OLLAMA_DOWN if self.provider == "ollama" else
                                f"Cannot reach {self.provider}: check your internet connection") from None
        ms = int((time.time() - start) * 1000)
        u = getattr(r, "usage", None)
        pt, ct = (getattr(u, "prompt_tokens", 0) or 0), (getattr(u, "completion_tokens", 0) or 0)
        reported = getattr(u, "prompt_tokens", None) is not None or getattr(u, "completion_tokens", None) is not None
        m = USAGE["by_model"].setdefault(self.spec, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "ms": 0})
        for d in (USAGE, m):
            d["calls"] += 1
            d["prompt_tokens"] += pt
            d["completion_tokens"] += ct
            d["ms"] += ms
        record_usage(self.spec, pt, ct, ms, reported)
        return r


def record_usage(model: str, prompt_tokens: int, completion_tokens: int, ms: int = 0, reported: bool = True):
    """Add one completed call to today's persistent row for `model`. Never raises: a database failure is logged.
    reported=False means the provider returned no token counts (stored as 0 and counted in unreported_calls)."""
    try:
        db.x("""INSERT INTO llm_usage (day, model, calls, prompt_tokens, completion_tokens, unreported_calls, ms)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(day, model) DO UPDATE SET calls = calls + 1,
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    unreported_calls = unreported_calls + excluded.unreported_calls, ms = ms + excluded.ms""",
             (date.today().isoformat(), model, prompt_tokens, completion_tokens, 0 if reported else 1, ms))
    except Exception:
        try:
            config.path("logs", "errors.log").open("a", encoding="utf-8").write(
                f"--- llm usage {time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n")
        except Exception:
            pass


def usage_history(days: int = 30) -> dict:
    """Persistent usage for the last `days` local days (today included): per-day per-model rows plus totals.
    Token counts are only what providers reported; calls with no counts are in `unreported_calls`."""
    days = max(1, int(days))
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = db.q("SELECT day, model, calls, prompt_tokens, completion_tokens, unreported_calls, ms FROM llm_usage "
                "WHERE day >= ? ORDER BY day DESC, model", (since,))
    keys = ("calls", "prompt_tokens", "completion_tokens", "unreported_calls", "ms")
    return {"days": days, "since": since, "rows": rows, "totals": {k: sum(r[k] for r in rows) for k in keys}}


def spec_for(role: str = "default") -> str:
    return config.settings()["models"].get(role) or config.default_model()


def _key_missing(spec: str) -> str:
    """Name of the API key variable `spec` needs and does not have, or ''. A `custom` model with no
    BROWSERPILOT_BASE_URL reports that variable, so an unset remote server is skipped rather than fatal."""
    provider = spec.partition("/")[0]
    if provider == "custom" and not os.environ.get("BROWSERPILOT_BASE_URL"):
        return "BROWSERPILOT_BASE_URL"
    key_env = config.bp.PROVIDERS.get(provider, (None, None, None))[1]
    return key_env if key_env and provider != "custom" and not os.environ.get(key_env) else ""


def _model(spec: str) -> SparxLLM:
    if spec not in _cache:
        _cache[spec] = SparxLLM(spec)
    return _cache[spec]


def backups(spec: str) -> list[SparxLLM]:
    """Models that take over while `spec` is busy: settings models.fallbacks, minus itself and any with no key."""
    listed = config.settings()["models"].get("fallbacks") or []
    return [_model(s) for s in listed if isinstance(s, str) and s != spec and not _key_missing(s)] \
        if isinstance(listed, list) else []


def get(role: str = "default") -> SparxLLM:
    spec = spec_for(role)
    if key_env := _key_missing(spec):
        raise FirstRunError(missing_key(key_env))
    return _model(spec)


async def complete(system: str, user: str, role: str = "fast") -> str:
    """One-shot text completion."""
    return await get(role).text(system, user)


async def complete_json(system: str, user: str, role: str = "fast") -> dict:
    """One-shot completion parsed as a JSON object ({} if the model did not return one)."""
    text = await get(role).text(system + "\nReply with one JSON object and nothing else.", user)
    return extract_json(text) or {}
