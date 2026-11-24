"""Permission modes and allow/deny/ask rules on top of the per-risk policy in security.permission.

    mode:   default (the risk policy) | accept_edits (file-editing tools run without asking) | plan | bypass (ask nothing)
    rules:  settings "permission_rules": {"allow": ["code_edit", "shell_run(git status*)"], "deny": ["shell_run(rm *)"],
            "ask": [...]}. `tool` matches the tool name (globs allowed); `(pattern)` matches the tool's main argument
            (command, path, url, query) with a glob. deny beats ask beats allow. Session rules ("always allow" in the
            approval prompt) live in ctx.data and last until the chat ends.

Rules and modes only move a call between ask and allow, or refuse it. They never lift a hard refusal inside a tool
(protected paths, catastrophic commands) and never touch plan mode, which is checked first."""
import fnmatch
import re

from . import config

MODES = ("default", "accept_edits", "plan", "bypass")
EDIT_TOOLS = {"code_edit", "code_write", "code_multi_edit", "code_rename_symbol", "notebook_edit", "file_write"}
MAIN_ARG = ("command", "path", "file", "url", "query", "name")
_RULE = re.compile(r"^([^()]+?)\s*(?:\((.*)\))?$", re.S)


def parse(rule: str) -> tuple[str, str]:
    """('shell_run', 'git status*') for 'shell_run(git status*)'; the pattern is '' when none is given."""
    m = _RULE.match((rule or "").strip())
    return (m.group(1).strip(), m.group(2) or "") if m else ("", "")


def main_arg(args: dict) -> str:
    for k in MAIN_ARG:
        if isinstance(args.get(k), str):
            return args[k]
    return ""


def matches(rule: str, name: str, args: dict) -> bool:
    tool, pat = parse(rule)
    if not tool or not fnmatch.fnmatchcase(name, tool):
        return False
    return not pat or fnmatch.fnmatchcase(main_arg(args).strip(), pat)


def rules() -> dict[str, list[str]]:
    raw = config.settings().get("permission_rules") or {}
    return {k: [str(r) for r in raw.get(k, []) if isinstance(r, str)] for k in ("allow", "ask", "deny")}


def mode(ctx) -> str:
    m = ctx.data.get("permission_mode") or config.settings().get("permission_mode", "default")
    return m if m in MODES else "default"


def decide(ctx, tool, args: dict, policy: str) -> tuple[str, str]:
    """(decision, why): the final allow | ask | deny for this call, starting from the risk policy `policy`."""
    r = rules()
    for rule in r["deny"]:
        if matches(rule, tool.name, args):
            return "deny", f"denied by the rule '{rule}'"
    if policy == "deny":
        return "deny", ""
    for rule in r["ask"]:
        if matches(rule, tool.name, args):
            return "ask", ""
    if mode(ctx) == "bypass":
        return "allow", ""
    for rule in r["allow"] + ctx.data.get("session_allow", []):
        if matches(rule, tool.name, args):
            return "allow", ""
    if mode(ctx) == "accept_edits" and tool.name in EDIT_TOOLS:
        return "allow", ""
    return policy, ""


def allow_for_session(ctx, tool_name: str, args: dict | None = None):
    """Remember 'always allow' for the rest of the chat: the tool, narrowed to its argument when it is a command."""
    arg = main_arg(args or {})
    rule = f"{tool_name}({arg})" if tool_name == "shell_run" and arg else tool_name
    if rule not in ctx.data.setdefault("session_allow", []):
        ctx.data["session_allow"].append(rule)
    return rule


def add_rule(kind: str, rule: str) -> str:
    """Persist a rule in settings. Returns a message."""
    if kind not in ("allow", "ask", "deny") or not parse(rule)[0]:
        return "Usage: /permissions allow|ask|deny TOOL[(pattern)]"
    cur = rules()
    for k in cur:
        cur[k] = [x for x in cur[k] if x != rule]
    cur[kind].append(rule)
    config.set_key("permission_rules", cur)
    return f"{kind}: {rule}"


def remove_rule(rule: str) -> str:
    cur, found = rules(), False
    for k in cur:
        if rule in cur[k]:
            cur[k].remove(rule)
            found = True
    if found:
        config.set_key("permission_rules", cur)
    return f"Removed {rule}." if found else f"No rule '{rule}'."


def summary(ctx=None) -> str:
    r = rules()
    lines = [f"mode: {mode(ctx) if ctx else config.settings().get('permission_mode', 'default')}",
             "risk policy: " + ", ".join(f"{k}={v}" for k, v in config.settings()["permissions"].items())]
    for k in ("deny", "ask", "allow"):
        lines += [f"{k}: {x}" for x in r[k]]
    if ctx and ctx.data.get("session_allow"):
        lines += [f"allow (this chat): {x}" for x in ctx.data["session_allow"]]
    return "\n".join(lines)
