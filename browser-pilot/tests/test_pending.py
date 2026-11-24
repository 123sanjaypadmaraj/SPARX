import json
from datetime import datetime, timedelta

from sparx import config, db, pending, security, tools
from sparx.tools import Context, tool

from conftest import run_tool

RAN = []


@tool("tp_switch", "switch", {"name": {"type": "string"}, "on": {"type": "boolean"}}, ["name"], category="testing", risk="control")
def _switch(ctx, name, on=True):
    RAN.append((name, on))
    return f"{name} is {'on' if on else 'off'}"


@tool("tp_shell", "shell", {"cmd": {"type": "string"}}, ["cmd"], category="testing", risk="exec")
def _shell(ctx, cmd):
    RAN.append(cmd)
    return "ran"


@tool("tp_login", "login", {"site": {"type": "string"}, "password": {"type": "string"}}, ["site"], category="testing", risk="control")
def _login(ctx, site, password=""):
    RAN.append(site)
    return "logged in"


def auto_ctx():
    c = Context(session="job", agent="daemon")
    c.data["automated"] = True
    return c


def setup_function():
    RAN.clear()


async def test_automated_control_call_is_queued_once():
    text = await run_tool("tp_switch", auto_ctx(), name="fan", on=True)
    assert text.startswith("Not done:") and text.endswith("Queued as pending action #1.") and RAN == []
    assert (await run_tool("tp_switch", auto_ctx(), on=True, name="fan")).endswith("Queued as pending action #1.")
    (row,) = pending.rows()
    assert row["tool"] == "tp_switch" and row["args"] == {"name": "fan", "on": True}
    assert row["agent"] == "daemon" and row["status"] == "pending" and row["expires"] > row["ts"]
    assert (await run_tool("tp_switch", auto_ctx(), name="pump")).endswith("#2.")       # different arguments: its own row
    assert [a["summary"] for a in security.audit_log(tool="tp_switch")] == ["not approved"] * 3


async def test_what_is_never_queued(ctx):
    for name, args in (("tp_shell", {"cmd": "dir"}),                                     # exec is not kept for later
                       ("tp_login", {"site": "bank", "password": "hunter2"}),            # redacted: a credential
                       ("tp_switch", {"name": "ghp_abcdefghijklmnopqrstuvwx"}),          # redacted: a token in a value
                       ("robot_command", {"name": "rover", "action": "go"}),
                       ("home_security_set", {"target": "lock.front_door", "action": "unlock"})):
        text = await run_tool(name, auto_ctx(), **args)
        assert text.startswith("Not done:") and "Queued" not in text, name
    assert tools.REGISTRY["robot_command"].risk == "control" and tools.REGISTRY["home_security_set"].risk == "control"

    interactive = Context(session="chat")                    # nobody to ask, but not a background job
    assert "Queued" not in await run_tool("tp_switch", interactive, name="fan")

    async def no(tool, args):
        return False
    declined = Context(session="chat", approve=no)
    declined.data["automated"] = True
    assert "Queued" not in await run_tool("tp_switch", declined, name="fan")

    config.set_key("limits.pending_minutes", 0)              # 0 switches the queue off
    assert "Queued" not in await run_tool("tp_switch", auto_ctx(), name="fan")
    assert pending.rows("") == [] and RAN == []
    assert "No pending actions" in await run_tool("pending_list", ctx)


async def test_approving_runs_the_tool_and_marks_it_done(ctx):
    await run_tool("tp_switch", auto_ctx(), name="fan", on=False)
    (listed,) = json.loads(await run_tool("pending_list", ctx))
    assert listed["id"] == 1 and listed["tool"] == "tp_switch"
    out = await run_tool("pending_approve", ctx, id=1)
    assert "was run" in out and "fan is off" in out and RAN == [("fan", False)]
    assert ctx.asked == ["tp_switch"]                        # the normal approval prompt, for the stored tool
    row = pending.get(1)
    assert row["status"] == "done" and row["result"] == "fan is off"
    assert security.audit_log(tool="tp_switch")[0]["ok"] == 1
    assert "No pending actions" in await run_tool("pending_list", ctx)
    assert json.loads(await run_tool("pending_list", ctx, all=True))[0]["status"] == "done"
    assert "already done" in await run_tool("pending_approve", ctx, id=1) and len(RAN) == 1
    assert "No pending action 9" in await run_tool("pending_approve", ctx, id=9)


async def test_declined_approval_leaves_it_pending():
    await run_tool("tp_switch", auto_ctx(), name="fan")

    async def no(tool, args):
        return False
    out = await run_tool("pending_approve", Context(session="chat", approve=no), id=1)
    assert out.startswith("Not done:") and "the user declined" in out and RAN == []
    assert pending.get(1)["status"] == "pending"

    out = await run_tool("pending_approve", auto_ctx(), id=1)          # a job cannot approve its own action
    assert out.startswith("Not done:") and out.endswith("Queued as pending action #1.") and RAN == []
    assert [r["id"] for r in pending.rows()] == [1]


async def test_expired_entry_is_refused(ctx):
    assert config.settings()["limits"]["pending_minutes"] == 60
    await run_tool("tp_switch", auto_ctx(), name="fan")
    row = pending.get(1)
    assert timedelta(minutes=59) < datetime.fromisoformat(row["expires"]) - datetime.fromisoformat(row["ts"]) <= timedelta(minutes=60)
    db.x("UPDATE pending_actions SET expires = ? WHERE id = 1", ((datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"),))
    out = await run_tool("pending_approve", ctx, id=1)
    assert out.startswith("Error:") and "expired" in out and RAN == [] and ctx.asked == []
    assert pending.get(1)["status"] == "expired" and "expired" in await run_tool("pending_dismiss", ctx, id=1)
    assert (await run_tool("tp_switch", auto_ctx(), name="fan")).endswith("#2.")     # the same call can be queued afresh


async def test_dismiss(ctx):
    await run_tool("tp_switch", auto_ctx(), name="fan")
    assert "dismissed" in await run_tool("pending_dismiss", ctx, id=1)
    assert pending.get(1)["status"] == "dismissed" and pending.rows() == [] and RAN == []
    assert "already dismissed" in await run_tool("pending_approve", ctx, id=1)


async def test_hand_over_dismisses_the_matching_entry_with_a_pointer():
    await run_tool("tp_switch", auto_ctx(), name="fan", on=True)
    await run_tool("tp_switch", auto_ctx(), name="pump")
    assert pending.hand_over("tp_switch", {"on": True, "name": "fan"}, "Approve it with automation_resume(7).") == 1
    assert [r["args"]["name"] for r in pending.rows()] == ["pump"]
    row = pending.get(1)
    assert row["status"] == "dismissed" and row["result"] == "Approve it with automation_resume(7)."
    assert pending.hand_over("tp_switch", {"name": "fan", "on": True}, "again") == 0 and pending.get(1)["result"] != "again"
    assert pending.hand_over("tp_switch", '{"name": "pump"}', "x") == 1 and pending.rows() == [] and RAN == []


def _tap(ctx, x, y):
    RAN.append((x, y))
    return "tapped"


async def test_screen_position_actions_are_never_queued(monkeypatch):
    tools.load_all()                                         # a stand-in for the real tool: no phone in tests
    monkeypatch.setitem(tools.REGISTRY, "phone_tap", tools.Tool(
        "phone_tap", "tap", {"x": {"type": "integer"}, "y": {"type": "integer"}}, ["x", "y"], "testing", "control", False, 120, _tap))
    text = await run_tool("phone_tap", auto_ctx(), x=540, y=1200)
    assert text.startswith("Not done:") and "Queued" not in text and pending.rows("") == [] and RAN == []
    for name in ("phone_tap", "phone_tap_confirm", "phone_swipe", "phone_type", "phone_key", "phone_task",
                 "mouse_click", "key_type", "key_press"):
        t = tools.Tool(name, "", {}, [], "testing", "control", False, 120, _tap)
        assert pending.queue(t, {"x": 1}, "daemon") is None, name
    assert pending.rows("") == []
    assert (await run_tool("tp_switch", auto_ctx(), name="fan")).endswith("#1.")     # other control tools still are


async def test_approving_a_handed_over_entry_repeats_where_it_went(ctx):
    await run_tool("tp_switch", auto_ctx(), name="fan")
    pending.hand_over("tp_switch", {"name": "fan"}, "Approve it with automation_resume(7).")
    for name in ("pending_approve", "pending_dismiss"):
        out = await run_tool(name, ctx, id=1)
        assert out.startswith("Error:") and "already dismissed" in out and "Approve it with automation_resume(7)." in out
    assert RAN == [] and ctx.asked == []
