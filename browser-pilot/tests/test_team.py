import asyncio
import json
import re

import pytest

from browserpilot.llm import Call, Reply
from sparx import llm, security
from sparx.agents import team
from sparx.tools import Context, tool

from conftest import run_tool

FILES = {}


@tool("tt_write", "write a note", {"name": {"type": "string"}}, ["name"], category="testteam", risk="write")
def _write(ctx, name):
    FILES[name] = True
    return f"wrote {name}"


@tool("tt_exists", "does a note exist", {"name": {"type": "string"}}, ["name"], category="testteam")
def _exists(ctx, name):
    return {"exists": name in FILES}


@tool("tt_send", "send", {"to": {"type": "string"}}, ["to"], category="testteam", risk="send")
def _send(ctx, to):
    return f"sent to {to}"


def ev(claim, source="https://example.org/a", quote="q"):
    return {"claim": claim, "source": source, "quote": quote}


class TeamLLM:
    """Routes each call by agent and subtask, so parallel runs stay deterministic.

    work: {"research:1": [items], "execution:2": [...], "verify:1": [...]}; an item is text, a dict (sent as JSON)
    or (tool, args). Defaults: workers answer "done", the verifier passes."""
    spec = "fake/team"

    def __init__(self, plan, work=None, quality=None, conflicts=None, replan=None):
        self.plan, self.quality, self.conflicts, self.replan = plan, quality or {}, conflicts or {}, replan or {}
        self.work = {k: list(v) for k, v in (work or {}).items()}
        self.log, self.roles, self.prompts = [], [], {}
        self.active = self.peak = 0

    async def chat(self, messages, schemas):
        system, task = messages[0]["content"], messages[1]["content"]
        kind = "verify" if "verification agent" in system else "research" if "research agent" in system else "execution"
        key = f"{kind}:{re.search(r'Subtask (\S+):', task)[1]}"
        if messages[-1]["role"] == "user":
            self.log.append(key)
            self.prompts.setdefault(key, []).append(task)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        script = self.work.get(key)
        item = script.pop(0) if script else ({"verdict": "pass", "reasons": []} if kind == "verify" else "done")
        if isinstance(item, tuple):
            return Reply("", [Call(f"c{len(self.log)}", item[0], json.dumps(item[1]))], {"role": "assistant", "content": ""})
        text = item if isinstance(item, str) else json.dumps(item)
        return Reply(text, [], {"role": "assistant", "content": text})

    def result_message(self, call, text):
        return {"role": "tool", "tool_call_id": call.id, "content": text}

    async def text(self, system, user):
        out = (self.replan if "Some subtasks failed" in system else self.plan if "orchestrator" in system
               else self.conflicts if "contradict" in system else self.quality)
        self.log.append("replan" if out is self.replan else "plan" if out is self.plan
                        else "conflict" if out is self.conflicts else "quality")
        return json.dumps(out)


@pytest.fixture
def team_llm(monkeypatch):
    def install(*args, **kw):
        fake = TeamLLM(*args, **kw)
        monkeypatch.setattr(llm, "get", lambda role="default": (fake.roles.append(role), fake)[1])
        return fake
    FILES.clear()
    return install


def sub(id, agent="research", deps=(), **kw):
    return {"id": id, "title": f"task {id}", "instructions": f"do {id}", "agent": agent, "depends_on": list(deps), **kw}


def test_plan_parsing_and_dependency_order():
    plan = team.normalise_plan({"subtasks": [
        {"id": 1, "title": "A", "agent": "Research"}, {"id": 2, "instructions": "B", "agent": "writer", "depends_on": "1"},
        {"id": 3, "title": "C", "depends_on": [1, 2, 9, 3]}, "junk", {"id": 4, "title": "D", "depends_on": [5]},
        {"id": 5, "title": "E", "depends_on": [4]}]})
    assert [(t["id"], t["agent"], t["depends_on"]) for t in plan[:3]] == \
        [("1", "research", []), ("2", "execution", ["1"]), ("3", "execution", ["1", "2"])]
    assert plan[3]["depends_on"] == plan[4]["depends_on"] == []          # the 4 <-> 5 cycle is broken
    assert team.waves(plan)[:3] == [["1", "4", "5"], ["2"], ["3"]]
    assert team.normalise_plan("nonsense") == []


def test_model_routing():
    assert team.route({"title": "Summarise the article", "instructions": "", "complexity": ""}, "research") == "fast"
    assert team.route({"title": "Design the schema", "instructions": "compare options", "complexity": ""}, "execution") == "smart"
    assert team.route({"title": "Write the file", "instructions": "save it", "complexity": ""}, "execution") == "default"
    assert team.route({"title": "x", "instructions": "y", "complexity": "high"}, "research") == "smart"


async def test_parallel_execution_dependencies_and_evidence(ctx, team_llm):
    fake = team_llm(
        {"subtasks": [sub("1"), sub("2"), sub("3", "execution", ["1", "2"], needs=["testteam"], done_when="note exists")]},
        {"research:1": [{"summary": "found A", "evidence": [ev("A is 1")]}],
         "research:2": [{"summary": "found B", "evidence": [ev("B is 2", "https://example.org/b")]}],
         "execution:3": [("tt_write", {"name": "out"}), {"summary": "wrote out"}],
         "verify:3": [("tt_exists", {"name": "out"}), {"verdict": "pass", "reasons": ["file exists"]}]},
        quality={"achieved": True, "missing": [], "claims": [
            {"text": "A is 1 and B is 2", "evidence": ["E1", 2]}, {"text": "C is probably 3", "evidence": []},
            {"text": "D is 4", "evidence": [99]}]})
    r = await team.run_team("find A and B then write them", ctx)

    assert fake.peak >= 2                                                # 1 and 2 ran side by side
    assert max(fake.log.index("verify:1"), fake.log.index("verify:2")) < fake.log.index("execution:3")
    assert "found A" in fake.prompts["execution:3"][0] and "found B" in fake.prompts["execution:3"][0]
    assert "found A" in fake.prompts["verify:1"][0] and "do 1" in fake.prompts["verify:1"][0]
    assert [t["status"] for t in r["tasks"]] == ["passed"] * 3 and r["success"] and FILES == {"out": True}
    assert r["findings"] == [{"text": "A is 1 and B is 2", "evidence": [1, 2]}]
    assert r["unverified"] == ["C is probably 3", "D is 4"]              # listed, not dropped, not stated as fact
    assert "A is 1 and B is 2 [E1][E2]" in r["answer"] and "## Unverified" in r["answer"]
    assert "E2: https://example.org/b" in r["answer"]
    assert "smart" in fake.roles and r["metrics"]["tool_calls"] == 2 and r["metrics"]["retries"] == 0
    assert r["metrics"]["agents"]["research"] == {"attempts": 2, "passed": 2}
    st = team.status(r["run_id"])
    assert st["status"] == "done" and st["evidence"] == 2 and st["messages"] > 6


async def test_verifier_fail_then_retry_passes(ctx, team_llm):
    fake = team_llm({"subtasks": [sub("1")]},
                    {"research:1": [{"summary": "weak", "evidence": [ev("X is 5", quote="")]},
                                    {"summary": "better", "evidence": [ev("X is 6", quote="X is 6")]}],
                     "verify:1": [{"verdict": "fail", "reasons": ["quote missing"]}, {"verdict": "pass"}]})
    r = await team.run_team("what is X", ctx)
    t = r["tasks"][0]
    assert (t["status"], t["attempts"], r["metrics"]["retries"]) == ("passed", 2, 1)
    assert "quote missing" in fake.prompts["research:1"][1]
    assert [e["claim"] for e in r["evidence"]] == ["X is 6"]             # the rejected attempt's evidence is not used
    assert "X is 6 [E2]" in r["answer"]


async def test_failure_is_reassigned_then_reported(ctx, team_llm):
    fake = team_llm({"subtasks": [sub("1"), sub("2", "execution", ["1"])]},
                    {"verify:1": [{"verdict": "fail", "reasons": ["no"]}] * 3})
    r = await team.run_team("impossible thing", ctx)
    assert [k for k in fake.log if k.endswith(":1") and not k.startswith("verify")] == ["research:1"] * 2 + ["execution:1"]
    assert [(t["status"], t["attempts"]) for t in r["tasks"]] == [("failed", 3), ("blocked", 0)]
    assert not r["success"] and "## What failed" in r["answer"] and "replan" in fake.log


async def test_claims_without_source_are_unverified(ctx, team_llm):
    team_llm({"subtasks": [sub("1")]},
             {"research:1": [{"summary": "s", "evidence": [ev("sourced"), {"claim": "no source"}]}],
              "verify:1": [{"verdict": "pass", "unsupported": ["also unsupported"]}]})
    r = await team.run_team("g", ctx)
    assert r["unverified"] == ["no source", "also unsupported"]
    assert [f["text"] for f in r["findings"]] == ["sourced"]


async def test_conflict_is_surfaced(ctx, team_llm):
    team_llm({"subtasks": [sub("1"), sub("2")]},
             {"research:1": [{"summary": "a", "evidence": [ev("Price is 10")]}],
              "research:2": [{"summary": "b", "evidence": [ev("Price is 12", "https://example.org/b")]}]},
             conflicts={"conflicts": [{"evidence": [1, 2], "about": "the price"}, {"evidence": [1, 77], "about": "bogus"}]},
             quality={"achieved": False, "missing": ["a confirmed price"], "claims": []})
    r = await team.run_team("what is the price", ctx)
    assert r["conflicts"] == [{"about": "the price", "evidence": [1, 2], "claims": ["Price is 10", "Price is 12"]}]
    assert '[E1] "Price is 10" vs [E2] "Price is 12"' in r["answer"] and "a confirmed price" in r["answer"]
    assert not r["achieved"]


async def test_budget_stop_gives_partial_result(ctx, team_llm):
    ctx.max_tool_calls = 2
    team_llm({"subtasks": [sub("1", "execution", needs=["testteam"]), sub("2", "execution", ["1"], needs=["testteam"])]},
             {"execution:1": [("tt_write", {"name": "a"}), ("tt_write", {"name": "b"}), ("tt_write", {"name": "c"}), "x"]})
    r = await team.run_team("write lots", ctx)
    assert r["status"] == "stopped" and r["partial"] and "Tool-call limit" in r["stopped"]
    assert [t["status"] for t in r["tasks"]] == ["stopped", "stopped"] and FILES == {"a": True, "b": True}
    assert "partial result" in r["answer"] and not r["success"]


async def test_emergency_stop_returns_partial(ctx, team_llm):
    fake = team_llm({"subtasks": [sub("1")]})
    security.request_stop()
    r = await team.run_team("anything", ctx)
    assert r["status"] == "stopped" and r["tasks"] == [] and fake.log == []


async def test_pending_approvals_are_reported(team_llm):
    fake = team_llm({"subtasks": [sub("1", "execution", needs=["testteam"])]},
                    {"execution:1": [("tt_send", {"to": "boss@x.y"}), {"summary": "could not send"}]})
    r = await team.run_team("email the boss", Context(session="t", approve=None))
    assert r["pending_approvals"][0]["tool"] == "tt_send" and r["pending_approvals"][0]["task"] == "1"
    assert r["tasks"][0]["status"] == "needs_approval" and not r["success"]
    assert "## Needs your approval" in r["answer"] and "tt_send [send]" in r["answer"]
    assert fake.log.count("execution:1") == 1 and "verify:1" not in fake.log     # not retried


async def test_workflows_feedback_and_tools(ctx, team_llm):
    fake = team_llm({"subtasks": [sub("1"), sub("2", "execution", ["1"])]})
    out = await run_tool("team_run", ctx, goal="weekly sales report for the north region")
    run_id = json.loads(out)["run_id"]
    assert '"name": "sales"' in await run_tool("team_workflow_save", ctx, run_id=run_id, name="sales")

    r = await team.run_team("weekly sales report for the south region", ctx)   # similar goal: reuses the template
    assert fake.log.count("plan") == 1 and [t["id"] for t in r["tasks"]] == ["1", "2"]
    assert "north region" in fake.prompts["research:1"][1]
    await run_tool("team_feedback", ctx, run_id=r["run_id"], correction="Use INR, not USD")
    w = team.get_workflow("sales")
    assert (w["runs"], w["success_rate"], w["corrections"]) == (2, 0.5, ["Use INR, not USD"])

    r = await team.run_workflow("sales", ctx, goal="sales report for {region}", inputs={"region": "east"})
    assert r["goal"] == "sales report for east" and "Use INR, not USD" in fake.prompts["research:1"][2]
    await team.run_team("book a dentist appointment", ctx)                        # unrelated: planned afresh
    assert fake.log.count("plan") == 2
    team.feedback(r["run_id"], "wrong")
    assert team.get_workflow("sales")["success_rate"] == 0.33
    assert team.pick_workflow("weekly sales report for the west region") is None  # success rate fell below half
    m = json.loads(await run_tool("agent_metrics", ctx))
    assert m["runs"] == 4 and m["agents"]["research"]["attempts"] >= 4
    assert "only a run that succeeded" in await run_tool("team_workflow_save", ctx, run_id=r["run_id"], name="x")
    assert "sales" in await run_tool("team_workflow_list", ctx) and "weekly sales" in await run_tool("team_runs", ctx)


# ---- audit: team tools no test named, and budgets on the team paths

async def test_team_status_tool(ctx, team_llm):
    assert await run_tool("team_status", ctx, run_id=99) == "No team run 99."
    team_llm({"subtasks": [sub("1"), sub("2", "execution", ["1"])]},
             {"research:1": [{"summary": "found", "evidence": [ev("A is 1")]}]})
    run_id = json.loads(await run_tool("team_run", ctx, goal="find A then act"))["run_id"]
    st = json.loads(await run_tool("team_status", ctx, run_id=str(run_id)))      # a numeric string is accepted
    assert (st["id"], st["status"], st["success"], st["evidence"]) == (run_id, "done", 1, 1)
    assert [(t["id"], t["agent"], t["status"], t["verdict"]) for t in st["tasks"]] == \
        [("1", "research", "passed", "pass"), ("2", "execution", "passed", "pass")]
    assert st["metrics"]["agents"]["verifier"] == {"attempts": 2, "passed": 2}


async def test_team_workflow_run_tool_fills_inputs_and_keeps_the_budget(ctx, team_llm):
    out = await run_tool("team_workflow_run", ctx, workflow="nope")
    assert out.startswith("Error:") and "no workflow 'nope'" in out
    team_llm({"subtasks": [sub("1", "execution", needs=["testteam"], instructions="write the {what} notes")]})
    run_id = json.loads(await run_tool("team_run", ctx, goal="write the notes"))["run_id"]
    await run_tool("team_workflow_save", ctx, run_id=run_id, name="notes")

    fake = team_llm({"subtasks": []}, {"execution:1": [("tt_write", {"name": "a"}), {"summary": "wrote a"}],
                                       "verify:1": [("tt_exists", {"name": "a"}), {"verdict": "pass"}]})
    r = json.loads(await run_tool("team_workflow_run", ctx, workflow="notes", goal="write the {what} notes",
                                  inputs={"what": "tax"}))
    assert r["status"] == "done" and r["achieved"] and FILES == {"a": True}
    assert "plan" not in fake.log and "write the tax notes" in fake.prompts["execution:1"][0]
    assert team.get_workflow("notes")["runs"] == 2
    assert json.loads(await run_tool("team_workflow_run", ctx, workflow="1"))["status"] == "done"      # by id

    FILES.clear()
    team_llm({"subtasks": []}, {"execution:1": [("tt_write", {"name": n}) for n in "abcde"] + ["x"]})
    ctx.max_tool_calls = ctx.calls + 3                       # the workflow call itself, then two more
    r = json.loads(await run_tool("team_workflow_run", ctx, workflow="notes"))
    assert r["status"] == "stopped" and "Tool-call limit" in r["answer"] and FILES == {"a": True, "b": True}
    with pytest.raises(security.Stopped):                    # the team's calls were charged to the caller
        await run_tool("team_status", ctx, run_id=1)

    security.request_stop()
    with pytest.raises(security.Stopped):
        await run_tool("team_workflow_run", Context(session="t2"), workflow="notes")
    security.clear_stop()


async def test_a_subtask_cannot_start_another_team_run(ctx, team_llm):
    fake = team_llm({"subtasks": [sub("1", "execution", needs=["testteam"])]},
                    {"execution:1": [("team_run", {"goal": "do it all again"}),
                                     ("team_workflow_run", {"workflow": "anything"}),
                                     ("tt_write", {"name": "a"}), {"summary": "wrote a"}]})
    r = await team.run_team("write a note", ctx)
    assert fake.log.count("plan") == 1 and len(team.history()) == 1          # no second team was started
    assert r["status"] == "done" and FILES == {"a": True}
    assert r["metrics"]["tool_calls"] == 1 and r["metrics"]["tool_errors"] == 0   # refused as not its tools; one write
    assert ctx.calls == 1                                    # the shared counter survived the attempt
    ctx.data["team_calls"] = ctx.calls                       # and the tools refuse too, were a subtask to reach them
    assert "already in progress" in await run_tool("team_run", ctx, goal="again")
    assert "already in progress" in await run_tool("team_workflow_run", ctx, workflow="anything")
    assert len(team.history()) == 1


# ---- audit: roles are held to their tools, a verifier's blocked call is not queued, a cancelled run is closed

from sparx import pending, tools  # noqa: E402

SEEN = {}
RISKY = ("exec", "control", "delete", "spend")


def _risky(risk):
    def fn(ctx):
        FILES[risk] = True
        return f"{risk} ran"
    return fn


for _r in RISKY:
    tool(f"tt_{_r}", f"a {_r} action", category="testteam", risk=_r)(_risky(_r))


@tool("tt_probe", "look at the state", category="testteam")
def _probe(ctx):
    SEEN["ctx"] = ctx
    return "looked"


def _temp_tool(monkeypatch, name, group, risk):
    def fn(ctx):
        FILES[name] = True
        return f"{name} ran"
    monkeypatch.setitem(tools.REGISTRY, name, tools.Tool(name, name, {}, [], group, risk, False, 120, fn))


EVERYTHING = [("tt_write", {"name": "evil"}), ("tt_send", {"to": "x@y.z"}), *((f"tt_{r}", {}) for r in RISKY),
              ("tt_web_write", {}), ("tt_other_write", {}), ("load_tools", {"groups": ["testteam"]}),
              ("tt_write", {"name": "evil2"}), ("team_feedback", {"run_id": 1, "correction": "x"})]


async def test_research_and_verifier_can_only_read(ctx, team_llm, monkeypatch):
    _temp_tool(monkeypatch, "tt_web_write", "web", "write")       # a write tool inside a group research may read
    _temp_tool(monkeypatch, "tt_web_read", "web", "read")
    _temp_tool(monkeypatch, "tt_other_write", "ttother", "write")
    team_llm({"subtasks": [sub("1"), sub("2", "execution", needs=["testteam"])]},
             {"research:1": [*EVERYTHING, ("tt_web_read", {}), {"summary": "s", "evidence": []}],
              "verify:1": [*EVERYTHING, {"verdict": "pass"}],
              "verify:2": [*EVERYTHING, ("tt_exists", {"name": "evil"}), {"verdict": "pass"}]})
    r = await team.run_team("look things up", ctx)
    assert FILES == {"tt_web_read": True} and ctx.asked == []     # only the two read tools ran, nothing was asked
    assert r["metrics"]["tool_calls"] == 2 and r["pending_approvals"] == []
    assert not ctx.active                                         # load_tools did not widen anyone's groups


async def test_executor_is_held_to_its_groups_and_keeps_every_risk(ctx, team_llm, monkeypatch):
    _temp_tool(monkeypatch, "tt_other_write", "ttother", "write")
    team_llm({"subtasks": [sub("1", "execution", needs=["testteam"])]},
             {"execution:1": [("tt_other_write", {}), ("load_tools", {"groups": ["ttother"]}), ("tt_other_write", {}),
                              ("tt_write", {"name": "a"}), ("tt_exec", {}), ("tt_send", {"to": "x@y.z"}),
                              ("calculate", {"expression": "1+1"}), {"summary": "done"}]})
    r = await team.run_team("act", ctx)
    assert FILES == {"a": True, "exec": True} and ctx.asked == ["tt_exec", "tt_send"]   # ask-level tools still ask
    assert r["metrics"]["tool_calls"] == 4 and r["tasks"][0]["status"] == "passed"


async def test_verifier_blocked_call_is_not_queued(team_llm):
    def auto():
        return Context(session="t", approve=None, data={"automated": True})
    team_llm({"subtasks": [sub("1", "execution", needs=["testteam"])]},
             {"verify:1": [("tt_send", {"to": "verifier@x.y"}), ("tt_probe", {}), {"verdict": "pass"}]})
    r = await team.run_team("background check", auto())
    assert pending.rows() == [] and r["pending_approvals"] == []
    out = await tools.call("tt_send", {"to": "verifier@x.y"}, SEEN.pop("ctx"))     # the verifier's own context
    assert out.startswith("Not done") and "Queued" not in out and pending.rows() == []

    team_llm({"subtasks": [sub("1", "execution", needs=["testteam"])]},              # the plan's own action still is
             {"execution:1": [("tt_send", {"to": "boss@x.y"}), {"summary": "could not send"}]})
    r = await team.run_team("background send", auto())
    assert [(p["tool"], p["agent"]) for p in pending.rows()] == [("tt_send", "execution:1")]
    assert r["tasks"][0]["status"] == "needs_approval"


async def test_cancelled_run_is_closed(ctx, team_llm, monkeypatch):
    fake = team_llm({"subtasks": [sub("1"), sub("2", "execution", ["1"])]})

    async def slow(messages, schemas):
        await asyncio.sleep(30)
    fake.chat = slow
    monkeypatch.setattr(tools.REGISTRY["team_run"], "timeout", 0.2)
    out = await run_tool("team_run", ctx, goal="too slow")
    assert "timed out" in out
    st = team.status(team.history()[0]["id"])
    assert st["status"] == "cancelled" and st["finished"] and not st["success"]
    assert {t["status"] for t in st["tasks"]} == {"cancelled"}
    assert "team_calls" not in ctx.data                         # the next team_run in this session is not refused
    team_llm({"subtasks": [sub("1")]})
    assert json.loads(await run_tool("team_run", ctx, goal="again"))["status"] == "done"
