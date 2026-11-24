import asyncio
import signal

import pytest

from sparx import cli, config, db, planmode, tools
from sparx.assistant import Sparx


@pytest.fixture(autouse=True)
def reg():
    tools.load_all()
    ran = []

    @tools.tool("pm_read", "r", category="core", risk="read")
    def pm_read(ctx):
        return "read ok"

    @tools.tool("pm_write", "w", category="core", risk="write")
    def pm_write(ctx):
        ran.append(1)
        return "written"
    yield ran
    tools.REGISTRY.pop("pm_read", None)
    tools.REGISTRY.pop("pm_write", None)


async def test_read_allowed_others_refused(ctx, reg):
    await tools.call("plan_enter", {}, ctx)
    assert await tools.call("pm_read", {}, ctx) == "read ok"
    out = await tools.call("pm_write", {}, ctx)
    assert "plan mode" in out and "plan_exit" in out and reg == []


def test_every_non_read_tool_is_refused_regardless_of_policy(ctx):
    config.save_settings({"permissions": {r: "allow" for r in ("write", "exec", "control", "delete", "send", "spend")}})
    ctx.data["plan_mode"] = True
    n = 0
    for t in tools.REGISTRY.values():
        if t.name in planmode.EXEMPT:
            continue
        assert (planmode.refusal(ctx, t) is None) == (t.risk == "read"), t.name
        n += t.risk != "read"
    assert n > 20


async def test_no_bypass_shell_and_subagent(ctx):
    await tools.call("plan_enter", {}, ctx)
    for c in (ctx, ctx.child("sub")):
        assert "plan mode" in await tools.call("shell_run", {"command": "echo hi"}, c)
    assert "plan mode" in await tools.call("pm_write", {}, ctx.child("sub"))


async def test_exit_requires_approval(reg):
    async def no(t, a):
        return False
    for c in (tools.Context(approve=no), tools.Context()):
        await tools.call("plan_enter", {}, c)
        assert "did not approve" in await tools.call("plan_exit", {"plan": "do x"}, c)
        assert planmode.active(c)
        assert "plan mode" in await tools.call("pm_write", {}, c)


async def test_exit_approved(ctx, reg):
    await tools.call("plan_enter", {}, ctx)
    assert "approved" in await tools.call("plan_exit", {"plan": "the plan"}, ctx)
    assert ctx.asked == ["plan_exit"]
    assert await tools.call("pm_write", {}, ctx) == "written"


async def test_setting_turns_it_on(ctx):
    config.save_settings({"plan_mode": True})
    assert "plan mode" in await tools.call("pm_write", {}, ctx)


async def test_sparx_plan_flag_and_slash(fake_llm):
    fake = fake_llm
    fake.script = [("pm_write", {}), "plan: ..."]
    bot = Sparx()
    assert "ON" in cli._command(bot, "/plan")
    await bot.turn("do it")
    assert "plan mode" in fake.seen[1][-1]["content"]
    assert "off" in cli._command(bot, "/plan")


async def test_interruptible_cancels_only_the_work():
    async def slow():
        await asyncio.sleep(30)
    asyncio.get_running_loop().call_later(0.1, signal.raise_signal, signal.SIGINT)
    with pytest.raises(cli.Interrupted):
        await cli.interruptible(slow())
    assert await cli.interruptible(asyncio.sleep(0, "fine")) == "fine"


async def test_interrupted_turn_keeps_note_in_history(fake_llm):
    async def slow(m, t):
        await asyncio.sleep(30)
    fake_llm.chat = slow
    bot = Sparx()
    asyncio.get_running_loop().call_later(0.2, signal.raise_signal, signal.SIGINT)
    with pytest.raises(cli.Interrupted):
        await cli.interruptible(bot.turn("long job"))
    rows = db.q("SELECT role, content FROM messages ORDER BY id")
    assert [r["role"] for r in rows] == ["user", "assistant"] and rows[1]["content"].startswith("(Cancelled")
