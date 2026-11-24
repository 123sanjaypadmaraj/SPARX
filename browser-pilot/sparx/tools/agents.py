"""Team tools: hand a large goal to the multi-agent team, inspect runs, reuse successful plans as workflows."""
from ..agents import team
from . import I, O, S, ToolError, category, tool

category("agents", "Multi-agent team for large goals: plan, parallel research and execution, verification, workflows")


def _brief(r: dict) -> dict:
    return {"run_id": r["run_id"], "status": r["status"], "achieved": r["achieved"], "answer": r["answer"],
            "tasks": [{k: t[k] for k in ("id", "title", "agent", "status", "verdict", "attempts")} for t in r["tasks"]],
            "conflicts": r["conflicts"], "unverified": r["unverified"], "pending_approvals": r["pending_approvals"],
            "metrics": r["metrics"]}


def _guard(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise ToolError(str(e))


def _top_level(ctx):
    """A subtask of a running team may not start another team: a nested run gets its own max_agents and, when it
    ends, drops the outer run's shared tool-call counter."""
    if "team_calls" in ctx.data:
        raise ToolError("A team run is already in progress, and a subtask cannot start another one. "
                        "Do this subtask with your own tools.")


@tool("team_run","Give a large, multi-part goal to the agent team: it plans subtasks, researches and executes them in "
      "parallel, verifies each result, and reports what worked, what failed and what still needs approval, with "
      "sources. Use it for goals too big for one pass, not for simple questions.",
      {"goal": S}, ["goal"], category="agents", risk="write", timeout=1800)
async def team_run(ctx, goal: str):
    _top_level(ctx)
    return _brief(await team.run_team(goal, ctx))


@tool("team_status", "State of one team run: each subtask with its status and verdict, evidence count, metrics.",
      {"run_id": I}, ["run_id"], category="agents")
def team_status(ctx, run_id: int):
    return team.status(int(run_id)) or f"No team run {run_id}."


@tool("team_runs", "Recent team runs: goal, status, whether they succeeded, any user correction.", {"limit": I},
      category="agents")
def team_runs(ctx, limit: int = 10):
    return team.history(limit) or "No team runs yet."


@tool("team_workflow_save", "Save the plan of a successful team run as a reusable workflow.",
      {"run_id": I, "name": S}, ["run_id", "name"], category="agents", risk="write")
def team_workflow_save(ctx, run_id: int, name: str):
    return _guard(team.save_workflow, int(run_id), name.strip())


@tool("team_workflow_list", "Saved team workflows with how often each was run, its success rate and the corrections "
      "users gave.", category="agents")
def team_workflow_list(ctx):
    return [{k: v for k, v in w.items() if k != "plan"} | {"subtasks": [t["title"] for t in w["plan"]]}
            for w in team.workflows()] or "No saved workflows."


@tool("team_workflow_run", "Run a saved workflow again, for a new goal and/or with input values.",
      {"workflow": {**S, "description": "workflow name or id"}, "goal": {**S, "description": "the new goal; default is the original"},
       "inputs": {**O, "description": "values for {placeholders} in the workflow"}},
      ["workflow"], category="agents", risk="write", timeout=1800)
async def team_workflow_run(ctx, workflow: str, goal: str = "", inputs: dict | None = None):
    _top_level(ctx)
    try:
        return _brief(await team.run_workflow(workflow, ctx, goal, inputs))
    except ValueError as e:
        raise ToolError(str(e))


@tool("team_feedback", "Record the user's correction for a team run (what was wrong or should be done differently). "
      "Later runs of the same workflow receive it, and the run counts against that workflow's success rate.",
      {"run_id": I, "correction": S}, ["run_id", "correction"], category="agents", risk="write")
def team_feedback(ctx, run_id: int, correction: str):
    return _guard(team.feedback, int(run_id), correction)


@tool("agent_metrics", "Team performance over recent runs: success rate, tokens, latency, tool calls, tool errors, "
      "retries and per-agent success.", {"limit": I}, category="agents")
def agent_metrics(ctx, limit: int = 50):
    return team.agent_metrics(limit)
