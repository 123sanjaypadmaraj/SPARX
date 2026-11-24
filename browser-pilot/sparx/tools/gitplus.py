"""More git for Sparx: show, blame, fetch, pull, merge, rebase, cherry-pick, checkout/restore, reset, tag, worktree.

Rules: refs never start with '-', paths follow '--', nothing is ever forced or pushed, a conflicted merge / rebase /
cherry-pick is aborted so the repository is left as it was, and anything that moves HEAD records an undo entry."""
import os
import re
import shutil
import uuid
from pathlib import Path

from .. import config, undo
from . import B, I, S, A, ToolError, tool
from .coding import _clip, _git, _ref, _repo_root, _run, _safe_repo

_ENV = {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"}


def _gx(repo: Path, *args, timeout: float = 120) -> tuple[int, str]:
    """Run git non-interactively; returns (exit code, combined output)."""
    code, out, err = _run(["git", "-c", "core.quotepath=off", *args], repo, timeout,
                          {**os.environ, "GIT_TERMINAL_PROMPT": "0", **_ENV})
    return code, (out + err).strip()


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip() if _gx(repo, "rev-parse", "--verify", "-q", "HEAD")[0] == 0 else ""


def _branch(repo: Path) -> str:
    return _git(repo, "branch", "--show-current").strip()


def _dirty(repo: Path) -> bool:
    """True when tracked files have uncommitted changes (untracked files do not count)."""
    return bool(_git(repo, "status", "--porcelain", "-uno").strip())


def _clean(repo: Path, what: str):
    if _dirty(repo):
        raise ToolError(f"The working tree has uncommitted changes; commit or stash them before {what}. Nothing was done.")


def _conflicts(repo: Path) -> list[str]:
    return _git(repo, "diff", "--name-only", "--diff-filter=U", check=False).split()


def _name(repo: Path, name: str, kind: str = "branch") -> str:
    name = str(name or "").strip()
    flag = ["--branch"] if kind == "branch" else []
    probe = name if kind == "branch" else f"refs/tags/{name}"
    if not name or name.startswith("-") or _run(["git", "check-ref-format", *flag, probe], repo, 10)[0] != 0:
        raise ToolError(f"'{name}' is not a valid {kind} name.")
    return name


def _rel(repo: Path, file: str) -> str:
    """A path inside the repo (relative, '/' separated), allowed by the same safety rules as the coding tools."""
    f = str(file or "").strip()
    if not f:
        raise ToolError("a file path is required")
    p = _safe_repo((repo / f).resolve())
    try:
        return p.relative_to(repo).as_posix()
    except ValueError:
        raise ToolError(f"{f} is outside the repository.")


def _moved(ctx, repo: Path, old: str, what: str, mode: str = "hard") -> int | None:
    new = _head(repo)
    if new and old and new != old:
        return undo.record(ctx, "git_head", f"{what} in {repo.name}", {"repo": str(repo), "old": old, "new": new, "mode": mode})
    return None


@undo.handler("git_head")
def _undo_head(d: dict):
    repo = Path(d["repo"])
    if _head(repo) != d["new"]:
        raise ToolError("HEAD has moved since this action; not undoing it automatically.")
    if d["mode"] == "hard":
        _clean(repo, "undoing")
    _git(repo, "reset", f"--{d['mode']}", d["old"])


@undo.handler("git_switch")
def _undo_switch(d: dict):
    repo = Path(d["repo"])
    _clean(repo, "switching back")
    _git(repo, "switch", *(["--detach"] if d["detached"] else []), d["prev"])


@undo.handler("git_file")
def _undo_file(d: dict):
    shutil.copy2(d["backup"], d["path"])


@undo.handler("git_tag")
def _undo_tag(d: dict):
    repo = Path(d["repo"])
    if d["sha"]:
        _git(repo, "update-ref", f"refs/tags/{d['name']}", d["sha"])
    else:
        _git(repo, "tag", "-d", d["name"])


# ---- reading

@tool("git_show", "Show a commit (message, changed files and diff) or, with `file`, a file as it was at a ref.",
      {"path": S, "ref": {**S, "description": "commit, branch or tag; default HEAD"}, "file": {**S, "description": "optional file path to show at that ref"}},
      ["path"], category="coding", untrusted=True)
def git_show(ctx, path: str, ref: str = "HEAD", file: str = ""):
    repo = _repo_root(path)
    ref = _ref(ref or "HEAD")
    if file:
        return _clip(_git(repo, "show", f"{ref}:{_rel(repo, file)}"), 12000)
    return _clip(_git(repo, "show", "--no-color", "--stat", "--patch", "--date=short",
                      "--pretty=format:%h %an %ad%n%s%n%n%b", ref), 12000)


@tool("git_blame", "Who last changed each line of a file, with commit and date, for a line range (default 50 lines).",
      {"path": S, "file": S, "start": I, "end": I}, ["path", "file"], category="coding", untrusted=True)
def git_blame(ctx, path: str, file: str, start: int = 1, end: int = 0):
    repo = _repo_root(path)
    start = max(1, int(start))
    end = int(end) if int(end) >= start else start + 49
    end = min(end, start + 199)
    out = _git(repo, "blame", "-w", "--date=short", "-L", f"{start},{end}", "--", _rel(repo, file))
    return _clip(out, 12000) or "No lines."


# ---- remote

@tool("git_fetch", "Download new commits and branches from a remote without changing your files or branches.",
      {"path": S, "remote": {**S, "description": "default origin"}}, ["path"], category="coding", risk="write", timeout=150)
def git_fetch(ctx, path: str, remote: str = "origin"):
    repo = _repo_root(path)
    if not re.fullmatch(r"[\w.-]+", remote) or remote.startswith("-"):
        raise ToolError("remote must be a remote name such as 'origin'.")
    code, out = _gx(repo, "fetch", "--prune", remote)
    if code != 0:
        raise ToolError(f"fetch failed: {out[:600]}")
    return {"fetched": remote, "output": out[-600:] or "already up to date"}


@tool("git_pull", "Update the current branch from its remote, fast-forward only (never creates a merge commit, never overwrites). "
      "Fails with an explanation if the branches have diverged.",
      {"path": S, "remote": {**S, "description": "default: the branch's upstream"}, "branch": {**S, "description": "remote branch, with remote"}},
      ["path"], category="coding", risk="write", timeout=150)
def git_pull(ctx, path: str, remote: str = "", branch: str = ""):
    repo = _repo_root(path)
    args = ["pull", "--ff-only"]
    if remote:
        if not re.fullmatch(r"[\w.-]+", remote) or remote.startswith("-"):
            raise ToolError("remote must be a remote name such as 'origin'.")
        args += [remote, *([_ref(branch)] if branch else [])]
    cur = _branch(repo)
    if not cur:
        raise ToolError("HEAD is detached; switch to a branch first.")
    old = _head(repo)
    code, out = _gx(repo, *args, timeout=140)
    if code != 0:
        raise ToolError(f"pull failed (nothing was forced): {out[:600]}")
    uid = _moved(ctx, repo, old, f"Pulled {cur}")
    return {"branch": cur, "from": old[:8], "to": _head(repo)[:8], "undo_id": uid, "output": out[-400:]}


# ---- history changes

def _integrate(ctx, repo: Path, verb: str, args: list, abort: list, what: str):
    """Run merge / rebase / cherry-pick on a clean tree; on conflict abort and report the files."""
    _clean(repo, verb)
    old = _head(repo)
    code, out = _gx(repo, *args)
    if code != 0:
        files = _conflicts(repo)
        _gx(repo, *abort)
        if files:
            return {"done": False, "conflicts": files, "note": f"{verb} hit conflicts and was aborted; the repository is unchanged."}
        raise ToolError(f"{verb} failed and was aborted: {out[:500]}")
    uid = _moved(ctx, repo, old, what)
    return {"done": True, "head": _head(repo)[:8], "branch": _branch(repo), "undo_id": uid, "output": out[-300:]}


@tool("git_merge", "Merge a branch into the current one. On conflicts it aborts cleanly and lists the conflicted files.",
      {"path": S, "branch": S}, ["path", "branch"], category="coding", risk="write")
def git_merge(ctx, path: str, branch: str):
    repo = _repo_root(path)
    return _integrate(ctx, repo, "merge", ["merge", "--no-edit", _ref(branch)], ["merge", "--abort"], f"Merged {branch}")


@tool("git_rebase", "Rebase the current branch onto another (non-interactive). Rewrites this branch's commits; on conflicts it "
      "aborts and lists the conflicted files. Undoable. Do not rebase commits already pushed and shared.",
      {"path": S, "onto": S}, ["path", "onto"], category="coding", risk="write")
def git_rebase(ctx, path: str, onto: str):
    repo = _repo_root(path)
    if not _branch(repo):
        raise ToolError("HEAD is detached; switch to a branch first.")
    return _integrate(ctx, repo, "rebase", ["rebase", _ref(onto)], ["rebase", "--abort"], f"Rebased onto {onto}")


@tool("git_cherry_pick", "Apply one or more existing commits onto the current branch. On conflicts it aborts and lists the files.",
      {"path": S, "commits": {**A, "description": "commit hashes, oldest first"}}, ["path", "commits"], category="coding", risk="write")
def git_cherry_pick(ctx, path: str, commits: list):
    repo = _repo_root(path)
    commits = [_ref(str(c).strip()) for c in (commits if isinstance(commits, list) else str(commits).split()) if str(c).strip()]
    if not commits:
        raise ToolError("give at least one commit")
    return _integrate(ctx, repo, "cherry-pick", ["cherry-pick", *commits], ["cherry-pick", "--abort"], f"Cherry-picked {len(commits)} commit(s)")


# ---- moving around

@tool("git_checkout", "Switch to a branch (or tag/commit), optionally creating the branch. Refuses if local changes would be "
      "overwritten. To discard changes to a file use git_restore.",
      {"path": S, "target": {**S, "description": "branch, tag or commit"}, "create": {**B, "description": "create the branch first"},
       "start": {**S, "description": "start point for a new branch, default HEAD"}}, ["path", "target"], category="coding", risk="write")
def git_checkout(ctx, path: str, target: str, create: bool = False, start: str = ""):
    repo = _repo_root(path)
    prev, sha = _branch(repo), _head(repo)
    if create:
        _git(repo, "switch", "-c", _name(repo, target), *([_ref(start)] if start else []))
    else:
        code, out = _gx(repo, "switch", "-q", _ref(target))
        if code != 0 and "a branch is expected" not in out and "invalid reference" not in out:
            raise ToolError(f"checkout failed (nothing was discarded): {out[:500]}")
        if code != 0:                                   # a tag or commit: detached HEAD
            _git(repo, "switch", "-q", "--detach", _ref(target))
    now = _branch(repo)
    uid = undo.record(ctx, "git_switch", f"Switched {repo.name} to {target}", {"repo": str(repo), "prev": prev or sha, "detached": not prev})
    return {"branch": now or f"(detached at {_head(repo)[:8]})", "was": prev or sha[:8], "undo_id": uid}


@tool("git_restore", "Discard uncommitted changes to a file, or restore it from a ref. The overwritten version is kept for undo.",
      {"path": S, "file": S, "source": {**S, "description": "ref to restore from; default the index/HEAD"}},
      ["path", "file"], category="coding", risk="delete")
def git_restore(ctx, path: str, file: str, source: str = ""):
    repo = _repo_root(path)
    rel = _rel(repo, file)
    p = repo / rel
    backup = ""
    if p.is_file():
        backup = str(config.path("code_undo", f"{uuid.uuid4().hex}-{p.name}.bak"))
        shutil.copy2(p, backup)
    _git(repo, "restore", *([f"--source={_ref(source)}"] if source else []), "--", rel)
    uid = undo.record(ctx, "git_file", f"Restored {rel}", {"path": str(p), "backup": backup}) if backup else None
    return {"restored": rel, "undo_id": uid}


@tool("git_reset", "Move the current branch to another commit, keeping your file changes (soft keeps them staged, mixed unstaged). "
      "Undoable. For a hard reset use git_reset_hard.",
      {"path": S, "ref": {**S, "description": "default HEAD (unstage everything)"}, "mode": {**S, "enum": ["soft", "mixed"]}},
      ["path"], category="coding", risk="write")
def git_reset(ctx, path: str, ref: str = "HEAD", mode: str = "mixed"):
    repo = _repo_root(path)
    if mode not in ("soft", "mixed"):
        raise ToolError("mode must be soft or mixed; a hard reset needs git_reset_hard.")
    old = _head(repo)
    _git(repo, "reset", f"--{mode}", _ref(ref or "HEAD"))
    return {"head": _head(repo)[:8], "was": old[:8], "undo_id": _moved(ctx, repo, old, f"Reset {mode} to {ref}", "soft")}


@tool("git_reset_hard", "Move the current branch to a commit and DISCARD all uncommitted changes to tracked files. The previous "
      "commit is restorable with undo, but uncommitted work is not.",
      {"path": S, "ref": {**S, "description": "default HEAD"}}, ["path"], category="coding", risk="delete")
def git_reset_hard(ctx, path: str, ref: str = "HEAD"):
    repo = _repo_root(path)
    old, lost = _head(repo), _git(repo, "status", "--porcelain", "-uno").splitlines()
    _git(repo, "reset", "--hard", _ref(ref or "HEAD"))
    return {"head": _head(repo)[:8], "was": old[:8], "discarded_files": len(lost),
            "undo_id": _moved(ctx, repo, old, f"Hard reset to {ref}"),
            "note": "uncommitted changes were discarded" if lost else ""}


@tool("git_tag", "List tags, create a tag (annotated if a message is given) or delete a local tag. Never pushes.",
      {"path": S, "action": {**S, "enum": ["list", "create", "delete"]}, "name": S, "ref": {**S, "description": "commit to tag, default HEAD"},
       "message": S}, ["path"], category="coding", risk="write", untrusted=True)
def git_tag(ctx, path: str, action: str = "list", name: str = "", ref: str = "", message: str = ""):
    repo = _repo_root(path)
    if action == "list":
        return _git(repo, "tag", "--list", "--sort=-creatordate").split()[:100] or "No tags."
    if action not in ("create", "delete"):
        raise ToolError("action must be list, create or delete")
    name = _name(repo, name, "tag")
    if action == "create":
        _git(repo, "tag", *(["-a", name, "-m", message] if message else [name]), *([_ref(ref)] if ref else []))
        uid = undo.record(ctx, "git_tag", f"Created tag {name}", {"repo": str(repo), "name": name, "sha": ""})
        return {"created": name, "at": _git(repo, "rev-parse", "--short", f"{name}^{{commit}}").strip(), "undo_id": uid}
    sha = _git(repo, "rev-parse", "--verify", f"refs/tags/{name}").strip()
    _git(repo, "tag", "-d", name)
    return {"deleted": name, "undo_id": undo.record(ctx, "git_tag", f"Deleted tag {name}", {"repo": str(repo), "name": name, "sha": sha})}


@tool("git_worktree", "Manage extra working folders of the same repository: add (a branch checked out in a new folder), list, or "
      "remove (refuses if the worktree has uncommitted changes).",
      {"path": S, "action": {**S, "enum": ["list", "add", "remove"]}, "folder": {**S, "description": "worktree folder (relative to the repo)"},
       "branch": {**S, "description": "branch to check out, or the new branch name with new_branch"}, "new_branch": B},
      ["path"], category="coding", risk="write")
def git_worktree(ctx, path: str, action: str = "list", folder: str = "", branch: str = "", new_branch: bool = False):
    repo = _repo_root(path)
    if action == "list":
        return _git(repo, "worktree", "list").splitlines()
    if action not in ("add", "remove"):
        raise ToolError("action must be list, add or remove")
    if not folder.strip():
        raise ToolError("folder is required")
    f = Path(folder.strip())
    target = _safe_repo(f if f.is_absolute() else repo / f)
    if action == "remove":
        _git(repo, "worktree", "remove", str(target))
        return {"removed": str(target)}
    if target.exists() and any(target.iterdir()):
        raise ToolError(f"{target} already exists and is not empty.")
    if new_branch:
        _git(repo, "worktree", "add", "-b", _name(repo, branch), str(target))
    else:
        _git(repo, "worktree", "add", str(target), *([_ref(branch)] if branch else []))
    return {"added": str(target), "branch": branch or "(detached HEAD)"}
