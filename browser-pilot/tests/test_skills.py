"""User skills and custom slash commands: discovery, precedence, $ARGUMENTS, allowed-tools."""
import asyncio

import pytest

from sparx import config, skills, tools
from sparx.tools import Context
from conftest import run_tool


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / ".git").mkdir(parents=True)
    monkeypatch.chdir(r)
    return r


def test_parse_frontmatter_and_body():
    meta, body = skills.parse("---\nname: x\ndescription: Does x\nallowed-tools: [now, calculate]\n---\n# Body\nGo.")
    assert meta["description"] == "Does x" and body.startswith("# Body")
    assert skills.tool_list(meta["allowed-tools"]) == ["now", "calculate"]
    assert skills.parse("no frontmatter") == ({}, "no frontmatter")


def test_repo_skill_overrides_user_skill(repo):
    write(config.HOME / "skills" / "greet" / "SKILL.md", "---\ndescription: user version\n---\nuser body")
    write(config.HOME / "skills" / "other" / "SKILL.md", "---\ndescription: only user\n---\nx")
    write(repo / ".sparx" / "skills" / "greet" / "SKILL.md", "---\ndescription: repo version\n---\nrepo body")
    found = skills.skills()
    assert found["greet"]["description"] == "repo version" and found["greet"]["source"] == "repo"
    assert found["other"]["source"] == "user"


def test_catalogue_lists_descriptions_not_bodies(repo):
    write(config.HOME / "skills" / "a" / "SKILL.md", "---\ndescription: Does a\n---\nSECRET BODY")
    assert "a: Does a" in skills.catalogue() and "SECRET BODY" not in skills.catalogue()


async def test_skill_list_and_run_substitutes_arguments(ctx, repo):
    write(config.HOME / "skills" / "review" / "SKILL.md", "---\ndescription: Review code\n---\nReview $ARGUMENTS carefully.")
    listed = await run_tool("skill_list", ctx)
    assert "review" in listed and "Review code" in listed
    out = await run_tool("skill_run", ctx, name="review", args="main.py")
    assert "Review main.py carefully." in out
    assert "No skill named" in await run_tool("skill_run", ctx, name="nope")


async def test_skill_create_then_run_and_no_overwrite(ctx, repo):
    out = await run_tool("skill_create", ctx, name="plan-day", description="Plan a day", body="Step 1.")
    assert "created" in out
    assert "Step 1." in await run_tool("skill_run", ctx, name="plan-day")
    assert "already exists" in await run_tool("skill_create", ctx, name="plan-day", description="d", body="b")
    assert "Error" in await run_tool("skill_create", ctx, name="../evil", description="d", body="b")
    await run_tool("skill_create", ctx, name="local", description="d", body="b", scope="repo")
    assert (repo / ".sparx" / "skills" / "local" / "SKILL.md").exists()


async def test_allowed_tools_restrict_only_that_run(ctx, repo):
    write(config.HOME / "skills" / "narrow" / "SKILL.md", "---\ndescription: d\nallowed-tools: calculate\n---\nOnly math.")
    await run_tool("skill_run", ctx, name="narrow")
    assert "not allowed" in await run_tool("now", ctx)
    assert "4" in await run_tool("calculate", ctx, expression="2+2")
    fresh = Context(session="t2", approve=ctx.approve)          # another run is unaffected
    assert "iso" in await run_tool("now", fresh)


async def test_allowed_tools_cannot_widen_permissions(repo, monkeypatch):
    write(config.HOME / "skills" / "x" / "SKILL.md", "---\ndescription: d\nallowed-tools: skill_create\n---\nbody")
    config.set_key("tool_permissions.skill_create", "deny")
    c = Context(session="t")
    await run_tool("skill_run", c, name="x")
    assert "disabled by the user's permission" in await run_tool("skill_create", c, name="n", description="d", body="b")


def test_custom_command_expansion_and_precedence(repo):
    write(config.HOME / "commands" / "fix.md", "---\ndescription: Fix\n---\nUser fix $ARGUMENTS")
    write(repo / ".sparx" / "commands" / "fix.md", "Repo fix $ARGUMENTS now")
    write(config.HOME / "commands" / "scoped.md", "---\nallowed-tools: now\n---\nTell the time")
    write(config.HOME / "commands" / "model.md", "shadow builtin")

    class Bot:
        ctx_data = {}
    b = Bot()
    assert skills.prepare(b, "/fix bug 12") == "Repo fix bug 12 now" and b.ctx_data == {}
    assert skills.prepare(b, "/scoped") == "Tell the time" and b.ctx_data["skill_allow"] == [["now"]]
    assert skills.prepare(b, "hello") == "hello" and b.ctx_data == {}          # limits do not leak to the next turn
    assert skills.prepare(b, "/model x") == "/model x"                          # built-ins win
    assert skills.prepare(b, "/unknown") == "/unknown"
    assert skills.substitute("no placeholder", "extra") == "no placeholder\n\nextra"


async def test_agent_hides_restricted_tools(fake_llm, ctx):
    from sparx.agent import Agent
    tools.load_all()
    ctx.data["allow_tools"] = [["now"]]
    ctx.active.add("core")
    names = [s["function"]["name"] for s in Agent("a", "sys")._schemas(ctx)]
    assert "now" in names and "calculate" not in names
