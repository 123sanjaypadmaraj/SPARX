"""Headless `sparx run`: output formats, exit codes, approval denial, stdin, tool limits, resume, sessions."""
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sparx import config, headless, tools
from sparx.tools import tool, category

category("testhl", "headless test tools")


@tool("hl_danger", "Pretend to run a command.", category="testhl", risk="exec")
def hl_danger(ctx):
    return "ran"


def args(*prompt, **kw):
    d = dict(prompt=list(prompt), output_format="json", max_turns=None, allowed_tools=None, deny_tools=None,
             yes=False, permission_mode="deny", continue_=False, resume=None)
    d.update(kw)
    return SimpleNamespace(**d)


def run(a, stdin=None):
    out = io.StringIO()
    code = headless.main(a, stdin=stdin or io.StringIO(""), out=out)
    return code, out.getvalue()


def test_json_output_has_text_tools_usage_duration(fake_llm):
    fake_llm.script = [("calculate", {"expression": "6*7"}), "The answer is 42."]
    code, out = run(args("what is 6*7"))
    r = json.loads(out)
    assert code == 0 and r["ok"] and r["result"] == "The answer is 42."
    assert [c["tool"] for c in r["tool_calls"]] == ["calculate"] and r["tool_calls"][0]["ok"] is True
    assert set(r["usage"]) >= {"prompt_tokens", "completion_tokens"} and r["duration_ms"] >= 0


def test_text_format_prints_only_the_answer(fake_llm):
    fake_llm.script = ["hello there"]
    code, out = run(args("hi", output_format="text"))
    assert code == 0 and out.strip() == "hello there"


def test_stream_json_emits_events_then_result(fake_llm):
    fake_llm.script = [("now", {}), "done"]
    code, out = run(args("time?", output_format="stream-json"))
    lines = [json.loads(l) for l in out.splitlines()]
    assert [l["type"] for l in lines] == ["tool_start", "tool_end", "result"] and code == 0


def test_approval_needed_is_denied_and_exits_2(fake_llm):
    fake_llm.script = [("hl_danger", {}), "I could not run it."]
    code, out = run(args("do it"))
    r = json.loads(out)
    assert code == 2 and r["exit_code"] == 2 and r["approvals_denied"][0]["tool"] == "hl_danger"
    assert not r["tool_calls"]
    assert "Not done" in json.dumps(fake_llm.seen[-1])


def test_yes_approves(fake_llm):
    fake_llm.script = [("hl_danger", {}), "ok"]
    code, out = run(args("do it", yes=True))
    assert code == 0 and json.loads(out)["tool_calls"][0]["tool"] == "hl_danger"
    fake_llm.script = [("hl_danger", {}), "ok"]
    assert run(args("do it", permission_mode="yes"))[0] == 0


def test_error_exits_1(fake_llm, monkeypatch):
    async def boom(messages, schemas):
        raise RuntimeError("model down")
    monkeypatch.setattr(fake_llm, "chat", boom)
    code, out = run(args("x"))
    assert code == 1 and json.loads(out)["ok"] is False and "model down" in json.loads(out)["error"]


def test_empty_prompt_exits_1():
    assert run(args())[0] == 1


def test_prompt_from_stdin(fake_llm):
    fake_llm.script = ["got it"]
    code, out = run(args(), stdin=io.StringIO("summarise this log\n"))
    assert code == 0 and "summarise this log" in json.dumps(fake_llm.seen[0])
    fake_llm.script = ["got it"]
    run(args("Review:", "-"), stdin=io.StringIO("DIFF HERE"))
    flat = json.dumps(fake_llm.seen[1])
    assert "Review:" in flat and "DIFF HERE" in flat


def test_allowed_and_denied_tools(fake_llm):
    fake_llm.script = [("now", {}), "done"]
    r = json.loads(run(args("t", allowed_tools="calculate"))[1])
    assert not r["tool_calls"] and "not allowed" in json.dumps(fake_llm.seen[-1])
    fake_llm.script = [("calculate", {"expression": "1+1"}), "done"]
    r = json.loads(run(args("t", deny_tools="calc*"))[1])
    assert not r["tool_calls"] and "denied" in json.dumps(fake_llm.seen[-1])


def test_max_turns(fake_llm):
    fake_llm.script = [("now", {})] * 5 + ["gave up"]
    r = json.loads(run(args("loop", max_turns=2))[1])
    assert len(r["tool_calls"]) == 2


def test_continue_and_resume_and_sessions(fake_llm):
    fake_llm.script = ["first"]
    first = json.loads(run(args("remember kiwi"))[1])["conversation"]
    fake_llm.script = ["second"]
    other = json.loads(run(args("something else"))[1])["conversation"]
    assert first != other
    rows = headless.sessions()
    assert [r["id"] for r in rows] == [other, first] and rows[1]["first_prompt"] == "remember kiwi"
    fake_llm.script = ["third"]
    r = json.loads(run(args("and now?", resume=first))[1])
    assert r["conversation"] == first and "remember kiwi" in json.dumps(fake_llm.seen[-1])
    fake_llm.script = ["fourth"]
    assert json.loads(run(args("again", continue_=True))[1])["conversation"] == first    # latest activity
    assert run(args("x", resume="nope-id"))[0] == 1


def test_continue_with_nothing_to_continue():
    assert run(args("x", continue_=True))[0] == 1


def test_run_does_not_touch_interactive_conversation(fake_llm):
    from sparx.assistant import kv_get, kv_set
    kv_set("conversation", "chat-1")
    fake_llm.script = ["ok"]
    run(args("hi"))
    assert kv_get("conversation") == "chat-1"


STUB = '''
import sys, json
sys.path.insert(0, {root!r})
from sparx import config, llm, cli
config.HOME = __import__("pathlib").Path({home!r})
from browserpilot.llm import Reply
class Stub:
    spec = "stub/model"
    async def chat(self, messages, schemas):
        return Reply("stub says hi", [], {{"role": "assistant", "content": "stub says hi"}})
llm.get = lambda role="default": Stub()
sys.argv = ["sparx", "run", "--output-format", "json", "hello"]
cli.main()
'''


def test_real_subprocess_smoke(tmp_path):
    root = Path(__file__).resolve().parent.parent
    script = tmp_path / "stub_run.py"
    script.write_text(STUB.format(root=str(root), home=str(tmp_path / "h")), encoding="utf-8")
    p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=90,
                       stdin=subprocess.DEVNULL, cwd=root)
    assert p.returncode == 0, p.stderr[-800:]
    assert json.loads(p.stdout.strip().splitlines()[-1])["result"] == "stub says hi"
