import pytest

from sparx import config, tools
from sparx.agent import Agent
from sparx.tools import subagent
from conftest import run_tool


def test_default_is_read_only():
    tools.load_all()
    names = subagent.default_tools()
    assert names and all(tools.REGISTRY[n].risk == "read" for n in names if n in tools.REGISTRY)
    assert not names & subagent.BARRED


async def test_isolation_and_report(ctx, fake_llm):
    parent_hist = [{"role": "user", "content": "secret parent context"}]
    before = list(parent_hist)
    fake_llm.script = [("now", {}), "final report: ok " + "x" * 5000]
    out = await run_tool("subagent_run", ctx, task="find the time")
    assert parent_hist == before
    first = fake_llm.seen[0]
    assert [m["role"] for m in first] == ["system", "user"] and first[1]["content"] == "find the time"
    assert out.startswith("final report") or "final report" in out
    assert len(out) < 4000 and "more characters cut" in out      # capped; tool transcript not returned


async def test_allow_list_enforced(ctx, fake_llm):
    tools.load_all()
    fake_llm.script = [("memory_forget" if "memory_forget" in tools.REGISTRY else "file_write", {}), "done"]
    await run_tool("subagent_run", ctx, task="x")
    tool_msgs = [m for m in fake_llm.seen[1] if m["role"] == "tool"]
    assert "not one of your tools" in tool_msgs[0]["content"]


async def test_explicit_write_tool_still_needs_approval(ctx, fake_llm, monkeypatch):
    tools.load_all()
    w = tools.REGISTRY["note_add"] if "note_add" in tools.REGISTRY else next(
        t for t in tools.REGISTRY.values() if t.risk == "write" and t.category == "memory")
    asked = []

    async def deny(t, a):
        asked.append(t.name)
        return False
    ctx.approve = deny
    monkeypatch.setattr("sparx.security.permission", lambda risk, name: "ask" if risk == "write" else "allow")
    fake_llm.script = [(w.name, {"text": "x", "content": "x", "title": "x", "fact": "x"}), "pending"]
    await run_tool("subagent_run", ctx, task="x", tools=[w.name])
    assert asked == [w.name]   # the registry asked, and the declined call did not run


async def test_unknown_tool_and_no_nesting(ctx, fake_llm):
    assert "Unknown tool" in await run_tool("subagent_run", ctx, task="x", tools=["nope"])
    fake_llm.script = [("subagent_run", {"task": "again"}), "done"]
    await run_tool("subagent_run", ctx, task="x", tools=["subagent_run"])      # barred from the allow-list
    assert "not one of your tools" in [m for m in fake_llm.seen[1] if m["role"] == "tool"][0]["content"]
    # even if called directly from a subagent context
    child = ctx.child("subagent:1")
    assert "cannot start another subagent" in await run_tool("subagent_run", child, task="y")


async def test_parallel_cap(ctx, fake_llm, monkeypatch):
    s = config.settings()
    s["limits"]["max_agents"] = 2
    monkeypatch.setattr(config, "settings", lambda: s)
    fake_llm.script = ["r1", "r2"]
    out = await run_tool("subagent_run_many", ctx, tasks=["a", "b", "c"])
    import json
    rows = json.loads(out[out.index("["):out.rindex("]") + 1])
    assert len(rows) == 3 and "Not started" in rows[2]["report"]
    assert {rows[0]["report"], rows[1]["report"]} == {"r1", "r2"}
