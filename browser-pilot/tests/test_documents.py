import csv
import json
import sys

import pytest

from sparx import config, undo
from sparx.tools import files

from conftest import run_tool

SECTIONS = [
    {"heading": "Summary", "paragraphs": ["Revenue grew 12% <fast>.", "Costs were flat."], "bullets": ["Hire two", "Ship v2"]},
    {"heading": "Numbers", "table": [["Quarter", "Revenue"], ["Q1", 100], ["Q2", 112, "extra"]]},
    "A bare string becomes a paragraph.",
]


@pytest.fixture(autouse=True)
def area(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(files, "_home", lambda: home)
    work = tmp_path / "work"
    work.mkdir()
    config.set_key("allowed_roots", [str(work)])
    return work


async def make(tool, ctx, **args):
    out = await run_tool(tool, ctx, **args)
    assert out.startswith("{"), out
    r = json.loads(out)
    r["file"] = files.Path(r["path"])
    assert r["file"].is_file() and r["size"] == r["file"].stat().st_size > 0
    return r


async def test_markdown_content_and_default_location(ctx, home):
    r = await make("doc_create_markdown", ctx, title="Q2: Report?", sections=SECTIONS)
    assert r["file"].parent == home / "files" and r["file"].name == "Q2 Report.md"
    text = r["file"].read_text(encoding="utf-8")
    assert text.startswith("# Q2: Report?\n\n## Summary\n\nRevenue grew 12% <fast>.\n\nCosts were flat.\n\n- Hire two\n- Ship v2\n")
    assert "| Quarter | Revenue |  |\n| --- | --- | --- |\n| Q1 | 100 |  |\n| Q2 | 112 | extra |" in text
    assert text.rstrip().endswith("A bare string becomes a paragraph.")
    assert ctx.asked == []                                    # risk write: no approval needed


async def test_html_is_escaped_and_structured(ctx, area):
    r = await make("doc_create_html", ctx, title="A & B", sections=SECTIONS, path=str(area))
    assert r["file"] == area / "A & B.html"
    text = r["file"].read_text(encoding="utf-8")
    assert "<title>A &amp; B</title>" in text and "<h2>Summary</h2>" in text and "12% &lt;fast&gt;." in text
    assert "<li>Hire two</li>" in text and "<th>Quarter</th>" in text and "<td>112</td>" in text and "<fast>" not in text


async def test_word_document_has_headings_bullets_and_table(ctx, area):
    docx = pytest.importorskip("docx")
    r = await make("doc_create_word", ctx, title="Report", sections=SECTIONS, path=str(area / "out" / "r.docx"))
    doc = docx.Document(str(r["file"]))
    paras = [(p.style.name, p.text) for p in doc.paragraphs if p.text]
    assert paras[:3] == [("Title", "Report"), ("Heading 1", "Summary"), ("Normal", "Revenue grew 12% <fast>.")]
    assert ("List Bullet", "Ship v2") in paras and doc.core_properties.title == "Report"
    t = doc.tables[0]
    assert [[c.text for c in row.cells] for row in t.rows] == [["Quarter", "Revenue", ""], ["Q1", "100", ""], ["Q2", "112", "extra"]]
    assert t.cell(0, 0).paragraphs[0].runs[0].bold and not t.cell(1, 0).paragraphs[0].runs[0].bold
    assert "Revenue grew 12%" in files.extract(r["file"])[1]  # file_read can read it back


async def test_pdf_text_can_be_read_back(ctx, area):
    pytest.importorskip("fpdf")
    pypdf = pytest.importorskip("pypdf")
    r = await make("doc_create_pdf", ctx, title="Plan", sections=SECTIONS, path=str(area / "plan.pdf"))
    text = "\n".join(p.extract_text() for p in pypdf.PdfReader(str(r["file"])).pages)
    for want in ("Plan", "Summary", "Revenue grew 12%", "Hire two", "Quarter", "112"):
        assert want in text, want


async def test_spreadsheet_xlsx_sheets_formulas_and_objects(ctx, area):
    openpyxl = pytest.importorskip("openpyxl")
    r = await make("doc_create_spreadsheet", ctx, title="Budget", path=str(area), sheets={
        "Costs": [["Item", "Amount"], ["Rent", 1200], ["Food", 300.5], ["Total", "=SUM(B2:B3)"]],
        "People": [{"name": "Asha", "age": 30}, {"name": "Ravi", "city": "Mysuru"}]})
    assert r["file"].name == "Budget.xlsx" and r["sheets"] == {"Costs": 3, "People": 2}
    wb = openpyxl.load_workbook(str(r["file"]))
    assert wb.sheetnames == ["Costs", "People"]
    ws = wb["Costs"]
    assert ws["A1"].font.bold and ws["B2"].value == 1200 and ws["B4"].value == "=SUM(B2:B3)" and ws["B4"].data_type == "f"
    assert [[c.value for c in row] for row in wb["People"].iter_rows()] == [
        ["name", "age", "city"], ["Asha", 30, None], ["Ravi", None, "Mysuru"]]


async def test_spreadsheet_csv_and_validation(ctx, area):
    r = await make("doc_create_spreadsheet", ctx, title="List", rows=[["a", "b"], [1, "x,y"]], path=str(area / "list.csv"))
    with r["file"].open(encoding="utf-8-sig", newline="") as f:
        assert list(csv.reader(f)) == [["a", "b"], ["1", "x,y"]]
    r = await make("doc_create_spreadsheet", ctx, title="J", rows='[["a"],[1]]', format="csv")   # rows as a JSON string
    assert r["file"].suffix == ".csv"
    for bad in ({"rows": []}, {"rows": [["a"]], "format": "ods"}, {"rows": ["a", "b"]},
                {"sheets": {"A": [["x"]], "B": [["y"]]}, "format": "csv"}, {"sheets": [1]},
                {"rows": [["a"]], "path": str(area / "x.csv"), "format": "xlsx"}):
        assert (await run_tool("doc_create_spreadsheet", ctx, title="Bad", **bad)).startswith("Error:"), bad
    assert sorted(p.name for p in area.iterdir()) == ["list.csv"]


async def test_presentation_slides_notes_and_overflow(ctx, area):
    pptx = pytest.importorskip("pptx")
    slides = [{"title": "Why", "bullets": ["Faster", "Cheaper"], "notes": "Open with the demo"},
              {"title": "Long", "bullets": [f"point {i}" for i in range(10)]}, {"title": "Thanks"}]
    r = await make("doc_create_presentation", ctx, title="Pitch", subtitle="October", slides=slides, path=str(area))
    deck = pptx.Presentation(str(r["file"]))
    titles = [s.shapes.title.text for s in deck.slides]
    assert titles == ["Pitch", "Why", "Long", "Long (cont.)", "Thanks"] and r["slides"] == 5
    assert deck.slides[0].placeholders[1].text == "October"
    assert [p.text for p in deck.slides[1].placeholders[1].text_frame.paragraphs] == ["Faster", "Cheaper"]
    assert len(deck.slides[2].placeholders[1].text_frame.paragraphs) == 8
    assert [p.text for p in deck.slides[3].placeholders[1].text_frame.paragraphs] == ["point 8", "point 9"]
    assert deck.slides[1].notes_slide.notes_text_frame.text == "Open with the demo"
    assert (await run_tool("doc_create_presentation", ctx, title="X", slides=["just text"])).startswith("Error:")


async def test_paths_must_be_safe_and_have_the_right_extension(ctx, area, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for tool, ext in (("doc_create_markdown", "md"), ("doc_create_html", "html"), ("doc_create_word", "docx"),
                      ("doc_create_pdf", "pdf")):
        assert "Refused" in await run_tool(tool, ctx, title="T", path=str(outside / f"t.{ext}"))
        assert "must end in" in await run_tool(tool, ctx, title="T", path=str(area / "t.exe"))
    assert "Refused" in await run_tool("doc_create_spreadsheet", ctx, title="T", rows=[["a"]], path=str(outside))
    assert "Refused" in await run_tool("doc_create_presentation", ctx, title="T", slides=[{"title": "a"}], path=str(outside))
    assert "Refused" in await run_tool("doc_create_markdown", ctx, title="T", path=str(area / ".ssh"))
    assert list(outside.iterdir()) == [] and list(area.iterdir()) == []


async def test_never_overwrites_and_undo_removes_the_file(ctx, area):
    a = await make("doc_create_markdown", ctx, title="Notes", path=str(area / "sub"))
    b = await make("doc_create_markdown", ctx, title="Notes", path=str(area / "sub"))
    assert (a["file"].name, b["file"].name) == ("Notes.md", "Notes (2).md")
    assert undo.run().startswith("Undone")
    assert a["file"].exists() and not b["file"].exists()
    undo.run()
    assert not (area / "sub").exists()                        # the folder it created goes too


async def test_bad_sections_are_explained(ctx):
    assert "sections must be" in await run_tool("doc_create_markdown", ctx, title="T", sections=[1, 2])
    assert "not valid JSON" in await run_tool("doc_create_html", ctx, title="T", sections="[{broken")
    assert "must be a list of cells" in await run_tool("doc_create_word", ctx, title="T", sections=[{"table": ["a", "b"]}])


async def test_missing_optional_package_says_how_to_install(ctx, monkeypatch):
    for mod in ("docx", "pptx", "openpyxl", "fpdf"):
        monkeypatch.setitem(sys.modules, mod, None)
    assert "pip install python-docx" in await run_tool("doc_create_word", ctx, title="T")
    assert "pip install python-pptx" in await run_tool("doc_create_presentation", ctx, title="T", slides=[{"title": "a"}])
    assert "pip install openpyxl" in await run_tool("doc_create_spreadsheet", ctx, title="T", rows=[["a"]])
    assert "pip install fpdf2" in await run_tool("doc_create_pdf", ctx, title="T")
    assert (await run_tool("doc_create_spreadsheet", ctx, title="T", rows=[["a"]], format="csv")).startswith("{")


# ---------------------------------------------------------------- Unicode PDFs (fpdf2 + Nirmala UI)

HINDI = "नमस्ते दुनिया, यह एक परीक्षण है। क्षत्रिय विद्यार्थी"
KANNADA = "ನಮಸ್ಕಾರ ಜಗತ್ತು, ಇದು ಒಂದು ಪರೀಕ್ಷೆ. ಕನ್ನಡ ಸ್ವಾಗತ"
INDIC = [{"heading": "हिंदी ಕನ್ನಡ", "paragraphs": [HINDI, KANNADA], "bullets": ["पहला", "ಎರಡು"],
          "table": [["नाम", "ಊರು", "Age"], ["आशा", "ಊಟ", 30], ["Ravi", "Mysuru"]]}]


def _fpdf2(font: str = ""):
    """Skip unless fpdf2 (2.x) is installed and, when asked for, the Windows font file exists."""
    import os
    fpdf = pytest.importorskip("fpdf")
    if int(str(getattr(fpdf, "__version__", "1")).split(".")[0]) < 2:
        pytest.skip("classic fpdf 1.x: no Unicode fonts, no tables")
    if font and not (files.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts" / font).is_file():
        pytest.skip(f"{font} is not installed")
    return pytest.importorskip("pypdf")


def _pdf_text(pypdf, path) -> str:
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(str(path)).pages)


async def test_pdf_keeps_hindi_and_kannada(ctx, area):
    pypdf = _fpdf2("Nirmala.ttc")
    r = await make("doc_create_pdf", ctx, title="भारत ಕರ್ನಾಟಕ", sections=INDIC, path=str(area / "indic.pdf"))
    text = _pdf_text(pypdf, r["file"])
    lost = {c for c in "भारत ಕರ್ನಾಟಕ" + HINDI + KANNADA + "पहला ಎರಡು" if not c.isspace()} - set(text)
    assert not lost and "?" not in text and "\ufffd" not in text, lost
    for cell in ("नाम", "ಊರು", "Age", "आशा", "ಊಟ", "30", "Ravi", "Mysuru"):          # every table cell is extractable
        assert cell in text, cell
    note = r.get("note", "")
    assert not note or ("shaping" in note and "replaced" not in note and "could not be loaded" not in note), note


async def test_pdf_without_shaping_says_so_and_keeps_exact_text(ctx, area, monkeypatch):
    pypdf = _fpdf2("Nirmala.ttc")
    monkeypatch.setitem(sys.modules, "uharfbuzz", None)                              # as if it were not installed
    r = await make("doc_create_pdf", ctx, title="Plan", sections=INDIC, path=str(area / "plain.pdf"))
    text = _pdf_text(pypdf, r["file"])
    assert HINDI in text and KANNADA in text and "?" not in text
    assert "shaping is unavailable" in r["note"] and "uharfbuzz" in r["note"] and "replaced" not in r["note"]
    latin = await make("doc_create_pdf", ctx, title="Plan", sections=SECTIONS, path=str(area / "latin.pdf"))
    assert not latin.get("note")                                                     # Latin text needs no shaping: no note


def test_pdf_notes_characters_no_font_covers(area):
    pypdf = _fpdf2("arial.ttf")
    note = files.write_pdf([("p", "before \U0001F600 after"), ("table", [["a", "\U0001F600"]])], area / "gap.pdf")
    text = _pdf_text(pypdf, area / "gap.pdf")
    assert "before ? after" in text and "replaced with '?'" in note and "1 different character" in note


def test_pdf_without_any_unicode_font_says_what_is_missing(area, monkeypatch, tmp_path):
    pypdf = _fpdf2()
    monkeypatch.setenv("SystemRoot", str(tmp_path / "nowhere"))
    assert files.write_pdf([("h1", "Plain"), ("p", "ascii only"), ("table", [["a", "b"]])], area / "core.pdf") == ""
    note = files.write_pdf([("p", "Namaste " + HINDI)], area / "lossy.pdf")
    assert "No usable Unicode font" in note and "Nirmala.ttc" in note and "fpdf2" not in note
    assert "Namaste ?" in _pdf_text(pypdf, area / "lossy.pdf")
    (tmp_path / "nowhere" / "Fonts").mkdir(parents=True)
    (tmp_path / "nowhere" / "Fonts" / "arial.ttf").write_bytes(b"not a font")
    assert "arial.ttf could not be loaded" in files.write_pdf([("p", "ascii only")], area / "broken.pdf")


def test_doctor_lists_fpdf_and_flags_the_old_one(monkeypatch):
    import types
    from sparx import cli
    pytest.importorskip("fpdf")
    line = next(ln for ln in cli.doctor().splitlines() if " fpdf " in ln)
    assert "PDF writing" in line and "missing" not in line
    monkeypatch.setitem(sys.modules, "fpdf", types.SimpleNamespace(__version__="1.7.2"))
    assert "old: no Unicode, no tables" in next(ln for ln in cli.doctor().splitlines() if " fpdf " in ln)
    monkeypatch.setitem(sys.modules, "fpdf", None)
    assert "missing fpdf" in cli.doctor()


async def test_reserved_device_name_as_title_still_creates_a_real_file(ctx, area):
    for t in ("CON", "nul"):
        r = await make("doc_create_markdown", ctx, title=t, path=str(area))
        assert r["file"].read_text(encoding="utf-8").startswith("# ")


async def test_pdf_without_fpdf_creates_nothing(ctx, area, monkeypatch):
    def missing(module, pip=""):
        raise files.ToolError(f"Install {pip or module} to use this: pip install {pip or module}")
    monkeypatch.setattr("sparx.tools.documents.need", missing)
    out = await run_tool("doc_create_pdf", ctx, title="T", path=str(area / "newdir" / "t.pdf"))
    assert "Install fpdf2" in out and not (area / "newdir").exists()
