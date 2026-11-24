import json
import os
import zipfile

import pytest

from sparx import config, jobs, undo
from sparx.tools import ToolError, files

from conftest import run_tool


@pytest.fixture(autouse=True)
def area(tmp_path, monkeypatch):
    """A fake home folder plus one allowed work folder; the real home is never reachable."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(files, "_home", lambda: home)
    work = tmp_path / "work"
    work.mkdir()
    config.set_key("allowed_roots", [str(work)])
    return work


def make(folder, name, text="hello"):
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ---- path safety

def test_safe_path_allows_home_sparx_and_allowed_roots(area, tmp_path):
    assert files.safe_path(area / "a.txt") == (area / "a.txt").resolve()
    assert files.safe_path("notes.txt") == (tmp_path / "user" / "notes.txt").resolve()
    assert files.safe_path("~/x.txt") == (tmp_path / "user" / "x.txt").resolve()
    assert files.safe_path(config.path("files", "x.txt"))


@pytest.mark.parametrize("bad", [
    lambda t: t / "elsewhere" / "a.txt",                                   # outside every root
    lambda t: t / "work" / ".." / "elsewhere" / "a.txt",                   # traversal out of a root
    lambda t: os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "win.ini"),
    lambda t: t / "user" / ".ssh" / "id_rsa",
    lambda t: t / "work" / ".env",
    lambda t: t / "work" / "server.pem",
    lambda t: t / "user" / "AppData" / "Roaming" / "SomeApp" / "data.db",  # another app's data
    lambda t: t / "work" / "Google" / "Chrome" / "User Data" / "Default" / "History",
    lambda t: config.path("settings.json"),                                # Sparx's own control files
    lambda t: config.path("sparx.db"),
])
def test_safe_path_refuses(bad, tmp_path):
    with pytest.raises(ToolError, match="Refused"):
        files.safe_path(bad(tmp_path))


def test_system_folder_refused_even_when_listed(area):
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    config.set_key("allowed_roots", [str(area), windir])
    with pytest.raises(ToolError, match="system folder"):
        files.safe_path(os.path.join(windir, "notepad.exe"))


def test_symlink_out_of_the_safe_area_is_refused(area, tmp_path):
    secret = make(tmp_path / "elsewhere", "secret.txt")
    link = area / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks need extra privileges on this machine")
    with pytest.raises(ToolError, match="Refused"):
        files.safe_path(link)
    assert link not in list(files.walk(area))


def test_roots_cannot_be_moved_or_deleted(area, tmp_path):
    for p in (area, tmp_path / "user", tmp_path / "user" / "Documents", config.HOME):
        with pytest.raises(ToolError, match="protected"):
            files.safe_path(p, mutate=True)


async def test_tools_refuse_through_the_registry(ctx, tmp_path):
    outside = make(tmp_path / "elsewhere", "a.txt")
    assert "Refused" in await run_tool("file_read", ctx, path=str(outside))
    assert "Refused" in await run_tool("file_write", ctx, path=str(tmp_path / "elsewhere" / "b.txt"), content="x")
    assert "Refused" in await run_tool("file_delete", ctx, path=str(outside))
    assert outside.exists() and not (tmp_path / "elsewhere" / "b.txt").exists()


# ---- read, write, find, search

async def test_write_read_and_undo(ctx, area):
    p = make(area, "note.txt", "first")
    await run_tool("file_write", ctx, path=str(p), content="second")
    out = await run_tool("file_read", ctx, path=str(p))
    assert "second" in out and out.startswith("<untrusted")
    assert undo.run().startswith("Undone") and p.read_text(encoding="utf-8") == "first"
    files.file_write(ctx, str(area / "new" / "n.txt"), "x")
    undo.run()
    assert not (area / "new").exists()


def test_append_and_undo(ctx, area):
    p = make(area, "log.txt", "a")
    files.file_write(ctx, str(p), "b", append=True)
    assert p.read_text(encoding="utf-8") == "ab"
    undo.run()
    assert p.read_text(encoding="utf-8") == "a"


def test_read_refuses_binary_and_pages_long_text(ctx, area):
    (area / "blob.bin").write_bytes(b"\0\1\2" * 50)
    with pytest.raises(ToolError, match="binary"):
        files.file_read(ctx, str(area / "blob.bin"))
    make(area, "long.txt", "x" * 100)
    out = files.file_read(ctx, str(area / "long.txt"), max_chars=40, offset=30)
    assert len(out["content"]) == 40 and out["more"] and out["chars"] == 100


def test_find_by_name_extension_size_date_and_content(ctx, area):
    make(area, "Invoice March.txt", "total 500 rupees")
    make(area, "sub/invoice-april.md", "total 900")
    make(area, "photo.txt", "nothing")
    old = make(area, "old invoice.txt", "total 1")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    names = lambda r: sorted(os.path.basename(f["path"]) for f in r["files"])
    assert names(files.file_find(ctx, str(area), name="invoice")) == ["Invoice March.txt", "invoice-april.md", "old invoice.txt"]
    assert names(files.file_find(ctx, str(area), name="inv*", extensions=["md"])) == ["invoice-april.md"]
    assert names(files.file_find(ctx, str(area), name="invoice", modified_after="7 days ago")) == ["Invoice March.txt", "invoice-april.md"]
    assert names(files.file_find(ctx, str(area), modified_before="2005-01-01")) == ["old invoice.txt"]
    assert names(files.file_find(ctx, str(area), contains="RUPEES")) == ["Invoice March.txt"]
    assert names(files.file_find(ctx, str(area), min_size="10", max_size="1kb", name="march")) == ["Invoice March.txt"]
    with pytest.raises(ToolError):
        files.file_find(ctx, str(area), modified_after="soonish")


def test_find_skips_credentials(ctx, area):
    make(area, ".ssh/config", "invoice")
    make(area, ".env", "invoice")
    assert files.file_find(ctx, str(area), contains="invoice")["found"] == 0


def test_search_text_lines_and_pdf_pages(ctx, area):
    make(area, "a.py", "import os\nprint('needle here')\n")
    make(area, "notes.md", "one\ntwo\nNEEDLE again\n")
    files.write_pdf([("p", "first page")], area / "doc.pdf")
    files.file_convert(ctx, str(make(area, "src.txt", "alpha\nthe needle is in a pdf\n")), "pdf")
    hits = files.file_search_text(ctx, "needle", str(area))["matches"]
    where = {(os.path.basename(h["path"]), h["where"]) for h in hits}
    assert {("a.py", "line 2"), ("notes.md", "line 3"), ("src.txt", "line 2"), ("src.pdf", "page 1")} <= where
    assert files.file_search_text(ctx, r"need\w+ again", str(area), regex=True)["matches"][0]["where"] == "line 3"
    with pytest.raises(ToolError, match="regular expression"):
        files.file_search_text(ctx, "(", str(area), regex=True)


def test_info_list_tree(ctx, area):
    make(area, "a.txt", "abc")
    make(area, "sub/b.txt")
    make(area, ".hidden")
    info = files.file_info(ctx, str(area / "a.txt"))
    assert info["size"] == 3 and len(info["sha256"]) == 64 and info["type"] == "text/plain"
    assert files.file_info(ctx, str(area))["files"] == 3
    assert [e["name"] for e in files.folder_list(ctx, str(area))["entries"]] == ["sub", "a.txt"]
    tree = files.folder_tree(ctx, str(area))
    assert "  sub/" in tree and "    b.txt" in tree


# ---- move, copy, rename, organise, delete

def test_move_and_undo(ctx, area):
    src = make(area, "a.txt")
    out = files.file_move(ctx, str(src), str(area / "deep" / "er" / "b.txt"))
    assert not src.exists() and os.path.isfile(out["to"])
    assert undo.run().startswith("Undone")
    assert src.exists() and not (area / "deep").exists()


def test_move_into_folder_and_no_overwrite(ctx, area):
    src, other = make(area, "a.txt"), make(area, "dir/a.txt", "keep")
    with pytest.raises(ToolError, match="already exists"):
        files.file_move(ctx, str(src), str(area / "dir"))
    assert other.read_text(encoding="utf-8") == "keep" and src.exists()
    (area / "dir2").mkdir()
    assert files.file_move(ctx, str(src), str(area / "dir2"))["to"].endswith("a.txt")


def test_copy_and_undo(ctx, area):
    make(area, "proj/a.txt")
    make(area, "proj/.env", "SECRET=1")
    files.file_copy(ctx, str(area / "proj"), str(area / "copy"))
    assert (area / "copy" / "a.txt").exists() and not (area / "copy" / ".env").exists()
    undo.run()
    assert not (area / "copy").exists() and (area / "proj" / "a.txt").exists()


def test_rename_single_keeps_extension(ctx, area):
    src = make(area, "report.pdf")
    files.file_rename(ctx, str(src), new_name="final")
    assert (area / "final.pdf").exists()
    undo.run()
    assert src.exists()
    with pytest.raises(ToolError, match="valid file name"):
        files.file_rename(ctx, str(src), new_name="a/b.pdf")


def test_rename_batch_template_find_replace_and_undo(ctx, area):
    for n in ("IMG_b.jpg", "IMG_a.jpg", "keep.txt"):
        make(area, n)
    plan = files.file_rename(ctx, str(area), match="*.jpg", template="trip-{n:02}", dry_run=True)
    assert plan["renamed"] == 0 and plan["plan"][0] == {"from": "IMG_a.jpg", "to": "trip-01.jpg"}
    assert (area / "IMG_a.jpg").exists()
    files.file_rename(ctx, str(area), match="*.jpg", template="trip-{n:02}")
    assert sorted(p.name for p in area.iterdir()) == ["keep.txt", "trip-01.jpg", "trip-02.jpg"]
    files.file_rename(ctx, str(area), find=r"trip-(\d+)", replace=r"goa_\1", regex=True)
    assert (area / "goa_02.jpg").exists()
    undo.run()
    undo.run()
    assert (area / "IMG_b.jpg").exists()
    with pytest.raises(ToolError, match="collide"):
        files.file_rename(ctx, str(area), match="*.jpg", template="same")


def test_organize_is_dry_run_by_default_then_applies_and_undoes(ctx, area):
    for n in ("a.jpg", "b.pdf", "c.xyz", "d.py"):
        make(area, n)
    make(area, "sub/untouched.jpg")
    plan = files.folder_organize(ctx, str(area))
    assert plan["dry_run"] and plan["folders"] == {"Images": 1, "PDFs": 1, "Other": 1, "Code": 1}
    assert (area / "a.jpg").exists() and not (area / "Images").exists()
    done = files.folder_organize(ctx, str(area), dry_run=False)
    assert not done["dry_run"] and (area / "Images" / "a.jpg").exists() and (area / "Other" / "c.xyz").exists()
    assert (area / "sub" / "untouched.jpg").exists()
    undo.run()
    assert (area / "a.jpg").exists() and not (area / "Images").exists()
    assert set(files.folder_organize(ctx, str(area), by="extension")["folders"]) == {"jpg", "pdf", "xyz", "py"}
    assert len(files.folder_organize(ctx, str(area), by="date")["folders"]) == 1


async def test_delete_goes_to_trash_asks_first_and_undoes(ctx, area):
    src = make(area, "gone.txt", "precious")
    out = json.loads(await run_tool("file_delete", ctx, path=str(src)))
    assert ctx.asked == ["file_delete"] and not src.exists()
    assert files._inside(out["trash"], config.path("trash", mkdir=False)) and os.path.isfile(out["trash"])
    assert "Undone" in await run_tool("undo", ctx)
    assert src.read_text(encoding="utf-8") == "precious"


def test_undo_refuses_when_the_place_is_taken(ctx, area):
    src = make(area, "a.txt", "old")
    files.file_delete(ctx, str(src))
    make(area, "a.txt", "newer")
    with pytest.raises(ToolError, match="something else"):
        undo.run()
    assert src.read_text(encoding="utf-8") == "newer"


# ---- convert

def test_convert_csv_json_xlsx_roundtrip(ctx, area):
    src = make(area, "people.csv", "name,age\nAsha,31\nRavi,28\n")
    j = files.file_convert(ctx, str(src), "json")["created"]
    assert json.loads(open(j, encoding="utf-8").read()) == [{"name": "Asha", "age": "31"}, {"name": "Ravi", "age": "28"}]
    back = files.file_convert(ctx, j, "csv", str(area / "back.csv"))["created"]
    assert open(back, encoding="utf-8-sig").read().splitlines() == ["name,age", "Asha,31", "Ravi,28"]
    pytest.importorskip("openpyxl")
    x = files.file_convert(ctx, str(src), "xlsx")["created"]
    assert "Asha\t31" in files.file_read(ctx, x)["content"]
    assert files.file_convert(ctx, x, "json", str(area / "x.json"))["created"].endswith("x.json")


def test_convert_markdown_text_pdf_and_images(ctx, area):
    from PIL import Image
    md = make(area, "doc.md", "# Title\n\nSome **bold** text.\n\n- one\n- two\n")
    html = open(files.file_convert(ctx, str(md), "html")["created"], encoding="utf-8").read()
    assert "<h1>Title</h1>" in html and "<li>one</li>" in html
    pdf = files.file_convert(ctx, str(md), "pdf")["created"]
    txt = open(files.file_convert(ctx, pdf, "txt")["created"], encoding="utf-8").read()
    assert "Title" in txt and "- one" in txt and "bold" in txt
    Image.new("RGBA", (40, 20), "red").save(area / "pic.png")
    out = files.file_convert(ctx, str(area / "pic.png"), "jpg", width=20)["created"]
    with Image.open(out) as im:
        assert im.size == (20, 10) and im.format == "JPEG"
    assert files.file_convert(ctx, str(area / "pic.png"), "png")["created"].endswith("pic (2).png")
    with pytest.raises(ToolError, match="Cannot convert"):
        files.file_convert(ctx, str(md), "mp3")


def test_pdf_merge_and_split(ctx, area):
    for n in ("a", "b", "c"):
        files.write_pdf([("p", f"page {n}")], area / f"{n}.pdf")
    merged = files.pdf_merge(ctx, [str(area / f"{n}.pdf") for n in "abc"], str(area / "all"))
    assert merged["pages"] == 3 and merged["created"].endswith("all.pdf")
    out = files.pdf_split(ctx, merged["created"], "1-2,3")
    assert out["files"] == ["all_1-2.pdf", "all_3.pdf"]
    assert files.pdf_split(ctx, merged["created"])["files"] == ["all_1.pdf", "all_2.pdf", "all_3.pdf"]
    with pytest.raises(ToolError, match="Bad page range"):
        files.pdf_split(ctx, merged["created"], "2-9")


# ---- archives and backups

def test_archive_roundtrip(ctx, area):
    make(area, "proj/a.txt", "A")
    make(area, "proj/sub/b.txt", "B")
    make(area, "proj/.env", "SECRET")
    z = files.archive_create(ctx, [str(area / "proj")])
    assert z["files"] == 2 and z["created"].endswith("proj.zip")
    out = files.archive_extract(ctx, z["created"], str(area / "out"))
    assert (area / "out" / "proj" / "sub" / "b.txt").read_text(encoding="utf-8") == "B" and out["files"] == 2
    with pytest.raises(ToolError, match="already exists"):
        files.archive_extract(ctx, z["created"], str(area / "out"))


@pytest.mark.parametrize("evil", ["../escaped.txt", "a/../../escaped.txt", "/abs.txt", "C:/abs.txt", "..\\escaped.txt"])
def test_zip_slip_is_refused(ctx, area, evil):
    bad = area / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("ok.txt", "fine")
        z.writestr(evil, "pwned")
    with pytest.raises(ToolError, match="unsafe entry"):
        files.archive_extract(ctx, str(bad), str(area / "out"))
    assert not (area / "escaped.txt").exists() and not (area / "out" / "ok.txt").exists()


def test_backup_create_list_restore(ctx, area):
    make(area, "proj/a.txt", "v1")
    b = files.backup_create(ctx, str(area / "proj"))
    assert files._inside(b["file"], config.path("backups", mkdir=False)) and b["files"] == 1
    assert files.backup_list(ctx)[0]["exists"]
    (area / "proj" / "a.txt").write_text("v2", encoding="utf-8")
    new = files.backup_restore(ctx, str(b["id"]))
    assert new["restored_to"] != str(area / "proj") and "restored" in new["restored_to"]
    assert open(os.path.join(new["restored_to"], "a.txt")).read() == "v1"
    assert (area / "proj" / "a.txt").read_text(encoding="utf-8") == "v2"
    files.backup_restore(ctx, str(b["id"]), replace_original=True)
    assert (area / "proj" / "a.txt").read_text(encoding="utf-8") == "v1"
    undo.run()
    assert (area / "proj" / "a.txt").read_text(encoding="utf-8") == "v2"
    with pytest.raises(ToolError, match="No backup"):
        files.backup_restore(ctx, "999")


# ---- watching

def test_watch_poll_reports_new_changed_removed(ctx, area):
    keep, gone = make(area, "keep.txt", "1"), make(area, "gone.txt")
    w = files.watch_add(ctx, str(area))
    assert w["files_now"] == 2 and files.watch_poll() == []
    make(area, "new.txt")
    keep.write_text("changed content", encoding="utf-8")
    gone.unlink()
    events = {(os.path.basename(e["path"]), e["change"]) for e in files.watch_poll()}
    assert events == {("new.txt", "new"), ("keep.txt", "changed"), ("gone.txt", "removed")}
    assert files.watch_poll() == []
    assert len(files.watch_poll("someone-else")) == 0     # a new consumer starts from now, not from history


def test_watch_job_notifies_and_consumers_are_independent(ctx, area):
    files.watch_add(ctx, str(area), pattern="*.pdf")
    files.watch_poll()
    files.folder_watch(ctx)
    make(area, "report.pdf")
    make(area, "ignored.txt")
    files.folder_watch(ctx)
    note = jobs.unread()[0]
    assert "new: report.pdf" in note["body"] and "ignored" not in note["body"] and note["why"]
    assert [os.path.basename(e["path"]) for e in files.watch_poll()] == ["report.pdf"]
    assert jobs.JOBS["folder_watch"].flag == "monitors"


def test_watch_list_and_remove(ctx, area):
    w = files.watch_add(ctx, str(area), recursive=True)
    assert files.watch_list(ctx)[0]["path"] == str(area)
    assert files.watch_remove(ctx, str(w["id"])) == "Stopped watching."
    assert files.watch_list(ctx) == "No folders are being watched."
    with pytest.raises(ToolError):
        files.watch_add(ctx, str(area / "missing"))


# ---- audit: Windows name tricks, rollbacks, watch robustness

@pytest.mark.parametrize("bad", ["id_rsa.", "id_rsa ", ".env:stream", "notes.txt:hidden", "CON", "nul.txt", "sub/COM1.log", "aux"])
def test_safe_path_refuses_windows_name_tricks(bad):
    with pytest.raises(ToolError, match="Refused"):
        files.safe_path(bad)


def test_file_write_failure_keeps_the_original(ctx, area):
    p = make(area, "keep.txt", "original")
    with pytest.raises(UnicodeEncodeError):
        files.file_write(ctx, str(p), "bad \ud800 text")
    assert p.read_text(encoding="utf-8") == "original"
    assert not any(config.path("trash", mkdir=False).rglob("keep.txt")) or p.exists()


def test_file_copy_does_not_follow_a_junction_out_of_the_roots(ctx, area, tmp_path):
    import subprocess
    secret = make(tmp_path / "elsewhere", "secret.txt", "TOP SECRET")
    src = area / "proj"
    src.mkdir()
    make(src, "a.txt")
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(src / "jn"), str(secret.parent)], capture_output=True)
    if r.returncode:
        pytest.skip("cannot create a junction here")
    out = files.file_copy(ctx, str(src), str(area / "copy"))
    assert (area / "copy" / "a.txt").exists() and not (area / "copy" / "jn").exists()
    assert out["to"].endswith("copy")


def test_batch_rename_that_fails_midway_is_rolled_back(ctx, area, monkeypatch):
    for n in "abc":
        make(area, f"{n}.txt")
    real, calls = os.rename, []

    def flaky(a, b):
        calls.append(a)
        if len(calls) == 3:
            raise OSError("locked")
        return real(a, b)
    monkeypatch.setattr(files.os, "rename", flaky)
    with pytest.raises(ToolError, match="rolled back|Nothing"):
        files.file_rename(ctx, str(area), find=".txt", replace=".md")
    assert sorted(p.name for p in area.iterdir()) == ["a.txt", "b.txt", "c.txt"]


@pytest.mark.parametrize("new", ["x.", "CON", "nul.txt"])
def test_rename_refuses_names_windows_would_change(ctx, area, new):
    p = make(area, "a.txt")
    with pytest.raises(ToolError, match="not a valid file name"):
        files.file_rename(ctx, str(p), new_name=new)
    assert p.exists()


def test_organize_that_fails_midway_is_rolled_back(ctx, area, monkeypatch):
    make(area, "a.pdf"), make(area, "b.png"), make(area, "c.txt")
    real, calls = files.shutil.move, []

    def flaky(a, b):
        calls.append(a)
        if len(calls) == 3:
            raise OSError("disk full")
        return real(a, b)
    monkeypatch.setattr(files.shutil, "move", flaky)
    with pytest.raises(ToolError, match="rolled back"):
        files.folder_organize(ctx, str(area), dry_run=False)
    monkeypatch.undo()
    assert sorted(p.name for p in area.iterdir()) == ["a.pdf", "b.png", "c.txt"]


@pytest.mark.parametrize("evil", ["a.txt:stream", "dir/CON", "dir/nul.txt", "trail."])
def test_zip_entries_with_windows_tricks_are_refused(ctx, area, evil):
    bad = area / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr(evil, "x")
    with pytest.raises(ToolError, match="unsafe entry"):
        files.archive_extract(ctx, str(bad), str(area / "out"))


def test_zip_with_duplicate_entries_is_refused(ctx, area):
    import warnings
    bad = area / "dup.zip"
    with zipfile.ZipFile(bad, "w") as z, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        z.writestr("a.txt", "first")
        z.writestr("a.txt", "second")
    with pytest.raises(ToolError, match="twice|duplicate"):
        files.archive_extract(ctx, str(bad), str(area / "out"))


def test_failed_extraction_leaves_nothing_behind(ctx, area, monkeypatch):
    z = area / "ok.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("one.txt", "1")
        f.writestr("two.txt", "2")
    real, calls = files.shutil.copyfileobj, []

    def flaky(a, b, *rest):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(a, b, *rest)
    monkeypatch.setattr(files.shutil, "copyfileobj", flaky)
    with pytest.raises(OSError):
        files.archive_extract(ctx, str(z), str(area / "out"))
    assert not list((area / "out").rglob("*.txt"))


def test_restore_with_replace_original_puts_the_folder_back_on_failure(ctx, area):
    make(area, "proj/a.txt", "v1")
    b = files.backup_create(ctx, str(area / "proj"))
    with zipfile.ZipFile(b["file"], "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(ToolError, match="unsafe entry"):
        files.backup_restore(ctx, str(b["id"]), replace_original=True)
    assert (area / "proj" / "a.txt").read_text(encoding="utf-8") == "v1"


def test_file_read_and_find_outputs_are_bounded(ctx, area):
    p = make(area, "big.txt", "x" * 600_000)
    assert len(files.file_read(ctx, str(p), max_chars=10 ** 9)["content"]) <= 200_000
    assert files.file_read(ctx, str(p), max_chars=5, offset=-3)["offset"] == 0
    for i in range(600):
        make(area, f"many/f{i}.txt")
    assert len(files.file_find(ctx, str(area / "many"), limit=10 ** 6)["files"]) <= 500
    assert len(files.file_search_text(ctx, "x", str(area / "many"), limit=10 ** 6)["matches"]) <= 500


@pytest.mark.parametrize("t", ["file_find", "folder_list", "folder_tree"])
def test_listing_tools_that_return_file_names_are_untrusted(t):
    from sparx import tools
    tools.load_all()
    assert tools.REGISTRY[t].untrusted


def test_convert_rejects_a_path_like_target_format(ctx, area):
    p = make(area, "a.csv", "a,b\n1,2\n")
    with pytest.raises(ToolError):
        files.file_convert(ctx, str(p), "../../x")


def test_convert_says_when_it_drops_content(ctx, area):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["a"])
    wb.create_sheet("Two").append(["b"])
    wb.save(area / "book.xlsx")
    assert "Two" in files.file_convert(ctx, str(area / "book.xlsx"), "csv").get("note", "")
    make(area, "ragged.csv", "a,b\n1,2,3\n")
    assert "extra" in files.file_convert(ctx, str(area / "ragged.csv"), "json").get("note", "")


def test_one_broken_watch_does_not_stop_the_others(ctx, area, tmp_path):
    other = tmp_path / "work" / "second"
    other.mkdir()
    files.watch_add(ctx, str(area / "second"))
    files.watch_add(ctx, str(area))
    files.watch_poll()
    from sparx import db
    db.x("UPDATE file_watches SET snapshot = 'not json' WHERE path = ?", (str(other),))
    make(area, "new.txt")
    names = [os.path.basename(e["path"]) for e in files.watch_poll()]
    assert "new.txt" in names
    assert files.watch_poll() == []        # and it recovered rather than failing on every scan


def test_watch_outside_the_allowed_roots_is_not_scanned(ctx, area, tmp_path):
    from sparx import db
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    db.x("INSERT INTO file_watches (path, recursive, pattern, snapshot, created) VALUES (?,?,?,?,?)",
         (str(outside), 0, "", "{}", db.now()))
    files.watch_poll()
    make(outside, "leak.txt")
    assert files.watch_poll() == []


def test_folder_watch_does_not_notify_every_cycle_for_a_busy_folder(ctx, area):
    files.watch_add(ctx, str(area))
    files.watch_poll()
    files.folder_watch(ctx)
    before = len(jobs.unread())
    for i in range(4):
        make(area, f"log{i}.txt")
        files.folder_watch(ctx)
    assert len(jobs.unread()) - before <= 1
