"""File checkpoints: before a file-changing tool runs, the files it names are copied aside, grouped by chat turn.
`/rewind [n]` puts the files of the last n turns back (files a turn created are moved to trash).

    ~/.sparx/checkpoints/<conversation>/<turn>/meta.json + blobs

Only files named in the tool arguments are saved; a shell command's side effects are not tracked, and neither is
anything outside the tools in TOOLS. Rewinding changes files only, not the conversation."""
import json
import shutil
import time
import uuid
from pathlib import Path

from . import config

TOOLS = {"code_edit", "code_write", "code_multi_edit", "notebook_edit", "file_write"}
MAX_BYTES = 5_000_000       # a bigger file is not copied (its turn then cannot be rewound for that file)


def _root(conversation: str) -> Path:
    return config.path("checkpoints", conversation or "none", mkdir=False)


def begin(conversation: str, prompt: str) -> int:
    """Open a checkpoint for the next turn and return its number."""
    root = _root(conversation)
    turn = max([int(p.name) for p in root.iterdir() if p.name.isdigit()] + [0]) + 1 if root.is_dir() else 1
    d = root / f"{turn:04d}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"prompt": prompt[:200], "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                             "files": []}), encoding="utf-8")
    return turn


def _paths(name: str, args: dict) -> list[str]:
    out = [args[k] for k in ("path", "file") if isinstance(args.get(k), str)]
    for e in args.get("edits") or []:
        if isinstance(e, dict) and isinstance(e.get("path"), str):
            out.append(e["path"])
    return out


def snapshot(ctx, name: str, args: dict):
    """Called by the tool registry just before a tool runs. Never raises."""
    turn = ctx.data.get("checkpoint_turn")
    if not turn or name not in TOOLS:
        return
    try:
        d = _root(ctx.session) / f"{turn:04d}"
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        seen = {f["path"] for f in meta["files"]}
        for raw in _paths(name, args):
            p = Path(raw).expanduser().resolve()
            if str(p) in seen or p.is_dir():
                continue
            entry = {"path": str(p), "existed": p.is_file(), "blob": ""}
            if entry["existed"]:
                if p.stat().st_size > MAX_BYTES:
                    continue
                entry["blob"] = uuid.uuid4().hex
                shutil.copy2(p, d / entry["blob"])
            meta["files"].append(entry)
            seen.add(str(p))
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    except Exception:
        pass            # a failed snapshot must never stop the edit the user asked for


def turns(conversation: str) -> list[dict]:
    """Turns that saved at least one file, newest first."""
    root, out = _root(conversation), []
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta["files"]:
            out.append({"turn": int(d.name), **meta})
    return out


def rewind(conversation: str, count: int = 1) -> str:
    """Restore the files of the last `count` turns that changed files. Returns what was done."""
    done, trash = [], config.path("trash", "rewind", mkdir=False)
    for t in turns(conversation)[:max(count, 1)]:
        d = _root(conversation) / f"{t['turn']:04d}"
        for f in t["files"]:
            p = Path(f["path"])
            try:
                if f["existed"]:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(d / f["blob"], p)
                    done.append(f"restored {p}")
                elif p.is_file():
                    trash.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(trash / f"{uuid.uuid4().hex[:8]}-{p.name}"))
                    done.append(f"removed {p} (created in turn {t['turn']}; moved to trash)")
            except OSError as e:
                done.append(f"could not restore {p}: {e}")
        shutil.rmtree(d, ignore_errors=True)
    return "\n".join(done) or "No file changes to rewind in this conversation."
