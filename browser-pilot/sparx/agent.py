"""One agent: a model, a system prompt, a set of tools, and the observe/act loop."""
import json
import time

from . import compact, config, llm, loopguard, security, tools
from .tools import Context


SCHEMA_BUDGET_FACTOR = 3        # tool schemas get this many times limits.max_history_chars


def schema_budget() -> int:
    """Characters of tool schema one model call may carry. Idle groups are unloaded to stay inside it."""
    return SCHEMA_BUDGET_FACTOR * int(config.settings()["limits"]["max_history_chars"])


class Groups(set):
    """The loaded tool groups of one conversation, remembering in which turn each was last used."""

    def __init__(self, items=()):
        super().__init__(items)
        self.used: dict[str, int] = {}
        self.turn = 0


def _use(ctx: Context, group: str):
    if isinstance(ctx.active, Groups):
        ctx.active.used[group] = ctx.active.turn


def _size(schemas: list) -> int:
    return len(json.dumps(schemas, ensure_ascii=False))


def unload_idle(agent: "Agent", ctx: Context) -> list[str]:
    """While the tool schemas are over schema_budget(), drop the least recently used loaded group. Never an always-on
    group, nor one loaded or used in the current turn. Returns the groups dropped."""
    groups, dropped, budget = ctx.active, [], schema_budget()
    if not isinstance(groups, Groups):
        return dropped
    keep = set(config.settings()["always_on"])
    while _size(agent._schemas(ctx)) > budget:
        idle = [g for g in groups if g not in keep and groups.used.get(g, 0) < groups.turn]
        if not idle:
            break
        g = min(sorted(idle), key=lambda x: groups.used.get(x, 0))
        groups.discard(g)
        dropped.append(g)
    return dropped


def unloaded_note(dropped: list[str]) -> str:
    return (f"[system note] These tool groups were unloaded to save space: {', '.join(dropped)}. "
            f"Call load_tools to use one again.")


class Agent:
    def __init__(self, name: str, system: str, tool_names=(), categories=(), role: str = "default",
                 max_rounds: int | None = None, strict: bool = False, max_calls: int | None = None):
        self.name = name
        self.system = system
        self.tool_names = set(tool_names)
        self.categories = set(categories)
        self.role = role
        self.max_rounds = max_rounds
        self.max_calls = max_calls      # this agent's own tool-call budget (the run's shared budget still applies)
        self.strict = strict            # refuse tools this agent was not given (for agents that read untrusted screens)

    def _schemas(self, ctx: Context) -> list[dict]:
        from . import skills
        return [s for s in tools.schemas(self.tool_names, self.categories | ctx.active)
                if not skills.restricted(ctx.data, s["function"]["name"])]

    def _autoload(self, name: str, ctx: Context) -> str:
        """A call to a tool of an unloaded group loads that group, when this agent may load groups (it was offered
        load_tools). Returns the group name when it was just loaded. Records the group as used either way."""
        t = tools.REGISTRY.get(name)
        if not t or self.strict:
            return ""
        _use(ctx, t.category)
        offered = {s["function"]["name"] for s in self._schemas(ctx)}
        if name in offered or "load_tools" not in offered:
            return ""
        ctx.active.add(t.category)
        _use(ctx, t.category)
        return t.category

    async def run(self, task: str, ctx: Context, history: list | None = None) -> str:
        """Work on `task` until the model answers without calling a tool. Returns the answer text."""
        tools.load_all()
        model = llm.get(self.role)
        messages = [{"role": "system", "content": self.system}, *(history or []), {"role": "user", "content": task}]
        rounds = self.max_rounds or config.settings()["limits"]["max_rounds"]
        guard = loopguard.LoopGuard(config.settings()["limits"])
        made = 0
        try:
            for _ in range(rounds):
                if security.stop_requested():
                    raise security.Stopped("Emergency stop is set. Run `sparx resume` to clear it.")
                if time.time() - ctx.started > config.settings()["limits"]["max_seconds"]:
                    raise security.Stopped("Time limit for this run reached.")     # also when no real tool runs
                messages = await compact.shrink(messages)       # prune old tool outputs / summarise when the turn grows big
                schemas = self._schemas(ctx)
                if isinstance(ctx.active, Groups) and any(s["function"]["name"] == "load_tools" for s in schemas):
                    for g in ctx.active:                        # a group loaded since the last round counts as used now
                        ctx.active.used.setdefault(g, ctx.active.turn)
                    if dropped := unload_idle(self, ctx):
                        schemas = self._schemas(ctx)
                        last = messages[-1]
                        if isinstance(last.get("content"), str) and last.get("role") in ("tool", "user"):
                            last["content"] += "\n" + unloaded_note(dropped)
                        else:
                            messages.append({"role": "user", "content": unloaded_note(dropped)})
                reply = await model.chat(messages, schemas)
                messages.append(reply.message)
                if not reply.calls:
                    return reply.text or "(no reply)"
                notes = []
                for call in reply.calls:
                    if self.max_calls is not None and made >= self.max_calls:
                        messages.append(model.result_message(call, "Error: your tool-call budget is spent. Answer now."))
                        continue
                    made += 1
                    args = call.args
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except ValueError:
                            args = None
                    if not isinstance(args, dict):
                        messages.append(model.result_message(call, "Error: arguments were not a JSON object."))
                        continue
                    if self.strict and call.name not in {s["function"]["name"] for s in self._schemas(ctx)}:
                        messages.append(model.result_message(call, f"Error: '{call.name}' is not one of your tools."))
                        continue
                    loaded = self._autoload(call.name, ctx)
                    try:
                        text = await tools.call(call.name, args, ctx)
                        if loaded:
                            text += (f'\n[Note: tool group "{loaded}" was not loaded; loaded now. '
                                     f'Next time call load_tools(["{loaded}"]) first.]')
                    except security.Stopped:
                        raise
                    except (Exception, SystemExit) as e:    # one tool (or a library calling sys.exit) must not end the turn
                        text = f"Error: {call.name} failed: {type(e).__name__}: {str(e)[:400]}"
                    if h := loopguard.hint(text):
                        text += f"\nHint: {h}"
                    messages.append(model.result_message(call, text))
                    level, note = guard.observe(call.name, args, text)
                    if level == "stop":
                        return f"I stopped because I was going in circles: {note}. Tell me how you want to proceed."
                    if note:
                        notes.append(note)
                if notes:       # after all tool results of this round, so the call/result pairing stays intact
                    messages.append({"role": "user", "content": "[system note] " + " ".join(notes)})
            # Out of rounds: one last call without tools so the user gets a summary, not a dead end.
            messages.append({"role": "user", "content":
                             "You have used all your steps. Summarise what is done, what is not, and what you need."})
            return (await model.chat(messages, [])).text or "I ran out of steps before finishing."
        except security.Stopped as e:
            return f"Stopped: {e}"
