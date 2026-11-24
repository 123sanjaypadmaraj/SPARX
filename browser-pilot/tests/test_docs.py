"""Keeps docs/SPARX.md in step with the code: every tool, setting, command, environment variable and optional
package the guide names must exist. No network and no model."""
import re
from pathlib import Path

import pytest

from sparx import config, tools

ROOT = Path(__file__).resolve().parent.parent
GUIDE = (ROOT / "docs" / "SPARX.md").read_text(encoding="utf-8")
QUOTED = re.findall(r"`([^`\n]+)`", GUIDE)
SETTING_GROUPS = ("permissions", "tool_permissions", "limits", "retention", "proactive", "voice", "models", "server",
                  "integrations")
IMPORT_NAMES = {"python-docx": "docx", "python-pptx": "pptx", "opencv-python": "cv2", "paho-mqtt": "paho",
                "faster-whisper": "faster_whisper", "edge-tts": "edge_tts", "fpdf2": "fpdf", "pillow": "PIL",
                "pymupdf": "fitz", "beautifulsoup4": "bs4", "scikit-learn": "sklearn", "pyyaml": "yaml"}
PROGRAMS = {"ffmpeg", "tesseract", "ollama"}        # programs, not Python packages

tools.load_all()


def source(*packages) -> str:
    return "\n".join(f.read_text(encoding="utf-8", errors="replace")
                     for pkg in packages for f in sorted((ROOT / pkg).rglob("*.py")))


SPARX = source("sparx")


def default_keys(d=None) -> set:
    d = config.DEFAULTS if d is None else d
    return set(d) | {k for v in d.values() if isinstance(v, dict) for k in default_keys(v)}


def in_a_string(name: str) -> bool:
    """True when the name appears inside a quoted string somewhere in sparx/ (a table, column, setting or status)."""
    return bool(re.search(r"""["'][^"'\n]*\b%s\b[^"'\n]*["']""" % re.escape(name), SPARX))


def lookup(dotted: str):
    cur = config.DEFAULTS
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur


def test_every_snake_case_name_in_the_guide_exists():
    known = default_keys()
    names = {n for q in QUOTED for n in re.findall(r"(?<![\w.\-/])[a-z]+(?:_[a-z0-9]+)+(?![\w/]|\.py)", q)}
    missing = sorted(n for n in names if n not in tools.REGISTRY and n not in known and not in_a_string(n))
    assert not missing, f"docs/SPARX.md names things the code does not have: {missing}"


def test_every_settings_key_in_the_guide_exists():
    missing = []
    for q in QUOTED:
        for key in re.findall(r"(?<![\w.])(?:%s)(?:\.[\w<>*]+)+" % "|".join(SETTING_GROUPS), q):
            parts = [p for p in key.split(".") if p != "*" and not p.startswith("<")]
            try:
                lookup(".".join(parts))
            except KeyError:
                if len(parts) < 2 or not in_a_string(parts[-1]):
                    missing.append(key)
    assert not missing, f"docs/SPARX.md names settings the code does not read: {sorted(set(missing))}"


def test_every_sparx_command_in_the_guide_exists():
    cli = (ROOT / "sparx" / "cli.py").read_text(encoding="utf-8")
    commands = set(re.findall(r'add_parser\("([\w-]+)"', cli))
    used = {m for q in QUOTED for m in re.findall(r"(?:^|[\s(])sparx ([a-z]+(?:-[a-z]+)*)\b", q)}
    missing = sorted(used - commands)
    assert commands and not missing, f"docs/SPARX.md shows `sparx` subcommands that do not exist: {missing}"


def test_every_environment_variable_in_the_guide_is_read_somewhere():
    code = SPARX + source("browserpilot")
    names = {n for q in QUOTED for n in re.findall(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", q)
             if n.startswith("SPARX_") or n.endswith(("_KEY", "_TOKEN", "_PASSWORD"))}
    missing = sorted(n for n in names if n not in code)
    assert names and not missing, f"docs/SPARX.md names environment variables nothing reads: {missing}"


def optional_packages() -> list[str]:
    section = GUIDE.split("## Optional dependencies", 1)
    assert len(section) == 2, "the guide has no '## Optional dependencies' section"
    cells = re.findall(r"^\|([^|\n]+)\|", section[1].split("\n## ", 1)[0], re.M)
    return [p.strip() for cell in cells for p in re.findall(r"`([^`]+)`", cell)]


def test_the_optional_package_table_matches_sparx_doctor():
    """`sparx doctor` and the guide must name the same optional packages."""
    cli = (ROOT / "sparx" / "cli.py").read_text(encoding="utf-8")
    block = re.search(r"optional = \{(.*?)\}", cli, re.S)
    assert block, "sparx doctor no longer builds its `optional` table the way this test reads it"
    checked = set(re.findall(r'"(\w+)":', block[1])) | {"fpdf"}
    checked.discard("playwright")       # the guide lists it under the required packages, where pyproject puts it
    listed = {IMPORT_NAMES.get(p.lower(), p.replace("-", "_")) for p in optional_packages()} - PROGRAMS
    assert listed == checked, f"only in the guide: {sorted(listed - checked)}; only in sparx doctor: {sorted(checked - listed)}"


def test_every_optional_package_in_the_guide_is_used():
    packages = optional_packages()
    missing = []
    for pkg in packages:
        name = IMPORT_NAMES.get(pkg.lower(), pkg.replace("-", "_"))
        used = in_a_string(pkg) if pkg.lower() in PROGRAMS else \
            re.search(r"^\s*(?:import|from)\s+%s\b" % re.escape(name), SPARX, re.M) or \
            re.search(r"""(?:import_module|find_spec|importorskip)\(\s*["']%s\b""" % re.escape(name), SPARX) or in_a_string(name)
        if not used:
            missing.append(pkg)
    assert packages and not missing, f"docs/SPARX.md lists optional packages the code never imports: {missing}"


def test_every_default_the_guide_states_matches_the_code():
    wrong = []
    for key, stated in re.findall(r"`([a-z_]+(?:\.[a-z_]+)+)`\s*\(default\s+`?([^`\s,)]+)`?", GUIDE):
        try:
            actual = lookup(key)
        except KeyError:
            continue                    # a key with no default is checked by the settings test above
        if str(actual).lower() != stated.lower() and str(actual) != stated.strip("\"'"):
            wrong.append(f"{key}: guide says {stated}, code has {actual!r}")
    assert not wrong, "docs/SPARX.md states defaults that differ from config.DEFAULTS: " + "; ".join(wrong)


@pytest.mark.parametrize("claim", ["limits.pending_minutes", "limits.automation_runs_per_hour", "job_status",
                                   "pending_approve", "automation_enable"])
def test_the_checks_above_actually_see_the_guide(claim):
    """Guards against the regexes silently matching nothing: these names are in the guide and in the code."""
    assert f"`{claim}`" in GUIDE


def test_every_subcommand_is_documented():
    cli = (ROOT / "sparx" / "cli.py").read_text(encoding="utf-8")
    commands = set(re.findall(r'add_parser\("([\w-]+)"', cli))
    missing = sorted(c for c in commands if f"`sparx {c}" not in GUIDE)
    assert commands and not missing, f"docs/SPARX.md does not mention these subcommands: {missing}"
