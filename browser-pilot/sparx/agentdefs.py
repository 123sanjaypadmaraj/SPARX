"""Custom subagents: Markdown files with frontmatter, the same layout Claude Code uses.

    ~/.sparx/agents/<name>.md      <repo>/.sparx/agents/<name>.md      (repo wins; ~/.claude and <repo>/.claude/agents too)

    ---
    name: reviewer
    description: Reviews diffs for bugs. Use after any code change.
    tools: Read, Grep, Glob            # Claude Code names are mapped to Sparx tools; omit for the read-only default
    ---
    You are a careful reviewer ...     # the body is the subagent's role prompt

Run one with subagent_run(agent="reviewer", task=...). Like skills, an agent file is guidance: its `tools` can only
narrow what a subagent may use, never add the barred tools, and every call still goes through approvals."""
from pathlib import Path

from . import config, skills

# Claude Code tool names -> Sparx tools
CLAUDE_TOOLS = {
    "read": ["code_read"], "grep": ["code_search"], "glob": ["code_glob"], "edit": ["code_edit", "code_multi_edit"],
    "multiedit": ["code_multi_edit"], "write": ["code_write"], "bash": ["shell_run"], "notebookedit": ["notebook_edit"],
    "webfetch": ["web_fetch"], "websearch": ["web_search"], "todowrite": ["todo_write"], "ls": ["code_glob"],
}


def _roots() -> list[Path]:
    repo = skills.repo_root()
    cfg = config.settings().get("skills", {})
    roots = []
    if cfg.get("claude", True):
        user_claude = Path(cfg["claude_dir"]).expanduser() if cfg.get("claude_dir") else (
            config.HOME.parent / ".claude" if config.HOME.name == ".sparx" else None)
        if user_claude:
            roots.append(user_claude)
    roots.append(config.HOME)
    if cfg.get("claude", True):
        roots.append(repo / ".claude")
    roots.append(repo / ".sparx")
    return roots


def map_tools(value) -> list[str] | None:
    """Sparx tool names for a `tools:` list (Claude Code names mapped, Sparx names and globs kept). None = default."""
    names = skills.tool_list(value)
    if not names:
        return None
    out = []
    for n in names:
        out += CLAUDE_TOOLS.get(n.lower(), [n]) if n.lower() in CLAUDE_TOOLS else [n]
    return list(dict.fromkeys(out))


def agents() -> dict[str, dict]:
    found = {}
    for root in _roots():
        base = root / "agents"
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*.md")):
            try:
                meta, body = skills.parse(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = (meta.get("name") or f.stem).lower()
            if not skills.NAME.match(name):
                continue
            found[name] = {"name": name, "description": meta.get("description", "")[:300], "body": body,
                           "tools": map_tools(meta.get("tools")), "path": str(f)}
    return found


def catalogue() -> str:
    return "\n".join(f"- {n}: {a['description'][:90]}" for n, a in sorted(agents().items()))
