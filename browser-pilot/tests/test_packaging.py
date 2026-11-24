"""The optional-dependency extras in pyproject.toml must match the guide's "Optional dependencies" table."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRAS = ("docs", "voice", "vision", "home")


def extras() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]


def table_packages() -> set[str]:
    text = (ROOT / "docs" / "SPARX.md").read_text(encoding="utf-8")
    section = text.split("## Optional dependencies", 1)[1].split("\n## ", 1)[0]
    names = set()
    for row in section.splitlines():
        if row.startswith("|") and not row.startswith("| ---") and not row.startswith("| Package"):
            first = row.split("|")[1]
            first = re.sub(r"\(`[^`]*`\)", "", first)
            names |= set(re.findall(r"`([^`]+)`", first))
    return names - {"ffmpeg"}


def test_every_documented_package_is_in_exactly_one_extra():
    e = extras()
    for name in sorted(table_packages()):
        homes = [x for x in EXTRAS if name in e[x]]
        assert len(homes) == 1, f"{name} is in extras {homes}"


def test_every_extra_package_is_documented():
    e = extras()
    listed = {p for x in EXTRAS for p in e[x]}
    assert listed == table_packages(), f"differences: {listed ^ table_packages()}"


def test_all_names_the_four_extras():
    inner = re.search(r"\[(.*)\]", extras()["all"][0]).group(1).split(",")
    assert sorted(inner) == sorted(EXTRAS)


def test_gitignore_keeps_secrets_out():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in lines
