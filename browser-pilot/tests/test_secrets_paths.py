"""Credential files are never opened by a tool: one check (security.secret_path) behind every path gate.

Decisions pinned here: `.env`, `.env.<anything>` and `<name>.env` are refused, except the placeholder files
`.env.example`, `.env.sample` and `.env.template`; `*.key` is refused (that includes Keynote decks); Sparx's own
`.env`, `auth.json` and `secrets/` are refused by exact location, so an `auth.json` elsewhere is an ordinary file.
"""
import json
import os
import zipfile
from pathlib import Path

import pytest

from sparx import auth, config, db, security, voice
from sparx.tools import coding, files

from conftest import run_tool

SECRET = "hunter2-TOPSECRET-9f3a"
REFUSED = "does not open"          # the phrase every refusal carries


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    """A fake home folder plus one allowed work folder (the same layout tests/test_files.py uses)."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(files, "_home", lambda: home)
    w = tmp_path / "work"
    w.mkdir()
    config.set_key("allowed_roots", [str(w)])
    return w


def make(folder, name, text=f"API_KEY={SECRET}\n"):
    p = Path(folder) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def project(work):
    """A folder with ordinary files and exactly one credentials file."""
    d = work / "proj"
    make(d, "readme.md", "the launch plan mentions API_KEY settings\n")
    make(d, "app.py", "API_KEY = load()\n")
    make(d, "prod.env")
    return d


# what must be refused: (path as created, path as asked for)
NAMES = [
    (".env", ".env"), (".env", ".ENV"), (".env", ".env."), (".env", ".env "), (".env", ".env::$DATA"),
    (".env.local", ".env.local"), (".env.production", ".Env.Production"), ("prod.env", "prod.env"),
    (".npmrc", ".npmrc"), (".netrc", ".netrc"), (".git-credentials", ".git-credentials"), (".pypirc", ".pypirc"),
    ("server.pem", "server.pem"), ("server.pem", "SERVER.PEM"), ("server.pem", "server.pem."), ("tls.key", "tls.key"),
    ("cert.pfx", "cert.pfx"), ("id_rsa", "id_rsa"), (".ssh/id_ed25519", ".ssh/id_ed25519"), (".ssh/config", ".SSH/config"),
    (".aws/credentials", ".aws/credentials"), (".aws/credentials", ".aws./credentials"),
    ("Google/Chrome/User Data/Default/Login Data", "google/chrome/user data/Default/Login Data"),
    ("backup/Login Data", "backup/Login Data"),
]
ORDINARY = ["environment.md", "keynote.pdf", "my.env.example", ".env.example", ".env.sample", "keys.txt", "id_rsa.pub",
            "monkey.md", "password-policy.md", "envelope.txt", "auth.json", "pem.txt", "npmrc-notes.md", "ssh/notes.md"]


# ---- the check itself

@pytest.mark.parametrize("made,asked", NAMES)
def test_secret_path_names(work, made, asked):
    make(work, made)
    assert security.secret_path(work / asked)
    assert security.secret_path(str(work / asked))


@pytest.mark.parametrize("name", ORDINARY)
def test_ordinary_names_are_not_secrets(work, name):
    assert security.secret_path(make(work, name, "plain")) is None
    assert security.secret_path(work / "nothing-here" / name) is None


def test_sparxs_own_files_by_exact_location(work):
    assert security.secret_path(config.path(".env"))
    assert security.secret_path(config.path("auth.json"))
    assert security.secret_path(str(config.path("AUTH.JSON")) + ".")
    assert security.secret_path(config.path("secrets", "github.bin"))
    assert security.secret_path(config.path("notes", "auth.json")) is None
    assert security.secret_path(config.path("files", "report.md")) is None


@pytest.mark.parametrize("junk", [None, "", "\0", "a\0b", 12, 3.5, b"x", object(), "?" * 40000, "::", "\\\\?\\", [], "~nobody/x"],
                         ids=lambda v: type(v).__name__)
def test_secret_path_never_raises(junk):
    assert security.secret_path(junk) is None or isinstance(security.secret_path(junk), str)


def test_symlink_to_a_secret_is_a_secret(work):
    env = make(work, ".env")
    link = work / "notes.txt"
    try:
        link.symlink_to(env)
    except OSError:
        pytest.skip("symlinks are not permitted for this user")
    assert security.secret_path(link)
    assert security.secret_path(link, resolve=False) is None


# ---- files: read, copy, zip, backup, search

@pytest.mark.parametrize("made,asked", NAMES)
async def test_file_read_refuses(ctx, work, made, asked):
    make(work, made)
    out = await run_tool("file_read", ctx, path=str(work / asked))
    assert out.startswith("Error") and SECRET not in out


@pytest.mark.parametrize("made", [".env", "prod.env", "server.pem", "tls.key", ".npmrc", ".aws/credentials"])
async def test_file_read_says_why_in_plain_words(ctx, work, made):
    out = await run_tool("file_read", ctx, path=str(make(work, made)))
    assert "credentials" in out and REFUSED in out and "yourself" in out and SECRET not in out


@pytest.mark.parametrize("name", [n for n in ORDINARY if not n.endswith(".pdf")])
async def test_file_read_still_reads_ordinary_files(ctx, work, name):
    out = await run_tool("file_read", ctx, path=str(make(work, name, "plain words here")))
    assert "plain words here" in out


async def test_file_copy_refuses_a_secret_and_skips_it_inside_a_folder(ctx, work):
    d = project(work)
    out = await run_tool("file_copy", ctx, source=str(d / "prod.env"), destination=str(work / "copy.txt"))
    assert REFUSED in out and not (work / "copy.txt").exists()
    out = json.loads(await run_tool("file_copy", ctx, source=str(d), destination=str(work / "proj2")))
    assert sorted(os.listdir(work / "proj2")) == ["app.py", "readme.md"]
    assert out["skipped_credentials"].startswith("1 ")


async def test_copy_and_move_cannot_launder_a_secret_into_another_name(ctx, work):
    env = make(work, ".env")
    for tool in ("file_copy", "file_move"):
        assert "Error" in await run_tool(tool, ctx, source=str(env), destination=str(work / "plain.txt"))
    assert env.exists() and not (work / "plain.txt").exists()


async def test_archive_and_backup_skip_secrets_and_say_so(ctx, work):
    d = project(work)
    assert REFUSED in await run_tool("archive_create", ctx, sources=[str(d / "prod.env")])
    out = json.loads(await run_tool("archive_create", ctx, sources=[str(d)], destination=str(work / "p.zip")))
    assert sorted(zipfile.ZipFile(out["created"]).namelist()) == ["proj/app.py", "proj/readme.md"]
    assert out["files"] == 2 and out["skipped_credentials"].startswith("1 ")
    out = json.loads(await run_tool("backup_create", ctx, folder=str(d)))
    assert sorted(zipfile.ZipFile(out["file"]).namelist()) == ["app.py", "readme.md"]
    assert out["skipped_credentials"].startswith("1 ")


async def test_text_search_skips_secrets_and_says_so(ctx, work):
    d = project(work)
    raw = await run_tool("file_search_text", ctx, query="API_KEY", path=str(d))
    assert SECRET not in raw and "prod.env" not in raw and "readme.md" in raw and "app.py" in raw
    assert '"skipped_credentials": "1 ' in raw
    assert REFUSED in await run_tool("file_search_text", ctx, query="API_KEY", path=str(d / "prod.env"))
    assert SECRET not in await run_tool("file_find", ctx, folder=str(d), contains="API_KEY")


async def test_folder_without_secrets_reports_nothing_skipped(ctx, work):
    d = make(work, "clean/a.txt", "hello").parent
    assert "skipped_credentials" not in await run_tool("file_search_text", ctx, query="hello", path=str(d))
    assert "skipped_credentials" not in await run_tool("archive_create", ctx, sources=[str(d)])


# ---- knowledge base

@pytest.mark.parametrize("made,asked", [(".env", ".env"), (".env", ".ENV"), (".env", ".env."), (".env", ".env::$DATA"),
                                        (".npmrc", ".npmrc"), ("logins.json", "Logins.JSON"), ("id_rsa", "id_rsa"),
                                        (".aws/credentials", ".aws/credentials"), (".env.local", ".env.local")])
async def test_kb_add_refuses(ctx, work, made, asked):
    make(work, made)
    out = await run_tool("kb_add", ctx, path=str(work / asked))
    assert out.startswith("Error") and SECRET not in out
    assert SECRET not in await run_tool("kb_search", ctx, query="API_KEY")


async def test_kb_add_says_why_and_other_kb_readers_refuse_too(ctx, work, fake_llm):
    env = make(work, ".env")
    out = await run_tool("kb_add", ctx, path=str(env))
    assert "credentials" in out and REFUSED in out
    for tool, args in (("kb_summarize", {"path": str(env)}), ("kb_extract", {"path": str(env), "what": "keys"}),
                       ("kb_compare", {"path_a": str(env), "path_b": str(make(work, "b.md", "x"))})):
        out = await run_tool(tool, ctx, **args)
        assert REFUSED in out and SECRET not in out
    assert not fake_llm.prompts and not fake_llm.seen


async def test_kb_add_folder_skips_the_secret_and_says_so(ctx, work):
    d = project(work)
    make(d, "logins.json", json.dumps({"password": SECRET}))
    raw = await run_tool("kb_add", ctx, path=str(d))
    assert "readme.md" in raw and "app.py" in raw and "logins.json" not in raw.split("skipped_credentials")[0]
    assert '"skipped_credentials": "2 ' in raw
    assert SECRET not in await run_tool("kb_search", ctx, query="password")
    assert "logins.json" not in await run_tool("kb_list", ctx)


async def test_kb_add_still_indexes_ordinary_files(ctx, work):
    for name in ("environment.md", "auth.json", "keys.txt"):
        assert name in await run_tool("kb_add", ctx, path=str(make(work, name, "plain words here")))
    out = await run_tool("kb_add", ctx, path=str(make(work, "my.env.example", "plain")))
    assert "Unsupported file type" in out and REFUSED not in out        # refused for its type, not as a secret


# ---- coding

@pytest.mark.parametrize("made,asked", [(".env", ".env"), (".env", ".ENV"), (".env", ".env."), (".env", ".env::$DATA"),
                                        ("prod.env", "prod.env"), ("server.pem", "Server.Pem"), ("tls.key", "tls.key"),
                                        (".npmrc", ".npmrc"), ("deploy/.aws/credentials", "deploy/.aws/credentials")])
async def test_code_read_refuses(ctx, work, made, asked):
    make(work, made)
    for tool, arg in (("code_read", "path"), ("code_outline", "file")):
        out = await run_tool(tool, ctx, **{arg: str(work / asked)})
        assert out.startswith("Error") and SECRET not in out


async def test_code_read_says_why_and_reads_ordinary_files(ctx, work):
    out = await run_tool("code_read", ctx, path=str(make(work, ".env")))
    assert "credentials" in out and REFUSED in out
    for name in ("environment.md", ".env.example", "my.env.example"):
        assert "plain words here" in await run_tool("code_read", ctx, path=str(make(work, name, "plain words here")))


async def test_repo_walk_and_search_skip_secrets_and_say_so(ctx, work, monkeypatch):
    d = project(work)
    make(d, "certs/server.pem")
    monkeypatch.setattr(coding.shutil, "which", lambda name: None)       # no git, no rg: the Python walk
    assert coding._files(d) == ["app.py", "readme.md"]
    raw = await run_tool("code_search", ctx, path=str(d), pattern="API_KEY")
    assert SECRET not in raw and "prod.env" not in raw and "app.py:1" in raw and "2 credential" in raw
    raw = await run_tool("repo_map", ctx, path=str(d))
    assert "prod.env" not in raw and "server.pem" not in raw and "2 credential" in raw
    assert REFUSED in await run_tool("code_search", ctx, path=str(d / "prod.env"), pattern="API")


async def test_ripgrep_hits_inside_secrets_are_dropped(ctx, work, monkeypatch):
    d = project(work)
    monkeypatch.setattr(coding.shutil, "which", lambda name: "rg" if name == "rg" else None)
    monkeypatch.setattr(coding, "_run", lambda cmd, *a, **k: (
        0, f"./app.py:1:API_KEY = load()\n./prod.env:1:API_KEY={SECRET}\n.\\certs\\server.pem:1:API_KEY={SECRET}\n", ""))
    raw = await run_tool("code_search", ctx, path=str(d), pattern="API_KEY")
    assert SECRET not in raw and "app.py:1" in raw and "2 credential" in raw


# ---- audio and other voice.resolve_file callers

@pytest.mark.parametrize("made,asked", [(".env", ".env"), (".env", ".ENV"), (".env", ".env."), (".env", ".env::$DATA"),
                                        ("tls.key", "tls.key"), ("server.pem", "server.pem"),
                                        (".ssh/id_rsa", ".ssh/id_rsa"), ("prod.env", "prod.env")])
async def test_audio_tools_refuse(ctx, work, monkeypatch, made, asked):
    heard = []
    monkeypatch.setattr(voice, "load_audio", lambda p: heard.append(p) or [])
    make(work, made)
    for tool, args in (("audio_transcribe", {"path": str(work / asked)}),
                       ("speech_translate", {"source": str(work / asked), "target": "hi"}),
                       ("speaker_identify", {"path": str(work / asked)}),
                       ("image_describe", {"path": str(work / asked)})):
        out = await run_tool(tool, ctx, **args)
        assert "Error" in out and SECRET not in out and "credentials" in out and REFUSED in out, (tool, out)
    assert not heard


def test_resolve_file_still_returns_ordinary_files(work):
    for name in ("environment.md", "keynote.pdf", "my.env.example", "standup.wav"):
        f = make(work, name, "x")
        assert voice.resolve_file(str(f)) == f.resolve()


# ---- Sparx's own .env and auth file

async def test_sparxs_own_env_and_auth_file_are_refused_by_every_gate(ctx, work, monkeypatch):
    monkeypatch.setattr(voice, "load_audio", lambda p: [])
    env = make(config.HOME, ".env", f"SPARX_EMAIL_PASSWORD={SECRET}\n")
    token = auth.ensure_token()
    auth.set_pin("4821")
    auth_file = config.path("auth.json")
    assert auth_file.is_file()
    stored = json.loads(auth_file.read_text(encoding="utf-8"))["token"]
    for f, leak in ((env, SECRET), (auth_file, stored)):
        for tool, args in (("file_read", {"path": str(f)}), ("file_copy", {"source": str(f), "destination": str(work / "x.txt")}),
                           ("archive_create", {"sources": [str(f)]}), ("kb_add", {"path": str(f)}),
                           ("kb_summarize", {"path": str(f)}), ("code_read", {"path": str(f)}),
                           ("audio_transcribe", {"path": str(f)}), ("file_search_text", {"query": "a", "path": str(f)})):
            out = await run_tool(tool, ctx, **args)
            assert "Error" in out and leak not in out, (tool, out)
    assert not (work / "x.txt").exists()
    assert token and auth.check(token)                   # Sparx itself still reads its auth file


async def test_sparx_home_folder_operations_leave_the_secrets_out(ctx, work):
    make(config.HOME, ".env", f"SPARX_EMAIL_PASSWORD={SECRET}\n")
    auth.ensure_token()
    make(config.HOME, "notes/plan.md", "the plan")
    out = json.loads(await run_tool("archive_create", ctx, sources=[str(config.HOME)], destination=str(work / "home.zip")))
    names = zipfile.ZipFile(out["created"]).namelist()
    assert "sparx-home/notes/plan.md" in names and not [n for n in names if n.endswith((".env", "auth.json"))]
    assert "skipped_credentials" in out
    raw = await run_tool("kb_add", ctx, path=str(config.HOME))
    assert "plan.md" in raw and "auth.json" not in raw.split("skipped_credentials")[0]
    assert SECRET not in await run_tool("file_search_text", ctx, query="SPARX_EMAIL", path=str(config.HOME))


def test_sparx_still_loads_its_own_env_and_auth(monkeypatch):
    monkeypatch.delenv("SPARX_TEST_ONLY_SECRET", raising=False)
    make(config.HOME, ".env", f"SPARX_TEST_ONLY_SECRET={SECRET}\n")
    config._load_env()
    assert config.secret("SPARX_TEST_ONLY_SECRET") == SECRET
    monkeypatch.delenv("SPARX_TEST_ONLY_SECRET")
    token = auth.ensure_token()
    auth.set_pin("4821")
    assert auth.check(token) and auth.check_pin("4821") and auth.ensure_token() is None


# ---- gates outside the four shared path checks: attachments and the import tools

async def test_import_and_attachment_tools_refuse_credentials_files(ctx, tmp_path, monkeypatch):
    import smtplib
    from sparx import config
    secret = tmp_path / ".env"
    secret.write_text("date,amount,description\n2026-06-01,10,TOKEN=abc\n", encoding="utf-8")

    def no_smtp(*a, **k):
        raise AssertionError("SMTP was used")
    monkeypatch.setattr(smtplib, "SMTP_SSL", no_smtp)
    monkeypatch.setattr(smtplib, "SMTP", no_smtp)
    monkeypatch.setenv("SPARX_EMAIL_PASSWORD", "pw")
    config.save_settings({"integrations": {"email": {"imap_host": "imap.example.com", "smtp_host": "smtp.example.com",
                                                     "user": "me@example.com"}}})
    calls = (("expense_import", {"path": str(secret)}), ("receipt_add", {"item": "Laptop", "amount": 10, "file": str(secret)}),
             ("activity_import", {"path": str(secret)}), ("calendar_ics", {"action": "import", "path": str(secret)}))
    for name, args in calls:
        out = await run_tool(name, ctx, **args)
        assert "credentials file" in out and "Error:" in out, (name, out)
        assert "TOKEN=abc" not in out, name

    from sparx.tools import ToolError, comms            # no tool takes attachment paths directly; drafts carry them
    with pytest.raises(ToolError, match="credentials file"):
        await comms._send_email(["a@b.com"], "s", "b", attachments=[str(secret)])


# ---- Sparx's own control files (database, settings): not credentials, but not for the knowledge base or audio/vision

CONTROL = ("sparx.db", "sparx.db-wal", "sparx.db-shm", "sparx.db.bak", "settings.json", "daemon.heartbeat")


@pytest.mark.parametrize("name", CONTROL)
async def test_kb_add_refuses_sparx_control_files(ctx, name):
    db.q("SELECT 1")                    # create the live database first; never overwrite it
    p = config.HOME / name
    if not p.exists():
        make(config.HOME, name, "conversation rows and memories")
    out = await run_tool("kb_add", ctx, path=str(p))
    assert "Error:" in out and "control files" in out, out
    assert "kb_docs" not in out and not db.q("SELECT 1 FROM kb_docs")


@pytest.mark.parametrize("spelled", ("SPARX.DB", "settings.json.", "Settings.JSON", "sparx.db::$DATA"))
def test_voice_resolve_file_refuses_control_files_however_spelled(spelled):
    db.q("SELECT 1")                    # the live database file exists in the test home
    make(config.HOME, "settings.json", "{}")
    from sparx.tools import ToolError
    with pytest.raises(ToolError, match="control files"):
        voice.resolve_file(str(config.HOME / spelled))


async def test_kb_add_on_sparx_home_indexes_notes_and_leaves_control_files_out(ctx):
    make(config.HOME, "note.md", "a plain note about the garden\n")
    make(config.HOME, "settings.json", "{}")
    make(config.HOME, "sparx.db-wal", "WALDATA")
    raw = await run_tool("kb_add", ctx, path=str(config.HOME))
    assert "note.md" in raw and "skipped_control_files" in raw
    assert [r["name"] for r in db.q("SELECT name FROM kb_docs")] == ["note.md"]


def test_control_files_elsewhere_are_ordinary(tmp_path):
    other = make(tmp_path / "elsewhere", "settings.json", "{}")
    unrelated = make(tmp_path / "elsewhere", "something.db", "x")
    assert security.control_path(other) is None and security.control_path(unrelated) is None
    assert security.control_path(config.HOME / "sub" / "sparx.db") is None
    assert security.control_path(config.HOME / "STOP") and security.control_path(config.HOME / "stop.")
    assert security.control_path(config.HOME / "sparx.db-wal") and security.control_path(config.HOME / "SPARX.DB.")
    assert security.secret_path(config.HOME / "sparx.db") is None and security.secret_path(config.HOME / "settings.json") is None


def test_sparx_still_opens_its_own_db_and_settings():
    config.save_settings({"name": "x"})
    assert config.settings()["name"] == "x"
    db.q("SELECT 1")


async def test_code_read_refuses_sparxs_own_database_and_settings(ctx):
    config.set_key("personality", "casual")                      # makes sure settings.json and sparx.db exist
    for name in ("settings.json", "sparx.db"):
        out = await run_tool("code_read", ctx, path=str(config.HOME / name))
        assert out.startswith("Error:") and "casual" not in out, (name, out[:200])
