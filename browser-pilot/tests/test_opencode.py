import subprocess

import pytest

from sparx import config, tools
from sparx.tools import ToolError, opencode


def fake_run(stdout="\x1b[0m\n> plan · m\n\x1b[0m\nhello there\n", stderr="", code=0, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)
    return run


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(opencode, "binary", lambda: "opencode.exe")
    monkeypatch.setattr(opencode, "_down_until", 0.0)


def test_run_strips_banner_and_colours_and_feeds_the_prompt_on_stdin(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", fake_run(seen=seen))
    assert opencode.run("what is 2+2") == "hello there"
    assert seen["input"] == "what is 2+2" and seen["cmd"][:3] == ["opencode.exe", "run", "-m"]
    assert "--auto" not in seen["cmd"] and seen["cmd"][seen["cmd"].index("--agent") + 1] == "plan"


def test_build_mode_auto_approves_and_failure_is_reported(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", fake_run(seen=seen))
    opencode.run("fix it", agent="build", auto=True)
    assert "--auto" in seen["cmd"]
    monkeypatch.setattr(subprocess, "run", fake_run(stdout="", stderr="model not found", code=1))
    with pytest.raises(ToolError, match="model not found"):
        opencode.run("x")


def test_model_comes_from_settings():
    assert opencode.model() == opencode.DEFAULT_MODEL
    config.set_key("models.opencode", "opencode/big-pickle")
    assert opencode.model() == "opencode/big-pickle"


def test_backup_reply_can_be_switched_off_and_backs_off_after_a_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run())
    assert opencode.backup_reply([{"role": "user", "content": "hi"}]) == "hello there"
    config.set_key("models.opencode_fallback", False)
    assert opencode.backup_reply([{"role": "user", "content": "hi"}]) is None
    config.set_key("models.opencode_fallback", True)
    monkeypatch.setattr(subprocess, "run", fake_run(stdout="", stderr="boom", code=1))
    assert opencode.backup_reply([{"role": "user", "content": "hi"}]) is None
    monkeypatch.setattr(subprocess, "run", fake_run())
    assert opencode.backup_reply([{"role": "user", "content": "hi"}]) is None     # still backing off


def test_transcript_keeps_roles_and_flattens_content_lists():
    t = opencode.transcript([{"role": "system", "content": "be brief"},
                             {"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert "[system]\nbe brief" in t and "[user]\nhi" in t


def test_tool_is_registered_and_asks_before_running():
    tools.load_all()
    t = next(t for t in tools.by_category()["coding"] if t.name == "opencode_task")
    assert t.risk == "exec"
