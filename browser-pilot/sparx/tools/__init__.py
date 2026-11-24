"""Tool registry. Every capability Sparx has is a function registered here.

    from sparx.tools import tool, category, S, I, N, B

    category("finance", "Expenses, budgets, subscriptions, watchlists")

    @tool("expense_add", "Record an expense.",
          {"amount": N, "what": S, "when": {"type": "string", "description": "ISO date, default today"}},
          ["amount", "what"], category="finance", risk="write")
    async def expense_add(ctx, amount: float, what: str, when: str = ""):
        ...
        return {"id": 3, "total_this_month": 4200}       # str, dict or list

Rules:
- First parameter is always `ctx` (a Context). Functions may be async or plain (plain ones run in a thread).
- `risk` decides whether the user is asked first (see config DEFAULTS["permissions"]):
  read | write | exec | control | delete | send | spend. Pick the highest that applies.
- `untrusted=True` for anything returning text Sparx did not write (web pages, file contents, emails,
  OCR, transcripts, device payloads). The registry wraps it so the model treats it as data.
- Raise ToolError("message") for expected failures; the message is shown to the model.
- Modules in this package are imported automatically by load_all(). Optional dependencies must be
  imported inside the function, never at module top level.
"""
import asyncio
import importlib
import inspect
import json
import pkgutil
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import config, security

S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
B = {"type": "boolean"}
A = {"type": "array", "items": {"type": "string"}}
O = {"type": "object"}

MAX_RESULT = 20000


class ToolError(Exception):
    pass


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    required: list
    category: str
    risk: str
    untrusted: bool
    timeout: float
    fn: Callable

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params, "required": self.required},
        }}


@dataclass
class Context:
    """Carried through one conversation turn or agent run."""
    session: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent: str = "sparx"
    scope: str = "personal"                 # memory scope: personal | work | study | <project>
    approve: Callable | None = None         # async (tool: Tool, args: dict) -> bool. None = nobody to ask
    emit: Callable | None = None            # sync (event: dict) -> None, for UIs
    active: set = field(default_factory=set)  # tool categories loaded for this run
    calls: int = 0
    max_tool_calls: int = 0                 # 0 = settings default
    started: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)  # scratch space shared by tools in this run
    stats: dict = field(default_factory=lambda: {"tool_calls": 0, "tool_errors": 0, "tool_ms": 0})
    parent: "Context | None" = field(default=None, repr=False, compare=False)   # set by child(): calls are charged up

    def event(self, **ev):
        if self.emit:
            try:
                self.emit(ev)
            except Exception:
                pass

    def child(self, agent: str) -> "Context":
        """Context for a sub-agent: same session, approvals, scratch data and stats. Its tool calls are charged to
        this context too (see _charge), so a sub-agent spends the parent's budget, not a fresh one."""
        c = Context(session=self.session, agent=agent, scope=self.scope, approve=self.approve, emit=self.emit,
                    active=set(self.active), max_tool_calls=self.max_tool_calls, started=self.started,
                    data=self.data, stats=self.stats, parent=self)
        c.calls = self.calls
        return c


def _charge(ctx: Context, limit: int) -> bool:
    """Count one tool call on ctx and on every context it descends from. True when any of them is over its limit.
    A context whose counter is shared storage with one already charged (the team keeps one in ctx.data) is counted
    once, and a child never reports fewer calls than its parent, so `parent.calls = child.calls` loses nothing."""
    chain, c = [], ctx
    while c is not None and not any(c is x for x in chain):
        chain.append(c)
        c = getattr(c, "parent", None)
    before = [c.calls for c in chain]
    for c, n in zip(chain, before):
        if c.calls == n:
            c.calls = n + 1
    for c, up in zip(chain[-2::-1], chain[:0:-1]):          # from the top down: catch up with calls made by siblings
        if c.calls < up.calls:
            c.calls = up.calls
    return any(c.calls > (c.max_tool_calls or limit) for c in chain)


REGISTRY: dict[str, Tool] = {}
CATEGORIES: dict[str, str] = {}
LOAD_ERRORS: dict[str, str] = {}
PREVIEWS: dict[str, Callable] = {}      # tool name -> fn(args) saying what the arguments refer to, for the approver
_loaded = False


def category(name: str, description: str):
    CATEGORIES[name] = description


def tool(name: str, description: str, params: dict | None = None, required: list | None = None, *,
         category: str, risk: str = "read", untrusted: bool = False, timeout: float = 120):
    if risk not in security.RISKS:
        raise ValueError(f"{name}: unknown risk '{risk}'")

    def deco(fn):
        REGISTRY[name] = Tool(name, description, params or {}, required or [], category, risk, untrusted, timeout, fn)
        return fn
    return deco


def preview(name: str):
    """Register fn(args) -> str | dict | None for a tool whose arguments point at something stored (a draft id).
    The approver is shown its result as `approving`; the tool, the audit log and the events get the real arguments."""
    def deco(fn):
        PREVIEWS[name] = fn
        return fn
    return deco


def _shown(name: str, args: dict) -> dict:
    """The arguments as the approver sees them: with `approving` first when the tool has a preview that works."""
    try:
        what = PREVIEWS[name](dict(args)) if name in PREVIEWS else None
        if what in (None, "", {}, []):
            return args
        what = security.redact(what)
        what = what if isinstance(what, str) else json.dumps(what, ensure_ascii=False, default=str)
        return {"approving": what[:1500], **args}
    except Exception:                       # a failing preview means no preview, never a failed call
        return args


def load_all(force: bool = False):
    """Import every module in sparx.tools (and plugins) so their tools register. A broken module is skipped."""
    global _loaded
    if _loaded and not force:
        return
    _loaded = True
    for m in pkgutil.iter_modules(__path__):
        try:
            importlib.import_module(f"{__name__}.{m.name}")
        except Exception as e:
            LOAD_ERRORS[m.name] = f"{type(e).__name__}: {e}"
    try:
        from .. import plugins
        plugins.load()
    except ImportError:
        pass
    except Exception as e:
        LOAD_ERRORS["plugins"] = f"{type(e).__name__}: {e}"


def by_category() -> dict[str, list[Tool]]:
    out: dict[str, list[Tool]] = {}
    for t in REGISTRY.values():
        out.setdefault(t.category, []).append(t)
    return out


def schemas(names=None, categories=None) -> list[dict]:
    names, categories = set(names or ()), set(categories or ())
    return [t.schema() for t in REGISTRY.values() if t.name in names or t.category in categories]


def _to_text(result: Any) -> str:
    if result is None:
        return "Done."
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, default=str)
    if len(result) > MAX_RESULT:
        result = result[:MAX_RESULT] + f"\n…[{len(result) - MAX_RESULT} more characters cut]"
    return result


async def call(name: str, args: dict, ctx: Context) -> str:
    """Run a tool with permission checks, limits, a timeout and an audit entry. Never raises except Stopped."""
    if security.stop_requested():           # the stop and both budgets come first: they apply to every call,
        raise security.Stopped("Emergency stop is set. Run `sparx resume` to clear it.")     # unknown tools included
    limits = config.settings()["limits"]
    if _charge(ctx, limits["max_tool_calls"]):
        raise security.Stopped("Tool-call limit for this run reached.")
    if time.time() - ctx.started > limits["max_seconds"]:
        raise security.Stopped("Time limit for this run reached.")
    t = REGISTRY.get(name)
    if not t:
        return f"Error: unknown tool '{name}'."
    from .. import skills as _sk            # per-run limits (headless --allowed-tools, a skill's allowed-tools)
    if _why := _sk.restricted(ctx.data, name):
        return f"Error: {_why}"

    if not isinstance(args, dict):
        return "Error: arguments must be a JSON object."
    from ..argcheck import coerce           # "5" -> 5, "true" -> True, JSON text -> list/dict, or a clear Error
    args, bad = coerce(t, args)
    if bad:
        return bad
    missing = [r for r in t.required if args.get(r) in (None, "")]
    if missing:
        return f"Error: missing required argument(s): {', '.join(missing)}."
    accepted = set(inspect.signature(t.fn).parameters) - {"ctx"}
    has_kwargs = any(p.kind is p.VAR_KEYWORD for p in inspect.signature(t.fn).parameters.values())
    if not has_kwargs:
        args = {k: v for k, v in args.items() if k in accepted}

    from .. import hooks, planmode
    if (why := planmode.refusal(ctx, t)):       # plan mode: before policy, so no setting can let a write through
        security.audit(ctx.session, ctx.agent, name, t.risk, args, False, "refused: plan mode", 0)
        return why
    if (why := await hooks.fire("pre_tool", ctx, name, args)):
        security.audit(ctx.session, ctx.agent, name, t.risk, args, False, f"blocked by hook: {why}"[:300], 0)
        return f"Error: blocked by a user hook: {why}"

    from .. import checkpoints, permissions
    policy, rule_why = permissions.decide(ctx, t, args, security.permission(t.risk, name))
    if policy == "deny":
        security.audit(ctx.session, ctx.agent, name, t.risk, args, False, "denied by policy", 0)
        return f"Error: '{name}' is disabled by the user's permission settings ({rule_why or t.risk + ' = deny'})."
    if policy == "ask":
        ok = False
        if ctx.approve:
            try:
                ans = await ctx.approve(t, _shown(name, args))      # a display copy: `args` itself is untouched
                ok = bool(ans)
                if ans == "always":                                 # "always allow" in the approval prompt
                    permissions.allow_for_session(ctx, name, args)
            except Exception:
                ok = False
        if not ok:
            why = "the user declined" if ctx.approve else "it needs the user's approval and nobody is available to ask"
            security.audit(ctx.session, ctx.agent, name, t.risk, args, False, "not approved", 0)
            text = f"Not done: '{name}' was not run because {why}. Do not retry it; tell the user what is pending."
            if not ctx.approve and ctx.data.get("automated"):      # a background job: keep it for the user to approve later
                from .. import pending
                queued = pending.queue(t, args, ctx.agent)
                if queued:
                    text += f" Queued as pending action #{queued}."
            return text

    checkpoints.snapshot(ctx, name, args)
    ctx.event(type="tool_start", tool=name, args=security.redact(args), risk=t.risk)
    start = time.time()
    ok = True
    is_async = inspect.iscoroutinefunction(t.fn)
    deadline = asyncio.timeout(t.timeout)
    try:
        async with deadline:
            result = await (t.fn(ctx, **args) if is_async else asyncio.to_thread(t.fn, ctx, **args))
        text = _to_text(result)
    except security.Stopped:
        raise
    except asyncio.CancelledError:          # cancelled from outside (an outer timeout, a closed connection): record, pass on
        security.audit(ctx.session, ctx.agent, name, t.risk, args, False,
                       "cancelled while running; the action may have partly happened", int((time.time() - start) * 1000))
        raise
    except ToolError as e:
        ok, text = False, f"Error: {e}"
    except TimeoutError as e:
        if not deadline.expired():          # the tool's own timeout (a socket, a subprocess), not this one
            ok, text = False, f"Error: TimeoutError: {str(e)[:400]}"
        else:                               # a thread cannot be stopped: a plain tool goes on after we stop waiting
            ok, text = False, (f"Error: {name} timed out after {t.timeout:g}s and was " +
                               ("cancelled. " if is_async else "abandoned, but it could not be stopped and may still be "
                                "running. ") + "The action may have partly happened: check before you retry it.")
    except TypeError as e:
        ok, text = False, f"Error: bad arguments for {name}: {e}"
    except (Exception, SystemExit) as e:    # SystemExit: a library called sys.exit(); it must not end the caller
        ok, text = False, f"Error: {type(e).__name__}: {str(e)[:400]}"
        config.path("logs", "errors.log").open("a", encoding="utf-8").write(
            f"--- {name} {time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n")
    ms = int((time.time() - start) * 1000)
    ctx.stats["tool_calls"] += 1
    ctx.stats["tool_errors"] += 0 if ok else 1
    ctx.stats["tool_ms"] += ms
    security.audit(ctx.session, ctx.agent, name, t.risk, args, ok, text[:300], ms)
    ctx.event(type="tool_end", tool=name, ok=ok, ms=ms)
    await hooks.fire("post_tool", ctx, name, args, text)
    if ok and t.untrusted:
        text = security.wrap_untrusted(text, f"tool:{name}")
    return text
