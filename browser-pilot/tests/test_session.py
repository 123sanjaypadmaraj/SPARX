import json

import pytest

from sparx import skills
from sparx.tools import coding, session
from conftest import run_tool


async def test_todos_round_trip_and_rules(ctx):
    out = await run_tool("todo_write", ctx, todos=[{"content": "a", "status": "completed"},
                                                   {"content": "b", "status": "in_progress"}, {"content": "c"}])
    assert "1/3 done" in out and "[~] b" in out and "[ ] c" in out
    assert "[x] a" in await run_tool("todo_read", ctx)
    assert "only one" in await run_tool("todo_write", ctx, todos=[{"content": "x", "status": "in_progress"},
                                                                  {"content": "y", "status": "in_progress"}])
    assert "status must be" in await run_tool("todo_write", ctx, todos=[{"content": "x", "status": "later"}])
    assert "[~] b" in await run_tool("todo_read", ctx)         # a rejected update changes nothing


async def test_ask_user_formats_choices(ctx):
    out = await run_tool("ask_user", ctx, question="Which db?", options=["sqlite", "postgres"])
    assert "Which db?" in out and "1. sqlite" in out and "2. postgres" in out and "End your turn" in out


@pytest.fixture
def nb(tmp_path, monkeypatch):
    monkeypatch.setattr(coding.Path, "home", classmethod(lambda cls: tmp_path))
    p = tmp_path / "n.ipynb"
    p.write_text(json.dumps({"cells": [
        {"cell_type": "code", "metadata": {}, "source": ["x = 1\n"], "execution_count": 3,
         "outputs": [{"output_type": "stream", "name": "stdout", "text": ["1\n"]}]},
        {"cell_type": "markdown", "metadata": {}, "source": ["# hi"]}], "metadata": {}, "nbformat": 4,
        "nbformat_minor": 5}), encoding="utf-8")
    return p


async def test_notebook_edit(ctx, nb):
    assert "has not been read" in await run_tool("notebook_edit", ctx, path=str(nb), cell=0, source="x = 2")
    await run_tool("code_read", ctx, path=str(nb))
    await run_tool("notebook_edit", ctx, path=str(nb), cell=0, source="x = 2\nprint(x)")
    cells = json.loads(nb.read_text(encoding="utf-8"))["cells"]
    assert "".join(cells[0]["source"]) == "x = 2\nprint(x)" and cells[0]["outputs"] == []
    await run_tool("notebook_edit", ctx, path=str(nb), cell=0, action="insert", source="y = 3")
    await run_tool("notebook_edit", ctx, path=str(nb), cell=2, action="delete")
    cells = json.loads(nb.read_text(encoding="utf-8"))["cells"]
    assert [c["cell_type"] for c in cells] == ["code", "code"] and "".join(cells[1]["source"]) == "y = 3"
    assert "must be between" in await run_tool("notebook_edit", ctx, path=str(nb), cell=9)


def test_claude_dirs_are_read(tmp_path, monkeypatch):
    home = tmp_path / "h"
    (home / ".claude" / "skills" / "cc").mkdir(parents=True)
    (home / ".claude" / "skills" / "cc" / "SKILL.md").write_text(
        "---\nname: cc\ndescription: From claude\nallowed-tools: Bash Read\n---\nBody", encoding="utf-8")
    (home / ".claude" / "commands").mkdir()
    (home / ".claude" / "commands" / "go.md").write_text("Do $ARGUMENTS", encoding="utf-8")
    from sparx import config
    monkeypatch.setattr(config, "HOME", home / ".sparx")
    monkeypatch.chdir(tmp_path)
    s = skills.skills()["cc"]
    assert s["description"] == "From claude" and s["allowed_tools"] is None      # Claude tool names must not lock the run
    assert "go" in skills.commands()
    config.set_key("skills.claude", False)
    assert "cc" not in skills.skills()


def test_catalogue_is_capped(monkeypatch):
    fake = {f"s{i:02}": {"description": "d" * 300, "source": "user", "allowed_tools": None} for i in range(60)}
    monkeypatch.setattr(skills, "skills", lambda: fake)
    cat = skills.catalogue()
    assert len(cat) < skills.CATALOGUE_CHARS + 1200 and "more (names only): s" in cat
