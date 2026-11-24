import json
import os
import subprocess
import time

import pytest

from sparx import config, undo
from sparx.tools import ToolError, codeplus, coding

from conftest import run_tool


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "a.py").write_text('def helper(x):\n    """helper docs"""\n    return x  # helper\n\n\nprint("helper")\n',
                                    encoding="utf-8")
    (r / "pkg" / "b.py").write_text("from pkg.a import helper\n\nvalue = helper(2)\nmy_helper = 1\n", encoding="utf-8")
    (r / "web.ts").write_text("export function helper(a) { return a }\nconst my_helper = helper(1)\n", encoding="utf-8")
    (r / "notes.md").write_text("helper is documented here\n", encoding="utf-8")
    (r / "top.py").write_text("x = 1\n", encoding="utf-8")
    (r / "skip.log").write_text("x\n", encoding="utf-8")
    (r / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    git(r, "config", "commit.gpgsign", "false")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "init")
    config.set_key("allowed_roots", [str(tmp_path)])
    return r


# ---- code_glob

def test_glob_patterns_gitignore_and_mtime_order(ctx, repo):
    old = repo / "top.py"
    os.utime(old, (1, 1))
    r = codeplus.code_glob(ctx, "**/*.py", str(repo))
    assert set(r["files"]) == {"pkg/a.py", "pkg/b.py", "top.py"} and r["files"][-1] == "top.py"
    assert codeplus.code_glob(ctx, "*.py", str(repo))["count"] == 3          # no slash: any depth
    assert codeplus.code_glob(ctx, "pkg/*.py", str(repo))["files"].count("top.py") == 0
    assert codeplus.code_glob(ctx, "*.log", str(repo))["count"] == 0         # .gitignore
    (repo / "skip.log").write_text("y")
    assert codeplus.code_glob(ctx, "**/*.log", str(repo))["files"] == []
    r = codeplus.code_glob(ctx, "**/*", str(repo), limit=2)
    assert len(r["files"]) == 2 and "more matches" in r["note"]


def test_glob_without_git_uses_gitignore(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(codeplus.shutil, "which", lambda n: None)
    d = tmp_path / "plain"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "k.py").write_text("1")
    (d / "z.log").write_text("1")
    (d / ".gitignore").write_text("*.log\n")
    config.set_key("allowed_roots", [str(tmp_path)])
    assert codeplus.code_glob(ctx, "**/k.py", str(d))["files"] == ["sub/k.py"]
    assert codeplus.code_glob(ctx, "*.log", str(d))["count"] == 0


# ---- code_read

def test_read_large_range_and_continue_hint(ctx, tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"line {i}" for i in range(1, 3001)), encoding="utf-8")
    config.set_key("allowed_roots", [str(tmp_path)])
    t = coding.code_read(ctx, str(f))
    assert "line 2000" in t and "line 2001" not in t and "start=2001" in t
    t = coding.code_read(ctx, str(f), start=2500, limit=10)
    assert "line 2509" in t and "line 2510" not in t and "showing 2500-2509" in t
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ToolError, match="binary"):
        coding.code_read(ctx, str(tmp_path / "bin.dat"))


def test_read_notebook(ctx, tmp_path):
    nb = {"cells": [{"cell_type": "code", "source": ["print(1)\n", "x = 2"],
                     "outputs": [{"output_type": "stream", "text": ["1\n"]}]},
                    {"cell_type": "markdown", "source": ["# Title"]}]}
    f = tmp_path / "n.ipynb"
    f.write_text(json.dumps(nb), encoding="utf-8")
    config.set_key("allowed_roots", [str(tmp_path)])
    t = coding.code_read(ctx, str(f))
    assert "cell 0 [code]" in t and "x = 2" in t and "output: 1" in t and "cell 1 [markdown]" in t
    f.write_text("nope", encoding="utf-8")
    with pytest.raises(ToolError, match="not a valid Jupyter"):
        coding.code_read(ctx, str(f))


def test_read_pdf_pages(ctx, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    w = pypdf.PdfWriter()
    for _ in range(3):
        w.add_blank_page(200, 200)
    f = tmp_path / "d.pdf"
    with open(f, "wb") as fh:
        w.write(fh)
    config.set_key("allowed_roots", [str(tmp_path)])
    t = coding.code_read(ctx, str(f), pages="2-3")
    assert "3 pages in total" in t and "--- page 2 ---" in t and "--- page 1 ---" not in t
    with pytest.raises(ToolError, match="pages must be inside"):
        coding.code_read(ctx, str(f), pages="9")
    with pytest.raises(ToolError, match="pages must look like"):
        coding.code_read(ctx, str(f), pages="a-b")


def test_read_image_uses_vision(ctx, tmp_path, monkeypatch):
    from sparx.tools import vision

    async def fake(src, question=""):
        return "a red square"
    monkeypatch.setattr(vision, "describe_image", fake)
    f = tmp_path / "i.png"
    f.write_bytes(b"\x89PNG")
    config.set_key("allowed_roots", [str(tmp_path)])
    assert "a red square" in coding.code_read(ctx, str(f))


# ---- read-before-edit

def test_edit_and_write_require_a_fresh_read(ctx, repo):
    f = repo / "top.py"
    with pytest.raises(ToolError, match="Read it with code_read first"):
        coding.code_edit(ctx, str(f), "x = 1", "x = 2")
    with pytest.raises(ToolError, match="code_read first"):
        coding.code_write(ctx, str(f), "x = 3\n")
    coding.code_read(ctx, str(f))
    coding.code_edit(ctx, str(f), "x = 1", "x = 2")
    coding.code_edit(ctx, str(f), "x = 2", "x = 4")                  # our own write counts as seen
    f.write_text("x = 9\n", encoding="utf-8")                         # changed behind our back
    os.utime(f, ns=(time.time_ns() + 5_000_000_000,) * 2)
    with pytest.raises(ToolError, match="changed on disk"):
        coding.code_edit(ctx, str(f), "x = 9", "x = 5")
    coding.code_read(ctx, str(f))
    assert coding.code_write(ctx, str(f), "x = 5\n")["created"] is False
    new = repo / "fresh.py"                                           # new files need no read
    assert coding.code_write(ctx, str(new), "y = 1\n")["created"]


def test_read_tracking_is_per_session(repo):
    from sparx import tools
    a, b = tools.Context(session="s1"), tools.Context(session="s2")
    coding.code_read(a, str(repo / "top.py"))
    coding.code_edit(a, str(repo / "top.py"), "x = 1", "x = 2")
    with pytest.raises(ToolError, match="not been read"):
        coding.code_edit(b, str(repo / "top.py"), "x = 2", "x = 3")


# ---- code_multi_edit

def test_multi_edit_is_atomic_and_undoable(ctx, repo):
    a, b = repo / "pkg" / "a.py", repo / "pkg" / "b.py"
    orig = {a: a.read_bytes(), b: b.read_bytes()}
    coding.code_read(ctx, str(a))
    coding.code_read(ctx, str(b))
    bad = [{"path": str(a), "old": "return x", "new": "return x + 1"},
           {"path": str(b), "old": "does not exist", "new": "z"}]
    with pytest.raises(ToolError, match="edit 2"):
        codeplus.code_multi_edit(ctx, bad)
    assert a.read_bytes() == orig[a] and b.read_bytes() == orig[b]
    good = [{"path": str(a), "old": "return x", "new": "return x + 1"},
            {"path": str(a), "old": "x + 1", "new": "x + 2"},               # sequential on the same file
            {"path": str(b), "old": "helper(2)", "new": "helper(3)"}]
    r = codeplus.code_multi_edit(ctx, good)
    assert r["replacements"] == 3 and "+    return x + 2" in r["diff"] and len(r["files"]) == 2
    assert "x + 2" in a.read_text(encoding="utf-8") and "helper(3)" in b.read_text(encoding="utf-8")
    assert undo.run().startswith("Undone: Multi-edited")
    assert a.read_bytes() == orig[a] and b.read_bytes() == orig[b]


def test_multi_edit_safety_rules(ctx, repo):
    a = repo / "pkg" / "a.py"
    with pytest.raises(ToolError, match="not been read"):
        codeplus.code_multi_edit(ctx, [{"path": str(a), "old": "return x", "new": "return 1"}])
    coding.code_read(ctx, str(a))
    with pytest.raises(ToolError, match="occurs"):
        codeplus.code_multi_edit(ctx, [{"path": str(a), "old": "helper", "new": "h"}])
    with pytest.raises(ToolError, match="non-empty"):
        codeplus.code_multi_edit(ctx, [])
    r = codeplus.code_multi_edit(ctx, [{"path": str(a), "old": "helper", "new": "h", "replace_all": True},
                                       {"path": str(repo / "made.py"), "old": "", "new": "q = 1\n"}])
    assert (repo / "made.py").read_text(encoding="utf-8") == "q = 1\n" and r["replacements"] >= 3
    undo.run()
    assert not (repo / "made.py").exists()


# ---- code_rename_symbol

def test_rename_python_is_token_aware_with_dry_run_default(ctx, repo):
    r = codeplus.code_rename_symbol(ctx, str(repo), "helper", "assist")
    assert r["dry_run"] is True and (repo / "pkg" / "a.py").read_text(encoding="utf-8").count("helper") == 4
    files = {f["file"]: f["occurrences"] for f in r["files"]}
    assert files == {"pkg/a.py": 1, "pkg/b.py": 2, "web.ts": 2}       # md skipped; a.py: def only (string/comment/doc kept)
    r = codeplus.code_rename_symbol(ctx, str(repo), "helper", "assist", dry_run=False)
    a = (repo / "pkg" / "a.py").read_text(encoding="utf-8")
    assert "def assist(x)" in a and '"helper docs"' in a and "# helper" in a and 'print("helper")' in a
    b = (repo / "pkg" / "b.py").read_text(encoding="utf-8")
    assert "from pkg.a import assist" in b and "value = assist(2)" in b and "my_helper" in b   # whole word only
    assert "export function assist" in (repo / "web.ts").read_text(encoding="utf-8")
    assert "helper is documented" in (repo / "notes.md").read_text(encoding="utf-8")
    assert undo.run().startswith("Undone: Renamed")
    assert (repo / "pkg" / "a.py").read_text(encoding="utf-8").count("helper") == 4


def test_rename_validation_and_glob(ctx, repo):
    for old, new in (("not valid", "x"), ("helper", "1bad"), ("helper", "helper")):
        with pytest.raises(ToolError):
            codeplus.code_rename_symbol(ctx, str(repo), old, new)
    with pytest.raises(ToolError, match="No whole-word"):
        codeplus.code_rename_symbol(ctx, str(repo), "nonexistent_name", "x")
    r = codeplus.code_rename_symbol(ctx, str(repo), "helper", "assist", glob="pkg/*.py")
    assert {f["file"] for f in r["files"]} == {"pkg/a.py", "pkg/b.py"}
    (repo / "pkg" / "c.py").write_text("assist = 1\nhelper = 2\n", encoding="utf-8")
    git(repo, "add", ".")
    assert "already appears" in codeplus.code_rename_symbol(ctx, str(repo), "helper", "assist")["warning"]


# ---- code_diagnostics

def test_diagnostics_reports_syntax_errors_and_lints(ctx, repo):
    (repo / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    git(repo, "add", ".")
    r = codeplus.code_diagnostics(ctx, str(repo), tools="py_compile")
    assert r["tools"] == ["py_compile"] and r["ok"] is False
    assert r["diagnostics"][0]["file"] == "broken.py" and r["diagnostics"][0]["line"] == 1
    one = codeplus.code_diagnostics(ctx, str(repo / "pkg" / "a.py"), tools="py_compile")
    assert one["ok"] is True and one["count"] == 0
    if coding._py_tool("ruff"):
        (repo / "lint.py").write_text("import os\n", encoding="utf-8")
        r = codeplus.code_diagnostics(ctx, str(repo / "lint.py"), tools="ruff")
        assert r["diagnostics"] and r["diagnostics"][0]["file"] == "lint.py" and r["diagnostics"][0]["tool"] == "ruff"
    with pytest.raises(ToolError, match="does not exist"):
        codeplus.code_diagnostics(ctx, str(repo / "nope.py"))


# ---- end to end through the registry

async def test_registry_smoke(ctx, repo):
    from sparx import tools
    tools.load_all()
    for n in ("code_glob", "code_multi_edit", "code_rename_symbol", "code_diagnostics"):
        assert n in tools.REGISTRY, n
    g = await run_tool("code_glob", ctx, pattern="**/*.py", path=str(repo))
    assert "pkg/a.py" in g
    out = await run_tool("code_edit", ctx, path=str(repo / "top.py"), old="x = 1", new="x = 2")
    assert out.startswith("Error:") and "code_read" in out
    assert "x = 1" in await run_tool("code_read", ctx, path=str(repo / "top.py"))
    assert "x = 2" in await run_tool("code_edit", ctx, path=str(repo / "top.py"), old="x = 1", new="x = 2")
    m = await run_tool("code_multi_edit", ctx, edits=[{"path": str(repo / "top.py"), "old": "x = 2", "new": "x = 7"}])
    assert "x = 7" in m
    r = await run_tool("code_rename_symbol", ctx, path=str(repo), old="helper", new="assist")
    assert "dry_run" in r and "Nothing was changed" in r
    d = await run_tool("code_diagnostics", ctx, path=str(repo), tools="py_compile")
    assert "py_compile" in d
