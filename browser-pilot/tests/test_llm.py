"""Backup models: a rate-limited or overloaded model hands the call to settings models.fallbacks."""
import asyncio
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

from browserpilot.llm import LLM
from sparx import config, llm


def limited(retry_after) -> openai.RateLimitError:
    headers = {} if retry_after is None else {"retry-after": str(retry_after)}
    return openai.RateLimitError("rate limited", body=None, response=httpx.Response(
        429, headers=headers, request=httpx.Request("POST", "http://test")))


@pytest.fixture
def provider(monkeypatch):
    """Patches the provider call. provider.plan[spec] is a list of results, each an Exception to raise or a reply
    text; the last entry repeats. provider.asked lists the specs called, in order."""
    state = SimpleNamespace(plan={}, asked=[], sent=[], slept=0.0)

    async def fake_create(self, **kw):
        state.asked.append(self.spec)
        state.sent.append(kw)
        steps = state.plan[self.spec]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1), text=step)

    async def sleep(seconds):       # waiting moves Sparx's clock forward instead of taking real time
        state.slept += seconds

    monkeypatch.setattr(LLM, "_create", fake_create)
    monkeypatch.setattr(llm, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(llm, "time", SimpleNamespace(time=lambda: time.time() + state.slept, strftime=time.strftime))
    monkeypatch.setattr(llm, "_cache", {})
    monkeypatch.setattr(llm, "_opencode_backup", lambda kw: None)      # never start the real OpenCode CLI
    monkeypatch.setattr(llm, "USAGE", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "ms": 0, "by_model": {}})
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return state


def ask(spec="openai/main"):
    return asyncio.run(llm._model(spec)._create(messages=[])).text


def test_backup_answers_while_primary_is_rate_limited(provider):
    config.save_settings({"models": {"fallbacks": ["groq/backup"]}})
    provider.plan = {"openai/main": [limited(60)], "groq/backup": ["from backup"]}
    assert ask() == "from backup"
    assert ask() == "from backup"
    assert provider.asked == ["openai/main", "groq/backup", "groq/backup"]      # the busy model is not asked again
    assert llm.USAGE["by_model"]["groq/backup"]["calls"] == 2 and "openai/main" not in llm.USAGE["by_model"]


def test_overloaded_model_also_fails_over(provider):
    config.save_settings({"models": {"fallbacks": ["groq/backup"]}})
    busy = openai.InternalServerError("high demand", body=None, response=httpx.Response(
        503, request=httpx.Request("POST", "http://test")))
    provider.plan = {"openai/main": [busy], "groq/backup": ["ok"]}
    assert ask() == "ok"


def test_backup_without_a_key_or_equal_to_the_model_is_ignored(provider):
    config.save_settings({"models": {"fallbacks": ["gemini/no-key", "openai/main", "groq/backup"]}})
    assert [m.spec for m in llm.backups("openai/main")] == ["groq/backup"]


def status(code: int, message: str) -> openai.APIStatusError:
    kind = openai.BadRequestError if code == 400 else openai.APIStatusError
    return kind(message, body=None, response=httpx.Response(code, request=httpx.Request("POST", "http://test")))


def test_without_backups_it_waits_and_retries(provider):
    provider.plan = {"openai/main": [limited(30), limited(30), "third time"]}
    assert ask() == "third time"
    assert provider.asked == ["openai/main"] * 3 and 55 <= provider.slept <= 65


def test_opencode_answers_a_plain_chat_when_every_model_is_rate_limited(provider, monkeypatch):
    provider.plan = {"openai/main": [limited(60)]}
    monkeypatch.setattr(llm, "_opencode_backup", lambda kw: "free answer")
    r = asyncio.run(llm._model("openai/main")._create(messages=[]))
    assert r.choices[0].message.content == "free answer" and r.choices[0].message.tool_calls is None
    assert provider.slept == 0
    with pytest.raises(openai.RateLimitError):     # a request that needs tools is never handed to a text-only backup
        asyncio.run(llm._model("openai/main")._create(messages=[], tools=[{"type": "function"}]))


def test_request_too_large_for_the_model_goes_to_a_backup(provider):
    config.save_settings({"models": {"fallbacks": ["groq/backup"]}})
    provider.plan = {"openai/main": [status(413, "request too large")], "groq/backup": ["roomier"]}
    assert ask() == "roomier" and provider.slept == 0
    provider.plan["groq/backup"] = [status(400, "no")]
    with pytest.raises(openai.BadRequestError):     # nobody can take it and nobody is busy: no waiting
        ask()
    assert provider.slept == 0
    config.save_settings({"models": {"fallbacks": []}})
    with pytest.raises(openai.APIStatusError, match="too large"):
        ask()


def test_gemini_gets_a_signature_on_tool_calls_other_models_made(provider, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    provider.plan = {"gemini/g": ["ok"], "groq/backup": ["ok"]}
    theirs = {"id": "a", "type": "function", "function": {"name": "now", "arguments": "{}"}}
    own = {**theirs, "id": "b", "extra_content": {"google": {"thought_signature": "real"}}}
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "tool_calls": [theirs, own]}]
    asyncio.run(llm._model("gemini/g")._create(messages=messages))
    sent = provider.sent[-1]["messages"][1]["tool_calls"]
    assert sent[0]["extra_content"] == llm.UNSIGNED and sent[1] == own
    assert "extra_content" not in theirs                 # the conversation itself is not changed
    asyncio.run(llm._model("groq/backup")._create(messages=messages))
    assert provider.sent[-1]["messages"] is messages     # other providers get it as it is


def test_a_wait_longer_than_the_limit_raises_at_once(provider):
    config.save_settings({"models": {"fallbacks": ["groq/backup"]}})
    provider.plan = {"openai/main": [limited(3600)], "groq/backup": [limited(3600)]}
    with pytest.raises(openai.RateLimitError):
        ask()
    assert provider.asked == ["openai/main", "groq/backup"]


def test_backup_that_rejects_the_conversation_is_skipped(provider):
    config.save_settings({"models": {"fallbacks": ["groq/backup"]}})
    provider.plan = {"openai/main": [limited(10), "main again"], "groq/backup": [status(400, "unknown field")]}
    assert ask() == "main again" and 9 <= provider.slept <= 11


def test_primary_that_is_down_falls_back_when_a_backup_exists(provider):
    config.save_settings({"models": {"fallbacks": ["groq/backup"]}})
    provider.plan = {"openai/main": [llm.FirstRunError("Cannot reach openai")], "groq/backup": ["local"]}
    assert ask() == "local"
    provider.plan["openai/main"] = [status(530, "tunnel gone")]
    assert ask() == "local"


def test_primary_that_is_down_with_no_backup_raises(provider):
    config.save_settings({"models": {"fallbacks": []}})
    provider.plan = {"openai/main": [llm.FirstRunError("Cannot reach openai")]}
    with pytest.raises(llm.FirstRunError):
        ask()


def test_custom_backup_without_a_base_url_is_ignored(provider, monkeypatch):
    monkeypatch.delenv("BROWSERPILOT_BASE_URL", raising=False)
    config.save_settings({"models": {"fallbacks": ["custom/qwen2.5:14b", "groq/backup"]}})
    assert [m.spec for m in llm.backups("openai/main")] == ["groq/backup"]
