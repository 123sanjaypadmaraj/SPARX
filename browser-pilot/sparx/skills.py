"""User skills and slash commands: Markdown files with optional frontmatter.

    ~/.sparx/skills/<name>/SKILL.md      <repo>/.sparx/skills/<name>/SKILL.md      (repo wins over user)
    ~/.sparx/commands/<name>.md          <repo>/.sparx/commands/<name>.md          (repo wins over user)

The same layouts under ~/.claude and <repo>/.claude are read too, so Claude Code skills and commands work unchanged
(order, later wins: ~/.claude, ~/.sparx, <repo>/.claude, <repo>/.sparx). Turn it off with the `skills.claude` setting;
`skills.claude_dir` points at a ~/.claude elsewhere (needed when SPARX_HOME is not a folder named .sparx).

Frontmatter keys: name, description, allowed-tools (comma/space list or [a, b]; globs allowed). A skill or command is
guidance only: it never changes permissions, and allowed-tools can only RESTRICT the tools of the current run.
"""
import fnmatch
import re
from pathlib import Path

from . import config

CATALOGUE_CHARS = 2500
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
BUILTIN = {"tools", "load", "scope", "model", "usage", "audit", "undo", "stop", "resume", "new", "exit", "quit",
           "help", "clear", "cls", "status", "cost", "context", "memory", "config", "permissions", "mode", "hooks",
           "agents", "mcp", "export", "sessions", "rewind", "add-dir", "init", "doctor", "todos", "output-style",
           "statusline", "skills", "review", "commit", "test", "vim", "login", "bug", "plan", "compact", "models"}


def parse(text: str) -> tuple[dict, str]:
    """(frontmatter, body). Frontmatter is flat `key: value` lines between two --- lines."""
    text = text.lstrip("﻿")
    m = re.match(r"---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return {}, text.strip()
    meta = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip():
            meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, text[m.end():].strip()


def tool_list(value) -> list[str] | None:
    if not value:
        return None
    return [p for p in re.split(r"[,\s]+", str(value).strip("[] ")) if p] or None


def repo_root() -> Path:
    """Nearest folder from the working directory upwards that has .sparx or .git (else the working directory)."""
    cwd = Path.cwd().resolve()
    for d in (cwd, *cwd.parents):
        if (d / ".sparx").is_dir() or (d / ".git").exists():
            return d
    return cwd


def _scan(kind: str) -> dict[str, dict]:
    found = {}
    repo = repo_root()
    roots = [(config.HOME, "user"), (repo / ".sparx", "repo")]
    cfg = config.settings().get("skills", {})
    user_claude = Path(cfg["claude_dir"]).expanduser() if cfg.get("claude_dir") else (
        config.HOME.parent / ".claude" if config.HOME.name == ".sparx" else None)       # beside ~/.sparx, so tests stay isolated
    if cfg.get("claude", True) and user_claude:
        roots = [(user_claude, "user"), roots[0], (repo / ".claude", "repo"), roots[1]]
    for root, where in roots:                                                          # later replaces earlier
        base = root / ("skills" if kind == "skill" else "commands")
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*/SKILL.md") if kind == "skill" else base.glob("*.md")):
            try:
                meta, body = parse(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            stem = f.parent.name if kind == "skill" else f.stem
            name = (meta.get("name") or stem).lower() if kind == "skill" else stem.lower()
            desc = meta.get("description") or next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), "")
            found[name] = {"kind": kind, "name": name, "description": desc[:300], "body": body, "path": str(f),
                           "allowed_tools": None if root.name == ".claude" else tool_list(meta.get("allowed-tools")),
                           "source": where}       # Claude Code tool names mean nothing here; they must not lock the run
    return found


def skills() -> dict[str, dict]:
    return _scan("skill")


def commands() -> dict[str, dict]:
    return _scan("command")


def catalogue() -> str:
    """One line per skill for the system prompt: names and short descriptions only, never bodies. Past the size cap the
    remaining skills are listed by name (skill_list still shows everything)."""
    out, size, rest = [], 0, []
    for n, s in sorted(skills().items()):
        line = f"- {n}: {s['description'][:90]}"
        if size + len(line) > CATALOGUE_CHARS:
            rest.append(n)
            continue
        out.append(line)
        size += len(line) + 1
    if rest:
        out.append("- more (names only): " + ", ".join(rest))
    return "\n".join(out)


def substitute(body: str, args: str) -> str:
    return body.replace("$ARGUMENTS", args) if "$ARGUMENTS" in body else (f"{body}\n\n{args}" if args else body)


def matches(pattern: str, name: str) -> bool:
    return fnmatch.fnmatchcase(name, pattern)


def restricted(data: dict, name: str) -> str | None:
    """Why `name` may not run under these context limits (ctx.data), or None. Used by tools.call and Agent."""
    for allow in (data.get("allow_tools") or []) + (data.get("skill_allow") or []):
        if name not in ("skill_run", "skill_list") and not any(matches(p, name) for p in allow):
            return f"'{name}' is not allowed in this run (allowed: {', '.join(allow)})."
    if any(matches(p, name) for p in data.get("deny_tools") or []):
        return f"'{name}' is denied in this run."
    return None


def restrict(data: dict, allow: list[str] | None = None, deny: list[str] | None = None, key: str = "allow_tools"):
    """Add limits to ctx.data. Every allow list added must match (they intersect); deny lists accumulate."""
    if allow:
        data.setdefault(key, []).append(list(allow))
    if deny:
        data.setdefault("deny_tools", []).extend(deny)


def prepare(bot, line: str) -> str:
    """For the chat loops: expand a custom /command into a prompt and set its tool limits for that turn only.
    Plain text, built-in commands and unknown names come back unchanged."""
    bot.ctx_data = {}
    if not line.startswith("/"):
        return line
    name, _, arg = line[1:].partition(" ")
    cmd = commands().get(name.lower())
    if not cmd or name.lower() in BUILTIN:
        return line
    restrict(bot.ctx_data, cmd["allowed_tools"], key="skill_allow")
    return substitute(cmd["body"], arg.strip())
