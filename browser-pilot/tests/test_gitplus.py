import json
import subprocess

import pytest

from sparx import config, undo
from sparx.tools import REGISTRY, load_all

from conftest import run_tool


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text, msg=None):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg or f"edit {name}")
    return git(repo, "rev-parse", "HEAD")


def init(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "Tester"), ("commit.gpgsign", "false")):
        git(path, "config", k, v)
    return path


@pytest.fixture
def repo(tmp_path):
    r = init(tmp_path / "repo")
    commit(r, "a.txt", "one\ntwo\nthree\n", "initial")
    config.set_key("allowed_roots", [str(tmp_path)])
    return r


async def call(ctx, tool_name, /, **a):
    return await run_tool(tool_name, ctx, **a)


def js(out):
    return json.loads(out[out.index("{"):out.rindex("}") + 1])


def branch_with_change(repo, name, file, text):
    git(repo, "switch", "-q", "-c", name)
    commit(repo, file, text)
    git(repo, "switch", "-q", "main")


async def test_registered_with_right_risks():
    load_all()
    risks = {n: REGISTRY[n].risk for n in ("git_show", "git_blame", "git_merge", "git_restore", "git_reset", "git_reset_hard",
                                          "git_checkout", "git_pull", "git_tag", "git_worktree")}
    assert risks["git_restore"] == risks["git_reset_hard"] == "delete"
    assert risks["git_show"] == risks["git_blame"] == "read"
    assert all(REGISTRY[n].category == "coding" for n in risks)


async def test_show_and_blame(ctx, repo):
    c2 = commit(repo, "a.txt", "one\nTWO\nthree\n", "change two")
    out = await call(ctx, "git_show", path=str(repo))
    assert "change two" in out and "TWO" in out
    assert "one\ntwo" in await call(ctx, "git_show", path=str(repo), ref="HEAD~1", file="a.txt")
    b = await call(ctx, "git_blame", path=str(repo), file="a.txt", start=1, end=3)
    assert c2[:7] in b and "TWO" in b and "Tester" in b
    assert "Error" in await call(ctx, "git_show", path=str(repo), ref="--output=x")
    assert "Error" in await call(ctx, "git_show", path=str(repo), file="../../etc/passwd")


async def test_merge_clean_and_undo(ctx, repo):
    branch_with_change(repo, "feat", "b.txt", "b\n")
    before = git(repo, "rev-parse", "HEAD")
    r = js(await call(ctx, "git_merge", path=str(repo), branch="feat"))
    assert r["done"] and (repo / "b.txt").exists()
    assert "Undone" in undo.run(r["undo_id"])
    assert git(repo, "rev-parse", "HEAD") == before and not (repo / "b.txt").exists()


async def test_merge_conflict_is_aborted(ctx, repo):
    branch_with_change(repo, "feat", "a.txt", "one\nFEAT\nthree\n")
    head = commit(repo, "a.txt", "one\nMAIN\nthree\n")
    r = js(await call(ctx, "git_merge", path=str(repo), branch="feat"))
    assert r["done"] is False and r["conflicts"] == ["a.txt"]
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()


async def test_refuses_dirty_tree_and_dash_refs(ctx, repo):
    branch_with_change(repo, "feat", "b.txt", "b\n")
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    assert "uncommitted" in await call(ctx, "git_merge", path=str(repo), branch="feat")
    assert "uncommitted" in await call(ctx, "git_rebase", path=str(repo), onto="feat")
    for tool_, kw in (("git_merge", {"branch": "--abort"}), ("git_checkout", {"target": "-f"}), ("git_rebase", {"onto": "-i"}),
                      ("git_cherry_pick", {"commits": ["--abort"]}), ("git_reset", {"ref": "--hard"}),
                      ("git_reset_hard", {"ref": "--soft"}), ("git_pull", {"remote": "-x"}), ("git_fetch", {"remote": "--all"})):
        out = await call(ctx, tool_, path=str(repo), **kw)
        assert out.startswith("Error"), (tool_, out)
    assert (repo / "a.txt").read_text() == "dirty\n"


async def test_rebase_and_conflict(ctx, repo):
    branch_with_change(repo, "base", "b.txt", "b\n")
    git(repo, "switch", "-q", "-c", "topic")
    commit(repo, "c.txt", "c\n")
    old = git(repo, "rev-parse", "HEAD")
    r = js(await call(ctx, "git_rebase", path=str(repo), onto="base"))
    assert r["done"] and (repo / "b.txt").exists() and git(repo, "rev-parse", "HEAD") != old
    undo.run(r["undo_id"])
    assert git(repo, "rev-parse", "HEAD") == old
    # conflict
    git(repo, "switch", "-q", "main")
    branch_with_change(repo, "x", "a.txt", "X\ntwo\nthree\n")
    commit(repo, "a.txt", "MAIN\ntwo\nthree\n")
    git(repo, "switch", "-q", "-c", "y")
    commit(repo, "a.txt", "Y\ntwo\nthree\n")
    y = git(repo, "rev-parse", "HEAD")
    r = js(await call(ctx, "git_rebase", path=str(repo), onto="x"))
    assert r["done"] is False and "a.txt" in r["conflicts"]
    assert git(repo, "rev-parse", "HEAD") == y and git(repo, "branch", "--show-current") == "y"
    assert not (repo / ".git" / "rebase-merge").exists()


async def test_cherry_pick(ctx, repo):
    git(repo, "switch", "-q", "-c", "other")
    c = commit(repo, "n.txt", "n\n")
    git(repo, "switch", "-q", "main")
    r = js(await call(ctx, "git_cherry_pick", path=str(repo), commits=[c]))
    assert r["done"] and (repo / "n.txt").exists()
    # conflicting pick
    git(repo, "switch", "-q", "other")
    k = commit(repo, "a.txt", "one\nOTHER\nthree\n")
    git(repo, "switch", "-q", "main")
    commit(repo, "a.txt", "one\nMAIN\nthree\n")
    r = js(await call(ctx, "git_cherry_pick", path=str(repo), commits=[k]))
    assert r["done"] is False and r["conflicts"] == ["a.txt"]
    assert git(repo, "status", "--porcelain") == ""


async def test_checkout_branch_tag_and_undo(ctx, repo):
    git(repo, "tag", "v0")
    r = js(await call(ctx, "git_checkout", path=str(repo), target="dev", create=True))
    assert r["branch"] == "dev" and r["was"] == "main"
    undo.run(r["undo_id"])
    assert git(repo, "branch", "--show-current") == "main"
    r = js(await call(ctx, "git_checkout", path=str(repo), target="v0"))
    assert "detached" in r["branch"]
    undo.run(r["undo_id"])
    assert git(repo, "branch", "--show-current") == "main"
    assert "Error" in await call(ctx, "git_checkout", path=str(repo), target="nope")
    assert "Error" in await call(ctx, "git_checkout", path=str(repo), target="bad name~", create=True)


async def test_checkout_refuses_to_clobber(ctx, repo):
    git(repo, "switch", "-q", "-c", "other")
    commit(repo, "a.txt", "other\n")
    git(repo, "switch", "-q", "main")
    (repo / "a.txt").write_text("precious\n", encoding="utf-8")
    assert "Error" in await call(ctx, "git_checkout", path=str(repo), target="other")
    assert (repo / "a.txt").read_text() == "precious\n"


async def test_restore_file_with_undo(ctx, repo):
    (repo / "a.txt").write_text("scribble\n", encoding="utf-8")
    r = js(await call(ctx, "git_restore", path=str(repo), file="a.txt"))
    assert (repo / "a.txt").read_text() == "one\ntwo\nthree\n"
    undo.run(r["undo_id"])
    assert (repo / "a.txt").read_text() == "scribble\n"
    assert "Error" in await call(ctx, "git_restore", path=str(repo), file="../outside.txt")


async def test_reset_modes(ctx, repo):
    first = git(repo, "rev-parse", "HEAD")
    second = commit(repo, "a.txt", "changed\n")
    r = js(await call(ctx, "git_reset", path=str(repo), ref="HEAD~1", mode="soft"))
    assert git(repo, "rev-parse", "HEAD") == first and git(repo, "diff", "--cached", "--name-only") == "a.txt"
    undo.run(r["undo_id"])
    assert git(repo, "rev-parse", "HEAD") == second
    assert "hard" in await call(ctx, "git_reset", path=str(repo), mode="hard")
    (repo / "a.txt").write_text("unsaved\n", encoding="utf-8")
    r = js(await call(ctx, "git_reset_hard", path=str(repo), ref="HEAD~1"))
    assert r["discarded_files"] == 1 and git(repo, "rev-parse", "HEAD") == first
    assert (repo / "a.txt").read_text() == "one\ntwo\nthree\n"
    undo.run(r["undo_id"])
    assert git(repo, "rev-parse", "HEAD") == second


async def test_tag_create_list_delete_undo(ctx, repo):
    r = js(await call(ctx, "git_tag", path=str(repo), action="create", name="v1", message="first"))
    assert "v1" in await call(ctx, "git_tag", path=str(repo))
    d = js(await call(ctx, "git_tag", path=str(repo), action="delete", name="v1"))
    assert "No tags" in await call(ctx, "git_tag", path=str(repo))
    undo.run(d["undo_id"])
    assert git(repo, "tag", "--list") == "v1" and git(repo, "cat-file", "-t", "v1") == "tag"
    assert "Error" in await call(ctx, "git_tag", path=str(repo), action="create", name="-d")
    assert r["created"] == "v1"


async def test_worktree_add_list_remove(ctx, repo, tmp_path):
    wt = tmp_path / "wt"
    r = js(await call(ctx, "git_worktree", path=str(repo), action="add", folder=str(wt), branch="wtb", new_branch=True))
    assert (wt / "a.txt").exists()
    assert "wt" in await call(ctx, "git_worktree", path=str(repo))
    (wt / "a.txt").write_text("dirty\n", encoding="utf-8")
    assert "Error" in await call(ctx, "git_worktree", path=str(repo), action="remove", folder=str(wt))
    (wt / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    await call(ctx, "git_worktree", path=str(repo), action="remove", folder=str(wt))
    assert not wt.exists() and r["added"]


async def test_fetch_and_pull_ff_only(ctx, repo, tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(bare), str(other))
    for k, v in (("user.email", "t@example.com"), ("user.name", "Tester"), ("commit.gpgsign", "false")):
        git(other, "config", k, v)
    commit(other, "r.txt", "remote\n")
    git(other, "push", "-q", "origin", "main")
    assert "Error" not in await call(ctx, "git_fetch", path=str(repo))
    assert not (repo / "r.txt").exists()
    r = js(await call(ctx, "git_pull", path=str(repo)))
    assert (repo / "r.txt").exists()
    undo.run(r["undo_id"])
    assert not (repo / "r.txt").exists()
    # diverge: local commit plus a new remote commit -> pull refuses, nothing changes
    commit(repo, "l.txt", "local\n")
    commit(other, "r2.txt", "remote2\n")
    git(other, "push", "-q", "origin", "main")
    head = git(repo, "rev-parse", "HEAD")
    out = await call(ctx, "git_pull", path=str(repo))
    assert out.startswith("Error") and git(repo, "rev-parse", "HEAD") == head
