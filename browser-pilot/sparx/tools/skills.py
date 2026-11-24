"""User skills: Markdown guidance loaded on demand. See sparx/skills.py."""
from .. import config, skills as sk
from . import S, ToolError, by_category, tool


@tool("skill_list", "List the user's skills with their descriptions. Use it to find a skill that fits the task.",
      category="core")
def skill_list(ctx):
    return [{"name": n, "description": s["description"], "source": s["source"],
             **({"allowed_tools": s["allowed_tools"]} if s["allowed_tools"] else {})}
            for n, s in sorted(sk.skills().items())] or "No skills yet. skill_create makes one."


@tool("skill_run", "Load a skill by name and follow its instructions for the user's task. Pass the task details in args.",
      {"name": S, "args": {**S, "description": "what the skill should be applied to"}}, ["name"], category="core")
def skill_run(ctx, name: str, args: str = ""):
    s = sk.skills().get(name.lower())
    if not s:
        raise ToolError(f"No skill named '{name}'. Available: {', '.join(sorted(sk.skills())) or 'none'}.")
    note = ""
    if s["allowed_tools"]:
        sk.restrict(ctx.data, s["allowed_tools"], key="skill_allow")
        for cat, ts in by_category().items():       # make the permitted tools visible; nothing is widened
            if any(sk.matches(p, t.name) for p in s["allowed_tools"] for t in ts):
                ctx.active.add(cat)
        note = (f"\n\n[For the rest of this run only these tools are available: {', '.join(s['allowed_tools'])}. "
                "Permission rules still apply.]")
    return f"Skill '{s['name']}' ({s['source']}). Follow it for this task:\n\n{sk.substitute(s['body'], args)}{note}"


@tool("skill_create", "Save a new skill (Markdown instructions) for later reuse. scope is 'user' (all projects) or 'repo' (this repo).",
      {"name": S, "description": S, "body": {**S, "description": "the instructions, in Markdown"},
       "allowed_tools": {**S, "description": "optional comma-separated tools to restrict the skill to"},
       "scope": {**S, "description": "user (default) or repo"}},
      ["name", "description", "body"], category="core", risk="write")
def skill_create(ctx, name: str, description: str, body: str, allowed_tools: str = "", scope: str = "user"):
    name = name.strip().lower()
    if not sk.NAME.match(name):
        raise ToolError("Skill names are lowercase letters, digits, - and _ (max 48).")
    if "\n" in description or "\n" in allowed_tools:
        raise ToolError("description and allowed_tools must be one line.")
    f = (sk.repo_root() / ".sparx" if scope == "repo" else config.HOME) / "skills" / name / "SKILL.md"
    if f.exists():
        raise ToolError(f"Skill '{name}' already exists at {f}; choose another name.")
    f.parent.mkdir(parents=True, exist_ok=True)
    head = f"---\nname: {name}\ndescription: {description}\n" + (f"allowed-tools: {allowed_tools}\n" if allowed_tools else "") + "---\n"
    f.write_text(head + body.strip() + "\n", encoding="utf-8")
    return {"created": str(f)}
