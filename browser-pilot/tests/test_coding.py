import json
import subprocess
from pathlib import Path

import pytest

from sparx import config, db, jobs, undo
from sparx.tools import ToolError, coding

from conftest import run_tool

PY = '''"""Maths helpers."""


def add(a: int, b: int = 1) -> int:
    """Add two numbers.

    More detail."""
    return a + b


class Shape(dict):
    """A shape."""

    async def area(self, scale=1.0):
        """Area of it."""
        return 0  # TODO: implement area
'''

JS = '''export function greet(name) { return "hi " + name }
export default class Widget extends Base {}
export const double = (x) => x * 2
const hidden = async (a, b) => a
export interface Props { a: string }
'''


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "node_modules" / "x").mkdir(parents=True)
    (r / "pkg" / "maths.py").write_text(PY, encoding="utf-8")
    (r / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (r / "main.py").write_text("from pkg.maths import add\nprint(add(1))\n", encoding="utf-8")
    (r / "web.ts").write_text(JS, encoding="utf-8")
    (r / "node_modules" / "x" / "index.js").write_text("export function junk() {}\n", encoding="utf-8")
    (r / "secret.log").write_text("ignored\n", encoding="utf-8")
    (r / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (r / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = ["fastapi>=0.1", "httpx"]\n'
                                      '[project.scripts]\ndemo = "pkg.maths:add"\n', encoding="utf-8")
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Tester")
    git(r, "config", "commit.gpgsign", "false")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "initial")
    config.set_key("allowed_roots", [str(tmp_path)])
    return r


# ---- path safety

def test_safe_repo_rejects_system_and_private_paths(repo):
    assert coding._safe_repo(repo) == repo.resolve()
    for bad in (coding._system_dirs()[0], config.HOME / "sparx.db", Path.home() / ".ssh" / "id_rsa", repo / ".git" / "config"):
        with pytest.raises(ToolError):
            coding._safe_repo(bad)


def test_safe_repo_rejects_outside_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    with pytest.raises(ToolError, match="outside"):
        coding._safe_repo(tmp_path / "elsewhere")
    config.set_key("allowed_roots", [str(tmp_path / "elsewhere")])
    assert coding._safe_repo(tmp_path / "elsewhere" / "a.py")


# ---- repo map

def test_repo_map_symbols_stack_and_ignores(repo):
    m = coding._repo_map(repo)
    assert m["cached"] is False
    files = {f"{d}/{n}" for d, names in m["tree"].items() for n in names}
    assert "pkg/maths.py" in files and "./main.py" in files
    assert not any("node_modules" in f or f.endswith(".log") for f in files)
    assert m["symbols"]["pkg/maths.py"] == [
        "L4 def add(a: int, b: int=1) -> int  # Add two numbers.",
        "L11 class Shape(dict)  # A shape.",
        "  L14 async def area(self, scale=1.0)  # Area of it."]
    ts = "\n".join(m["symbols"]["web.ts"])
    assert all(n in ts for n in ("greet", "Widget", "double", "hidden", "Props"))
    assert m["languages"]["Python"] == len(PY.splitlines()) + 2
    assert "Python (pyproject.toml)" in m["stack"] and "fastapi" in m["stack"]
    assert m["dependencies"]["pyproject.toml"] == ["fastapi>=0.1", "httpx"]
    assert "demo -> pkg.maths:add (console script)" in m["entry_points"] and "main.py" in m["entry_points"]


def test_repo_map_is_cached_until_a_file_changes(repo):
    coding._repo_map(repo)
    assert coding._repo_map(repo)["cached"] is True
    (repo / "extra.py").write_text("def new_one():\n    pass\n", encoding="utf-8")
    m = coding._repo_map(repo)
    assert m["cached"] is False and "extra.py" in m["symbols"]


def test_files_without_git_uses_gitignore(tmp_path, monkeypatch):
    d = tmp_path / "plain"
    (d / "build").mkdir(parents=True)
    for name in ("a.py", "b.log", "build/out.py", "pic.png"):
        (d / name).write_text("x = 1\n", encoding="utf-8")
    (d / ".gitignore").write_text("*.log\n", encoding="utf-8")
    monkeypatch.setattr(coding.shutil, "which", lambda name: None)
    assert coding._files(d) == [".gitignore", "a.py"]


async def test_repo_map_tool_output_is_untrusted(ctx, repo):
    out = await run_tool("repo_map", ctx, path=str(repo))
    assert out.startswith('<untrusted source="tool:repo_map">') and "def add" in out


def test_js_outline_marks_exports():
    syms = {s["name"]: s for s in coding._js_outline(JS)["symbols"]}
    assert syms["greet"]["exported"] and syms["greet"]["kind"] == "function"
    assert syms["Widget"]["kind"] == "class" and syms["Props"]["kind"] == "type"
    assert syms["hidden"]["exported"] is False and syms["double"]["kind"] == "function"


def test_outline_reports_syntax_error():
    assert "syntax error at line 1" in coding._py_outline("def broken(:\n")["error"]


# ---- search / read

def test_code_search_and_read(ctx, repo, monkeypatch):
    monkeypatch.setattr(coding.shutil, "which", lambda name: None)      # force the Python fallback
    r = coding.code_search(ctx, str(repo), r"def \w+\(", "*.py")
    assert r["matches"] == ["pkg/maths.py:4: def add(a: int, b: int = 1) -> int:",
                            "pkg/maths.py:14: async def area(self, scale=1.0):"]
    assert coding.code_search(ctx, str(repo), "greet", "*.py")["count"] == 0
    with pytest.raises(ToolError, match="bad regular expression"):
        coding.code_search(ctx, str(repo), "(")
    text = coding.code_read(ctx, str(repo / "pkg" / "maths.py"), 4, 5)
    assert "    4| def add(a: int, b: int = 1) -> int:" in text and "showing 4-5" in text and "class Shape" not in text


# ---- editing

def test_code_edit_must_match_exactly_once(ctx, repo):
    f = repo / "dup.py"
    f.write_text("x = 1\nx = 1\ny = 2\n", encoding="utf-8")
    with pytest.raises(ToolError, match="has not been read"):      # read-before-edit
        coding.code_edit(ctx, str(f), "y = 2", "y = 3")
    coding.code_read(ctx, str(f))
    with pytest.raises(ToolError, match="occurs 2 times"):
        coding.code_edit(ctx, str(f), "x = 1", "x = 9")
    with pytest.raises(ToolError, match="not found"):
        coding.code_edit(ctx, str(f), "z = 1", "z = 2")
    assert f.read_text(encoding="utf-8") == "x = 1\nx = 1\ny = 2\n"
    r = coding.code_edit(ctx, str(f), "y = 2", "y = 3")
    assert r["replacements"] == 1 and "-y = 2" in r["diff"] and "+y = 3" in r["diff"]
    assert coding.code_edit(ctx, str(f), "x = 1", "x = 9", replace_all=True)["replacements"] == 2
    assert f.read_text(encoding="utf-8") == "x = 9\nx = 9\ny = 3\n"


def test_code_edit_keeps_crlf(ctx, repo):
    f = repo / "win.py"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    coding.code_read(ctx, str(f))
    coding.code_edit(ctx, str(f), "a = 1\nb = 2", "a = 1\nb = 3")
    assert f.read_bytes() == b"a = 1\r\nb = 3\r\n"


def test_edit_and_write_are_undoable(ctx, repo):
    f = repo / "main.py"
    original = f.read_bytes()
    coding.code_read(ctx, str(f))
    coding.code_edit(ctx, str(f), "add(1)", "add(2)")
    new = repo / "sub" / "new.py"
    w = coding.code_write(ctx, str(new), "print('hi')\n")
    assert w["created"] and "+print('hi')" in w["diff"] and new.is_file()
    assert undo.run().startswith("Undone: Wrote")
    assert not new.exists() and list(config.path("trash").parent.glob("trash/*new.py"))
    assert undo.run().startswith("Undone: Edited")
    assert f.read_bytes() == original


async def test_write_outside_allowed_area_is_refused(ctx, repo):
    out = await run_tool("code_write", ctx, path=str(config.HOME / "settings.json"), content="{}")
    assert out.startswith("Error:") and "protected" in out


# ---- test output parsing

PYTEST_OUT = """
============================= test session starts =============================
collected 5 items

tests/test_a.py .F.                                                      [ 60%]
tests/test_b.py sE                                                       [100%]

=================================== ERRORS ====================================
________________________ ERROR at setup of test_needs_db ______________________
tests/test_b.py:5: in db
    raise RuntimeError("no database")
E   RuntimeError: no database
================================== FAILURES ===================================
_______________________________ TestMath.test_add _____________________________
tests/test_a.py:9: in test_add
    assert add(1, 2) == 4
E   assert 3 == 4
=========================== short test summary info ===========================
FAILED tests/test_a.py::TestMath::test_add - assert 3 == 4
ERROR tests/test_b.py::test_needs_db - RuntimeError: no database
============== 1 failed, 2 passed, 1 skipped, 1 error in 0.12s ================
"""


def test_parse_pytest_counts_names_and_tracebacks():
    r = coding.parse_pytest(PYTEST_OUT)
    assert (r["passed"], r["failed"], r["errors"], r["skipped"]) == (2, 1, 1, 1)
    fail, err = r["failures"]
    assert fail["name"] == "tests/test_a.py::TestMath::test_add" and fail["message"] == "assert 3 == 4"
    assert "assert add(1, 2) == 4" in fail["traceback"] and "RuntimeError" not in fail["traceback"]
    assert err["kind"] == "error" and "no database" in err["traceback"]


def test_parse_pytest_all_passing_and_quiet():
    r = coding.parse_pytest("...\n3 passed in 0.01s\n")
    assert r["passed"] == 3 and r["failed"] == 0 and r["failures"] == []


def test_parse_other_runners():
    jest = coding.parse_js_tests("FAIL src/a.test.js\n  ● math › adds\n\nTests:       1 failed, 4 passed, 5 total\n")
    assert (jest["failed"], jest["passed"]) == (1, 4) and "math › adds" in [f["name"] for f in jest["failures"]]
    node = coding.parse_js_tests("not ok 2 - subtracts\n# tests 3\n# pass 2\n# fail 1\n")
    assert (node["passed"], node["failed"], node["failures"][0]["name"]) == (2, 1, "subtracts")
    cargo = coding.parse_cargo("test a::ok ... ok\ntest a::bad ... FAILED\n\n---- a::bad stdout ----\npanicked at 'boom'\n\n"
                               "failures:\n    a::bad\n\ntest result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured\n")
    assert (cargo["passed"], cargo["failed"]) == (1, 1) and "boom" in cargo["failures"][0]["traceback"]
    go = coding.parse_go("=== RUN   TestA\n--- PASS: TestA (0.00s)\n=== RUN   TestB\n--- FAIL: TestB (0.00s)\n    x_test.go:9: got 1\nFAIL\n")
    assert (go["passed"], go["failed"]) == (1, 1) and "got 1" in go["failures"][0]["traceback"]


def test_parse_lint_lines():
    f = coding.parse_lint("pkg/a.py:3:1: F401 `os` imported but unused\nFound 1 error.\nsrc/x.ts(4,7): error TS2322: bad type\n", "ruff")
    assert f[0] == {"file": "pkg/a.py", "line": 3, "col": 1, "message": "F401 `os` imported but unused", "tool": "ruff"}
    assert f[1]["file"] == "src/x.ts" and f[1]["line"] == 4 and "TS2322" in f[1]["message"]


def _add_tests(repo, body):
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_maths.py").write_text("from pkg.maths import add\n\n" + body, encoding="utf-8")


def test_run_tests_real_pytest(repo):
    _add_tests(repo, "def test_ok():\n    assert add(1, 2) == 3\n\ndef test_bad():\n    assert add(1, 2) == 4\n")
    r = coding._run_tests(repo)
    assert r["ok"] is False and (r["passed"], r["failed"]) == (1, 1)
    assert r["failures"][0]["name"].endswith("test_maths.py::test_bad") and "assert" in r["failures"][0]["traceback"]


async def test_run_tests_needs_approval(repo):
    from sparx.tools import Context
    out = await run_tool("run_tests", Context(session="t"), path=str(repo))
    assert out.startswith("Not done")


def test_run_tests_without_setup_says_so(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(ToolError, match="No test setup detected"):
        coding._run_tests(d)


def test_run_times_out_and_kills(tmp_path):
    import sys
    code, _, err = coding._run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 1)
    assert code == -1 and "timed out" in err


def test_lint_and_build_fall_back_to_syntax_check(ctx, repo, monkeypatch):
    monkeypatch.setattr(coding, "_py_tool", lambda name: None)
    assert coding.run_build(ctx, str(repo))["ok"] is True
    (repo / "broken.py").write_text("def f(:\n", encoding="utf-8")
    r = coding.run_lint(ctx, str(repo))
    assert r["tools"] == ["python-syntax"] and not r["ok"] and r["findings"][0]["file"] == "broken.py"
    assert "pip install ruff" in r["notes"][0]
    assert coding.run_build(ctx, str(repo))["errors"][0]["line"] == 1


# ---- stack traces

PY_TRACE = '''Traceback (most recent call last):
  File "main.py", line 2, in <module>
    print(add(1))
  File "{maths}", line 8, in add
    return a + b
TypeError: unsupported operand type(s) for +: 'int' and 'str'
'''

JS_TRACE = '''/app/src/server.js:10
    throw new TypeError("bad input");
TypeError: bad input
    at handler (/app/src/server.js:10:11)
    at async Promise.all (index 0)
    at Object.<anonymous> (C:\\app\\src\\index.js:3:5)
    at node:internal/main/run_main_module:23:47
'''

JAVA_TRACE = '''Exception in thread "main" java.lang.IllegalStateException: wrapper
\tat com.acme.App.main(App.java:12)
Caused by: java.lang.NullPointerException: name is null
\tat com.acme.svc.Users.find(Users.java:41)
\t... 1 more
'''


def test_parse_python_trace():
    t = coding.parse_trace(PY_TRACE.format(maths="/x/pkg/maths.py"))
    assert t["language"] == "python" and t["error"].startswith("TypeError: unsupported operand")
    assert t["frames"] == [{"file": "main.py", "line": 2, "function": "<module>"},
                           {"file": "/x/pkg/maths.py", "line": 8, "function": "add"}]
    assert t["innermost"]["function"] == "add"


def test_parse_js_and_java_traces():
    j = coding.parse_trace(JS_TRACE)
    assert j["language"] == "javascript" and j["error"] == "TypeError: bad input"
    assert j["frames"][0] == {"file": "/app/src/server.js", "line": 10, "function": "handler"}
    assert {"file": "C:\\app\\src\\index.js", "line": 3, "function": "Object.<anonymous>"} in j["frames"]
    assert j["innermost"]["function"] == "handler"
    v = coding.parse_trace(JAVA_TRACE)
    assert v["language"] == "java" and v["error"] == "java.lang.NullPointerException: name is null"
    assert v["frames"][1] == {"file": "Users.java", "line": 41, "function": "com.acme.svc.Users.find"}
    assert coding.parse_trace("it just broke")["frames"] == []


async def test_debug_trace_reads_source_inside_repo(ctx, repo, fake_llm):
    fake_llm.script = [{"root_cause": "b is a str", "fix": "convert b", "file": "pkg/maths.py", "line": 8, "confidence": "high"}]
    r = await coding.debug_trace(ctx, PY_TRACE.format(maths=repo / "pkg" / "maths.py"), str(repo))
    assert r["analysis"]["root_cause"] == "b is a str" and r["source_read_for"] == 2
    assert ">    8|     return a + b" in r["frames"][1]["source"]
    prompt = fake_llm.prompts[0][1]
    assert "return a + b" in prompt and "print(add(1))" in prompt
    outside = await coding.debug_trace(ctx, 'File "C:/Windows/system.py", line 1, in x\nValueError: no', str(repo))
    assert outside["source_read_for"] == 0


# ---- git

def test_git_status_and_commit(ctx, repo):
    s = coding.git_status(ctx, str(repo))
    assert s["branch"] == "main" and s["clean"]
    (repo / "main.py").write_text("print('changed')\n", encoding="utf-8")
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    s = coding.git_status(ctx, str(repo / "pkg"))          # any path inside the repo works
    assert s["modified"] == ["main.py"] and s["untracked"] == ["new.py"] and not s["clean"]
    assert "+print('changed')" in coding.git_diff(ctx, str(repo))
    c = coding.git_commit(ctx, str(repo), "Change main", ["main.py"])
    assert c["files"] == ["main.py"] and c["branch"] == "main"
    s = coding.git_status(ctx, str(repo))
    assert s["modified"] == [] and s["untracked"] == ["new.py"]
    assert [l["subject"] for l in coding.git_log(ctx, str(repo))] == ["Change main", "initial"]
    with pytest.raises(ToolError, match="Nothing to commit"):
        coding.git_commit(ctx, str(repo), "Again", ["main.py"])


def test_git_branches_stash_and_bad_names(ctx, repo):
    assert coding.git_branch_create(ctx, str(repo), "feature/x")["current"] == "feature/x"
    b = coding.git_branches(ctx, str(repo))
    assert b["current"] == "feature/x" and set(b["local"]) == {"main", "feature/x"}
    for bad in ("--force", "a..b", "has space"):
        with pytest.raises(ToolError):
            coding.git_branch_create(ctx, str(repo), bad)
    with pytest.raises(ToolError, match="not a valid git ref"):
        coding.git_diff(ctx, str(repo), ref="--output=/tmp/x")
    (repo / "main.py").write_text("wip\n", encoding="utf-8")
    coding.git_stash(ctx, str(repo), "push", "wip")
    assert coding.git_status(ctx, str(repo))["clean"] and "wip" in coding.git_stash(ctx, str(repo), "list")[0]
    coding.git_stash(ctx, str(repo), "pop")
    assert coding.git_status(ctx, str(repo))["modified"] == ["main.py"]


def test_no_destructive_git_tools_and_publishing_asks():
    from sparx import tools
    tools.load_all()
    names = [t.name for t in tools.by_category()["coding"]]
    assert not [n for n in names if "force" in n]
    allowed = {"git_reset": "write", "git_reset_hard": "delete"}      # honest, undoable; see sparx/tools/gitplus.py
    odd = [n for n in names if any(w in n for w in ("reset", "delete")) and tools.REGISTRY[n].risk != allowed.get(n)]
    assert not odd, f"destructive-sounding coding tools that are not the two vetted ones: {odd}"
    assert not [k for k in tools.REGISTRY["git_push"].params if "force" in k]
    assert tools.REGISTRY["git_push"].risk == "send" and tools.REGISTRY["pr_create"].risk == "send"
    assert all(tools.REGISTRY[n].risk == "exec" for n in ("run_tests", "run_lint", "run_build", "code_task"))


def test_not_a_repo_and_missing_gh(ctx, tmp_path, repo, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))   # the temp folder may itself sit inside a repo
    with pytest.raises(ToolError, match="not inside a git repository"):
        coding.git_status(ctx, str(plain))
    real = coding.shutil.which
    monkeypatch.setattr(coding.shutil, "which", lambda n: None if n == "gh" else real(n))
    with pytest.raises(ToolError, match="gh auth login"):
        coding.github_activity(ctx, str(repo))
    assert "CI cannot be checked yet" in coding.dev_watch_add(ctx, str(repo))["note"]


async def test_pr_summary_commit_uses_the_diff(ctx, repo, fake_llm):
    (repo / "main.py").write_text("print('summarise me')\n", encoding="utf-8")
    fake_llm.script = [{"subject": "Update main", "body": ""}]
    r = await coding.pr_summary(ctx, str(repo), "commit")
    assert r["subject"] == "Update main" and "summarise me" in fake_llm.prompts[0][1]
    git(repo, "checkout", "-q", "--", "main.py")
    with pytest.raises(ToolError, match="no diff"):
        await coding.pr_summary(ctx, str(repo), "commit")


# ---- docs, notes, monitoring

async def test_docs_generate_api_and_architecture(ctx, repo, fake_llm):
    api = await coding.docs_generate(ctx, str(repo), "api")
    assert "- `def add(a: int, b: int=1) -> int` - Add two numbers." in api["content"] and api["written"] is None
    assert not (repo / "docs").exists() and fake_llm.prompts == []
    fake_llm.script = ["Two components."]
    arch = await coding.docs_generate(ctx, str(repo), "architecture", write=True)
    doc = (repo / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert arch["written"].endswith("ARCHITECTURE.md") and "Two components." in doc
    ids = dict(reversed(l.strip().rstrip('"]').split('["')) for l in doc.splitlines() if '["' in l)
    assert f"  {ids['main.py']} --> {ids['pkg/maths.py']}" in doc


def test_project_notes_and_todos(ctx, repo):
    coding.project_note(ctx, str(repo), "decision", "Use SQLite")
    tid = coding.project_note(ctx, str(repo), "todo", "Add CI")["id"]
    with pytest.raises(ToolError):
        coding.project_note(ctx, str(repo), "rumour", "x")
    assert [n["text"] for n in coding.project_notes(ctx, str(repo))] == ["Add CI", "Use SQLite"]
    assert [n["kind"] for n in coding.project_notes(ctx, str(repo), "decision")] == ["decision"]
    t = coding.project_todos(ctx, str(repo))
    assert [s["text"] for s in t["stored"]] == ["Add CI"] and t["in_code"] == ["pkg/maths.py:16: TODO implement area"]
    coding.project_notes(ctx, str(repo), done_id=tid)
    assert coding.project_todos(ctx, str(repo))["stored"] == []


async def test_dev_monitor_notifies_only_new_failures(ctx, repo, monkeypatch):
    runs = [{"databaseId": 5, "name": "CI", "status": "completed", "conclusion": "failure", "headBranch": "main", "url": "u"}]
    monkeypatch.setattr(coding, "_ci_runs", lambda r: runs)
    coding.dev_watch_add(ctx, str(repo))
    assert "dev_monitor" in jobs.JOBS and jobs.JOBS["dev_monitor"].flag == "monitors"
    await coding.dev_monitor(ctx)
    assert jobs.unread() == []                                   # run 5 predates the watch
    runs.insert(0, {"databaseId": 6, "name": "CI", "status": "completed", "conclusion": "success", "headBranch": "main"})
    runs.insert(0, {"databaseId": 7, "name": "CI", "status": "completed", "conclusion": "failure", "headBranch": "dev", "url": "u7"})
    await coding.dev_monitor(ctx)
    await coding.dev_monitor(ctx)
    n = jobs.unread()
    assert len(n) == 1 and "CI on dev: failure" in n[0]["body"] and n[0]["why"]
    assert coding.dev_watch_add(ctx, str(repo), remove=True) == {"removed": True}


async def test_dev_server_check(ctx):
    with pytest.raises(ToolError, match="only checks servers on this machine"):
        await coding.dev_server_check(ctx, "https://example.com")
    r = await coding.dev_server_check(ctx, "127.0.0.1:9")       # discard port: nothing listens
    assert r["up"] is False and r["url"] == "http://127.0.0.1:9"


def test_env_info(ctx, monkeypatch):
    real = coding.shutil.which
    monkeypatch.setattr(coding.shutil, "which", lambda n: None if n in ("docker", "cargo") else real(n))
    info = coding.env_info(ctx)
    assert info["git"].startswith("git version") and info["cargo"] == "not installed" and "containers" not in info


# ---- multi-model

async def test_code_review_of_a_file(ctx, repo, fake_llm):
    fake_llm.script = [{"summary": "one issue", "findings": [
        {"file": "maths.py", "line": 9, "severity": "low", "issue": "no type check", "suggestion": "validate"},
        {"file": "maths.py", "line": "16", "severity": "HIGH", "issue": "area always 0"}, {"note": "not a finding"}]}]
    r = await coding.code_review(ctx, str(repo / "pkg" / "maths.py"))
    assert [f["where"] for f in r["findings"]] == ["maths.py:16", "maths.py:9"] and r["reviewed"]
    assert "   16|" in fake_llm.prompts[0][1]
    assert (await coding.code_review(ctx, "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n"))["reviewed"] is False
    with pytest.raises(ToolError, match="No uncommitted changes"):
        await coding.code_review(ctx, str(repo))


async def test_code_task_plans_implements_tests_and_reviews(ctx, repo, fake_llm):
    _add_tests(repo, "def test_default():\n    assert add(1) == 3\n")
    target = str(repo / "pkg" / "maths.py")
    fake_llm.script = [
        "1. Change the default of b to 2 in pkg/maths.py",                                   # plan (smart)
        ("code_read", {"path": target}),                                                      # coder reads first
        ("code_edit", {"path": target, "old": "b: int = 1", "new": "b: int = 2"}),            # coder
        "Changed the default.",
        {"summary": "fine but undocumented", "findings": [{"file": "pkg/maths.py", "line": 4, "issue": "docstring is stale"}]},
    ]
    ctx.active.add("email")
    r = await coding.code_task(ctx, str(repo), "make add default to 2")
    assert r["changed"]["modified"] == ["pkg/maths.py"] and r["changed"]["added"] == []
    assert r["tests"]["ok"] is True and r["tests"]["passed"] == 1
    assert r["unresolved_findings"][0]["where"] == "pkg/maths.py:4" and r["status"] == "needs attention"
    assert r["implementation"] == "Changed the default." and "default of b" in r["plan"]
    assert "b: int = 2" in fake_llm.prompts[-1][1]                    # the reviewer saw the real diff
    assert ctx.active == {"email"}


async def test_code_task_reports_when_nothing_changed(ctx, repo, fake_llm):
    fake_llm.script = ["1. Do it", "I could not find where to change this."]
    r = await coding.code_task(ctx, str(repo), "do the impossible")
    assert r["changed"] == {"added": [], "deleted": [], "modified": []} and r["status"] == "needs attention"
    assert r["unresolved_findings"] == [] and "Nothing was changed" in r["review_summary"]


async def test_test_generate_writes_runs_and_reports_failures(ctx, repo, fake_llm):
    fake_llm.script = ["```python\nfrom pkg.maths import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n\n\n"
                       "def test_wrong():\n    assert add(2, 2) == 5\n```"]
    r = await coding.test_generate(ctx, str(repo / "pkg" / "maths.py"), "add")
    assert Path(r["tests_file"]) == repo / "tests" / "test_maths_generated.py" and r["tests_written"] == 2
    assert (r["result"]["passed"], r["result"]["failed"]) == (1, 1) and r["result"]["ok"] is False
    assert r["result"]["failures"][0]["name"].endswith("::test_wrong")
    fake_llm.script = ["def broken(:"]
    with pytest.raises(ToolError, match="not valid Python"):
        await coding.test_generate(ctx, str(repo / "main.py"))
    with pytest.raises(ToolError, match="does not appear"):
        await coding.test_generate(ctx, str(repo / "main.py"), "nothing_like_this")


async def test_code_explain_adapts_to_level(ctx, repo, fake_llm):
    fake_llm.script = ["explained"]
    assert await coding.code_explain(ctx, str(repo / "main.py"), "beginner") == "explained"
    system, user = fake_llm.prompts[0]
    assert "a beginner" in system and "from pkg.maths import add" in user
    with pytest.raises(ToolError):
        await coding.code_explain(ctx, "x = 1", "guru")


# ---- audit: outline, outdated deps, and faults found in review

def test_code_outline_python_js_and_refusals(ctx, repo):
    r = coding.code_outline(ctx, str(repo / "pkg" / "maths.py"))
    assert r["doc"] == "Maths helpers." and not r.get("error")
    assert "L4 def add(a: int, b: int=1) -> int  # Add two numbers." in r["symbols"]
    assert any(s.strip().startswith("L14 async def area") for s in r["symbols"])
    js = coding.code_outline(ctx, str(repo / "web.ts"))["symbols"]
    assert any("function greet" in s for s in js) and any("class Widget" in s for s in js)
    (repo / "bad.py").write_text("def f(:\n", encoding="utf-8")
    assert "syntax error" in coding.code_outline(ctx, str(repo / "bad.py"))["error"]
    with pytest.raises(ToolError, match="understands Python"):
        coding.code_outline(ctx, str(repo / "pyproject.toml"))
    with pytest.raises(ToolError, match="not a file"):
        coding.code_outline(ctx, str(repo / "pkg"))
    with pytest.raises(ToolError, match="protected"):
        coding.code_outline(ctx, str(repo / ".git" / "config"))


def test_code_outline_caps_huge_files(ctx, repo):
    f = repo / "many.py"
    f.write_text("".join(f"def f{i}(): pass\n" for i in range(2000)), encoding="utf-8")
    r = coding.code_outline(ctx, str(f))
    assert len(r["symbols"]) <= 501 and "more" in r["symbols"][-1]


def test_deps_outdated_python_and_npm(ctx, repo, monkeypatch):
    calls = []

    def fake_run(cmd, cwd=None, timeout=60, env=None):
        calls.append(cmd)
        if "pip" in cmd:
            return 0, json.dumps([{"name": "httpx", "version": "0.1", "latest_version": "0.2", "latest_filetype": "wheel"}]), ""
        return 1, json.dumps({"left-pad": {"current": "1.0.0", "wanted": "1.0.1", "latest": "2.0.0"}}), ""
    monkeypatch.setattr(coding, "_run", fake_run)
    (repo / "package.json").write_text("{}", encoding="utf-8")
    r = coding.deps_outdated(ctx, str(repo))
    assert r["python"]["outdated"] == [{"name": "httpx", "current": "0.1", "latest": "0.2"}]
    assert r["npm"]["outdated"] == [{"name": "left-pad", "current": "1.0.0", "wanted": "1.0.1", "latest": "2.0.0"}]
    assert any(c[:2] == ["npm", "outdated"] for c in calls)


def test_deps_outdated_reports_failures_and_missing_manifests(ctx, repo, tmp_path, monkeypatch):
    monkeypatch.setattr(coding, "_run", lambda *a, **k: (1, "", "ERROR: no network"))
    r = coding.deps_outdated(ctx, str(repo))
    assert r["python"]["outdated"] is None and "no network" in r["python"]["error"]
    (repo / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(coding, "_run", lambda c, *a, **k: (1, "npm ERR! not json", "") if c[0] == "npm" else (0, "[]", ""))
    assert "error" in coding.deps_outdated(ctx, str(repo))["npm"]
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ToolError, match="No Python project files"):
        coding.deps_outdated(ctx, str(empty))


def test_code_edit_refuses_empty_old(ctx, repo):
    f = repo / "e.py"
    f.write_text("abc\n", encoding="utf-8")
    coding.code_read(ctx, str(f))
    for ra in (False, True):
        with pytest.raises(ToolError, match="empty"):
            coding.code_edit(ctx, str(f), "", "X", replace_all=ra)
    assert f.read_text(encoding="utf-8") == "abc\n"


def _link(link: Path, target: Path):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")


def test_symlink_out_of_the_allowed_area_is_not_read(ctx, repo, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    config.set_key("allowed_roots", [str(repo)])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET = 1\n", encoding="utf-8")
    _link(repo / "leak.txt", outside / "secret.txt")
    with pytest.raises(ToolError):
        coding.code_read(ctx, str(repo / "leak.txt"))
    monkeypatch.setattr(coding.shutil, "which", lambda name: None)      # Python search path, no git
    assert coding.code_search(ctx, str(repo), "TOPSECRET")["count"] == 0
    assert "leak.txt" not in coding._files(repo)


def test_symlink_listed_by_git_is_not_read(ctx, repo, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    config.set_key("allowed_roots", [str(repo)])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET = 1\n", encoding="utf-8")
    _link(repo / "leak.txt", outside / "secret.txt")
    assert "leak.txt" not in coding._files(repo)
    assert coding.code_search(ctx, str(repo), "TOPSECRET")["count"] == 0


def test_code_search_rg_never_treats_a_file_name_as_an_option(ctx, repo, monkeypatch):
    seen = []
    monkeypatch.setattr(coding.shutil, "which", lambda n: "rg" if n == "rg" else None)
    monkeypatch.setattr(coding, "_run", lambda cmd, cwd=None, timeout=60, env=None: (seen.append(cmd) or (1, "", "")))
    f = repo / "--pre=evil.py"
    f.write_text("x\n", encoding="utf-8")
    coding.code_search(ctx, str(f), "x")
    assert not seen[0][-1].startswith("-")


def test_code_read_bounds_very_long_lines(ctx, repo):
    f = repo / "min.py"
    f.write_text("x = '" + "a" * 500_000 + "'\n", encoding="utf-8")
    assert len(coding.code_read(ctx, str(f))) < 20_000


def test_git_status_output_is_bounded(ctx, repo):
    for i in range(300):
        (repo / f"u{i}.txt").write_text("x", encoding="utf-8")
    s = coding.git_status(ctx, str(repo))
    assert len(s["untracked"]) <= 200 and s["untracked_more"] == 100 and not s["clean"]


def test_credentials_are_scrubbed_from_command_output(ctx, repo, monkeypatch):
    import sys
    monkeypatch.setenv("GH_TOKEN", "sekret-token-value-123")
    token = "ghp_" + "a" * 36
    script = ("import sys; print('To https://user:%s@github.com/o/r.git'); "
              "print('token sekret-token-value-123 github_pat_%s', file=sys.stderr)" % (token, "b" * 30))
    code, out, err = coding._run([sys.executable, "-c", script], repo, 20)
    assert token not in out and "sekret-token-value-123" not in err and "github_pat_" not in err
    monkeypatch.setattr(coding, "_run", lambda *a, **k: (1, "", f"fatal: https://x-access-token:{token}@github.com denied"))
    monkeypatch.setattr(coding.shutil, "which", lambda n: "gh")
    with pytest.raises(ToolError) as e:
        coding._gh(repo, "pr", "list")
    assert token not in str(e.value)


def test_run_reports_a_command_that_cannot_start(repo, monkeypatch):
    import sys

    def boom(*a, **k):
        raise OSError(193, "not a valid application")
    monkeypatch.setattr(coding.subprocess, "Popen", boom)
    with pytest.raises(ToolError, match="could not be started"):
        coding._run([sys.executable, "-V"], repo, 5)
    with pytest.raises(ToolError):
        coding._run_tests(repo, [sys.executable, "-m", "pytest"])


def test_lint_does_not_say_clean_when_the_linter_failed(ctx, repo, monkeypatch):
    monkeypatch.setattr(coding, "_py_tool", lambda name: ["ruff"] if name == "ruff" else None)
    monkeypatch.setattr(coding, "_run", lambda *a, **k: (2, "", "ruff failed: invalid config"))
    r = coding.run_lint(ctx, str(repo))
    assert r["ok"] is False and any("ruff" in n and "exit" in n for n in r["notes"])
    monkeypatch.setattr(coding, "_run", lambda cmd, *a, **k: (0, "", "") if cmd[0] == "ruff" else (2, "", ""))
    assert coding.run_lint(ctx, str(repo))["ok"] is True


def test_write_through_a_symlinked_folder_cannot_reach_git(ctx, repo):
    (repo / ".git" / "hooks").mkdir(exist_ok=True)
    _link(repo / "docs", repo / ".git")
    with pytest.raises(ToolError, match="protected"):
        coding._write(ctx, repo / "docs" / "hooks" / "pre-commit", "evil", "Wrote")
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


def test_repository_text_tools_are_untrusted_and_risks_are_honest():
    from sparx import tools
    tools.load_all()
    for n in ("git_status", "git_branches", "git_stash", "pr_summary", "code_review", "code_explain", "code_task",
              "test_generate", "docs_generate", "deps_outdated", "code_outline", "code_read", "code_search", "git_log",
              "git_diff", "github_activity", "run_tests", "run_lint", "run_build", "debug_trace", "repo_map", "project_todos"):
        assert tools.REGISTRY[n].untrusted, n
    assert tools.REGISTRY["git_push"].risk == "send" and tools.REGISTRY["pr_create"].risk == "send"


async def test_git_reset_is_undoable(ctx, repo):
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "main.py").write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "second"], cwd=repo, check=True)
    out = await run_tool("git_reset", ctx, path=str(repo), ref="HEAD~1", mode="soft")
    assert "undo_id" in out
    head = lambda: subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert head() == first
    undo.run()
    assert head() != first
