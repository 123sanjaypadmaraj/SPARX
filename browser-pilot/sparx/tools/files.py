"""Files and folders: find, read, write, move, organise, convert, archive, back up and watch.

Path safety lives here and is shared by system.py and documents.py: every path goes through safe_path(),
which resolves links first and only admits the home folder, Sparx's HOME and settings()["allowed_roots"].
"""
import csv
import fnmatch
import hashlib
import importlib
import io
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from .. import config, db, jobs, security, undo
from . import A, B, I, S, ToolError, category, tool

category("files", "Find, read, write, move, organise, convert, zip, back up and watch files and folders")

db.schema("""
CREATE TABLE IF NOT EXISTS file_watches (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, recursive INTEGER NOT NULL DEFAULT 0,
    pattern TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_watch_events (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, watch_id INTEGER NOT NULL, folder TEXT NOT NULL,
    path TEXT NOT NULL, change TEXT NOT NULL, size INTEGER
);
CREATE TABLE IF NOT EXISTS file_watch_cursors (consumer TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS file_backups (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL, file TEXT NOT NULL, files INTEGER, bytes INTEGER
);
""")

# ---------------------------------------------------------------- path safety

_KNOWN = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "OneDrive", "AppData")
_NOISE = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache"}


def _home() -> Path:
    return Path.home()


def _norm(p) -> str:
    return os.path.normcase(str(p)).rstrip("\\/")


def _inside(p, root) -> bool:
    a, b = _norm(p), _norm(root)
    return a == b or a.startswith(b + os.sep)


def _real(p) -> Path | None:
    try:
        return Path(os.path.expandvars(str(p))).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def bad_name(name: str) -> str:
    """Why Windows would treat this single file name as something else (stream, device, stripped dot/space), or ''."""
    if name in ("", ".", ".."):
        return ""
    if ":" in name:
        return "':' would address an alternate data stream"
    if name != name.rstrip(" ."):
        return "Windows strips trailing dots and spaces from names"
    if name.split(".")[0].rstrip(" ").lower() in _RESERVED:
        return "it is a reserved Windows device name"
    return ""


def _policy() -> dict:
    """Everything the checks need, read once per tool call (settings() reads a file)."""
    home, sparx = _home().resolve(), config.HOME.resolve()
    extra = [r for r in map(_real, config.settings().get("allowed_roots") or []) if r]
    env = [os.environ.get(k) for k in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")]
    system = [Path(e) for e in env if e]
    if os.name != "nt":
        system += [Path(x) for x in ("/etc", "/bin", "/sbin", "/usr", "/boot", "/sys", "/proc", "/dev", "/lib")]
    return {"home": home, "sparx": sparx, "extra": extra, "system": system, "roots": [home, sparx, *extra]}


def _denied(p: Path, pol: dict) -> str:
    """Why this (already resolved) path is always off limits, or ''."""
    name = p.name.lower()
    if any(_inside(p, d) for d in pol["system"]):
        return "it is a system folder"
    if why := security.secret_path(p, resolve=False):       # the one credentials list, shared by every path gate
        pol["secrets"] = pol.get("secrets", 0) + 1
        return why
    if _norm(p.parent) == _norm(pol["sparx"]) and (name in ("settings.json", "stop") or name.startswith("sparx.db")):
        return "it is one of Sparx's own control files"
    appdata = pol["home"] / "AppData"
    if _inside(p, appdata) and not _inside(p, pol["sparx"]) and not any(
            _inside(r, appdata) and _inside(p, r) for r in pol["extra"]):
        return "it is another app's data folder"
    return ""


def safe_path(path, base: Path | None = None, exists: bool = False, mutate: bool = False, pol: dict | None = None) -> Path:
    """Resolve a user-supplied path (links included) and refuse anything outside the allowed area.

    Relative paths are taken from `base` (default: the home folder). `mutate=True` also protects the roots
    themselves and the well-known home folders from being moved, renamed or deleted.
    """
    raw = str(path or "").strip().strip('"')
    if not raw:
        raise ToolError("A path is required.")
    pol = pol or _policy()
    if raw[0] == "~" and raw[1:2] in ("", "/", "\\"):
        raw = str(pol["home"] / raw[2:])
    p = Path(os.path.expandvars(raw))
    if not p.is_absolute():
        p = (base or pol["home"]) / p
    for part in p.parts[1:] if p.anchor else p.parts:
        if bad := bad_name(part):
            raise ToolError(f"Refused: '{part}' is not a usable name because {bad}.")
    try:
        p = p.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        raise ToolError(f"Cannot resolve the path {raw}: {e}")
    why = _denied(p, pol)
    if not why and not any(_inside(p, r) for r in pol["roots"]):
        why = "it is outside your home folder and the folders in the allowed_roots setting"
    if why:
        raise ToolError(f"Refused: {p} is off limits because {why}.")
    if mutate and any(_inside(r, p) for r in [*pol["roots"], *(pol["home"] / k for k in _KNOWN)]):
        raise ToolError(f"Refused: {p} is a protected top-level folder; it can be read but not moved, renamed or deleted.")
    if exists and not os.path.lexists(p):
        raise ToolError(f"Not found: {p}")
    return p


def _left_out(pol: dict) -> dict:
    """What a folder operation adds to its result when credential files inside the folder were skipped."""
    return {"skipped_credentials": security.secrets_skipped(pol["secrets"])} if pol.get("secrets") else {}


def walk(root: Path, recursive: bool = True, noise: bool = True, pol: dict | None = None):
    """Yield files under root. Links, junctions and off-limits folders are never entered or returned."""
    pol = pol or _policy()

    def ok(p: Path) -> bool:
        return not os.path.islink(p) and not _denied(p, pol)
    for d, dirs, names in os.walk(root):
        dp = Path(d)
        dirs[:] = sorted(x for x in dirs if recursive and (noise or x not in _NOISE)
                         and not os.path.isjunction(dp / x) and ok(dp / x))
        for n in sorted(names):
            if ok(dp / n):
                yield dp / n


def need(module: str, pip: str = ""):
    """Import an optional package or explain how to get it."""
    try:
        return importlib.import_module(module)
    except ImportError:
        raise ToolError(f"Install {pip or module} to use this: pip install {pip or module}")


# ---------------------------------------------------------------- small helpers

def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _entry(p: Path, st=None) -> dict:
    st = st or p.stat()
    return {"path": str(p), "size": st.st_size, "modified": _iso(st.st_mtime)}


def _list(v) -> list:
    if isinstance(v, str):
        return [x for x in re.split(r"[,\s]+", v) if x]
    return list(v or [])


def unique(p: Path) -> Path:
    """p, or 'name (2).ext' and so on when p is taken. Nothing is ever overwritten silently."""
    n, out = 2, p
    while os.path.lexists(out):
        out = p.with_name(f"{p.stem} ({n}){p.suffix}")
        n += 1
    return out


def _mkdirs(d: Path) -> list[str]:
    """Create d and return the folders that did not exist before (outermost first), for undo."""
    new, cur = [], d
    while not cur.exists():
        new.append(str(cur))
        cur = cur.parent
    d.mkdir(parents=True, exist_ok=True)
    return new[::-1]


def _trash_slot(name: str) -> Path:
    return config.path("trash", f"{_stamp()}-{uuid.uuid4().hex[:6]}", name)


def _trash(p: Path) -> Path:
    slot = _trash_slot(p.name)
    shutil.move(str(p), str(slot))
    return slot


def record(ctx, description: str, moves=(), created=(), dirs=()) -> int:
    """Log a reversible file change. moves are [from, to] pairs; created are new paths; dirs are new folders."""
    return undo.record(ctx, "file_ops", description, {"moves": [[str(a), str(b)] for a, b in moves],
                                                      "created": [str(c) for c in created], "dirs": list(dirs)})


@undo.handler("file_ops")
def _undo_ops(data: dict):
    created = {_norm(c) for c in data.get("created", [])}
    for src, dst in data.get("moves", []):
        if not os.path.lexists(dst):
            raise ToolError(f"Cannot undo: {dst} is no longer there.")
        if os.path.lexists(src) and _norm(src) not in created:
            raise ToolError(f"Cannot undo: something else is now at {src}.")
    for c in data.get("created", []):
        if os.path.lexists(c):
            _trash(Path(c))
    for src, dst in reversed(data.get("moves", [])):
        Path(src).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(dst, src)
    for d in reversed(data.get("dirs", [])):
        try:
            os.rmdir(d)
        except OSError:
            pass


_UNITS = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000, "y": 31536000}


def _when(s: str) -> float | None:
    """'2026-01-31', '2026-01-31T10:00', 'today', 'yesterday', '3 days ago', '2w' -> timestamp."""
    s = (s or "").strip().lower()
    if not s:
        return None
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if s in ("today", "yesterday"):
        return (midnight - timedelta(days=s == "yesterday")).timestamp()
    m = re.fullmatch(r"(?:last\s+)?(\d*)\s*(h|d|w|m|y)\w*(?:\s+ago)?", s)
    if m:
        return time.time() - int(m.group(1) or 1) * _UNITS[m.group(2)]
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        raise ToolError(f"Cannot read the date '{s}'. Use YYYY-MM-DD, 'today', 'yesterday' or e.g. '3 days ago'.")


def _size(v) -> int:
    """Bytes from 1048576, '10MB', '500 kb'."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?)i?b?\s*", str(v or "0").lower())
    if not m:
        raise ToolError(f"Cannot read the size '{v}'. Use bytes or e.g. '10MB'.")
    return int(float(m.group(1)) * 1024 ** " kmgt".index(m.group(2) or " "))


# ---------------------------------------------------------------- reading content

TEXT_MAX = 5_000_000
_IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif", ".ico"}
_TABLE = {".csv", ".tsv", ".json", ".xlsx"}


def _is_text(p: Path) -> bool:
    with p.open("rb") as f:
        head = f.read(4096)
    return head[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\0" not in head


def read_text(p: Path) -> str:
    b = p.read_bytes()
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", "replace")
    try:
        return b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def _pdf_pages(p: Path) -> list[str]:
    from pypdf import PdfReader
    try:
        r = PdfReader(str(p))
        if r.is_encrypted and not r.decrypt(""):
            raise ToolError(f"{p.name} is password-protected.")
        return [pg.extract_text() or "" for pg in r.pages]
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Cannot read the PDF {p.name}: {e}")


def _docx_paragraphs(p: Path) -> list[str]:
    doc = need("docx", "python-docx").Document(str(p))
    out = [x.text for x in doc.paragraphs if x.text.strip()]
    for t in doc.tables:
        out += ["\t".join(c.text for c in row.cells) for row in t.rows]
    return out


def _xlsx_sheets(p: Path, max_rows: int = 100000) -> dict[str, list[list]]:
    wb = need("openpyxl").load_workbook(str(p), read_only=True, data_only=True)
    try:
        return {ws.title: [list(r) for i, r in enumerate(ws.iter_rows(values_only=True)) if i < max_rows]
                for ws in wb.worksheets}
    finally:
        wb.close()


def _pptx_slides(p: Path) -> list[str]:
    prs = need("pptx", "python-pptx").Presentation(str(p))
    return ["\n".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame and sh.text_frame.text.strip())
            for s in prs.slides]


def extract(p: Path) -> tuple[str, str]:
    """(kind, text) for any file whose text can be read with what is installed."""
    ext = p.suffix.lower()
    if ext == ".pdf":
        return "pdf", "\n\n".join(f"[page {i}]\n{t}" for i, t in enumerate(_pdf_pages(p), 1))
    if ext == ".docx":
        return "docx", "\n".join(_docx_paragraphs(p))
    if ext in (".xlsx", ".xlsm"):
        return "xlsx", "\n\n".join(f"[sheet {n}]\n" + "\n".join("\t".join("" if c is None else str(c) for c in r) for r in rows)
                                   for n, rows in _xlsx_sheets(p, 2000).items())
    if ext == ".pptx":
        return "pptx", "\n\n".join(f"[slide {i}]\n{t}" for i, t in enumerate(_pptx_slides(p), 1))
    if p.stat().st_size > 4 * TEXT_MAX:
        raise ToolError(f"{p.name} is too large to read as text ({p.stat().st_size} bytes).")
    if not _is_text(p):
        raise ToolError(f"{p.name} is a binary file; file_info can describe it, file_convert may be able to convert it.")
    return "text", read_text(p)


# ---------------------------------------------------------------- find, search, read

@tool("file_find", "Find files by name, type, folder, date, size or text inside them. Use it when the user asks where a file is "
      "or wants files matching some description. Returns paths with size and modified time, newest first.",
      {"folder": {**S, "description": "where to look; default is the home folder"},
       "name": {**S, "description": "words that must be in the file name, or a pattern like 'invoice*2026*'"},
       "extensions": {**A, "description": "e.g. ['pdf', 'docx']"},
       "modified_after": {**S, "description": "YYYY-MM-DD, 'today', 'yesterday', '7 days ago'"},
       "modified_before": S,
       "min_size": {**S, "description": "e.g. '10MB'"}, "max_size": S,
       "contains": {**S, "description": "text that must appear inside the file (text files only)"},
       "limit": I}, category="files", untrusted=True, timeout=90)
def file_find(ctx, folder: str = "", name: str = "", extensions=None, modified_after: str = "", modified_before: str = "",
              min_size="", max_size="", contains: str = "", limit: int = 50):
    pol = _policy()
    root = safe_path(folder or pol["home"], exists=True, pol=pol)
    exts = {"." + e.lower().lstrip(".") for e in _list(extensions)}
    after, before, lo, hi = _when(modified_after), _when(modified_before), _size(min_size), _size(max_size)
    pat, needle = name.strip().lower(), contains.lower()
    is_glob = any(c in pat for c in "*?[")
    hits, scanned, deadline = [], 0, time.time() + 60
    for f in walk(root, noise=False, pol=pol) if root.is_dir() else [root]:
        scanned += 1
        if time.time() > deadline:
            break
        low = f.name.lower()
        if exts and f.suffix.lower() not in exts:
            continue
        if pat and not (fnmatch.fnmatch(low, pat) if is_glob else all(w in low for w in pat.split())):
            continue
        try:
            st = f.stat()
            if (after and st.st_mtime < after) or (before and st.st_mtime > before) or st.st_size < lo or (hi and st.st_size > hi):
                continue
            if needle and (st.st_size > TEXT_MAX or not _is_text(f) or needle not in read_text(f).lower()):
                continue
        except OSError:
            continue
        hits.append(_entry(f, st))
    hits.sort(key=lambda h: h["modified"], reverse=True)
    return {"found": len(hits), "files": hits[:min(max(1, limit), 500)], "scanned": scanned,
            "stopped_early": time.time() > deadline}


@tool("file_search_text", "Search for text inside files: plain text and code, PDFs (by page) and Word documents (by paragraph). "
      "Give a file or a folder. Returns each match with its line, page or paragraph number.",
      {"query": S, "path": {**S, "description": "file or folder; default is the home folder"},
       "regex": {**B, "description": "treat query as a regular expression"},
       "extensions": {**A, "description": "only these file types"}, "limit": I},
      ["query"], category="files", untrusted=True, timeout=120)
def file_search_text(ctx, query: str, path: str = "", regex: bool = False, extensions=None, limit: int = 50):
    pol, limit = _policy(), min(max(1, limit), 500)
    root = safe_path(path or pol["home"], exists=True, pol=pol)
    try:
        rx = re.compile(query if regex else re.escape(query), re.I)
    except re.error as e:
        raise ToolError(f"Bad regular expression: {e}")
    exts = {"." + e.lower().lstrip(".") for e in _list(extensions)}
    hits, searched, skipped, deadline = [], 0, 0, time.time() + 90

    def scan(f: Path):
        ext = f.suffix.lower()
        if ext == ".pdf":
            units = [(f"page {i}", ln) for i, t in enumerate(_pdf_pages(f), 1) for ln in t.splitlines()]
        elif ext == ".docx":
            units = [(f"paragraph {i}", t) for i, t in enumerate(_docx_paragraphs(f), 1)]
        elif f.stat().st_size <= TEXT_MAX and _is_text(f):
            units = [(f"line {i}", ln) for i, ln in enumerate(read_text(f).splitlines(), 1)]
        else:
            return False
        for where, text in units:
            m = rx.search(text)
            if m and len(hits) < limit:
                a = max(0, m.start() - 80)
                hits.append({"path": str(f), "where": where, "text": text[a:m.end() + 120].strip()})
        return True

    for f in walk(root, pol=pol) if root.is_dir() else [root]:
        if len(hits) >= limit or time.time() > deadline:
            break
        if exts and f.suffix.lower() not in exts:
            continue
        try:
            ok = scan(f)
        except (ToolError, OSError):
            ok = False
        searched, skipped = searched + ok, skipped + (not ok)
    return {"matches": hits, "files_searched": searched, "files_skipped": skipped,
            "stopped_early": len(hits) >= limit or time.time() > deadline, **_left_out(pol)}


@tool("file_read", "Read a file's text: plain text and code, PDF, and Word/Excel/PowerPoint when the matching package is installed. "
      "Long files are cut at max_chars; pass offset to continue.",
      {"path": S, "max_chars": I, "offset": {**I, "description": "character to start from"}},
      ["path"], category="files", untrusted=True)
def file_read(ctx, path: str, max_chars: int = 12000, offset: int = 0):
    p = safe_path(path, exists=True)
    if p.is_dir():
        raise ToolError(f"{p} is a folder; use folder_list.")
    kind, text = extract(p)
    offset = max(0, offset)
    part = text[offset:offset + min(max(1, max_chars), 200_000)]
    return {"path": str(p), "type": kind, "chars": len(text), "offset": offset,
            "more": offset + len(part) < len(text), "content": part}


@tool("file_write", "Create a text file or replace/append to one. The previous version is kept for undo.",
      {"path": {**S, "description": "absolute, or relative to the home folder"}, "content": S,
       "append": {**B, "description": "add to the end instead of replacing"}},
      ["path", "content"], category="files", risk="write")
def file_write(ctx, path: str, content: str, append: bool = False):
    p = safe_path(path)
    if p.is_dir():
        raise ToolError(f"{p} is a folder.")
    moves, dirs = [], _mkdirs(p.parent)
    if p.exists():
        keep = _trash_slot(p.name)
        (shutil.copy2 if append else shutil.move)(str(p), str(keep))
        moves = [(p, keep)]
    try:
        with p.open("a" if append else "w", encoding="utf-8", newline="") as f:
            f.write(content)
    except Exception:
        if moves:
            p.unlink(missing_ok=True)
            shutil.move(str(moves[0][1]), str(p))
        raise
    record(ctx, f"{'Appended to' if append else 'Wrote'} {p}", moves, [p], dirs)
    return {"path": str(p), "size": p.stat().st_size, "replaced_existing": bool(moves) and not append}


@tool("file_info", "Details of one file or folder: size, dates, type, and where cheap also image dimensions, PDF pages and a SHA-256.",
      {"path": S}, ["path"], category="files")
def file_info(ctx, path: str):
    pol = _policy()
    p = safe_path(path, exists=True, pol=pol)
    st = p.stat()
    out = {**_entry(p, st), "created": _iso(st.st_ctime), "kind": "folder" if p.is_dir() else "file"}
    if p.is_dir():
        sizes = [f.stat().st_size for f in walk(p, pol=pol)]
        return {**out, "files": len(sizes), "size": sum(sizes)}
    ext = p.suffix.lower()
    out["type"] = mimetypes.guess_type(p.name)[0] or "unknown"
    if st.st_size <= 100_000_000:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out["sha256"] = h.hexdigest()
    try:
        if ext in _IMG:
            from PIL import Image
            with Image.open(p) as im:
                out["image"] = {"width": im.width, "height": im.height, "mode": im.mode}
        elif ext == ".pdf":
            out["pages"] = len(_pdf_pages(p))
    except Exception:
        pass
    return out


@tool("folder_list", "List what is directly inside a folder, with sizes and modified times.",
      {"path": {**S, "description": "default is the home folder"}, "show_hidden": B}, category="files", untrusted=True)
def folder_list(ctx, path: str = "", show_hidden: bool = False):
    pol = _policy()
    p = safe_path(path or pol["home"], exists=True, pol=pol)
    if not p.is_dir():
        raise ToolError(f"{p} is not a folder.")
    rows = []
    for e in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if (not show_hidden and e.name.startswith(".")) or _denied(e, pol):
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        rows.append({"name": e.name, "kind": "folder" if e.is_dir() else "file",
                     "size": None if e.is_dir() else st.st_size, "modified": _iso(st.st_mtime)})
    return {"path": str(p), "count": len(rows), "entries": rows[:500]}


@tool("folder_tree", "Show a folder as an indented tree, a few levels deep. Good for understanding a project's layout.",
      {"path": S, "depth": {**I, "description": "levels to show, default 2"}}, ["path"], category="files", untrusted=True)
def folder_tree(ctx, path: str, depth: int = 2):
    pol = _policy()
    root = safe_path(path, exists=True, pol=pol)
    if not root.is_dir():
        raise ToolError(f"{root} is not a folder.")
    lines = [str(root)]

    def grow(d: Path, level: int):
        try:
            kids = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for e in kids:
            if len(lines) > 400:
                return
            if e.name in _NOISE or _denied(e, pol):
                continue
            is_dir = e.is_dir() and not os.path.islink(e) and not os.path.isjunction(e)
            lines.append("  " * level + e.name + ("/" if is_dir else ""))
            if is_dir and level < depth:
                grow(e, level + 1)
    grow(root, 1)
    return "\n".join(lines[:400]) + ("\n… (cut at 400 entries)" if len(lines) > 400 else "")


# ---------------------------------------------------------------- move, copy, rename, organise, delete

def _target(src: Path, destination: str, pol: dict) -> Path:
    dst = safe_path(destination, pol=pol)
    if dst.is_dir():
        dst = dst / src.name
    if os.path.lexists(dst):
        raise ToolError(f"{dst} already exists. Choose another name or move it away first.")
    if _inside(dst, src):
        raise ToolError("The destination is inside the source.")
    return dst


@tool("file_move", "Move a file or folder. If the destination is an existing folder the item goes inside it. Undoable.",
      {"source": S, "destination": S}, ["source", "destination"], category="files", risk="write")
def file_move(ctx, source: str, destination: str):
    pol = _policy()
    src = safe_path(source, exists=True, mutate=True, pol=pol)
    dst = _target(src, destination, pol)
    dirs = _mkdirs(dst.parent)
    shutil.move(str(src), str(dst))
    record(ctx, f"Moved {src} to {dst}", [(src, dst)], dirs=dirs)
    return {"moved": str(src), "to": str(dst)}


@tool("file_copy", "Copy a file or folder. Undoable (the copy goes to Sparx's trash).",
      {"source": S, "destination": S}, ["source", "destination"], category="files", risk="write", timeout=600)
def file_copy(ctx, source: str, destination: str):
    pol = _policy()
    src = safe_path(source, exists=True, pol=pol)
    dst = _target(src, destination, pol)
    dirs = _mkdirs(dst.parent)
    if src.is_dir():
        shutil.copytree(src, dst, ignore=lambda d, names: [
            n for n in names if os.path.islink(os.path.join(d, n)) or os.path.isjunction(os.path.join(d, n))
            or _denied(Path(d) / n, pol)])
    else:
        shutil.copy2(src, dst)
    record(ctx, f"Copied {src} to {dst}", created=[dst], dirs=dirs)
    return {"copied": str(src), "to": str(dst), **_left_out(pol)}


@tool("file_rename", "Rename one file or folder (give new_name), or batch-rename the files in a folder: either find/replace in "
      "the names, or a template such as 'trip-{n:03}' using {name} {ext} {n} {date}. Set dry_run to preview. Undoable.",
      {"path": {**S, "description": "the item to rename, or the folder whose files are renamed in batch"},
       "new_name": {**S, "description": "single rename; the extension is kept if you leave it out"},
       "match": {**S, "description": "batch: only names matching this pattern, e.g. '*.jpg'"},
       "find": S, "replace": S, "regex": {**B, "description": "find is a regular expression"},
       "template": S, "dry_run": B}, ["path"], category="files", risk="write")
def file_rename(ctx, path: str, new_name: str = "", match: str = "*", find: str = "", replace: str = "",
                regex: bool = False, template: str = "", dry_run: bool = False):
    pol = _policy()
    if new_name:
        src = safe_path(path, exists=True, mutate=True, pol=pol)
        if not Path(new_name).suffix and src.is_file():
            new_name += src.suffix
        pairs = [(src, new_name)]
    elif find or template:
        folder = safe_path(path, exists=True, pol=pol)
        if not folder.is_dir():
            raise ToolError("Batch rename needs a folder as path.")
        files = [f for f in sorted(folder.iterdir(), key=lambda f: f.name.lower()) if f.is_file()
                 and not f.name.startswith(".") and fnmatch.fnmatch(f.name.lower(), match.lower()) and not _denied(f, pol)]
        pairs = []
        for n, f in enumerate(files, 1):
            try:
                if template:
                    new = template.format(name=f.stem, ext=f.suffix.lstrip("."), n=n,
                                          date=datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d"))
                    new += "" if Path(new).suffix else f.suffix
                else:
                    new = re.sub(find, replace, f.name) if regex else f.name.replace(find, replace)
            except (re.error, KeyError, IndexError, ValueError) as e:
                raise ToolError(f"Bad {'template' if template else 'pattern'}: {e}")
            if new != f.name:
                pairs.append((f, new))
    else:
        raise ToolError("Give new_name for one item, or find/replace or template for a batch.")
    moves, seen = [], set()
    for src, new in pairs:
        if not new.strip(" .") or re.search(r'[\\/:*?"<>|]', new) or bad_name(new):
            raise ToolError(f"'{new}' is not a valid file name.")
        dst = safe_path(src.with_name(new), pol=pol)
        if _norm(dst) in seen or (os.path.lexists(dst) and _norm(dst) != _norm(src)):
            raise ToolError(f"Renaming {src.name} to {new} would collide with another file. Nothing was renamed.")
        seen.add(_norm(dst))
        moves.append((src, dst))
    if not dry_run:
        done = []
        try:
            for src, dst in moves:
                os.rename(src, dst)
                done.append((src, dst))
        except OSError as e:
            for src, dst in reversed(done):
                os.rename(dst, src)
            raise ToolError(f"Could not rename {src.name}: {e}. The {len(done)} earlier rename(s) were rolled back.")
        if moves:
            record(ctx, f"Renamed {len(moves)} item(s) in {moves[0][0].parent}", moves)
    return {"dry_run": dry_run, "renamed": 0 if dry_run else len(moves), "planned": len(moves),
            "plan": [{"from": s.name, "to": d.name} for s, d in moves[:100]]}


_TYPES = {
    "Images": _IMG | {".svg", ".heic", ".raw"}, "PDFs": {".pdf"},
    "Documents": {".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".epub"},
    "Spreadsheets": {".xls", ".xlsx", ".xlsm", ".csv", ".tsv", ".ods"}, "Presentations": {".ppt", ".pptx", ".odp", ".key"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"},
    "Video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".m4v"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "Installers": {".exe", ".msi", ".msix", ".dmg", ".apk", ".deb"},
    "Code": {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".xml", ".yml", ".yaml", ".java", ".c",
             ".cpp", ".h", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".ps1", ".bat", ".sql", ".ipynb"},
}


@tool("folder_organize", "Sort the loose files in a folder into subfolders by type (Images, Documents, …), by extension, or by "
      "month modified. By default this only returns the plan (dry run); call again with dry_run=false to do it. Undoable.",
      {"path": S, "by": {"type": "string", "enum": ["type", "extension", "date"]},
       "dry_run": {**B, "description": "default true: show the plan without moving anything"}},
      ["path"], category="files", risk="write")
def folder_organize(ctx, path: str, by: str = "type", dry_run: bool = True):
    pol = _policy()
    folder = safe_path(path, exists=True, pol=pol)
    if not folder.is_dir():
        raise ToolError(f"{folder} is not a folder.")
    if by not in ("type", "extension", "date"):
        raise ToolError("by must be type, extension or date.")
    moves = []
    for f in sorted(folder.iterdir(), key=lambda f: f.name.lower()):
        if not f.is_file() or os.path.islink(f) or f.name.startswith(".") or f.name.lower() == "desktop.ini" or _denied(f, pol):
            continue
        ext = f.suffix.lower()
        if by == "type":
            sub = next((k for k, v in _TYPES.items() if ext in v), "Other")
        elif by == "extension":
            sub = ext.lstrip(".") or "no-extension"
        else:
            sub = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m")
        moves.append((f, unique(folder / sub / f.name)))
    counts: dict[str, int] = {}
    for _, d in moves:
        counts[d.parent.name] = counts.get(d.parent.name, 0) + 1
    if not dry_run and moves:
        dirs, done = [], []
        try:
            for src, dst in moves:
                dirs += _mkdirs(dst.parent)
                shutil.move(str(src), str(dst))
                done.append((src, dst))
        except OSError as e:
            for src, dst in reversed(done):
                shutil.move(str(dst), str(src))
            for d in reversed(dirs):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
            raise ToolError(f"Could not move {src.name}: {e}. The {len(done)} earlier move(s) were rolled back.")
        record(ctx, f"Organised {len(moves)} file(s) in {folder} by {by}", moves, dirs=dirs)
    return {"dry_run": dry_run, "folder": str(folder), "files": len(moves), "folders": counts,
            "plan": [{"from": s.name, "to": f"{d.parent.name}/{d.name}"} for s, d in moves[:200]],
            "note": "Nothing was moved; call again with dry_run=false to apply." if dry_run else "Done. Use undo to reverse."}


@tool("file_delete", "Delete a file or folder by moving it to Sparx's trash. It can be restored with undo.",
      {"path": S}, ["path"], category="files", risk="delete")
def file_delete(ctx, path: str):
    src = safe_path(path, exists=True, mutate=True)
    slot = _trash(src)
    uid = record(ctx, f"Deleted {src}", [(src, slot)])
    return {"deleted": str(src), "trash": str(slot), "undo_id": uid}


# ---------------------------------------------------------------- convert

_LATIN = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", "•": "-",
                        "\u00a0": " ", "→": "->", "₹": "Rs "})


def write_pdf(blocks: list[tuple[str, str]], out: Path) -> str:
    """Render (style, text) blocks to a PDF with fpdf. Styles: h1 h2 p li pre table. Returns a note ('' if nothing was lost)."""
    fpdf = need("fpdf", "fpdf2")
    pdf = fpdf.FPDF()
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    family, lossy, notes, missing = "Helvetica", False, [], set()
    fonts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
    v2 = int(str(getattr(fpdf, "__version__", "1")).split(".")[0]) >= 2
    if v2:  # fpdf2 can embed Unicode fonts; classic fpdf 1.x only has Latin-1 core fonts
        want = {ord(c) for _s, t in blocks for c in (t if isinstance(t, str) else "".join(str(c) for r in t for c in r))
                if c.isprintable() and not c.isspace()}
        loaded, covered = [], set()
        for name, regular, bold, face in (("SparxSans", "arial.ttf", "arialbd.ttf", {}),      # Latin, Greek, Cyrillic...
                                          ("SparxIndic", "Nirmala.ttc", "Nirmala.ttc", {"collection_font_number": 1})):
            if loaded and not want - covered:
                break                                   # Nirmala UI (Indic scripts) is embedded only when needed
            if not (fonts / regular).is_file():
                continue
            try:
                pdf.add_font(name, "", str(fonts / regular))
                pdf.add_font(name, "B", str(fonts / (bold if (fonts / bold).is_file() else regular)), **face)
                covered |= set(pdf.fonts[name.lower()].cmap)
                loaded.append(name)
            except Exception as e:
                notes.append(f"The font file {regular} could not be loaded ({type(e).__name__}: {e}).")
        if loaded:
            family, missing = loaded[0], want - covered
            if loaded[1:]:
                pdf.set_fallback_fonts(loaded[1:], exact_match=False)
            if missing:
                notes.append(f"{len(missing)} different character(s), such as {' '.join(chr(c) for c in sorted(missing)[:5])}, "
                             f"are in none of the fonts Sparx could load from {fonts} (it looks for arial.ttf, and Nirmala.ttc "
                             "for Hindi and Kannada) and were replaced with '?'.")
            if any(0x0900 <= c <= 0x0DFF for c in want - missing):     # Indic: conjuncts and vowel signs need shaping
                try:
                    importlib.import_module("uharfbuzz")
                    pdf.set_text_shaping(True)
                except Exception:
                    notes.append("Text shaping is unavailable (pip install uharfbuzz), so Hindi and Kannada conjuncts and "
                                 "vowel signs may look unjoined or misplaced.")

    def fit(text: str) -> str:
        nonlocal lossy
        if family != "Helvetica":
            return "".join("?" if ord(c) in missing else c for c in text) if missing else text
        clean = text.translate(_LATIN).encode("latin-1", "replace").decode("latin-1")
        lossy = lossy or clean != text.translate(_LATIN)
        return clean
    for style, text in blocks:
        if style == "table":                  # text is a list of rows; a real grid needs fpdf2, 1.x gets " | " lines
            rows = [[fit(str(c)) for c in r] for r in text]
            pdf.set_font(family, "", 10)
            pdf.set_x(pdf.l_margin)
            if hasattr(pdf, "table"):
                with pdf.table() as grid:
                    for r in rows:
                        line = grid.row()
                        for c in r:
                            line.cell(c)
            else:
                for i, r in enumerate(rows):
                    pdf.set_font(family, "" if i else "B", 10)
                    pdf.set_x(pdf.l_margin)
                    pdf.multi_cell(0, 5, " | ".join(r) or " ")
            pdf.ln(2)
            continue
        text = fit(text)
        bold, size, height = {"h1": ("B", 18, 9), "h2": ("B", 13, 7), "pre": ("", 9, 4.5)}.get(style, ("", 11, 5.5))
        pdf.set_font("Courier" if style == "pre" and family == "Helvetica" else family, bold, size)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, height, ("- " + text) if style == "li" else text or " ")
        pdf.ln(0 if style in ("pre", "li") else 2)
    pdf.output(str(out))
    if lossy:
        notes.append(f"No usable Unicode font (arial.ttf or Nirmala.ttc) was found in {fonts}, so characters outside Latin-1 "
                     "were replaced with '?'." if v2 else
                     "Characters outside Latin-1 were replaced with '?'; install fpdf2 for full Unicode PDFs.")
    return " ".join(notes)


def _md_blocks(text: str, markdown: bool) -> list[tuple[str, str]]:
    if not markdown:
        return [("pre", ln) for ln in text.splitlines()]
    out, para = [], []

    def flush():
        if para:
            out.append(("p", " ".join(para)))
            para.clear()
    for ln in text.splitlines():
        s = ln.strip()
        h, li = re.match(r"(#{1,6})\s+(.*)", s), re.match(r"(?:[-*+]|\d+[.)])\s+(.*)", s)
        if h or li or not s:
            flush()
            if h:
                out.append(("h1" if len(h.group(1)) == 1 else "h2", h.group(2)))
            elif li:
                out.append(("li", li.group(1)))
        else:
            para.append(s)
    flush()
    return [(k, re.sub(r"(\*\*|__|`)", "", t)) for k, t in out]


def md_to_html(text: str, title: str) -> str:
    try:
        body = importlib.import_module("markdown_it").MarkdownIt("commonmark").enable("table").render(text)
    except ImportError:
        body = need("markdown").markdown(text, extensions=["tables"])
    import html
    return (f'<!doctype html>\n<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>\n'
            "<style>body{font:16px/1.6 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem}"
            "table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:.3rem .6rem}"
            "pre{background:#f4f4f4;padding:.8rem;overflow:auto}</style></head>\n<body>\n" + body + "</body></html>\n")


def _load_table(p: Path) -> list[list]:
    ext = p.suffix.lower()
    if ext in (".csv", ".tsv"):
        return list(csv.reader(io.StringIO(read_text(p), newline=""), delimiter="\t" if ext == ".tsv" else ","))
    if ext == ".xlsx":
        return next(iter(_xlsx_sheets(p).values()), [])
    try:
        data = json.loads(read_text(p))
    except ValueError as e:
        raise ToolError(f"{p.name} is not valid JSON: {e}")
    data = [data] if isinstance(data, dict) else data
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        head = list(dict.fromkeys(k for r in data for k in r))
        return [head] + [[r.get(k, "") for k in head] for r in data]
    if isinstance(data, list) and all(isinstance(r, list) for r in data):
        return data
    raise ToolError("The JSON must be a list of objects or a list of rows to become a table.")


def _cell(v):
    return v if isinstance(v, (int, float, str, bool)) or v is None else json.dumps(v, ensure_ascii=False, default=str)


def save_table(rows: list[list], dst: Path, sheets: dict | None = None):
    """Write rows (first row = header) as .csv/.tsv/.json/.xlsx. `sheets` ({name: rows}) is for multi-sheet xlsx."""
    ext = dst.suffix.lower()
    if ext in (".csv", ".tsv"):
        with dst.open("w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f, delimiter="\t" if ext == ".tsv" else ",").writerows([["" if c is None else _cell(c) for c in r] for r in rows])
    elif ext == ".json":
        head = [str(h) for h in rows[0]] if rows else []
        dst.write_text(json.dumps([dict(zip(head, r)) for r in rows[1:]], indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    else:
        openpyxl = need("openpyxl")
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for name, data in (sheets or {"Sheet1": rows}).items():
            ws = wb.create_sheet(re.sub(r"[\\/*?:\[\]]", " ", str(name))[:31] or "Sheet")
            for r in data:
                ws.append([_cell(c) for c in r])
            for c in ws[1] if data else []:
                c.font = openpyxl.styles.Font(bold=True)
        wb.save(str(dst))


@tool("file_convert", "Convert a file to another format: csv/tsv/json/xlsx between each other, Markdown to html, text or Markdown "
      "to pdf, images between png/jpg/webp/bmp/gif/tiff/ico/pdf (optionally resized), and pdf/docx/pptx/xlsx to txt. "
      "The original is kept; the new file's path is returned.",
      {"source": S, "to": {**S, "description": "target format, e.g. 'json', 'xlsx', 'pdf', 'webp'"},
       "destination": {**S, "description": "output file or folder; default is next to the source"},
       "width": {**I, "description": "images: resize to this width (height follows if omitted)"}, "height": I},
      ["source", "to"], category="files", risk="write")
def file_convert(ctx, source: str, to: str, destination: str = "", width: int = 0, height: int = 0):
    pol = _policy()
    src = safe_path(source, exists=True, pol=pol)
    if not src.is_file():
        raise ToolError(f"{src} is not a file.")
    ext, s = "." + to.lower().strip().lstrip("."), src.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
        raise ToolError(f"'{to}' is not a file format; use e.g. 'json', 'xlsx', 'pdf'.")
    dst = safe_path(destination, pol=pol) if destination else src.with_suffix(ext)
    if dst.is_dir():
        dst = dst / (src.stem + ext)
    dst = unique(dst if dst.suffix.lower() == ext else dst.with_name(dst.name + ext))
    dirs, note = _mkdirs(dst.parent), ""
    if s in _TABLE and ext in _TABLE:
        rows = _load_table(src)
        if s == ".xlsx":
            sheets = _xlsx_sheets(src)
            if len(sheets) > 1:
                note = f"Only the first sheet was converted; these were left out: {', '.join(list(sheets)[1:])}."
        if ext == ".json" and rows and s != ".json":
            head = [str(h) for h in rows[0]]
            if len(set(head)) < len(head) or any(len(r) > len(head) for r in rows[1:]):
                note = (note + " " if note else "") + ("Repeated column names or extra cells beyond the header were dropped "
                                                      "(a json object cannot hold them).")
        save_table(rows, dst)
    elif s in (".md", ".markdown") and ext in (".html", ".htm"):
        dst.write_text(md_to_html(read_text(src), src.stem), encoding="utf-8")
    elif s in _IMG and ext in _IMG | {".pdf"}:
        from PIL import Image
        with Image.open(src) as im:
            if width or height:
                w = width or round(im.width * height / im.height)
                im = im.resize((max(1, w), max(1, height or round(im.height * width / im.width))), Image.LANCZOS)
            if ext in (".jpg", ".jpeg", ".bmp", ".pdf") and im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(dst)
    elif ext == ".pdf" and s not in _IMG and _is_text(src):
        note = write_pdf(_md_blocks(read_text(src), s in (".md", ".markdown")), dst)
    elif ext == ".txt" and s in (".pdf", ".docx", ".pptx", ".xlsx", ".xlsm"):
        dst.write_text(extract(src)[1], encoding="utf-8")
    else:
        raise ToolError(f"Cannot convert {s or 'this file'} to {ext}. Supported: csv/tsv/json/xlsx among themselves, md to html, "
                        "txt/md to pdf, image to image/pdf, pdf/docx/pptx/xlsx to txt.")
    record(ctx, f"Converted {src.name} to {dst}", created=[dst], dirs=dirs)
    return {"created": str(dst), "size": dst.stat().st_size, **({"note": note} if note else {})}


@tool("pdf_merge", "Join several PDFs into one, in the order given.",
      {"sources": A, "destination": {**S, "description": "path of the merged PDF"}},
      ["sources", "destination"], category="files", risk="write")
def pdf_merge(ctx, sources: list, destination: str):
    from pypdf import PdfWriter
    pol = _policy()
    srcs = [safe_path(s, exists=True, pol=pol) for s in _list(sources)]
    if len(srcs) < 2:
        raise ToolError("Give at least two PDFs to merge.")
    dst = safe_path(destination, pol=pol)
    dst = unique(dst if dst.suffix.lower() == ".pdf" else dst.with_name(dst.name + ".pdf"))
    w = PdfWriter()
    try:
        for s in srcs:
            w.append(str(s))
        dirs = _mkdirs(dst.parent)
        w.write(str(dst))
    except Exception as e:
        raise ToolError(f"Could not merge: {e}")
    finally:
        w.close()
    record(ctx, f"Merged {len(srcs)} PDFs into {dst}", created=[dst], dirs=dirs)
    return {"created": str(dst), "pages": len(_pdf_pages(dst))}


@tool("pdf_split", "Split a PDF into several files: one per page range (e.g. '1-3,4,5-'), or one per page when no ranges are given.",
      {"source": S, "ranges": {**S, "description": "e.g. '1-3,7,10-'; empty = every page separately"},
       "destination": {**S, "description": "output folder; default '<name>-pages' next to the PDF"}},
      ["source"], category="files", risk="write")
def pdf_split(ctx, source: str, ranges: str = "", destination: str = ""):
    from pypdf import PdfReader, PdfWriter
    pol = _policy()
    src = safe_path(source, exists=True, pol=pol)
    try:
        reader = PdfReader(str(src))
        total = len(reader.pages)
    except Exception as e:
        raise ToolError(f"Cannot read the PDF {src.name}: {e}")
    spans = []
    for part in [x for x in ranges.replace(" ", "").split(",") if x] or [str(i) for i in range(1, total + 1)]:
        m = re.fullmatch(r"(\d*)(-?)(\d*)", part)
        a = int(m.group(1) or 1) if m else 0
        b = (int(m.group(3)) if m.group(3) else total) if m and m.group(2) else a
        if not m or not 1 <= a <= b <= total:
            raise ToolError(f"Bad page range '{part}'; the PDF has {total} page(s).")
        spans.append((a, b))
    if len(spans) > 500:
        raise ToolError(f"That would create {len(spans)} files; give page ranges instead.")
    out = safe_path(destination, pol=pol) if destination else unique(src.with_name(src.stem + "-pages"))
    dirs, made = _mkdirs(out), []
    for a, b in spans:
        w = PdfWriter()
        for i in range(a - 1, b):
            w.add_page(reader.pages[i])
        f = unique(out / f"{src.stem}_{a}{'' if a == b else f'-{b}'}.pdf")
        w.write(str(f))
        w.close()
        made.append(f)
    record(ctx, f"Split {src.name} into {len(made)} file(s) in {out}", created=made, dirs=dirs)
    return {"folder": str(out), "files": [f.name for f in made]}


# ---------------------------------------------------------------- archives and backups

def _zip(sources: list[Path], dst: Path, top: bool, pol: dict) -> tuple[int, int]:
    count = size = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for s in sources:
            for f in (walk(s, pol=pol) if s.is_dir() else [s]):
                if _norm(f) == _norm(dst):
                    continue
                arc = f.relative_to(s.parent if top or s.is_file() else s)
                try:
                    z.write(f, arc.as_posix())
                except OSError:
                    continue
                count, size = count + 1, size + f.stat().st_size
    return count, size


def _unzip(archive: Path, dest: Path) -> list[Path]:
    """Extract into dest. The whole archive is refused if any entry would land outside dest (zip slip)."""
    try:
        z = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as e:
        raise ToolError(f"{archive.name} is not a readable zip file: {e}")
    with z:
        infos = z.infolist()
        if len(infos) > 50000 or sum(i.file_size for i in infos) > 8 * 1024 ** 3:
            raise ToolError("The archive is too large to extract safely (over 50,000 entries or 8 GB unpacked).")
        base, plan, seen = dest.resolve(), [], set()
        for i in infos:
            name = i.filename.replace("\\", "/")
            target = (base / name).resolve()
            if (name.startswith("/") or re.match(r"[A-Za-z]:", name) or ".." in PurePosixPath(name).parts
                    or (i.external_attr >> 16) & 0o170000 == 0o120000 or not _inside(target, base)
                    or any(bad_name(x) for x in PurePosixPath(name).parts)):
                raise ToolError(f"Refused: the archive contains an unsafe entry ('{i.filename}') that would be written "
                                "outside the destination. Nothing was extracted.")
            if os.path.lexists(target) and not i.is_dir():
                raise ToolError(f"{target} already exists. Nothing was extracted; choose an empty destination.")
            if not i.is_dir():
                if _norm(target) in seen:
                    raise ToolError(f"The archive contains '{i.filename}' twice. Nothing was extracted.")
                seen.add(_norm(target))
            plan.append((i, target))
        made = []
        try:
            for i, target in plan:
                if i.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                made.append(target)
                with z.open(i) as r, target.open("wb") as w:
                    shutil.copyfileobj(r, w)
        except BaseException:
            for t in made:
                t.unlink(missing_ok=True)
            raise
        return made


@tool("archive_create", "Zip files and/or folders into one .zip archive.",
      {"sources": A, "destination": {**S, "description": "the .zip to create; default is next to the first source"}},
      ["sources"], category="files", risk="write", timeout=900)
def archive_create(ctx, sources: list, destination: str = ""):
    pol = _policy()
    srcs = [safe_path(s, exists=True, pol=pol) for s in _list(sources) if s]
    if not srcs:
        raise ToolError("Give at least one file or folder to zip.")
    dst = safe_path(destination, pol=pol) if destination else srcs[0].with_name(srcs[0].stem + ".zip")
    if dst.is_dir():
        dst = dst / (srcs[0].stem + ".zip")
    dst = unique(dst if dst.suffix.lower() == ".zip" else dst.with_name(dst.name + ".zip"))
    dirs = _mkdirs(dst.parent)
    count, size = _zip(srcs, dst, True, pol)
    record(ctx, f"Created archive {dst}", created=[dst], dirs=dirs)
    return {"created": str(dst), "files": count, "bytes_in": size, "bytes_zip": dst.stat().st_size, **_left_out(pol)}


@tool("archive_extract", "Unpack a .zip archive into a folder. Archives that try to write outside that folder are refused.",
      {"archive": S, "destination": {**S, "description": "folder to extract into; default is a new folder named after the archive"}},
      ["archive"], category="files", risk="write", timeout=900)
def archive_extract(ctx, archive: str, destination: str = ""):
    pol = _policy()
    src = safe_path(archive, exists=True, pol=pol)
    dest = safe_path(destination, pol=pol) if destination else unique(src.with_name(src.stem if src.suffix else src.name + "-extracted"))
    dirs = _mkdirs(dest)
    made = _unzip(src, dest)
    record(ctx, f"Extracted {src.name} into {dest}", created=made, dirs=dirs)
    return {"folder": str(dest), "files": len(made)}


@tool("backup_create", "Save a timestamped zip backup of a folder in Sparx's backups. Use before risky changes to a folder.",
      {"folder": S, "name": {**S, "description": "label for the backup; default is the folder's name"}},
      ["folder"], category="files", risk="write", timeout=1800)
def backup_create(ctx, folder: str, name: str = ""):
    pol = _policy()
    src = safe_path(folder, exists=True, pol=pol)
    if not src.is_dir():
        raise ToolError(f"{src} is not a folder.")
    label = re.sub(r"[^\w.-]+", "-", name or src.name).strip("-") or "backup"
    dst = unique(config.path("backups", f"{label}-{_stamp()}.zip"))
    count, size = _zip([src], dst, False, pol)
    bid = db.x("INSERT INTO file_backups (ts, source, file, files, bytes) VALUES (?,?,?,?,?)",
               (db.now(), str(src), dst.name, count, size))
    return {"id": bid, "file": str(dst), "files": count, "bytes": size, **_left_out(pol)}


@tool("backup_list", "List the folder backups Sparx has made, newest first.", category="files")
def backup_list(ctx):
    rows = db.q("SELECT * FROM file_backups ORDER BY id DESC LIMIT 100")
    for r in rows:
        r["exists"] = config.path("backups", r["file"]).is_file()
    return rows or "No backups yet."


@tool("backup_restore", "Restore a backup made by backup_create. By default it is unpacked into a NEW folder next to the original; "
      "replace_original=true swaps it in for the current folder (the current one goes to Sparx's trash, undoable).",
      {"backup": {**S, "description": "id or file name from backup_list"}, "destination": {**S, "description": "new folder to restore into"},
       "replace_original": B}, ["backup"], category="files", risk="write", timeout=1800)
def backup_restore(ctx, backup: str, destination: str = "", replace_original: bool = False):
    row = db.one("SELECT * FROM file_backups WHERE id = ? OR file = ?", (str(backup), str(backup)))
    if not row:
        raise ToolError(f"No backup '{backup}'. See backup_list.")
    file = config.path("backups", row["file"])
    if not file.is_file():
        raise ToolError(f"The backup file {file} is missing.")
    pol, source, moves = _policy(), Path(row["source"]), []
    if replace_original:
        dest = safe_path(source, mutate=True, pol=pol)
        if dest.exists():
            moves = [(dest, _trash(dest))]
    elif destination:
        dest = safe_path(destination, pol=pol)
        if dest.exists() and any(dest.iterdir()):
            raise ToolError(f"{dest} already has files in it. Choose a new folder.")
    else:
        dest = unique(safe_path(source.with_name(f"{source.name}-restored-{_stamp()}"), pol=pol))
    dirs = _mkdirs(dest)
    try:
        made = _unzip(file, dest)
    except BaseException:
        if moves:                       # the current folder was already moved aside; put it back
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(moves[0][1]), str(moves[0][0]))
        raise
    record(ctx, f"Restored backup {row['file']} into {dest}", moves, [dest] if replace_original else made, dirs)
    return {"restored_to": str(dest), "files": len(made), "replaced_original": replace_original}


# ---------------------------------------------------------------- folder watching

WATCH_MAX = 20000
_scan_lock = threading.Lock()


def _snapshot(path: str, recursive, pattern: str) -> dict | None:
    """{relative path: [mtime, size]} for a watched folder, or None while the folder is unreachable."""
    root = Path(path)
    if not root.is_dir():
        return None
    snap = {}
    for f in walk(root, recursive=bool(recursive)):
        if pattern and not fnmatch.fnmatch(f.name.lower(), pattern.lower()):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        snap[str(f.relative_to(root))] = [round(st.st_mtime, 3), st.st_size]
        if len(snap) > WATCH_MAX:
            break
    return snap


def _scan():
    with _scan_lock:
        pol = _policy()
        for w in db.q("SELECT * FROM file_watches"):
            try:
                safe_path(w["path"], pol=pol)          # the allowed roots may have changed since watch_add
                new = _snapshot(w["path"], w["recursive"], w["pattern"])
            except (ToolError, OSError):
                continue
            if new is None:
                continue
            try:
                old = json.loads(w["snapshot"])
            except ValueError:                             # unreadable snapshot: start over from what is there now
                db.x("UPDATE file_watches SET snapshot = ? WHERE id = ?", (json.dumps(new), w["id"]))
                continue
            ts = db.now()
            events = [(k, "new" if k not in old else "changed", v[1]) for k, v in new.items() if old.get(k) != v]
            events += [(k, "removed", None) for k in old if k not in new]
            if events:
                db.many("INSERT INTO file_watch_events (ts, watch_id, folder, path, change, size) VALUES (?,?,?,?,?,?)",
                        [(ts, w["id"], w["path"], str(Path(w["path"]) / k), change, size) for k, change, size in events])
                db.x("UPDATE file_watches SET snapshot = ? WHERE id = ?", (json.dumps(new), w["id"]))
        db.x("DELETE FROM file_watch_events WHERE id <= (SELECT MAX(id) FROM file_watch_events) - 5000")


def watch_poll(consumer: str = "automation") -> list[dict]:
    """Scan every watched folder and return the change events this consumer has not seen yet.

    Each event: {id, ts, watch_id, folder, path, change: new|changed|removed, size}. Consumers keep separate
    cursors, so the notification job and the automation engine both see every event exactly once.
    """
    cur = db.one("SELECT last_id FROM file_watch_cursors WHERE consumer = ?", (consumer,))
    start = cur["last_id"] if cur else db.one("SELECT COALESCE(MAX(id), 0) AS m FROM file_watch_events")["m"]
    _scan()
    events = db.q("SELECT * FROM file_watch_events WHERE id > ? ORDER BY id", (start,))
    db.x("INSERT INTO file_watch_cursors (consumer, last_id) VALUES (?, ?) "
         "ON CONFLICT(consumer) DO UPDATE SET last_id = excluded.last_id", (consumer, events[-1]["id"] if events else start))
    return events


NOTIFY_GAP = 300          # a busy folder gets one notification per five minutes, not one per scan
_held: dict[str, list[dict]] = {}
_last_note: dict[str, float] = {}


@jobs.every(20, "folder_watch", flag="monitors")
def folder_watch(ctx):
    for e in watch_poll("notify"):
        _held.setdefault(e["folder"], []).append(e)
    for folder in list(_held):
        if time.time() - _last_note.get(folder, 0) < NOTIFY_GAP:
            continue
        events = _held.pop(folder)
        _last_note[folder] = time.time()
        lines = [f"{e['change']}: {Path(e['path']).name}" for e in events[:6]]
        more = f"\n… and {len(events) - 6} more" if len(events) > 6 else ""
        jobs.notify(f"{len(events)} change(s) in {Path(folder).name}", "\n".join(lines) + more, kind="monitor",
                    why=f"You asked Sparx to watch {folder}.")


@tool("watch_add", "Watch a folder and get a notification when files in it are added, changed or removed "
      "(checked about every 20 seconds while `sparx daemon` runs).",
      {"path": S, "recursive": {**B, "description": "include subfolders"},
       "pattern": {**S, "description": "only file names matching this, e.g. '*.pdf'"}},
      ["path"], category="files", risk="write")
def watch_add(ctx, path: str, recursive: bool = False, pattern: str = ""):
    p = safe_path(path, exists=True)
    if not p.is_dir():
        raise ToolError(f"{p} is not a folder.")
    snap = _snapshot(str(p), recursive, pattern)
    if len(snap) > WATCH_MAX:
        raise ToolError(f"{p} has more than {WATCH_MAX} files to track; watch a smaller folder or add a pattern.")
    if db.one("SELECT id FROM file_watches WHERE path = ?", (str(p),)):
        db.x("UPDATE file_watches SET recursive = ?, pattern = ?, snapshot = ? WHERE path = ?",
             (int(recursive), pattern, json.dumps(snap), str(p)))
    else:
        db.x("INSERT INTO file_watches (path, recursive, pattern, snapshot, created) VALUES (?,?,?,?,?)",
             (str(p), int(recursive), pattern, json.dumps(snap), db.now()))
    wid = db.one("SELECT id FROM file_watches WHERE path = ?", (str(p),))["id"]
    return {"id": wid, "watching": str(p), "files_now": len(snap)}


@tool("watch_list", "List the folders Sparx is watching.", category="files")
def watch_list(ctx):
    rows = db.q("SELECT id, path, recursive, pattern, created, snapshot FROM file_watches ORDER BY id")
    for r in rows:
        r["files"], r["reachable"] = len(json.loads(r.pop("snapshot"))), Path(r["path"]).is_dir()
    return rows or "No folders are being watched."


@tool("watch_remove", "Stop watching a folder.", {"watch": {**S, "description": "id or path from watch_list"}},
      ["watch"], category="files", risk="write")
def watch_remove(ctx, watch: str):
    n = db.x("DELETE FROM file_watches WHERE id = ? OR path = ?", (str(watch), str(watch)))
    if not n:
        raise ToolError(f"No watch '{watch}'. See watch_list.")
    return "Stopped watching."
