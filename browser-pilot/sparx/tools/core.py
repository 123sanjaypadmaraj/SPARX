"""Always-on tools: loading other tool groups, time, arithmetic, undo, settings, emergency stop."""
import ast
import math
import operator
from datetime import datetime

from .. import config, jobs, pending, security, undo
from . import A, B, CATEGORIES, I, REGISTRY, S, ToolError, by_category, call, category, tool

category("core", "Load tool groups, date and time, calculator, undo, settings, notifications")


@tool("load_tools", "Make more tool groups available for this conversation. Call it before using a group that is not loaded yet.",
      {"groups": {**A, "description": "group names from the catalogue in your instructions"}}, ["groups"], category="core")
def load_tools(ctx, groups: list):
    known = by_category()
    out = {}
    for g in groups if isinstance(groups, list) else [groups]:
        if g in known:
            ctx.active.add(g)
            out[g] = [t.name for t in known[g]]
        else:
            out[g] = f"unknown group; choose from {', '.join(sorted(known))}"
    return out


@tool("list_tools", "List every tool group and its tools.", category="core")
def list_tools(ctx):
    return {c: {"about": CATEGORIES.get(c, ""), "tools": [t.name for t in ts]} for c, ts in sorted(by_category().items())}


@tool("now", "Current local date, time and weekday.", category="core")
def now(ctx):
    d = datetime.now().astimezone()
    return {"iso": d.isoformat(timespec="seconds"), "weekday": d.strftime("%A"), "date": d.strftime("%d %B %Y"),
            "time": d.strftime("%H:%M"), "timezone": str(d.tzinfo)}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_NAMES = {k: getattr(math, k) for k in ("pi", "e", "tau", "sqrt", "log", "log10", "log2", "exp", "sin", "cos", "tan",
                                         "asin", "acos", "atan", "degrees", "radians", "floor", "ceil", "factorial",
                                         "gcd", "comb", "perm", "hypot")}
_NAMES.update(abs=abs, round=round, min=min, max=max, sum=sum)


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(b) > 1000 or abs(a) > 1e6):
            raise ToolError("exponent too large")
        return _OPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _NAMES and not node.keywords:
        return _NAMES[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_eval(e) for e in node.elts]
    raise ToolError("only numbers, + - * / // % **, and math functions are allowed")


@tool("calculate", "Evaluate an arithmetic expression exactly, e.g. '1200*12*(1+0.07)**5' or 'sqrt(2)*pi'. Use it instead of mental maths.",
      {"expression": S}, ["expression"], category="core")
def calculate(ctx, expression: str):
    try:
        return {"expression": expression, "result": _eval(ast.parse(expression.replace("^", "**"), mode="eval").body)}
    except (SyntaxError, ZeroDivisionError, ValueError, OverflowError) as e:
        raise ToolError(f"cannot evaluate: {e}")


@tool("undo_list", "Recent reversible actions Sparx took.", {"limit": I}, category="core")
def undo_list(ctx, limit: int = 10):
    return undo.recent(limit) or "Nothing recorded."


@tool("undo", "Reverse a reversible action Sparx took (the latest one unless an id is given).",
      {"id": {**I, "description": "id from undo_list"}}, category="core", risk="write")
def undo_action(ctx, id: int = 0):
    return undo.run(id or None)


@tool("notifications", "Unread notifications and proactive suggestions, with the reason for each.",
      {"mark_read": {"type": "boolean"}}, category="core")
def notifications(ctx, mark_read: bool = False):
    rows = jobs.unread()
    if mark_read:
        jobs.mark_read()
    return rows or "No unread notifications."


@tool("settings_get", "Show Sparx's settings: name, language, personality, models, permissions, limits, proactive switches.",
      category="core")
def settings_get(ctx):
    return security.redact(config.settings())      # a token someone put under integrations is masked, never shown


@tool("settings_set", "Change a preference: name, user, language, personality, models.*, proactive.*, voice.*. "
      "Permissions and limits cannot be changed from here; the user edits those with `sparx config`.",
      {"key": {**S, "description": "dotted key, e.g. 'personality' or 'proactive.daily_briefing'"}, "value": S},
      ["key", "value"], category="core", risk="write")
def settings_set(ctx, key: str, value: str):
    top = key.split(".")[0]
    if top not in ("name", "user", "address", "language", "personality", "models", "proactive", "voice"):
        raise ToolError(f"'{top}' can only be changed by the user with `sparx config {key} <value>`.")
    try:
        return {"saved": key, "value": config.set_key(key, value)}
    except ValueError as e:
        raise ToolError(f"Not saved. {e}")


@tool("pending_list", "Actions a background job wanted to take but could not, because they need the user's approval "
      "(for example a sensor rule's device command). Each has an id for pending_approve or pending_dismiss.",
      {"all": {**B, "description": "also show done, dismissed and expired entries"}}, category="core")
def pending_list(ctx, all: bool = False):
    return pending.rows("" if all else "pending") or "No pending actions."


def _pending(id: int) -> dict:
    row = pending.get(id)
    if not row:
        raise ToolError(f"No pending action {id}. See pending_list.")
    if row["status"] == "expired":
        raise ToolError(f"Pending action {id} ({row['tool']}) expired at {row['expires'][11:16]} and will not be run. "
                        "If it is still wanted, ask for the action directly.")
    if row["status"] != "pending":
        note = f" {row['result'][:300]}" if row["result"] else ""      # for a handed-over entry: where it waits now
        raise ToolError(f"Pending action {id} is already {row['status']}.{note}")
    return row


@tool("pending_approve", "Run a pending action from pending_list now. Use it when the user says to go ahead with it. "
      "The action's normal approval prompt still applies; expired entries are refused.",
      {"id": {**I, "description": "id from pending_list"}}, ["id"], category="core", risk="write", timeout=600)
async def pending_approve(ctx, id: int):
    row = _pending(id)
    if row["tool"] not in REGISTRY:
        raise ToolError(f"Pending action {id} uses the tool '{row['tool']}', which is no longer available.")
    text = await call(row["tool"], row["args"], ctx)     # through the registry: permission, approval and audit apply
    if text.startswith(("Not done:", "Error:")):         # declined or failed: it stays pending until it expires
        pending.close(id, "pending", text)
        return text
    pending.close(id, "done", text)
    return f"Pending action {id} ({row['tool']}) was run.\n{text}"


@tool("pending_dismiss", "Drop a pending action from pending_list without running it.",
      {"id": {**I, "description": "id from pending_list"}}, ["id"], category="core", risk="write")
def pending_dismiss(ctx, id: int):
    row = _pending(id)
    pending.close(id, "dismissed", "dismissed by the user")
    return f"Pending action {id} ({row['tool']}) dismissed. It will not be run."


@tool("emergency_stop", "Halt every running Sparx agent and background task immediately.", category="core")
def emergency_stop(ctx):
    security.request_stop("requested in chat")
    return "Stop flag set. Running agents halt at their next step. `sparx resume` clears it."
