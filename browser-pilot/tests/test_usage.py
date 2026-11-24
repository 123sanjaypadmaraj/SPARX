"""Persistent LLM usage. fake_llm replaces llm.get(), so it bypasses SparxLLM._create where usage is counted;
the recording path is therefore tested through llm.record_usage and through SparxLLM._create with the provider call patched."""
import asyncio
import json
from types import SimpleNamespace

from browserpilot.llm import LLM
from sparx import db, llm, tools
from conftest import run_tool

tools.load_all()


def rows():
    return db.q("SELECT * FROM llm_usage ORDER BY model")


def test_two_calls_one_row_and_second_model():
    llm.record_usage("a/m1", 10, 5, 100)
    llm.record_usage("a/m1", 20, 7, 50)
    llm.record_usage("b/m2", 1, 1)
    r = rows()
    assert len(r) == 2
    assert (r[0]["model"], r[0]["calls"], r[0]["prompt_tokens"], r[0]["completion_tokens"], r[0]["ms"]) == ("a/m1", 2, 30, 12, 150)
    assert r[1]["model"] == "b/m2" and r[1]["calls"] == 1


def test_survives_restart():
    llm.record_usage("a/m1", 10, 5)
    saved = {k: v for k, v in llm.USAGE.items()}
    llm.USAGE.update(calls=0, prompt_tokens=0, completion_tokens=0, ms=0, by_model={})
    h = llm.usage_history(7)
    llm.USAGE.update(saved)
    assert h["totals"]["calls"] == 1 and h["totals"]["prompt_tokens"] == 10 and h["rows"][0]["model"] == "a/m1"


def _model(monkeypatch, usage):
    async def fake_create(self, **kw):
        return SimpleNamespace(usage=usage)
    monkeypatch.setattr(LLM, "_create", fake_create)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    return llm.SparxLLM("openai/gpt-test")


def test_create_records_and_keeps_in_memory_usage(monkeypatch):
    m = _model(monkeypatch, SimpleNamespace(prompt_tokens=11, completion_tokens=4))
    before = llm.USAGE["calls"]
    asyncio.run(m._create(messages=[]))
    assert llm.USAGE["calls"] == before + 1 and llm.USAGE["by_model"][m.spec]["prompt_tokens"] >= 11
    r = rows()[0]
    assert (r["calls"], r["prompt_tokens"], r["completion_tokens"], r["unreported_calls"]) == (1, 11, 4, 0)
    llm.USAGE["by_model"].pop(m.spec, None)


def test_provider_without_counts_is_marked_unreported(monkeypatch):
    m = _model(monkeypatch, None)
    asyncio.run(m._create(messages=[]))
    r = rows()[0]
    assert (r["calls"], r["prompt_tokens"], r["unreported_calls"]) == (1, 0, 1)
    llm.USAGE["by_model"].pop(m.spec, None)


def test_db_failure_does_not_break_call(monkeypatch, home):
    m = _model(monkeypatch, SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "x", boom)
    r = asyncio.run(m._create(messages=[]))
    assert r.usage.prompt_tokens == 1
    assert "disk full" in (home / "logs" / "errors.log").read_text(encoding="utf-8")
    llm.USAGE["by_model"].pop(m.spec, None)


def test_usage_report_tool(ctx, fake_llm):
    assert "No model calls" in asyncio.run(run_tool("usage_report", ctx))
    llm.record_usage("a/m1", 10, 5)
    llm.record_usage("a/m1", 0, 0, 0, reported=False)
    out = json.loads(asyncio.run(run_tool("usage_report", ctx, days=3)))
    assert out["totals"]["calls"] == 2 and out["totals"]["prompt_tokens"] == 10 and out["totals"]["unreported_calls"] == 1
    assert "no token counts" in out["note"] and "not estimated" in out["note"]


def test_cli_usage_includes_the_last_30_days():
    from sparx import cli
    llm.record_usage("a/m1", 10, 5, 100)
    out = json.loads(cli._command(SimpleNamespace(last_ctx=None), "/usage"))
    assert out["last_30_days"]["prompt_tokens"] == 10 and out["last_30_days"]["calls"] == 1
    assert "calls" in out["llm"] and out["tools"] == {} and "last 30 days" in cli.HELP
