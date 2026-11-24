"""First-run messages: sparx doctor, a missing key, a model that is not there. No network and no model."""
import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import openai
import pytest

from sparx import cli, config, llm, tools
from sparx.tools import phone

KEYS = [k for _b, k, _d in config.bp.PROVIDERS.values() if k]


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for k in KEYS + ["SPARX_MODEL"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(phone, "_exe", lambda: (_ for _ in ()).throw(tools.ToolError("adb is not installed.")))
    monkeypatch.setattr(cli, "_ollama_models", lambda base, timeout=2: ["gpt-oss:latest"])


def test_providers_line_without_keys_says_so():
    assert "providers   (no API keys set)" in cli.doctor()


def test_providers_line_lists_keyed_providers(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "secret-value-1")
    out = cli.doctor()
    assert "providers   groq" in out and "secret-value-1" not in out


def _down(base, timeout=2):
    raise OSError("refused")


def test_ollama_not_running(monkeypatch):
    monkeypatch.setattr(cli, "_ollama_models", _down)
    assert "Ollama is not running: start it, or set a key for a hosted model" in cli.doctor()


def test_ollama_model_not_pulled(monkeypatch):
    monkeypatch.setattr(cli, "_ollama_models", lambda base, timeout=2: ["llama3:latest"])
    assert "model gpt-oss:latest is not pulled: ollama pull gpt-oss:latest" in cli.doctor()


def test_ollama_model_ok():
    assert "model check ok" in cli.doctor()


def test_configured_model_without_key_names_the_variable():
    config.set_key("models.default", "groq/openai/gpt-oss-120b")
    out = cli.doctor()
    assert "GROQ_API_KEY is not set" in out and str(config.HOME / ".env") in out


def test_fallback_is_announced_with_the_missing_key(monkeypatch):
    monkeypatch.setattr(config.bp, "DEFAULT_MODEL", "groq/openai/gpt-oss-120b")
    out = cli.doctor()
    assert "  fallback   " in out and "GROQ_API_KEY" in out and str(config.HOME / ".env") in out


def test_no_fallback_notice_when_ollama_chosen():
    config.set_key("models.default", "ollama/gpt-oss:latest")
    assert "  fallback   " not in cli.doctor()


def test_hosted_model_with_key_is_not_asked_about_ollama(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    config.set_key("models.default", "groq/openai/gpt-oss-120b")
    monkeypatch.setattr(cli, "_ollama_models", _down)
    out = cli.doctor()
    assert "Ollama is not running" not in out and "  fallback   " not in out


def test_doctor_does_not_touch_settings(monkeypatch, tmp_path):
    adb = tmp_path / "adb.exe"
    adb.write_text("")
    monkeypatch.setattr(phone, "_exe", lambda: str(adb))
    monkeypatch.setattr(phone, "_exec", lambda cmd, timeout: (0, b"List of devices attached\nSER123\tdevice\n", b""))
    f = config.path("settings.json")
    f.write_text(json.dumps({"name": "Sparx"}), encoding="utf-8")
    before = f.read_bytes()
    cli.doctor()
    assert f.read_bytes() == before


def _boom(exc):
    async def turn(self, text):
        raise exc
    return turn


def test_missing_key_raises_the_actionable_sentence(monkeypatch):
    config.set_key("models.fast", "groq/openai/gpt-oss-120b")
    with pytest.raises(llm.FirstRunError) as e:
        llm.get("fast")
    assert "GROQ_API_KEY is not set" in str(e.value) and str(config.HOME / ".env") in str(e.value)


def _provider(monkeypatch, exc, spec="ollama/gpt-oss:latest"):
    async def fake_create(self, **kw):
        raise exc
    monkeypatch.setattr(llm.LLM, "_create", fake_create)
    return llm.SparxLLM(spec)


def test_connection_refused_becomes_ollama_sentence(monkeypatch):
    m = _provider(monkeypatch, openai.APIConnectionError(request=httpx.Request("POST", "http://x")))
    with pytest.raises(llm.FirstRunError, match="Ollama is not running: start it, or set a key for a hosted model"):
        asyncio.run(m.text("s", "u"))


def test_model_not_found_becomes_pull_sentence(monkeypatch):
    resp = httpx.Response(404, request=httpx.Request("POST", "http://x"))
    m = _provider(monkeypatch, openai.NotFoundError("model not found", response=resp, body=None))
    with pytest.raises(llm.FirstRunError, match="model gpt-oss:latest is not pulled: ollama pull gpt-oss:latest"):
        asyncio.run(m.text("s", "u"))


def test_other_errors_are_untouched(monkeypatch):
    m = _provider(monkeypatch, ValueError("odd"))
    with pytest.raises(ValueError):
        asyncio.run(m.text("s", "u"))


@pytest.mark.parametrize("msg", ["Ollama is not running: start it, or set a key for a hosted model",
                                 "GROQ_API_KEY is not set: put it in x", "model m is not pulled: ollama pull m"])
def test_ask_prints_the_sentence_alone(monkeypatch, fake_llm, capsys, msg):
    from sparx.assistant import Sparx
    monkeypatch.setattr(Sparx, "turn", _boom(llm.FirstRunError(msg)))
    with pytest.raises(SystemExit) as e:
        asyncio.run(cli.ask("hi", True))
    assert e.value.code == msg


def test_chat_prints_the_sentence_alone(monkeypatch, capsys):
    from sparx.assistant import Sparx
    monkeypatch.setattr(Sparx, "turn", _boom(llm.FirstRunError("Ollama is not running: x")))
    lines = iter(["hello", EOFError])

    def fake_input(prompt=""):
        v = next(lines)
        if v is EOFError:
            raise EOFError
        return v
    monkeypatch.setattr("builtins.input", fake_input)
    asyncio.run(cli.chat())
    out = capsys.readouterr().out
    assert "Ollama is not running: x" in out and "FirstRunError" not in out
