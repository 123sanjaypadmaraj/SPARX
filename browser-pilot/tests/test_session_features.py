"""Session features: checkpoints/rewind, permission modes and rules, extra hook events, custom subagents,
input shortcuts, output styles and the slash commands."""
import asyncio
import sys

import pytest

from sparx import (agentdefs, checkpoints, config, hooks, inputs, outputstyles, permissions, slash, tools)
from sparx.assistant import Sparx
from sparx.tools import ToolError

from conftest import run_tool

PY = f'"{sys.executable}"'


@pytest.fixture
def work(tmp_path):
    config.set_key("allowed_roots", [str(tmp_path)])
    return tmp_path


@pytest.fixture
def probe():
    tools.load_all()
    ran = []

    @tools.tool("px_exec", "needs approval", {"command": tools.S}, [], category="core", risk="exec")
    def px_exec(ctx, command: str = ""):
        ran.append(command)
        return "ran " + command
    yield ran
    tools.REGISTRY.pop("px_exec", None)


def make_ctx(approve=None, **data):
    return tools.Context(session="conv1", approve=approve, data=dict(data))


# ---- checkpoints

def test_rewind_restores_edited_and_removes_created_files(work):
    tools.load_all()
    f, g = work / "a.txt", work / "b.txt"
    f.write_text("original\n", encoding="utf-8")
    ctx = make_ctx(lambda t, a: asyncio.sleep(0, True))
    ctx.data["checkpoint_turn"] = checkpoints.begin("conv1", "change things")
    asyncio.run(run_tool("code_read", ctx, path=str(f)))
    asyncio.run(run_tool("code_edit", ctx, path=str(f), old="original", new="changed"))
    asyncio.run(run_tool("code_write", ctx, path=str(g), content="new file\n"))
    assert f.read_text(encoding="utf-8").startswith("changed") and g.exists()
    assert checkpoints.turns("conv1")[0]["prompt"] == "change things"

    msg = checkpoints.rewind("conv1")
    assert f.read_text(encoding="utf-8") == "original\n"
    assert not g.exists() and "created" in msg
    assert checkpoints.turns("conv1") == []
    assert "No file changes" in checkpoints.rewind("conv1")


def test_rewind_goes_back_one_turn_at_a_time(work):
    f = work / "a.txt"
    f.write_text("v0", encoding="utf-8")
    for n, text in enumerate(("v1", "v2"), 1):
        ctx = make_ctx()
        ctx.data["checkpoint_turn"] = checkpoints.begin("conv1", f"turn {n}")
        checkpoints.snapshot(ctx, "code_write", {"path": str(f)})
        f.write_text(text, encoding="utf-8")
    checkpoints.rewind("conv1", 1)
    assert f.read_text(encoding="utf-8") == "v1"
    checkpoints.rewind("conv1", 1)
    assert f.read_text(encoding="utf-8") == "v0"


def test_snapshot_ignores_other_tools_and_missing_turn(work):
    ctx = make_ctx()
    checkpoints.snapshot(ctx, "code_write", {"path": str(work / "x")})      # no turn open
    ctx.data["checkpoint_turn"] = checkpoints.begin("conv1", "p")
    checkpoints.snapshot(ctx, "shell_run", {"path": str(work / "x")})       # not a file tool
    assert checkpoints.turns("conv1") == []


# ---- permission modes and rules

def test_rule_matching():
    assert permissions.matches("shell_run", "shell_run", {"command": "ls"})
    assert permissions.matches("shell_run(git status*)", "shell_run", {"command": "git status -s"})
    assert not permissions.matches("shell_run(git status*)", "shell_run", {"command": "rm -rf x"})
    assert permissions.matches("code_*", "code_edit", {})
    assert not permissions.matches("code_edit", "code_write", {})


def test_allow_rule_skips_the_prompt_and_deny_beats_allow(probe):
    asked = []

    async def approve(tool, args):
        asked.append(tool.name)
        return False
    ctx = make_ctx(approve)
    assert "Not done" in asyncio.run(run_tool("px_exec", ctx, command="ls"))
    permissions.add_rule("allow", "px_exec(ls*)")
    assert asyncio.run(run_tool("px_exec", make_ctx(approve), command="ls -la")) == "ran ls -la"
    assert "Not done" in asyncio.run(run_tool("px_exec", make_ctx(approve), command="rm x"))
    permissions.add_rule("deny", "px_exec(ls*)")
    out = asyncio.run(run_tool("px_exec", make_ctx(approve), command="ls"))
    assert "disabled" in out and probe == ["ls -la"]
    assert "Removed" in permissions.remove_rule("px_exec(ls*)")


def test_modes(probe):
    deny = make_ctx(lambda t, a: asyncio.sleep(0, False), permission_mode="bypass")
    assert asyncio.run(run_tool("px_exec", deny, command="x")) == "ran x"       # bypass asks nothing
    ctx = make_ctx(lambda t, a: asyncio.sleep(0, False), permission_mode="accept_edits")
    assert "Not done" in asyncio.run(run_tool("px_exec", ctx, command="x"))     # commands still ask
    t = tools.REGISTRY["code_edit"]
    assert permissions.decide(ctx, t, {}, "ask")[0] == "allow"                  # edits do not
    assert permissions.decide(make_ctx(), t, {}, "ask")[0] == "ask"


def test_bypass_never_lifts_a_deny_policy_or_rule(probe):
    config.set_key("tool_permissions", {"px_exec": "deny"})
    ctx = make_ctx(permission_mode="bypass")
    assert "disabled" in asyncio.run(run_tool("px_exec", ctx, command="x"))


def test_always_allow_lasts_for_the_session(probe):
    answers = iter(["always"])

    async def approve(tool, args):
        return next(answers)
    ctx = make_ctx(approve)
    assert asyncio.run(run_tool("px_exec", ctx, command="a")) == "ran a"
    assert asyncio.run(run_tool("px_exec", ctx, command="b")) == "ran b"        # no second prompt (iterator is spent)
    assert ctx.data["session_allow"] == ["px_exec"]


def test_shell_always_allow_is_narrowed_to_the_command():
    ctx = make_ctx()
    assert permissions.allow_for_session(ctx, "shell_run", {"command": "git status"}) == "shell_run(git status)"


# ---- hooks

def hook(tmp_path, code):
    f = tmp_path / "h.py"
    f.write_text(code, encoding="utf-8")
    return f"{PY} {f}"


def test_user_prompt_hook_blocks_and_adds_context(tmp_path):
    ctx = make_ctx()
    config.set_key("hooks.user_prompt", [{"command": hook(tmp_path, "import sys; print('extra info')")}])
    assert asyncio.run(hooks.fire_prompt(ctx, "hi")) == (None, "extra info")
    config.set_key("hooks.user_prompt", [{"command": hook(tmp_path, "import sys; sys.stderr.write('no'); sys.exit(2)")}])
    assert asyncio.run(hooks.fire_prompt(ctx, "hi")) == ("no", "")


def test_blocked_prompt_never_reaches_the_model(tmp_path, fake_llm):
    config.set_key("hooks.user_prompt", [{"command": hook(tmp_path, "import sys; sys.exit(2)")}])
    out = asyncio.run(Sparx().turn("do it"))
    assert "blocked by a hook" in out and not fake_llm.seen


def test_info_hook_receives_payload(tmp_path):
    log = tmp_path / "log.txt"
    code = f"import sys, json; open({str(log)!r}, 'w').write(json.load(sys.stdin)['event'])"
    config.set_key("hooks.subagent_stop", [{"command": hook(tmp_path, code)}])
    asyncio.run(hooks.fire_info("subagent_stop", make_ctx(), report="x"))
    assert log.read_text() == "subagent_stop"


# ---- custom subagents

def test_agent_files_map_claude_tool_names(tmp_path, monkeypatch):
    d = config.HOME / "agents"
    d.mkdir(parents=True)
    (d / "scout.md").write_text("---\nname: scout\ndescription: Finds things\ntools: Read, Grep, Glob, git_*\n---\n"
                                "You only look.", encoding="utf-8")
    a = agentdefs.agents()["scout"]
    assert a["tools"] == ["code_read", "code_search", "code_glob", "git_*"] and a["body"] == "You only look."
    assert "scout: Finds things" in agentdefs.catalogue()


def test_subagent_run_uses_custom_agent_prompt_and_tools(fake_llm):
    d = config.HOME / "agents"
    d.mkdir(parents=True)
    (d / "scout.md").write_text("---\nname: scout\ntools: Read\n---\nYou only look.", encoding="utf-8")
    fake_llm.script = ["found it"]
    out = asyncio.run(run_tool("subagent_run", make_ctx(), task="find x", agent="scout"))
    assert "found it" in out
    assert "You only look." in fake_llm.seen[0][0]["content"]
    with pytest.raises(ToolError):
        from sparx.tools import subagent
        asyncio.run(subagent._one(make_ctx(), "t", None, "", 0, agent="nope"))


# ---- input shortcuts

def test_mentions_attach_files_but_not_secrets(tmp_path):
    (tmp_path / "notes.txt").write_text("hello\nworld\n", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=1\n", encoding="utf-8")
    prompt, notes = inputs.expand_mentions('look at @notes.txt and @.env and @missing.py', tmp_path)
    assert "1\thello" in prompt and "KEY=1" not in prompt and "<untrusted" in prompt
    assert "@notes.txt: attached" in notes and any("protected" in n for n in notes)
    assert inputs.expand_mentions("mail me@example.com", tmp_path)[0] == "mail me@example.com"


def test_shell_shortcut_and_remember(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    assert "hi" in inputs.run_shell("echo hi", tmp_path)
    assert inputs.remember("use tabs").endswith("SPARX.md")
    assert "- use tabs" in (tmp_path / "SPARX.md").read_text()
    inputs.remember("##global note")
    assert "- global note" in (config.HOME / "SPARX.md").read_text()


def test_preprocess_routes_shortcuts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bot = Sparx()
    prompt, shown = slash.preprocess(bot, "!echo ok")
    assert prompt is None and "ok" in shown
    assert slash.preprocess(bot, "#remember this")[0] is None
    assert slash.preprocess(bot, "/review src")[0].startswith("Review the current uncommitted changes (src)")
    assert slash.preprocess(bot, "/model x") == ("/model x", "")
    assert slash.preprocess(bot, "# heading in a pasted doc")[0] is None      # documented: '#' at the start is a note


# ---- output styles, modes and slash commands

def test_output_style_prompt_and_custom_file():
    assert outputstyles.prompt_block() == ""
    assert "Output style (concise)" in (outputstyles.set_style("concise") and outputstyles.prompt_block())
    d = config.HOME / "output-styles"
    d.mkdir(parents=True)
    (d / "pirate.md").write_text("---\ndescription: Arr\n---\nTalk like a pirate.", encoding="utf-8")
    assert "pirate" in outputstyles.styles() and "Unknown" in outputstyles.set_style("nope")
    outputstyles.set_style("pirate")
    assert "Talk like a pirate." in Sparx().system("")


def test_mode_cycling_and_plan_mode_sync():
    bot = Sparx()
    assert bot.mode == "default"
    assert [bot.cycle_mode() for _ in range(4)] == ["accept_edits", "plan", "bypass", "default"]
    bot.set_mode("plan")
    assert bot.plan_mode is True
    bot.set_mode("accept_edits")
    assert bot.plan_mode is False


def test_slash_commands_return_text():
    bot = Sparx()
    for cmd in ("status", "cost", "context", "memory", "hooks", "agents", "mcp", "skills", "todos", "sessions"):
        out = asyncio.run(slash.run(bot, "resume" if cmd == "sessions" else cmd, ""))
        assert isinstance(out, str) and out, cmd
    assert "mode: default" in asyncio.run(slash.run(bot, "permissions", ""))
    assert asyncio.run(slash.run(bot, "permissions", "allow x")) == "allow: x"
    assert "allow: x" in asyncio.run(slash.run(bot, "permissions", ""))
    assert "Plan mode" in asyncio.run(slash.run(bot, "mode", "plan")) and bot.plan_mode
    assert asyncio.run(slash.run(bot, "nope", "")) is slash.UNKNOWN
    assert "tokens" in asyncio.run(slash.run(bot, "context", ""))


def test_export_and_resume(tmp_path, monkeypatch):
    from sparx import db
    monkeypatch.chdir(tmp_path)
    bot = Sparx()
    db.many("INSERT INTO messages (conversation, role, content, ts, scope) VALUES (?,?,?,?,?)",
            [(bot.conversation, "user", "hello there", db.now(), "personal"),
             (bot.conversation, "assistant", "hi!", db.now(), "personal")])
    out = asyncio.run(slash.run(bot, "export", "chat.md"))
    assert "2 messages" in out and "hello there" in (tmp_path / "chat.md").read_text(encoding="utf-8")
    conv = bot.conversation
    bot.new_conversation()
    assert conv in asyncio.run(slash.run(bot, "resume", ""))
    assert asyncio.run(slash.run(bot, "resume", conv)).startswith("Resumed") and bot.conversation == conv


def test_cli_routes_new_commands_and_keeps_bare_resume():
    from sparx import cli, security
    bot = Sparx()
    out = cli._command(bot, "/status")
    assert asyncio.iscoroutine(out) and "model" in asyncio.run(out)
    security.request_stop()
    assert cli._command(bot, "/resume") == "Stop cleared."
    assert not security.stop_requested()
