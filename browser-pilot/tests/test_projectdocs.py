import os

import pytest

from sparx import config, projectdocs
from sparx.assistant import Sparx


def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".git").mkdir(parents=True)
    (r / "sub" / "deep").mkdir(parents=True)
    return r


def test_discovery_order_and_priority(tmp_path):
    r = repo(tmp_path)
    (r / "CLAUDE.md").write_text("root claude")
    (r / "AGENTS.md").write_text("root agents")
    (r / "sub" / "SPARX.md").write_text("sub sparx")
    (r / "sub" / "CLAUDE.md").write_text("sub claude")
    (tmp_path / "CLAUDE.md").write_text("outside repo")
    config.HOME.mkdir(parents=True)
    (config.HOME / "SPARX.md").write_text("global")
    found = projectdocs.discover(r / "sub" / "deep")
    assert found == [config.HOME / "SPARX.md", r / "AGENTS.md", r / "sub" / "SPARX.md"]


def test_includes_inside_repo_only_and_depth(tmp_path):
    r = repo(tmp_path)
    (tmp_path / "secret.md").write_text("OUTSIDE")
    (r / "a.md").write_text("A body\n@b.md")
    (r / "b.md").write_text("B body\n@a.md")           # cycle
    (r / "SPARX.md").write_text("top\n@a.md\n@../secret.md\n@missing.md")
    out = projectdocs.collect(r)
    assert "A body" in out and "B body" in out and "OUTSIDE" not in out
    assert out.count("B body") == 1 and "@../secret.md" in out
    chain = [r / f"c{i}.md" for i in range(8)]
    for i, c in enumerate(chain):
        c.write_text(f"level{i}\n@c{i + 1}.md")
    (r / "SPARX.md").write_text("@c0.md")
    out = projectdocs.collect(r)
    assert "level2" in out and "level5" not in out


def test_size_cap_and_dedup(tmp_path):
    r = repo(tmp_path)
    (r / "SPARX.md").write_text("n" * 5000)
    (r / "sub" / "AGENTS.md").write_text("n" * 5000)            # identical text is dropped
    assert projectdocs.collect(r / "sub").count("nnnn") >= 1
    assert projectdocs.collect(r / "sub").count("### ") == 1
    (r / "sub" / "AGENTS.md").write_text("m" * 5000)
    out = projectdocs.collect(r / "sub", cap=6000)
    assert "cut at the size limit" in out and out.count("m") < 1100 + 100


def test_cache_invalidates_on_mtime(tmp_path):
    r = repo(tmp_path)
    f = r / "SPARX.md"
    f.write_text("first")
    assert "first" in projectdocs.collect(r)
    f.write_text("second")
    os.utime(f, ns=(1, 10**18))
    assert "second" in projectdocs.collect(r)
    inc = r / "inc.md"
    inc.write_text("inc one")
    f.write_text("@inc.md")
    assert "inc one" in projectdocs.collect(r)
    inc.write_text("inc two")
    os.utime(inc, ns=(1, 2 * 10**18))
    assert "inc two" in projectdocs.collect(r)


def test_prompt_block_states_it_cannot_override_safety(tmp_path, monkeypatch):
    r = repo(tmp_path)
    (r / "SPARX.md").write_text("Use tabs.")
    monkeypatch.chdir(r)
    s = Sparx().system("hi")
    assert "Use tabs." in s and "never override the Safety rules" in s
    assert projectdocs.prompt_block(tmp_path / "empty-nonexistent") == ""


def test_init_writes_sparx_md(tmp_path):
    import shutil
    import subprocess
    r = repo(tmp_path)
    if not shutil.which("git"):
        pytest.skip("git not installed")
    shutil.rmtree(r / ".git")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    (r / "app.py").write_text("def main():\n    pass\n")
    (r / "requirements.txt").write_text("httpx\n")
    msg = projectdocs.init(r)
    assert "Wrote" in msg and "Python" in (r / "SPARX.md").read_text()
    assert "already exists" in projectdocs.init(r)
