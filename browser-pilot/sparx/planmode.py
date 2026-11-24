"""Plan mode: only `read` tools run; everything else is refused until the user approves the plan.

The flag lives in ctx.data["plan_mode"] (shared with sub-agents), seeded from the `plan_mode` setting. The check is
in the tool registry, so shell_run, plugins and sub-agents are all covered."""
from . import config

KEY = "plan_mode"
EXEMPT = {"plan_enter", "plan_exit"}      # plan_exit asks the user itself


def active(ctx) -> bool:
    v = ctx.data.get(KEY)
    return bool(config.settings().get("plan_mode", False)) if v is None else bool(v)


def refusal(ctx, tool) -> str | None:
    """The message for the model when plan mode forbids this tool, else None."""
    if tool.name in EXEMPT or tool.risk == "read" or not active(ctx):
        return None
    return (f"Refused: plan mode is on, so '{tool.name}' ({tool.risk}) cannot run. Use read-only tools to investigate, "
            "then call plan_exit(plan) with your plan so the user can approve it. Do not try to work around this.")
