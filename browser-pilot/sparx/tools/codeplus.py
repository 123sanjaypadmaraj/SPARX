"""Coding power tools: glob, atomic multi-file edits, whole-word rename and IDE-style diagnostics.

Builds on sparx.tools.coding (path safety, undo backups, read-before-edit tracking). Everything that changes files is
reversible with undo and goes through _safe_repo.
"""
import ast
import difflib
import io
import json
import os
import re
import shutil
import tokenize
import uuid
from pathlib import Path

from .. import config, security, undo
from . import B, I, O, S, ToolError, tool
from . import coding
from .coding import SKIP_DIRS, _dir, _inside, _mark_read, _read_strict, _require_read, _run, _safe_repo

# ---- code_glob


def _glob_rx(pattern: str) -> re.Pattern:
    """A glob with ** (any depth), * (within a folder), ? and [..]. Without a slash it matches names at any depth."""
    pat = pattern.strip().replace(chr(92), "/").removeprefix("./")
    pat = "*" if pat in ("", ".") else pat
    if "/" not in pat:
        pat = "**/" + pat
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pat.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pat[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pat[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pat[i] == "[" and (j := pat.find("]", i + 2)) > 0:
            body = pat[i + 1:j]
            body = "^" + body[1:] if body.startswith("!") else body
            out, i = out + "[" + body.replace(chr(92), chr(92) * 2) + "]", j + 1
        else:
            out, i = out + re.escape(pat[i]), i + 1
    return re.compile(out + r"\Z", re.I if os.name == "nt" else 0)


def _all_files(repo: Path) -> list[str]:
    """Every non-ignored file (binary included) as repo-relative posix paths: git's view, else a .gitignore walk."""
    rels = None
    if shutil.which("git"):
        code, out, _ = _run(["git", "-c", "core.quotepath=off", "ls-files", "-co", "--exclude-standard"], repo, 60)
        if code == 0:
            rels = out.splitlines()
    if rels is None:
        import fnmatch
        pats, rels = coding._gitignore(repo), []
        ignored = lambda rel: any(fnmatch.fnmatch(part, p) for p in pats for part in rel.split("/")) or \
            any(fnmatch.fnmatch(rel, p) for p in pats)
        for root, dirs, names in os.walk(repo):
            base = Path(root).relative_to(repo).as_posix()
            pre = "" if base == "." else base + "/"
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not ignored(pre + d)]
            rels += [pre + n for n in names if not ignored(pre + n)]
    root = repo.resolve()
    return [r for r in rels if not (set(r.split("/")[:-1]) & SKIP_DIRS) and (repo / r).is_file()
            and _inside((repo / r).resolve(), root) and not security.secret_path(repo / r)]


@tool("code_glob", "Find files by glob pattern in a repository (e.g. '**/*.py', 'src/**/*.ts', '*.md'), newest first. "
      "Respects .gitignore. Use it to locate files by name; use code_search to search inside files.",
      {"pattern": {**S, "description": "glob such as '**/*.py'; without a slash it matches at any depth"},
       "path": {**S, "description": "repository or folder to search (default: the current directory)"},
       "limit": {**I, "description": "max results, default 200, at most 1000"}},
      ["pattern"], category="coding", untrusted=True)
def code_glob(ctx, pattern: str, path: str = ".", limit: int = 200):
    if not pattern.strip():
        raise ToolError("a glob pattern is required, e.g. '**/*.py'")
    repo = _dir(path or ".")
    rx = _glob_rx(pattern)
    hits = []
    for rel in _all_files(repo):
        if rx.match(rel):
            try:
                hits.append((repo.joinpath(rel).stat().st_mtime, rel))
            except OSError:
                pass
    hits.sort(key=lambda t: (-t[0], t[1]))
    cap = max(1, min(int(limit or 200), 1000))
    out = {"root": str(repo), "count": len(hits), "files": [r for _, r in hits[:cap]]}
    if len(hits) > cap:
        out["note"] = f"{len(hits) - cap} more matches not shown; narrow the pattern or raise limit"
    return out


# ---- atomic multi-file writes

@undo.handler("code_multi_edit")
def _undo_multi(d: dict):
    for f in d["files"]:
        coding._undo_write(f)


def _commit(ctx, changes: dict[Path, tuple[str, str]], what: str) -> dict:
    """Write {path: (old text, new text)} all-or-nothing, with one undo entry and a unified diff."""
    done, entries = [], []
    try:
        for p, (old, new) in changes.items():
            p = _safe_repo(p)
            backup = ""
            if p.exists():
                backup = str(config.path("code_undo", f"{uuid.uuid4().hex}-{p.name}.bak"))
                shutil.copy2(p, backup)
            entries.append({"path": str(p), "backup": backup})
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(new.encode("utf-8"))
            done.append(p)
    except Exception as e:
        for ent in entries:         # roll back whatever was already written
            try:
                coding._undo_write(ent)
            except Exception:
                pass
        raise ToolError(f"Nothing was changed: writing {entries[-1]['path'] if entries else '?'} failed ({e}).")
    uid = undo.record(ctx, "code_multi_edit", f"{what} {len(entries)} file(s)", {"files": entries})
    for p in done:
        _mark_read(ctx, p)
    diff = "\n".join(line for p, (old, new) in changes.items() for line in difflib.unified_diff(
        old.splitlines(), new.splitlines(), f"a/{p.name}", f"b/{p.name}", lineterm=""))
    return {"files": [str(p) for p in done], "undo_id": uid,
            "diff": diff[:12000] + ("\n[diff cut]" if len(diff) > 12000 else "")}


def _apply(text: str, old: str, new: str, replace_all: bool, label: str) -> tuple[str, int]:
    if not old:
        raise ToolError(f"{label}: `old` is empty.")
    if old not in text and "\r\n" in text:
        old, new = old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
    n = text.count(old)
    if n == 0:
        raise ToolError(f"{label}: `old` was not found. Read the file again and copy the text exactly.")
    if n > 1 and not replace_all:
        raise ToolError(f"{label}: `old` occurs {n} times. Add surrounding lines or set replace_all.")
    if old == new:
        raise ToolError(f"{label}: `old` and `new` are identical.")
    return text.replace(old, new), n


@tool("code_multi_edit", "Apply several exact-string edits across one or many files atomically: either every edit applies or "
      "none does. Edits to the same file run in order. Each edit is {path, old, new, replace_all?}; existing files must "
      "have been read with code_read first. Returns a unified diff; one undo reverses the whole batch.",
      {"edits": {"type": "array", "description": "list of {path, old, new, replace_all}", "items": O}},
      ["edits"], category="coding", risk="write")
def code_multi_edit(ctx, edits: list):
    if not isinstance(edits, list) or not edits:
        raise ToolError("`edits` must be a non-empty list of {path, old, new, replace_all}.")
    if len(edits) > 100:
        raise ToolError("At most 100 edits per call.")
    state: dict[Path, list] = {}        # path -> [original, current]
    count = 0
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict) or not e.get("path"):
            raise ToolError(f"edit {i}: needs a `path`.")
        p = _safe_repo(e["path"])
        if p.is_dir():
            raise ToolError(f"edit {i}: {p} is a folder.")
        if p not in state:
            if p.exists():
                _require_read(ctx, p)
                text = _read_strict(p)
            elif e.get("old"):
                raise ToolError(f"edit {i}: {p} does not exist; use an empty `old` to create it.")
            else:
                text = ""
            state[p] = [text, text, p.exists()]
        label = f"edit {i} ({p.name})"
        if not e.get("old") and not state[p][2] and state[p][1] == "":
            state[p][1] = str(e.get("new", ""))
            continue
        state[p][1], n = _apply(state[p][1], str(e.get("old", "")), str(e.get("new", "")), bool(e.get("replace_all")), label)
        count += n
    changes = {p: (o, c) for p, (o, c, _) in state.items() if o != c}
    if not changes:
        raise ToolError("The edits change nothing.")
    return {**_commit(ctx, changes, "Multi-edited"), "edits": len(edits), "replacements": count}


# ---- rename

_IDENT = re.compile(r"[A-Za-z_$][\w$]*\Z")
_RENAME_SKIP_LANGS = {"Markdown", "JSON", "YAML", "TOML"}


def _py_rename(src: str, old: str, new: str) -> tuple[str, list[int]]:
    """Rename NAME tokens only: strings and comments (docstrings included) are left alone."""
    lines = src.splitlines(keepends=True)
    spots = [t.start for t in tokenize.generate_tokens(io.StringIO(src).readline)
             if t.type == tokenize.NAME and t.string == old]
    for row, col in reversed(spots):
        l = lines[row - 1]
        lines[row - 1] = l[:col] + new + l[col + len(old):]
    return "".join(lines), [r for r, _ in spots]


def _rx_rename(src: str, old: str, new: str) -> tuple[str, list[int]]:
    rx = re.compile(rf"(?<![\w$]){re.escape(old)}(?![\w$])")
    rows = [src.count("\n", 0, m.start()) + 1 for m in rx.finditer(src)]
    return rx.sub(lambda m: new, src), rows


@tool("code_rename_symbol", "Rename an identifier (function, class, variable, attribute) across a repository, whole-word. "
      "Python files are renamed token by token, so strings, comments and docstrings are left alone; other code files "
      "use a word-boundary match. dry_run (the default) only previews; set dry_run=false to apply. One undo reverses it.",
      {"path": {**S, "description": "repository folder"}, "old": {**S, "description": "current identifier"},
       "new": {**S, "description": "new identifier"},
       "dry_run": {**B, "description": "preview only (default true)"},
       "glob": {**S, "description": "optional file filter, e.g. 'src/**/*.py'"}},
      ["path", "old", "new"], category="coding", risk="write", untrusted=True)
def code_rename_symbol(ctx, path: str, old: str, new: str, dry_run: bool = True, glob: str = ""):
    if not _IDENT.match(old) or not _IDENT.match(new):
        raise ToolError("`old` and `new` must be plain identifiers (letters, digits, underscore).")
    if old == new:
        raise ToolError("`old` and `new` are identical.")
    repo = _dir(path)
    rx = _glob_rx(glob) if glob else None
    changes, report, shown, clash, skipped = {}, [], [], [], []
    for rel in coding._files(repo):
        ext = coding._ext(rel)
        if coding.LANGS.get(ext) in (None, *_RENAME_SKIP_LANGS) or (rx and not rx.match(rel)):
            continue
        p = repo / rel
        try:
            src = _read_strict(p)
        except ToolError:
            continue
        if not re.search(rf"(?<![\w$]){re.escape(old)}(?![\w$])", src):
            continue
        try:
            out, rows = (_py_rename if ext == ".py" else _rx_rename)(src, old, new)
        except (tokenize.TokenError, SyntaxError, IndentationError) as e:
            skipped.append(f"{rel}: could not tokenize ({e}); not renamed")
            continue
        if not rows:
            continue
        if re.search(rf"(?<![\w$]){re.escape(new)}(?![\w$])", src):
            clash.append(rel)
        changes[p] = (src, out)
        report.append({"file": rel, "occurrences": len(rows)})
        text = src.splitlines()
        shown += [f"{rel}:{r}: {text[r - 1].strip()[:160]}" for r in rows[:5] if r <= len(text)]
    if not changes:
        raise ToolError(f"No whole-word occurrence of `{old}` found" + (f" ({'; '.join(skipped)})" if skipped else "."))
    total = sum(r["occurrences"] for r in report)
    res = {"old": old, "new": new, "files": report, "occurrences": total, "preview": shown[:50]}
    if clash:
        res["warning"] = f"`{new}` already appears in {', '.join(clash[:5])}; check for a name collision"
    if skipped:
        res["skipped"] = skipped
    if dry_run:
        return {**res, "dry_run": True, "note": "Nothing was changed. Call again with dry_run=false to apply."}
    done = _commit(ctx, changes, f"Renamed {old} to {new} in")
    return {**res, "dry_run": False, "undo_id": done["undo_id"], "diff": done["diff"]}


# ---- diagnostics

_MYPY = re.compile(r"^(.+?):(\d+):(?:(\d+):)?\s*(error|warning|note):\s*(.+)$")


def _rel(root: Path, f: str) -> str:
    try:
        return Path(f).resolve().relative_to(root).as_posix() if Path(f).is_absolute() else f.replace("\\", "/")
    except ValueError:
        return f.replace("\\", "/")


def _py_syntax(root: Path, files: list[Path]) -> list[dict]:
    found = []
    for p in files:
        try:
            ast.parse(p.read_text(encoding="utf-8", errors="replace"), str(p))
        except SyntaxError as e:
            found.append({"file": _rel(root, str(p)), "line": e.lineno or 0, "col": e.offset or 0,
                          "message": f"SyntaxError: {e.msg}", "tool": "py_compile"})
    return found


@tool("code_diagnostics", "IDE-style diagnostics for a file or a whole repo, using whatever is installed: ruff, pyright, mypy "
      "(Python), tsc (TypeScript), plus a Python syntax check. Returns file:line:col messages. Run it after editing "
      "to catch errors before running tests.",
      {"path": {**S, "description": "a file or a repository folder"},
       "tools": {**S, "description": "optional comma list to restrict: ruff,pyright,mypy,tsc,py_compile"}},
      ["path"], category="coding", risk="exec", timeout=240, untrusted=True)
def code_diagnostics(ctx, path: str, tools: str = ""):
    p = _safe_repo(path)
    if not p.exists():
        raise ToolError(f"{p} does not exist.")
    root = p if p.is_dir() else coding._project_root(p)
    target = "." if p.is_dir() else p.relative_to(root).as_posix() if _inside(p, root) else str(p)
    want = {t.strip().lower() for t in tools.split(",") if t.strip()}
    use = lambda n: not want or n in want
    files = coding._files(root) if p.is_dir() else [p.relative_to(root).as_posix()]
    py = [root / f for f in files if coding._ext(f) == ".py"]
    ts = [f for f in files if coding._ext(f) in {".ts", ".tsx"}]
    findings, used, notes = [], [], []

    def run(name, cmd, parse, timeout=150):
        code, out, err = _run(cmd, root, timeout)
        used.append(name)
        got = parse(out, err)
        findings.extend(got)
        if code not in (0, 1) and not got or code == -1:
            notes.append(f"{name} exited with code {code} and reported nothing parseable: {(err or out).strip()[-300:]}")

    if py:
        if use("py_compile"):
            used.append("py_compile")
            findings += _py_syntax(root, py)
        ruff = coding._py_tool("ruff") if use("ruff") else None
        if ruff:
            run("ruff", [*ruff, "check", "--output-format=concise", "--no-cache", target],
                lambda o, e: [{**f, "file": _rel(root, f["file"])} for f in coding.parse_lint(o, "ruff")])
        elif use("ruff") and want:
            notes.append("ruff is not installed: pip install ruff")
        pyright = shutil.which("pyright") if use("pyright") else None
        if pyright:
            def pr(o, e):
                try:
                    return [{"file": _rel(root, d.get("file", "")), "line": d["range"]["start"]["line"] + 1,
                             "col": d["range"]["start"]["character"] + 1, "message": d["message"].replace("\n", " "),
                             "severity": d["severity"], "tool": "pyright"}
                            for d in json.loads(o or "{}").get("generalDiagnostics", [])]
                except (ValueError, KeyError):
                    return []
            run("pyright", [pyright, "--outputjson", target], pr, 200)
        mypy = coding._py_tool("mypy") if use("mypy") else None
        if mypy and (want or not pyright):      # one type checker by default; ask for the other by name
            def mp(o, e):
                return [{"file": _rel(root, m.group(1)), "line": int(m.group(2)), "col": int(m.group(3) or 0),
                         "message": m.group(5).strip(), "severity": m.group(4), "tool": "mypy"}
                        for l in o.splitlines() if (m := _MYPY.match(l.strip())) and m.group(4) != "note"]
            run("mypy", [*mypy, "--show-column-numbers", "--no-error-summary", "--no-color-output",
                         "--ignore-missing-imports", "--no-incremental", target], mp, 200)
    if ts and use("tsc"):
        tsc = coding._node_bin(root, "tsc") or shutil.which("tsc")
        if tsc and (root / "tsconfig.json").exists():
            names = {f for f in ts}
            run("tsc", [tsc, "--noEmit", "--pretty", "false"],
                lambda o, e: [f for f in coding.parse_lint(o, "tsc") if p.is_dir() or f["file"] in names], 200)
        else:
            notes.append("TypeScript files found but tsc or tsconfig.json is missing (npm install --save-dev typescript).")
    if not used:
        raise ToolError("Nothing to check: no Python/TypeScript files, or the requested tools are not installed. "
                        + " ".join(notes))
    if py and not (set(used) - {"py_compile"}):
        notes.append("Only a syntax check ran. Install a linter or type checker: pip install ruff mypy")
    findings.sort(key=lambda f: (f["file"], f["line"], f["col"]))
    return {"root": str(root), "tools": used, "ok": not findings and not any("exited with code" in n for n in notes),
            "count": len(findings), "diagnostics": findings[:200], "notes": notes,
            **({"truncated": f"{len(findings) - 200} more not shown"} if len(findings) > 200 else {})}
