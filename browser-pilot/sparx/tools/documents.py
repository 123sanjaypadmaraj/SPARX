"""Create real documents from structured content: Word, spreadsheets, presentations, PDF, Markdown and HTML."""
import html
import json
import re
from pathlib import Path

from .. import config
from . import S, ToolError, category, tool
from .files import _mkdirs, bad_name, need, record, safe_path, save_table, unique, write_pdf

category("documents", "Create Word, Excel, PowerPoint, PDF, Markdown and HTML files from structured content")

_SECTIONS = {"type": "array", "items": {"type": "object"}, "description":
             "list of sections, each {heading, paragraphs: [text], bullets: [text], table: [[header...], [row...]]}; "
             "every key is optional"}
_PATH = {**S, "description": "output file or folder; default is Sparx's files folder, named after the title"}
_CSS = ("body{font:16px/1.6 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem}"
        "table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:.3rem .6rem;text-align:left}")


def _json(v, what: str):
    """Models sometimes pass a list or object as a JSON string; accept that."""
    if isinstance(v, str) and v.strip()[:1] in ("[", "{"):
        try:
            return json.loads(v)
        except ValueError as e:
            raise ToolError(f"{what} is not valid JSON: {e}")
    return v


def _texts(v) -> list[str]:
    return [str(x) for x in ([v] if isinstance(v, (str, int, float)) else v or []) if str(x).strip()]


def _table(v, what: str = "table") -> list[list]:
    """Rows of equal width, first row the header. Accepts a list of rows or a list of objects."""
    v = _json(v, what) or []
    if not isinstance(v, list):
        raise ToolError(f"{what} must be a list of rows.")
    if v and all(isinstance(r, dict) for r in v):
        head = list(dict.fromkeys(k for r in v for k in r))
        v = [head] + [[r.get(k, "") for k in head] for r in v]
    if not all(isinstance(r, (list, tuple)) for r in v):
        raise ToolError(f"Every row of {what} must be a list of cells (or every row an object).")
    width = max((len(r) for r in v), default=0)
    return [list(r) + [""] * (width - len(r)) for r in v] if width else []


def _sections(sections) -> list[dict]:
    sections = _json(sections, "sections") or []
    if isinstance(sections, dict):
        sections = [sections]
    if not isinstance(sections, list) or not all(isinstance(s, (dict, str)) for s in sections):
        raise ToolError("sections must be a list of objects like {heading, paragraphs, bullets, table}.")
    return [{"heading": str(s.get("heading") or s.get("title") or "").strip(),
             "paragraphs": _texts(s.get("paragraphs") or s.get("text")), "bullets": _texts(s.get("bullets")),
             "table": _table(s.get("table"))}
            for s in ({"paragraphs": x} if isinstance(x, str) else x for x in sections)]


def _out(path: str, title: str, ext: str) -> Path:
    """Where the new document goes. Never overwrites: a taken name becomes 'name (2).ext'."""
    name = re.sub(r'[<>:"/\\|?*\s]+', " ", title).strip(" .")[:80].strip() or "document"
    if bad_name(name + ext):
        name = "_" + name
    if not path:
        return unique(config.path("files", name + ext))
    p = safe_path(path)
    if p.is_dir() or not p.suffix:
        p = safe_path(p / (name + ext))
    elif p.suffix.lower() != ext:
        raise ToolError(f"The path must end in {ext} (or be a folder); got {p.suffix}.")
    return unique(p)


def _done(ctx, p: Path, dirs: list, kind: str, **extra) -> dict:
    record(ctx, f"Created {kind} {p}", created=[p], dirs=dirs)
    return {"path": str(p), "size": p.stat().st_size, **{k: v for k, v in extra.items() if v not in ("", None)}}


def _markdown(title: str, sections: list[dict]) -> str:
    def cell(c) -> str:
        return str(c).replace("|", "\\|").replace("\n", " ")
    out = [f"# {title}", ""]
    for s in sections:
        if s["heading"]:
            out += [f"## {s['heading']}", ""]
        for p in s["paragraphs"]:
            out += [p, ""]
        if s["bullets"]:
            out += [f"- {b}" for b in s["bullets"]] + [""]
        if s["table"]:
            head, *rows = s["table"]
            out += ["| " + " | ".join(map(cell, r)) + " |" for r in (head, ["---"] * len(head), *rows)] + [""]
    return "\n".join(out).rstrip() + "\n"


def _html(title: str, sections: list[dict]) -> str:
    e = html.escape
    out = [f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title>',
           f"<style>{_CSS}</style></head>\n<body>", f"<h1>{e(title)}</h1>"]
    for s in sections:
        out.append("<section>")
        if s["heading"]:
            out.append(f"<h2>{e(s['heading'])}</h2>")
        out += [f"<p>{e(p)}</p>" for p in s["paragraphs"]]
        if s["bullets"]:
            out.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in s["bullets"]) + "</ul>")
        if s["table"]:
            head, *rows = s["table"]
            out.append("<table><thead><tr>" + "".join(f"<th>{e(str(c))}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in rows)
                       + "</tbody></table>")
        out.append("</section>")
    return "\n".join(out) + "\n</body></html>\n"


@tool("doc_create_markdown", "Create a Markdown (.md) file from a title and sections. Use it for notes, READMEs and drafts.",
      {"title": S, "sections": _SECTIONS, "path": _PATH}, ["title"], category="documents", risk="write")
def doc_create_markdown(ctx, title: str, sections=None, path: str = ""):
    text = _markdown(title, _sections(sections))
    p = _out(path, title, ".md")
    dirs = _mkdirs(p.parent)
    p.write_text(text, encoding="utf-8", newline="\n")
    return _done(ctx, p, dirs, "Markdown file")


@tool("doc_create_html", "Create a standalone, styled HTML page from a title and sections. Opens in any browser.",
      {"title": S, "sections": _SECTIONS, "path": _PATH}, ["title"], category="documents", risk="write")
def doc_create_html(ctx, title: str, sections=None, path: str = ""):
    text = _html(title, _sections(sections))
    p = _out(path, title, ".html")
    dirs = _mkdirs(p.parent)
    p.write_text(text, encoding="utf-8", newline="\n")
    return _done(ctx, p, dirs, "HTML page")


@tool("doc_create_word", "Create a Word document (.docx) from a title and sections with headings, paragraphs, bullet lists "
      "and tables. Use it for letters, reports and anything the user will edit in Word.",
      {"title": S, "sections": _SECTIONS, "path": _PATH}, ["title"], category="documents", risk="write")
def doc_create_word(ctx, title: str, sections=None, path: str = ""):
    docx = need("docx", "python-docx")
    secs = _sections(sections)
    p = _out(path, title, ".docx")
    doc = docx.Document()
    doc.core_properties.title = title
    doc.add_heading(title, 0)
    for s in secs:
        if s["heading"]:
            doc.add_heading(s["heading"], 1)
        for para in s["paragraphs"]:
            doc.add_paragraph(para)
        for b in s["bullets"]:
            doc.add_paragraph(b, style="List Bullet")
        if s["table"]:
            t = doc.add_table(rows=len(s["table"]), cols=len(s["table"][0]))
            t.style = "Table Grid"
            for i, row in enumerate(s["table"]):
                for j, c in enumerate(row):
                    run = t.cell(i, j).paragraphs[0].add_run(str(c))
                    run.bold = i == 0
            doc.add_paragraph()
    dirs = _mkdirs(p.parent)
    doc.save(str(p))
    return _done(ctx, p, dirs, "Word document", sections=len(secs))


@tool("doc_create_pdf", "Create a PDF from a title and sections with headings, paragraphs, bullet lists and tables. "
      "Use it for something to print or send as a finished document.",
      {"title": S, "sections": _SECTIONS, "path": _PATH}, ["title"], category="documents", risk="write")
def doc_create_pdf(ctx, title: str, sections=None, path: str = ""):
    need("fpdf", "fpdf2")
    blocks: list = [("h1", title)]
    for s in _sections(sections):
        blocks += [("h2", s["heading"])] if s["heading"] else []
        blocks += [("p", x) for x in s["paragraphs"]] + [("li", x) for x in s["bullets"]]
        blocks += [("table", s["table"])] if s["table"] else []
    p = _out(path, title, ".pdf")
    dirs = _mkdirs(p.parent)
    note = write_pdf(blocks, p)
    return _done(ctx, p, dirs, "PDF", note=note)


@tool("doc_create_spreadsheet", "Create a spreadsheet: .xlsx (one or several sheets, bold header row, cells starting with "
      "'=' are formulas) or .csv (one sheet). The first row of each sheet is the header.",
      {"title": {**S, "description": "used as the file name"},
       "rows": {"type": "array", "items": {"type": "array", "items": {}},
                "description": "rows of cells, header first; or a list of objects with the same keys"},
       "sheets": {"type": "object", "description": "for several sheets: {sheet name: rows}; use instead of rows"},
       "format": {"type": "string", "enum": ["xlsx", "csv"], "description": "default xlsx"}, "path": _PATH},
      ["title"], category="documents", risk="write")
def doc_create_spreadsheet(ctx, title: str, rows=None, sheets=None, format: str = "", path: str = ""):
    sheets = _json(sheets, "sheets")
    if sheets and not isinstance(sheets, dict):
        raise ToolError("sheets must be an object like {sheet name: rows}.")
    book = {str(k): _table(v, f"sheet '{k}'") for k, v in (sheets or {}).items()} or {"Sheet1": _table(rows, "rows")}
    if not any(book.values()):
        raise ToolError("Give rows (header first) or sheets; an empty spreadsheet was not created.")
    given = Path(path).suffix.lower()
    ext = "." + format.lower().lstrip(".") if format else given if given in (".xlsx", ".csv") else ".xlsx"
    if ext not in (".xlsx", ".csv"):
        raise ToolError("format must be xlsx or csv.")
    if ext == ".csv" and len(book) > 1:
        raise ToolError("A csv file holds one sheet; use xlsx for several sheets.")
    if ext == ".xlsx":
        need("openpyxl")
    p = _out(path, title, ext)
    dirs = _mkdirs(p.parent)
    save_table(next(iter(book.values())), p, book)
    return _done(ctx, p, dirs, "spreadsheet", sheets={k: max(len(v) - 1, 0) for k, v in book.items()})


_PER_SLIDE = 8


@tool("doc_create_presentation", "Create a PowerPoint presentation (.pptx): a title slide followed by one slide per item, "
      "each with a title and bullet points. Long bullet lists continue on a following slide.",
      {"title": S, "slides": {"type": "array", "items": {"type": "object"},
                              "description": "list of {title, bullets: [text], notes: speaker notes (optional)}"},
       "subtitle": S, "path": _PATH}, ["title", "slides"], category="documents", risk="write")
def doc_create_presentation(ctx, title: str, slides, subtitle: str = "", path: str = ""):
    pptx = need("pptx", "python-pptx")
    slides = _json(slides, "slides")
    if not isinstance(slides, list) or not slides or not all(isinstance(s, dict) for s in slides):
        raise ToolError("slides must be a non-empty list of objects like {title, bullets}.")
    p = _out(path, title, ".pptx")
    deck = pptx.Presentation()
    first = deck.slides.add_slide(deck.slide_layouts[0])
    first.shapes.title.text = title
    first.placeholders[1].text = subtitle
    for s in slides:
        bullets = _texts(s.get("bullets") or s.get("text"))
        chunks = [bullets[i:i + _PER_SLIDE] for i in range(0, len(bullets), _PER_SLIDE)] or [[]]
        for n, chunk in enumerate(chunks):
            slide = deck.slides.add_slide(deck.slide_layouts[1])
            slide.shapes.title.text = str(s.get("title") or s.get("heading") or "") + (" (cont.)" if n else "")
            frame = slide.placeholders[1].text_frame
            for i, b in enumerate(chunk):
                (frame.paragraphs[0] if i == 0 else frame.add_paragraph()).text = b
            if s.get("notes") and n == 0:
                slide.notes_slide.notes_text_frame.text = str(s["notes"])
    dirs = _mkdirs(p.parent)
    deck.save(str(p))
    return _done(ctx, p, dirs, "presentation", slides=len(deck.slides))
