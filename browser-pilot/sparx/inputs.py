"""Chat input shortcuts, applied before a line reaches the model.

    @path/to/file    the file's text is attached to the prompt (also @"path with spaces"); @folder/ lists the folder
    !command         run a shell command yourself; the output is shown and kept in the conversation
    #note            append a bullet to the project's SPARX.md (## note: to the global ~/.sparx/SPARX.md)

Nothing here runs a model or asks for approval: you typed it, so it is yours. Attached files obey the same
protected-path rules as the file tools (no .env, no ssh keys)."""
import os
import re
import subprocess
from pathlib import Path

from . import config, projectdocs, security

MAX_FILE = 20000
MAX_FILES = 8
_MENTION = re.compile(r'(?<![\w@])@(?:"([^"]+)"|([\w./\\~:-][^\s,;)]*))')


def _protected(p: Path) -> bool:
    return p.name.lower() in security.SECRET_FILES or any(part in security.SECRET_DIRS for part in p.parts)


def expand_mentions(line: str, cwd: str | Path | None = None) -> tuple[str, list[str]]:
    """(prompt with attachments appended, notes about what was attached or skipped). Mentions stay in the text."""
    base, notes, blocks, seen = Path(cwd or os.getcwd()), [], [], set()
    for m in _MENTION.finditer(line):
        raw = (m.group(1) or m.group(2)).rstrip(".,:!?")
        p = Path(raw).expanduser()
        p = (p if p.is_absolute() else base / p)
        if not (p.exists() and raw) or p in seen:
            continue
        seen.add(p)
        if len(blocks) >= MAX_FILES:
            notes.append(f"@{raw}: skipped (limit of {MAX_FILES} attachments)")
            continue
        if _protected(p):
            notes.append(f"@{raw}: protected, not attached")
        elif p.is_dir():
            names = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())[:200]
            blocks.append(f'<attached path="{p}">\n' + "\n".join(names) + "\n</attached>")
            notes.append(f"@{raw}: folder listing")
        else:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                notes.append(f"@{raw}: {e.strerror or 'unreadable'}")
                continue
            cut = len(text) > MAX_FILE
            numbered = "\n".join(f"{i}\t{l}" for i, l in enumerate(text[:MAX_FILE].splitlines(), 1))
            blocks.append(security.wrap_untrusted(numbered + ("\n[... file cut]" if cut else ""), f"file:{p}"))
            notes.append(f"@{raw}: attached" + (" (cut)" if cut else ""))
    return (line + "\n\n" + "\n\n".join(blocks) if blocks else line), notes


def run_shell(command: str, cwd: str | Path | None = None, timeout: int = 120) -> str:
    try:
        p = subprocess.run(command, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=str(cwd or os.getcwd()), timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout}s)"
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    out = out if len(out) <= 8000 else out[:8000] + "\n[... output cut]"
    return out + (f"\n(exit {p.returncode})" if p.returncode else "") or "(no output)"


def remember(note: str, cwd: str | Path | None = None) -> str:
    """Append `note` as a bullet to SPARX.md in the project (git root or cwd), or ~/.sparx/SPARX.md for '## note'."""
    glob = note.startswith("#")
    note = note.lstrip("#").strip()
    if not note:
        return "Nothing to remember."
    if glob:
        target = config.path("SPARX.md")
    else:
        cwd = Path(cwd or os.getcwd()).resolve()
        root = projectdocs.git_root(cwd) or cwd
        target = next((root / n for n in projectdocs.NAMES if (root / n).is_file()), root / "SPARX.md")
    old = target.read_text(encoding="utf-8") if target.is_file() else "# Project guidance\n"
    target.write_text(old.rstrip("\n") + f"\n- {note}\n", encoding="utf-8")
    return f"Added to {target}"
