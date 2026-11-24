"""Plan mode tools: enter a read-only planning state, and leave it only with the user's approval of a plan."""
from .. import planmode
from . import REGISTRY, S, ToolError, category, tool

category("planning", "Plan mode: investigate read-only, then get a plan approved before changing anything")


@tool("plan_enter", "Enter plan mode: from now only read-only tools run. Use before a large or risky change to investigate "
      "and write a plan.", category="planning", risk="read")
def plan_enter(ctx):
    ctx.data[planmode.KEY] = True
    return "Plan mode on. Only read-only tools work. Call plan_exit(plan) when your plan is ready."


@tool("plan_exit", "Show the user your plan and ask for approval to leave plan mode and start making changes.",
      {"plan": S}, ["plan"], category="planning", risk="write")
async def plan_exit(ctx, plan: str):
    if not planmode.active(ctx):
        return "Plan mode is not on."
    ok = False
    if ctx.approve:
        try:
            ok = bool(await ctx.approve(REGISTRY["plan_exit"], {"approving": "Leave plan mode and execute this plan", "plan": plan[:4000]}))
        except Exception:
            ok = False
    if not ok:
        raise ToolError("The user did not approve the plan. Stay in plan mode; revise the plan or ask what to change.")
    ctx.data[planmode.KEY] = False
    return "Plan approved. Plan mode is off; carry out the plan."
