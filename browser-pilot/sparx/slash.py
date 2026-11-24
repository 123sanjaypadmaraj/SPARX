"""Chat commands beyond the basics in cli.py, and the input pre-processor.

    preprocess(bot, line)  -> (prompt, shown)   @file, !shell and #note shortcuts, and prompt commands such as /review
    run(bot, cmd, arg)     -> text | UNKNOWN    the /commands that print something

Both chat loops call these; the web UI and Telegram keep their own handling."""
import json
import os
from pathlib import Path

from . import (agentdefs, checkpoints, config, db, headless, hooks, inputs, llm, outputstyles, permissions,
               projectdocs, skills, tools)

UNKNOWN = object()

NAMES = ("status", "cost", "context", "memory", "config", "permissions", "mode", "hooks", "agents", "mcp", "export",
         "resume", "rewind", "add-dir", "init", "doctor", "todos", "output-style", "statusline", "skills", "review",
         "commit", "test", "vim", "login", "bug")

PROMPTS = {
    "review": "Review the current uncommitted changes{arg} in this repository. Run git_diff, read the changed files, and "
              "report concrete bugs, risky edits and missing tests, ordered by severity. Change nothing.",
    "commit": "Look at the working tree with git_status and git_diff, then commit the related changes with a clear "
              "message in the style of the repo's recent git_log{arg}. Stage only the files that belong to the change.",
    "test": "Run the project's tests with run_tests{arg}. If anything fails, find the cause, fix it, and run them again "
            "until they pass or you can explain what is blocking.",
}


def preprocess(bot, line: str) -> tuple[str | None, str]:
    """(prompt for the model or None, text to show the user). `!cmd` and `#note` are done here and return no prompt."""
    if line.startswith("!") and len(line) > 1:
        cmd = line[1:].strip()
        out = inputs.run_shell(cmd)
        _record(bot, f"I ran this shell command myself: {cmd}\n\n{out}", "(Noted the command and its output.)")
        return None, f"$ {cmd}\n{out}"
    if line.startswith("#") and not line.startswith("#!") and len(line) > 1:
        return None, inputs.remember(line)
    if line.startswith("/"):
        name, _, arg = line[1:].partition(" ")
        if name.lower() in PROMPTS:
            return PROMPTS[name.lower()].format(arg=f" ({arg.strip()})" if arg.strip() else ""), ""
        return line, ""
    prompt, notes = inputs.expand_mentions(line)
    return prompt, "\n".join(notes)


def _record(bot, user_text: str, reply: str):
    db.many("INSERT INTO messages (conversation, role, content, ts, scope) VALUES (?,?,?,?,?)",
            [(bot.conversation, "user", user_text[:6000], db.now(), bot.scope),
             (bot.conversation, "assistant", reply, db.now(), bot.scope)])


def _tokens(chars: int) -> int:
    return chars // 4


def _context(bot) -> str:
    tools.load_all()
    system = bot.system("")
    history = sum(len(m["content"]) for m in bot.history())
    schemas = tools.schemas(categories=bot.active)
    schema_chars = len(json.dumps(schemas))
    budget = int(config.settings()["limits"]["max_history_chars"])
    rows = [("system prompt", len(system)), ("tool definitions", schema_chars), ("conversation", history)]
    total = sum(n for _l, n in rows)
    lines = [f"{label:<18} ~{_tokens(n):>6} tokens" for label, n in rows]
    lines += [f"{'total':<18} ~{_tokens(total):>6} tokens   ({len(schemas)} tools loaded)",
              f"history budget     {budget // 1000}k characters before older turns are summarised  (/compact now)"]
    return "\n".join(lines)


def _status(bot) -> str:
    cwd = os.getcwd()
    lines = [f"model        {llm.spec_for()}", f"conversation {bot.conversation}", f"directory    {cwd}",
             f"memory scope {bot.scope}", f"mode         {bot.mode}" + ("  (plan mode on)" if bot.plan_mode else ""),
             f"output style {outputstyles.current()}", f"tools        {len(tools.REGISTRY)} ({len(bot.active)} groups loaded)",
             f"checkpoints  {len(checkpoints.turns(bot.conversation))} turn(s) with file changes"]
    if tools.LOAD_ERRORS:
        lines.append("load errors  " + ", ".join(tools.LOAD_ERRORS))
    return "\n".join(lines)


def _memory() -> str:
    found = projectdocs.discover()
    if not found:
        return "No SPARX.md / AGENTS.md / CLAUDE.md found. `sparx init` writes one; `#note` appends a line to it."
    return "Loaded project guidance (most general first):\n" + "\n".join(
        f"  {p}  ({p.stat().st_size} bytes)" for p in found) + "\n\n`#note` adds a line to the project file, `##note` to the global one."


def _hooks() -> str:
    out = []
    for ev in hooks.EVENTS:
        for h in hooks.configured(ev):
            out.append(f"{ev:<14} {h.get('match', '*'):<14} {h['command'][:80]}")
    return "\n".join(out) or ("No hooks. Add them under \"hooks\" in settings.json, e.g. "
                              '{"pre_tool": [{"match": "code_.*", "command": "python check.py"}]}.')


def _agents() -> str:
    defs = agentdefs.agents()
    if not defs:
        return "No custom agents. Put name.md (frontmatter: name, description, tools) in ~/.sparx/agents or .sparx/agents."
    return "\n".join(f"{n:<16} {a['description'][:70]}  [{', '.join(a['tools']) if a['tools'] else 'read-only default'}]"
                     for n, a in sorted(defs.items()))


def _mcp() -> str:
    from . import plugins
    cfg = plugins.configured()
    if not cfg:
        return "No MCP servers configured (settings: integrations.mcp_servers)."
    return "\n".join(f"{n:<16} {c.get('url') or (str(c.get('command', '')) + ' ' + ' '.join(map(str, c.get('args', []))))[:80]}"
                     for n, c in sorted(cfg.items()))


def _export(bot, arg: str) -> str:
    rows = db.q("SELECT role, content, ts FROM messages WHERE conversation = ? ORDER BY id", (bot.conversation,))
    if not rows:
        return "Nothing to export yet."
    text = f"# Sparx conversation {bot.conversation}\n\n" + "\n\n".join(
        f"**{'You' if r['role'] == 'user' else config.settings()['name']}** ({r['ts']})\n\n{r['content']}" for r in rows)
    target = Path(arg or f"sparx-{bot.conversation}.md")
    target.write_text(text + "\n", encoding="utf-8")
    return f"Wrote {target.resolve()} ({len(rows)} messages)."


def _resume(bot, arg: str) -> str:
    if not arg:
        rows = headless.sessions(10)
        return "\n".join(f"{r['id']}  {r['messages']:>3} msgs  {r['first_prompt']}" for r in rows) or "No stored conversations."
    try:
        conv = headless.resolve_conversation(resume=arg)
    except ValueError as e:
        return str(e)
    from .assistant import kv_set
    bot.conversation = conv
    kv_set("conversation", conv)
    return f"Resumed {conv}."


def _permissions(bot, arg: str) -> str:
    ctx = bot.last_ctx or tools.Context(data={"permission_mode": bot.mode, "session_allow": bot.session_allow})
    kind, _, rule = arg.partition(" ")
    if not arg:
        return permissions.summary(ctx)
    if kind == "rm":
        return permissions.remove_rule(rule.strip())
    return permissions.add_rule(kind, rule.strip())


def _mode(bot, arg: str) -> str:
    if not arg:
        return f"mode: {bot.mode}   (options: {', '.join(permissions.MODES)})"
    if arg not in permissions.MODES:
        return f"Unknown mode. Options: {', '.join(permissions.MODES)}"
    bot.set_mode(arg)
    return {"default": "Default: Sparx asks according to the risk policy.",
            "accept_edits": "Accept edits: file edits run without asking; commands and everything else still ask.",
            "plan": "Plan mode: read-only until you approve a plan.",
            "bypass": "Bypass: nothing asks (deny rules, protected paths and the emergency stop still apply)."}[arg]


def _config(arg: str) -> str:
    if arg:
        key, _, value = arg.partition(" ")
        if value:
            config.set_key(key, value)
            return f"{key} = {value}"
    s = config.settings()
    return json.dumps({k: v for k, v in s.items() if k not in ("integrations",)}, indent=1, ensure_ascii=False)[:6000]


def _statusline(arg: str) -> str:
    if arg in ("off", "none"):
        config.set_key("statusline.command", "")
        return "Status line command cleared."
    if arg:
        config.set_key("statusline.command", arg)
        return f"Status line command set. It receives JSON on stdin (model, cwd, scope, mode) and prints one line."
    return f"statusline.command = {config.settings().get('statusline', {}).get('command') or '(none)'}"


def _skills() -> str:
    s, c = skills.skills(), skills.commands()
    lines = [f"skill   {n:<22} {v['description'][:60]}" for n, v in sorted(s.items())]
    lines += [f"command /{n:<21} {v['description'][:60]}" for n, v in sorted(c.items())]
    return "\n".join(lines) or "No skills or custom commands."


def _add_dir(arg: str) -> str:
    p = Path(arg).expanduser().resolve() if arg else None
    if not p or not p.is_dir():
        return "Usage: /add-dir PATH (an existing folder)"
    roots = list(config.settings().get("allowed_roots", []))
    if str(p) not in roots:
        config.set_key("allowed_roots", roots + [str(p)])
    return f"File tools may now use {p}."


async def run(bot, cmd: str, arg: str):
    """The text for a /command, or UNKNOWN. May be a coroutine result already awaited."""
    if cmd == "status":
        return _status(bot)
    if cmd == "cost":
        return json.dumps({"session": llm.USAGE, "last_30_days": llm.usage_history(30)["totals"]}, indent=1)
    if cmd == "context":
        return _context(bot)
    if cmd == "memory":
        return _memory()
    if cmd == "config":
        return _config(arg)
    if cmd == "permissions":
        return _permissions(bot, arg)
    if cmd == "mode":
        return _mode(bot, arg)
    if cmd == "hooks":
        return _hooks()
    if cmd == "agents":
        return _agents()
    if cmd == "mcp":
        return _mcp()
    if cmd == "export":
        return _export(bot, arg)
    if cmd == "resume" and arg is not None:
        return _resume(bot, arg)
    if cmd == "rewind":
        if arg == "list":
            return "\n".join(f"turn {t['turn']}  {t['ts']}  {len(t['files'])} file(s)  {t['prompt'][:50]}"
                             for t in checkpoints.turns(bot.conversation)) or "No file changes saved."
        return checkpoints.rewind(bot.conversation, int(arg) if arg.isdigit() else 1)
    if cmd == "add-dir":
        return _add_dir(arg)
    if cmd == "init":
        return projectdocs.init(".", force=arg == "--force")
    if cmd == "doctor":
        from . import cli
        return cli.doctor()
    if cmd == "todos":
        return tools.REGISTRY["todo_read"].fn(bot.last_ctx or tools.Context(session=bot.conversation))
    if cmd == "output-style":
        return outputstyles.set_style(arg) if arg else outputstyles.listing()
    if cmd == "statusline":
        return _statusline(arg)
    if cmd == "skills":
        return _skills()
    if cmd in ("login", "bug"):
        return {"login": "Sparx has no account: set provider keys in ~/.sparx/.env (see /model).",
                "bug": "Report problems in the browser-pilot repository; /doctor and /audit make good attachments."}[cmd]
    return UNKNOWN
