import pytest

from sparx import loopguard, tools
from sparx.agent import Agent
from sparx.loopguard import LoopGuard
from sparx.tools import ToolError, category, tool

category("testlg", "loopguard test tools")
state = {"n": 0}


@tool("lg_read", "read", {"path": {"type": "string"}}, ["path"], category="testlg")
def lg_read(ctx, path: str):
    return f"content of {path}"


@tool("lg_edit", "edit", {"old": {"type": "string"}}, ["old"], category="testlg")
def lg_edit(ctx, old: str):
    raise ToolError("old string not found in file")


@tool("lg_tick", "tick", category="testlg")
def lg_tick(ctx):
    state["n"] += 1
    return f"tick {state['n']}"


def agent():
    return Agent("t", "sys", tool_names={"lg_read", "lg_edit", "lg_tick"})


def test_guard_units():
    g = LoopGuard()
    assert [g.observe("a", {"x": 1}, "r")[0] for _ in range(2)] == ["ok", "ok"]
    assert g.observe("a", {"x": 1}, "r")[0] == "nudge"
    assert g.observe("a", {"x": 1}, "r")[0] == "ok"            # nudged once, not on every call
    for _ in range(2):
        lvl = g.observe("a", {"x": 1}, "r")[0]
    assert lvl == "stop"


def test_hints():
    assert "re-read" in loopguard.hint("Error: old string not found")
    assert "PATH" in loopguard.hint("Error: 'foo' is not recognized as an internal command")
    assert loopguard.hint("fine") == ""


async def test_repeat_is_nudged_then_stopped(ctx, fake_llm):
    fake_llm.script = [("lg_read", {"path": "a"})] * 10
    out = await agent().run("go", ctx)
    assert "going in circles" in out
    assert any("[system note]" in str(m.get("content")) for m in fake_llm.seen[-1])
    assert len(fake_llm.seen) == 6                              # stopped at the 6th identical call


async def test_failing_edit_gets_hint_and_stops(ctx, fake_llm):
    fake_llm.script = [("lg_edit", {"old": f"v{i}"}) for i in range(10)]
    out = await agent().run("go", ctx)
    assert "going in circles" in out and "failed the same way" in out
    assert any("Hint: " in str(m.get("content")) and "re-read" in str(m.get("content")) for m in fake_llm.seen[-1])


async def test_alternation(ctx, fake_llm):
    ab = [("lg_read", {"path": "a"}), ("lg_read", {"path": "b"})]
    fake_llm.script = ab * 6
    out = await agent().run("go", ctx)
    assert "alternating" in out


async def test_no_false_positive(ctx, fake_llm):
    fake_llm.script = [("lg_read", {"path": f"f{i}"}) for i in range(8)] + [("lg_tick", {})] * 8 + ["all done"]
    a = agent()
    a.max_rounds = 30
    assert await a.run("go", ctx) == "all done"                 # different files; same call but changing results
    assert not any("[system note]" in str(m.get("content")) for m in fake_llm.seen[-1])


async def test_disabled(ctx, fake_llm, monkeypatch):
    from sparx import config
    s = config.settings()
    s["limits"]["loop_guard"] = False
    monkeypatch.setattr(config, "settings", lambda: s)
    fake_llm.script = [("lg_read", {"path": "a"})] * 8 + ["done"]
    a = agent()
    a.max_rounds = 20
    assert await a.run("go", ctx) == "done"
