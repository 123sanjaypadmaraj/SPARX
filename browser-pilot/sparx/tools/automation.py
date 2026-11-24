"""Automation tools: create recipes (trigger + steps), run, pause/resume on approvals, inspect and undo runs."""
from .. import automation, llm
from . import B, I, O, REGISTRY, S, ToolError, category, tool

category("automation", "Recipes that run tool steps on a schedule, event, file change or on demand; run log, undo")

_STEPS = {"type": "array", "items": {"type": "object"}, "description":
          'ordered steps, each {"tool": name, "args": {...}} or {"prompt": "natural-language step"}. Use {input_name}, '
          "{event.field}, {prev} or {step1.field} placeholders. Optional per step: idempotent (safe to retry), retries, "
          "backoff (seconds), save_as, continue_on_error"}
_REF = {**S, "description": "automation name or id"}

FROM_TEXT = """Turn the user's instruction into one automation recipe:
{{"name": "short-kebab-name", "trigger": "<trigger>", "inputs": {{}}, "steps": [...]}}
Triggers: {triggers}
Steps run in order. Prefer {{"tool": name, "args": {{...}}}} with a tool from the list; use {{"prompt": "..."}} for a step
that needs judgement or writing. Placeholders: {{prev}} (previous step's output), {{step1}}, {{event.field}}, {{date}}.
Mark a tool step "idempotent": true only if repeating it is harmless.
Tools:
{tools}"""


def _guard(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except ValueError as e:
        raise ToolError(str(e))


@tool("automation_create", "Save an automation recipe from structured steps. It starts enabled unless enabled=false, "
      "or unless it is created by a background run with nobody there to confirm: then it is saved disabled. "
      "Risky steps still wait for the user's approval when it runs by itself.",
      {"name": S, "trigger": {**S, "description": automation.TRIGGER_HELP}, "steps": _STEPS,
       "inputs": {**O, "description": "default values for {placeholders}"}, "enabled": B},
      ["name", "trigger", "steps"], category="automation", risk="write")
def automation_create(ctx, name: str, trigger, steps: list, inputs: dict | None = None, enabled: bool = True):
    held = enabled and automation.unattended(ctx)       # nobody saw it: it must not start running by itself
    saved = _guard(automation.create, name, trigger, steps, inputs, enabled and not held)
    if held:
        saved["next"] = ("Saved DISABLED, because nobody was there to confirm it. Show it to the user; after they "
                         f"confirm in a conversation, automation_enable('{saved['name']}') turns it on.")
    return saved


@tool("automation_from_text", "Draft an automation from one plain-language instruction ('every weekday at 9 list my "
      "tasks and email me a summary'). Saves it DISABLED and returns it: show it to the user and call "
      "automation_enable only after they confirm.", {"text": S}, ["text"], category="automation", risk="write")
async def automation_from_text(ctx, text: str):
    catalogue = "\n".join(f"- {t.name} [{t.risk}]: {t.description[:90]}" for t in REGISTRY.values()
                          if t.category not in ("automation", "testing"))
    recipe = await llm.complete_json(FROM_TEXT.format(triggers=automation.TRIGGER_HELP, tools=catalogue), text,
                                     role="default")
    if not recipe.get("steps"):
        raise ToolError("The model did not produce a recipe. Rephrase the instruction or use automation_create.")
    name = base = str(recipe.get("name") or "automation").strip()[:60]
    n = 1
    while automation.get(name):
        n += 1
        name = f"{base}-{n}"
    try:
        saved = automation.create(name, recipe.get("trigger") or "manual", recipe["steps"], recipe.get("inputs") or {},
                                  enabled=False)
    except ValueError as e:
        raise ToolError(f"The drafted recipe is not valid ({e}). Draft was: {recipe}")
    return {"recipe": saved, "enabled": False,
            "next": f"Show this to the user. After they confirm, call automation_enable('{name}')."}


@tool("automation_list", "All saved automations with trigger, steps, whether enabled and the next scheduled run.",
      category="automation")
def automation_list(ctx):
    return automation.listing() or "No automations yet."


@tool("automation_run", "Run an automation now, optionally with input values for its placeholders.",
      {"name": _REF, "inputs": O}, ["name"], category="automation", risk="write", timeout=900)
async def automation_run(ctx, name: str, inputs: dict | None = None):
    auto = automation.get(name)
    if not auto:
        raise ToolError(f"No automation '{name}'.")
    return await automation.start(auto, ctx, inputs)


@tool("automation_enable", "Turn an automation's trigger on (after the user confirmed it).", {"name": _REF}, ["name"],
      category="automation", risk="write")
def automation_enable(ctx, name: str):
    if automation.unattended(ctx):
        raise ToolError("Not enabled: an automation is only turned on with the user present, not by a background run. "
                        "Tell the user it is waiting; they can enable it by asking or in the Automations panel.")
    return _guard(automation.set_enabled, name, True)


@tool("automation_disable", "Turn an automation's trigger off without deleting it.", {"name": _REF}, ["name"],
      category="automation", risk="write")
def automation_disable(ctx, name: str):
    return _guard(automation.set_enabled, name, False)


@tool("automation_delete", "Delete an automation (its run history is kept; undo restores it).", {"name": _REF},
      ["name"], category="automation", risk="delete")
def automation_delete(ctx, name: str):
    return _guard(automation.delete, name, ctx)


@tool("automation_runs", "Run history with the per-step log (status, output summary, ms). Use it to see why a run "
      "failed or what it is waiting for.", {"name": {**S, "description": "optional automation name or id"}, "limit": I},
      category="automation")
def automation_runs(ctx, name: str = "", limit: int = 10):
    return automation.runs(name or None, limit) or "No runs yet."


@tool("automation_resume", "Continue a run that paused for approval (or was stopped). The pending step asks the user "
      "before it runs.", {"run_id": I}, ["run_id"], category="automation", risk="write", timeout=900)
async def automation_resume(ctx, run_id: int):
    try:
        return await automation.resume(int(run_id), ctx)
    except ValueError as e:
        raise ToolError(str(e))


@tool("automation_cancel", "Drop an automation run that is waiting for approval (or was stopped) without running its "
      "pending step.", {"run_id": I}, ["run_id"], category="automation", risk="write")
def automation_cancel(ctx, run_id: int):
    return _guard(automation.cancel, int(run_id))


@tool("automation_events", "The events published in the last day (for example sensor.<device>.<metric> when a sensor "
      "rule is crossed). Use it to check why an event: automation did or did not run.", {"limit": I},
      category="automation", untrusted=True)
def automation_events(ctx, limit: int = 50):
    return automation.events(limit) or "No events in the last day."


@tool("automation_undo", "Reverse the reversible changes one automation run made, newest first.", {"run_id": I},
      ["run_id"], category="automation", risk="write")
def automation_undo(ctx, run_id: int):
    return _guard(automation.undo_run, int(run_id))


# sparx/proactive.py is outside this package; importing it here is what makes load_all() register its tools and jobs.
try:
    from .. import proactive  # noqa: E402,F401
except ImportError:
    pass
