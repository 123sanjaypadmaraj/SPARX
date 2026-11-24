import json
import sys

import pytest

from sparx import config, hooks, security, tools

PY = f'"{sys.executable}"'


@pytest.fixture
def echo_tool():
    tools.load_all()
    ran = []

    @tools.tool("hk_echo", "echo", {"x": tools.S}, [], category="core", risk="read")
    def hk_echo(ctx, x: str = "", api_key: str = ""):
        ran.append(x)
        return "echoed " + x
    yield ran
    tools.REGISTRY.pop("hk_echo", None)


def hook(tmp_path, code, name="h.py"):
    f = tmp_path / name
    f.write_text(code, encoding="utf-8")
    return f"{PY} {f}"


def set_hooks(**h):
    config.save_settings({"hooks": h})


async def test_pre_hook_blocks_with_stderr(echo_tool, ctx, tmp_path):
    set_hooks(pre_tool=[{"match": "hk_echo", "command": hook(tmp_path, "import sys;print('no way',file=sys.stderr);sys.exit(2)")}])
    out = await tools.call("hk_echo", {"x": "a"}, ctx)
    assert "blocked by a user hook" in out and "no way" in out and echo_tool == []


async def test_other_nonzero_is_warning_only(echo_tool, ctx, tmp_path):
    set_hooks(pre_tool=[{"command": hook(tmp_path, "import sys;sys.exit(1)")}])
    assert await tools.call("hk_echo", {"x": "a"}, ctx) == "echoed a"
    assert any(r["tool"] == "hook:pre_tool" and r["summary"].startswith("warn") for r in security.audit_log(10))


async def test_timeout_does_not_block(echo_tool, ctx, tmp_path):
    set_hooks(pre_tool=[{"command": hook(tmp_path, "import time;time.sleep(5)"), "timeout": 0.5}])
    assert await tools.call("hk_echo", {"x": "a"}, ctx) == "echoed a"
    assert any(r["summary"].startswith("timeout") for r in security.audit_log(10))


async def test_matching(echo_tool, ctx, tmp_path):
    cmd = hook(tmp_path, "import sys;sys.exit(2)")
    set_hooks(pre_tool=[{"match": "code_edit|code_write", "command": cmd}])
    assert await tools.call("hk_echo", {"x": "a"}, ctx) == "echoed a"
    set_hooks(pre_tool=[{"match": "hk_.*", "command": cmd}])
    assert "blocked" in await tools.call("hk_echo", {"x": "a"}, ctx)


async def test_stdin_payload_and_post_result(echo_tool, ctx, tmp_path):
    out = tmp_path / "seen.jsonl"
    code = f"import sys;open({str(out)!r},'a').write(sys.stdin.read()+'\\n')"
    set_hooks(pre_tool=[{"command": hook(tmp_path, code)}], post_tool=[{"command": hook(tmp_path, code, "p.py")}])
    await tools.call("hk_echo", {"x": "hi", "api_key": "sk-abcdefghijklmnop1234"}, ctx)
    pre, post = [json.loads(line) for line in out.read_text().splitlines()]
    assert pre["event"] == "pre_tool" and pre["tool"] == "hk_echo" and pre["args"]["x"] == "hi"
    assert "sk-abc" not in json.dumps(pre) and "result" not in pre
    assert post["event"] == "post_tool" and post["result"] == "echoed hi"


async def test_no_secrets_in_env(echo_tool, ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secretsecretsecret1234")
    monkeypatch.setenv("MY_TOKEN", "t")
    monkeypatch.setenv("HARMLESS", "yes")
    out = tmp_path / "env.json"
    set_hooks(pre_tool=[{"command": hook(tmp_path, f"import os,json;json.dump(dict(os.environ),open({str(out)!r},'w'))")}])
    await tools.call("hk_echo", {}, ctx)
    env = json.loads(out.read_text())
    assert "GROQ_API_KEY" not in env and "MY_TOKEN" not in env and env.get("HARMLESS") == "yes"
    assert "GROQ_API_KEY" not in hooks.clean_env()


async def test_block_is_audited(echo_tool, ctx, tmp_path):
    assert await tools.call("hk_echo", {"x": "a"}, ctx) == "echoed a"      # no hooks configured: nothing changes
    set_hooks(pre_tool=[{"command": hook(tmp_path, "import sys;sys.exit(2)")}])
    await tools.call("hk_echo", {"x": "a"}, ctx)
    assert any(r["tool"] == "hook:pre_tool" and r["summary"].startswith("block") for r in security.audit_log(10))


def test_settings_set_cannot_write_hooks():
    tools.load_all()
    from sparx.tools import core
    with pytest.raises(tools.ToolError):
        core.settings_set(tools.Context(), "hooks.pre_tool", "[]")
