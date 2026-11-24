"""Creative work: images, diagrams, narration, subtitles, simple video edits, scripts, written content, brand kits, web pages.

Everything is saved under Sparx's home and the path is returned. What is not here, because nothing local can do it
honestly: sound-effect or music generation, and video editing beyond what ffmpeg does deterministically.
"""
import base64
import html
import io
import json
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from .. import config, llm, voice
from . import A, I, N, S, ToolError, category, tool

category("creative", "Generate images, diagrams, narration audio, subtitles, video scripts, written content, web pages; video edits")

POLLINATIONS = "https://image.pollinations.ai/prompt/"
MERMAID_KINDS = ("flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram", "erDiagram", "gantt", "pie",
                 "mindmap", "timeline", "journey", "gitGraph", "quadrantChart")
FFMPEG_HELP = "This needs ffmpeg on PATH. Install it with `winget install Gyan.FFmpeg` and open a new terminal."


def _http() -> httpx.Client:
    return httpx.Client(timeout=180, follow_redirects=True)


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "untitled"


def _out(area: str, name: str, ext: str) -> Path:
    return config.path(area, f"{datetime.now():%Y%m%d-%H%M%S}-{slug(name)}-{uuid.uuid4().hex[:4]}{ext}")


def _fenced(text: str, lang: str = "") -> str:
    """The body of the first code fence, or the whole reply when the model did not use one."""
    m = re.search(r"```(?:%s|\w*)[ \t]*\n(.*?)(?:```|\Z)" % re.escape(lang), text, re.S)
    return (m.group(1) if m else text).strip()


# ---- images

def _size(size: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{2,4})\s*[xX×]\s*(\d{2,4})\s*", size or "1024x1024")
    if not m or not all(64 <= int(v) <= 2048 for v in m.groups()):
        raise ToolError("size must look like 1024x1024 (64 to 2048 pixels per side).")
    return int(m.group(1)), int(m.group(2))


def image_backends() -> list[str]:
    """Image generators in the order they are tried: the user's own Stable Diffusion, OpenAI, then the free service."""
    integ = config.settings()["integrations"]
    return ((["stable-diffusion"] if integ.get("sd_url") else []) + (["openai"] if config.secret("OPENAI_API_KEY") else [])
            + (["pollinations"] if integ.get("pollinations", True) else []))


CLOUD_IMAGE = {"openai": "OpenAI", "pollinations": "Pollinations"}


def _generate(backend: str, client: httpx.Client, prompt: str, w: int, h: int) -> bytes:
    if backend == "stable-diffusion":   # AUTOMATIC1111 / Forge web UI started with --api
        r = client.post(config.settings()["integrations"]["sd_url"].rstrip("/") + "/sdapi/v1/txt2img",
                        json={"prompt": prompt, "width": w - w % 8, "height": h - h % 8, "steps": 25})
        r.raise_for_status()
        return base64.b64decode(r.json()["images"][0])
    if backend == "openai":
        shape = "1024x1024" if abs(w - h) < 0.15 * max(w, h) else "1536x1024" if w > h else "1024x1536"
        r = client.post("https://api.openai.com/v1/images/generations",
                        headers={"Authorization": f"Bearer {config.secret('OPENAI_API_KEY')}"},
                        json={"model": "gpt-image-1", "prompt": prompt, "size": shape, "n": 1})
        r.raise_for_status()
        return base64.b64decode(r.json()["data"][0]["b64_json"])
    r = client.get(POLLINATIONS + quote(prompt[:1500], safe=""), params={"width": w, "height": h, "nologo": "true"})
    r.raise_for_status()
    return r.content


@tool("image_generate", "Create an image from a text prompt and save it as a PNG. Uses the user's Stable Diffusion web UI "
      "(integrations.sd_url) or OpenAI (OPENAI_API_KEY) when configured; otherwise the prompt is sent to "
      "Pollinations, a free third-party service, so do not put private details in the prompt in that case.",
      {"prompt": S, "size": {**S, "description": "WIDTHxHEIGHT, default 1024x1024"}},
      ["prompt"], category="creative", risk="write", timeout=300)
def image_generate(ctx, prompt: str, size: str = "1024x1024"):
    from PIL import Image
    w, h = _size(size)
    backends, errors, sent = image_backends(), [], []
    if not backends:
        raise ToolError("No image generator is enabled. Set integrations.sd_url (local Stable Diffusion web UI with "
                        "--api), add OPENAI_API_KEY to ~/.sparx/.env, or allow the free service with "
                        "`sparx config integrations.pollinations true`.")
    with _http() as client:
        for b in backends:
            if b in CLOUD_IMAGE:
                sent.append(CLOUD_IMAGE[b])
            try:
                im = Image.open(io.BytesIO(_generate(b, client, prompt, w, h)))
                im.load()
            except Exception as e:
                errors.append(f"{b}: {type(e).__name__}: {str(e)[:150]}")
                continue
            out = _out("images", prompt, ".png")
            im.save(out, "PNG")
            return {"saved_to": str(out), "size": f"{im.width}x{im.height}", "backend": b,
                    **({"note": f"Made by {CLOUD_IMAGE[b]}, a third-party service that received the prompt."}
                       if b in CLOUD_IMAGE else {}),
                    **({"earlier_failures": errors} if errors else {})}
    raise ToolError("Image generation failed. " + " | ".join(errors)
                    + (f" The prompt was sent to {' and '.join(sent)} (third-party services) and received the prompt." if sent else ""))


# ---- diagrams

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;background:#fafafa;color:#222}}h1{{font-size:1.1rem;font-weight:600}}
.mermaid{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1rem;overflow:auto}}</style></head>
<body><h1>{title}</h1>
<pre class="mermaid">
{source}
</pre>
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";
mermaid.initialize({{ startOnLoad: true, securityLevel: "strict" }});
</script></body></html>
"""


@tool("diagram_create", "Draw a diagram from a description: flowchart, sequence, class, state, ER, gantt, mindmap, "
      "timeline or pie. Saves Mermaid source (.mmd) and an .html page that renders it in a browser (the page loads "
      "the Mermaid library from the internet).",
      {"description": S, "kind": {**S, "description": "flowchart (default), sequence, class, state, er, gantt, mindmap, timeline, pie"}},
      ["description"], category="creative", risk="write", timeout=180)
async def diagram_create(ctx, description: str, kind: str = "flowchart"):
    source = _fenced(await llm.complete(
        f"Write a Mermaid {kind} diagram for the user's description. Output only Mermaid source in one ```mermaid "
        "block. Keep node labels short; put labels containing punctuation in double quotes.", description, role="default"),
        "mermaid")
    first = source.split(None, 1)[0] if source else ""
    if not first.startswith(MERMAID_KINDS):
        raise ToolError("The model did not return Mermaid source (it began with "
                        f"'{first[:30]}'). Try again or describe the diagram more simply.")
    mmd = _out("diagrams", description, ".mmd")
    mmd.write_text(source + "\n", encoding="utf-8")
    page = mmd.with_suffix(".html")
    page.write_text(PAGE.format(title=html.escape(description[:80]), source=html.escape(source)), encoding="utf-8")
    return {"mermaid_file": str(mmd), "html_file": str(page), "mermaid": source,
            "note": "Not rendered or syntax-checked here; open the html file to see it. Opening the page fetches a script "
                    "(Mermaid) from cdn.jsdelivr.net, a third party that can see that you opened it; the diagram source "
                    "itself is saved locally and is not sent anywhere."}


# ---- narration, subtitles, video

def _narrate(text: str, chosen: str | None, name: str) -> dict:
    spoken = voice.speakable(text)
    if not spoken:
        raise ToolError("There is nothing to narrate.")
    f = voice.synthesize(spoken, chosen, _out("narration", name or spoken[:30], ""))
    if not f.is_file() or not f.stat().st_size:
        raise ToolError("The voice produced no audio, so nothing was saved.")
    mp3 = f.suffix == ".mp3"
    return {"saved_to": str(f), "characters": len(spoken),
            "engine": f"edge-tts ({voice.pick_voice(spoken, chosen)})" if mp3 else "pyttsx3 offline voice (edge-tts was unreachable)",
            "note": "The text was sent to Microsoft's online edge-tts service to make this audio." if mp3 else
                    "The text may have been sent to Microsoft's online edge-tts service before this offline fallback was used."}


@tool("tts_narration", "Turn text into a narration audio file (natural edge-tts voice as MP3, which sends the text to Microsoft's "
      "online service; the offline Windows voice as WAV when there is no internet). Use it for voice-overs and audio versions of text; `say` is for speaking now.",
      {"text": S, "voice": {**S, "description": "voice name from voice_list; default follows the text's language"},
       "name": {**S, "description": "short file name, optional"}},
      ["text"], category="creative", risk="write", timeout=600)
def tts_narration(ctx, text: str, voice: str = "", name: str = ""):
    return _narrate(text, voice or None, name)


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(segments: list[dict]) -> str:
    return "\n".join(f"{i}\n{_srt_time(s['start'])} --> {_srt_time(max(s['end'], s['start'] + 0.3))}\n{s['text']}\n"
                     for i, s in enumerate((s for s in segments if s["text"].strip()), 1))


@tool("subtitles_create", "Make an .srt subtitle file for a video or audio file by transcribing its speech. "
      "Needs speech-to-text to be set up; timings are as accurate as the transcription.",
      {"path": S, "language": {**S, "description": "spoken-language hint: en, hi, kn; default auto-detect"}},
      ["path"], category="creative", risk="write", untrusted=True, timeout=1800)
def subtitles_create(ctx, path: str, language: str = ""):
    p = voice.resolve_file(path)
    out = voice.transcribe(voice.load_audio(p), language or None)
    srt = to_srt(out.get("segments") or [])
    if not srt:
        raise ToolError(f"No speech was recognised in {p.name}, so there is nothing to subtitle.")
    f = _out("subtitles", p.stem, ".srt")
    f.write_text(srt, encoding="utf-8")
    cloud = out.get("backend") not in (None, "", "faster-whisper")
    return {"saved_to": str(f), "cues": srt.count(" --> "), "language": out.get("language", ""), "preview": srt[:400],
            **({"note": f"The audio was sent to the {out['backend']} online speech-to-text service."} if cloud else {})}


def _ffmpeg(args: list, out: Path) -> Path:
    exe = voice.ffmpeg()
    if not exe:
        raise ToolError(FFMPEG_HELP)
    try:
        r = subprocess.run([exe, "-y", "-v", "error", *map(str, args), str(out)], capture_output=True, timeout=1500)
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        raise ToolError("ffmpeg timed out after 25 minutes and was stopped; no file was kept.")
    if r.returncode or not out.is_file() or not out.stat().st_size:
        out.unlink(missing_ok=True)
        raise ToolError(f"ffmpeg failed: {r.stderr.decode(errors='replace').strip()[-400:] or 'no output written'}")
    return out


@tool("video_edit", "Simple, exact video edits with ffmpeg: 'trim' (keep start..end seconds), 'concat' (join clips that "
      "share format and resolution), 'extract_audio' (to .m4a), 'add_subtitles' (attach an .srt as a selectable "
      "track). The original files are never changed; the result is a new file. Nothing generative.",
      {"action": {"type": "string", "enum": ["trim", "concat", "extract_audio", "add_subtitles"]},
       "path": {**S, "description": "the video (first clip for concat)"},
       "start": {**N, "description": "trim: seconds"}, "end": {**N, "description": "trim: seconds"},
       "more": {**A, "description": "concat: the following clips, in order"},
       "subtitles": {**S, "description": "add_subtitles: path of the .srt file"}},
      ["action", "path"], category="creative", risk="write", timeout=1800)
def video_edit(ctx, action: str, path: str, start: float = 0, end: float = 0, more: list | None = None, subtitles: str = ""):
    if not voice.ffmpeg():
        raise ToolError(FFMPEG_HELP)
    p = voice.resolve_file(path)
    if action == "trim":
        if end <= start or start < 0:
            raise ToolError("trim needs start and end in seconds, with end after start.")
        f = _ffmpeg(["-ss", start, "-to", end, "-i", p], _out("videos", f"{p.stem}-trim", p.suffix))
    elif action == "extract_audio":
        f = _ffmpeg(["-i", p, "-vn", "-c:a", "aac"], _out("videos", f"{p.stem}-audio", ".m4a"))
    elif action == "concat":
        clips = [p, *[voice.resolve_file(m) for m in more or []]]
        if len(clips) < 2:
            raise ToolError("concat needs at least one more clip in `more`.")
        if any(ch in str(c) for c in clips for ch in (chr(10), chr(13))):
            raise ToolError("A clip name contains a line break, which concat cannot take safely. Rename the file.")
        listing = _out("videos", "concat-list", ".txt")
        listing.write_text("".join("file '%s'\n" % c.as_posix().replace("'", "'\\''") for c in clips), encoding="utf-8")
        try:
            f = _ffmpeg(["-f", "concat", "-safe", "0", "-i", listing, "-c", "copy"], _out("videos", f"{p.stem}-joined", p.suffix))
        finally:
            listing.unlink(missing_ok=True)
    elif action == "add_subtitles":
        if not subtitles:
            raise ToolError("add_subtitles needs the .srt path in `subtitles` (make one with subtitles_create).")
        f = _ffmpeg(["-i", p, "-i", voice.resolve_file(subtitles), "-map", "0:v", "-map", "0:a?", "-map", "1",
                     "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text"], _out("videos", f"{p.stem}-subtitled", ".mp4"))
    else:
        raise ToolError("action must be trim, concat, extract_audio or add_subtitles.")
    return {"saved_to": str(f), "bytes": f.stat().st_size, "action": action}


# ---- writing

LENGTHS = {"short": "about 150 words", "medium": "about 400 words", "long": "about 900 words"}
KINDS = {"blog": "a blog post with a title and subheadings", "report": "a structured report with a summary first",
         "marketing": "marketing copy with a headline, benefits and a call to action",
         "linkedin": "a LinkedIn post: a strong first line, short paragraphs, no hashtag spam",
         "email": "an email with a subject line"}


@tool("content_write", "Draft written content from a brief and save it as Markdown: blog post, report, marketing copy, "
      "LinkedIn post or email. It only writes the draft; nothing is published or sent.",
      {"kind": {"type": "string", "enum": list(KINDS)}, "brief": S,
       "tone": {**S, "description": "e.g. friendly, formal, punchy; default follows the brief"},
       "length": {"type": "string", "enum": list(LENGTHS), "description": "default medium"}},
      ["kind", "brief"], category="creative", risk="write", timeout=300)
async def content_write(ctx, kind: str, brief: str, tone: str = "", length: str = "medium"):
    if kind not in KINDS:
        raise ToolError(f"kind must be one of: {', '.join(KINDS)}.")
    text = (await llm.complete(
        f"Write {KINDS[kind]}, {LENGTHS.get(length, LENGTHS['medium'])}, in Markdown. "
        f"{'Tone: ' + tone + '. ' if tone else ''}Use only facts given in the brief; where a needed fact is missing "
        "write [TO FILL: what is needed] instead of inventing it. Output only the piece itself.", brief, role="default")).strip()
    if not text:
        raise ToolError("The model returned nothing.")
    f = _out("content", f"{kind}-{brief[:30]}", ".md")
    f.write_text(text + "\n", encoding="utf-8")
    return {"saved_to": str(f), "words": len(text.split()), "text": text}


@tool("brainstorm", "Generate a list of distinct ideas on a topic, each with a one-line reason. Use it when the user "
      "wants options, names, angles or approaches to choose from.",
      {"topic": S, "n": {**I, "description": "how many, 3-30, default 10"}}, ["topic"], category="creative", timeout=180)
async def brainstorm(ctx, topic: str, n: int = 10):
    n = min(max(int(n), 3), 30)
    j = await llm.complete_json(f'Brainstorm {n} clearly different ideas for the user\'s topic. JSON: {{"ideas": '
                                '[{"idea": "...", "why": "one line"}]}. Vary the angle; no near-duplicates.', topic, role="default")
    ideas = [i for i in j.get("ideas") or [] if isinstance(i, dict) and i.get("idea")][:n]
    if not ideas:
        raise ToolError("The model returned no ideas; try rephrasing the topic.")
    return {"topic": topic, "ideas": ideas}


@tool("video_script", "Write a video script as a storyboard: scenes with duration, shots, narration and on-screen text. "
      "Saved as JSON. It plans the video; it does not render one.",
      {"topic": S, "seconds": {**I, "description": "target length, default 60"}, "style": {**S, "description": "e.g. explainer, ad, vlog"}},
      ["topic"], category="creative", risk="write", timeout=300)
async def video_script(ctx, topic: str, seconds: int = 60, style: str = "explainer"):
    j = await llm.complete_json(
        f"Write a {style} video script of about {int(seconds)} seconds as a storyboard. JSON: "
        '{"title": "...", "scenes": [{"seconds": 8, "shots": ["what the camera shows", "..."], "narration": '
        '"spoken words", "on_screen_text": ""}]}. Scene durations should add up to the target length.', topic, role="default")
    scenes = [{"scene": i, "seconds": s.get("seconds", 0), "shots": s.get("shots") or [], "narration": s.get("narration", ""),
               "on_screen_text": s.get("on_screen_text", "")}
              for i, s in enumerate((s for s in j.get("scenes") or [] if isinstance(s, dict)), 1)]
    if not scenes:
        raise ToolError("The model did not return a usable script; try again.")
    doc = {"title": j.get("title") or topic, "target_seconds": int(seconds), "scenes": scenes}
    f = _out("scripts", doc["title"], ".json")
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    total = sum(s["seconds"] for s in scenes if isinstance(s["seconds"], (int, float)))
    return {"saved_to": str(f), **doc, "scripted_seconds": total}


@tool("brand_kit", "Draft a starter brand kit for a product or business: name options, taglines, a colour palette "
      "(hex), fonts and voice. Saved as Markdown. Names are not checked for trademarks or free domains.",
      {"name": {**S, "description": "working name"}, "description": {**S, "description": "what it is and who it is for"}},
      ["name", "description"], category="creative", risk="write", timeout=300)
async def brand_kit(ctx, name: str, description: str):
    j = await llm.complete_json(
        'Create a starter brand kit. JSON: {"names": [5 alternative names], "taglines": [5], "palette": '
        '[{"name": "...", "hex": "#RRGGBB", "use": "primary|accent|background|text"}] (5 colours with enough '
        'contrast between text and background), "fonts": {"heading": "...", "body": "..."} (freely available '
        'fonts), "voice": "two sentences on tone of voice"}.', f"Working name: {name}\nAbout: {description}", role="default")
    palette = [c for c in j.get("palette") or [] if isinstance(c, dict) and re.fullmatch(r"#[0-9a-fA-F]{6}", str(c.get("hex", "")))]
    if not palette or not j.get("taglines"):
        raise ToolError("The model did not return a usable brand kit; try again.")
    fonts = j.get("fonts") if isinstance(j.get("fonts"), dict) else {}
    md = [f"# Brand kit: {name}", "", description, "", "## Name options", *[f"- {n}" for n in j.get("names") or []], "",
          "## Taglines", *[f"- {t}" for t in j["taglines"]], "", "## Palette", "| Colour | Hex | Use |", "|---|---|---|",
          *[f"| {c.get('name', '')} | `{c['hex'].upper()}` | {c.get('use', '')} |" for c in palette], "",
          "## Fonts", f"- Heading: {fonts.get('heading', '')}", f"- Body: {fonts.get('body', '')}", "",
          "## Voice", str(j.get("voice", "")), "",
          "_Draft generated by Sparx. Names are not checked against trademarks or domain availability._"]
    f = _out("brand", name, ".md")
    f.write_text("\n".join(md) + "\n", encoding="utf-8")
    return {"saved_to": str(f), "names": j.get("names") or [], "taglines": j["taglines"],
            "palette": [{"name": c.get("name", ""), "hex": c["hex"].upper(), "use": c.get("use", "")} for c in palette],
            "fonts": fonts, "voice": j.get("voice", "")}


def _page_risks(page: str) -> list[str]:
    """Things in model-written HTML that run on open or load from elsewhere."""
    out = []
    if re.search(r"<script", page, re.I):
        out.append("contains <script> code that runs when the page is opened")
    handlers = sorted({m.lower() for m in re.findall(r"\s(on[a-z]+)\s*=", page, re.I)})
    if handlers:
        out.append("has inline event handlers that run code: " + ", ".join(handlers))
    if re.search(r"(?:src|href|action|data)\s*=\s*[\"']?\s*(?:https?:)?//|url\(\s*[\"']?\s*(?:https?:)?//|@import\s+[\"']?(?:https?:)?//", page, re.I):
        out.append("loads or links external resources (the page contacts other servers when opened)")
    if re.search(r"<(iframe|object|embed)|http-equiv\s*=\s*[\"']?refresh", page, re.I):
        out.append("embeds frames/objects or redirects automatically")
    return out


@tool("website_generate", "Build a complete single-file responsive web page (HTML with inline CSS, no build step) from a "
      "brief and save it locally. Good for landing pages, portfolios and event pages. It is not published anywhere.",
      {"brief": {**S, "description": "what the page is for, its sections and any real text to use"},
       "name": {**S, "description": "short site name, used for the folder"}},
      ["brief"], category="creative", risk="write", timeout=600)
async def website_generate(ctx, brief: str, name: str = ""):
    page = _fenced(await llm.complete(
        "Build one complete, responsive HTML5 page for the user's brief. A single file: all CSS inline in <style>, "
        "no external scripts, fonts, frameworks or images (use CSS and inline SVG for visuals). Mobile-first, "
        "semantic tags, accessible contrast, a viewport meta tag. Use only facts from the brief; mark missing "
        "details as [TO FILL]. Output only the HTML in one ```html block.", brief, role="default"), "html")
    low = page.lower()
    if "<html" not in low or "</html>" not in low:
        raise ToolError("The model did not return a complete HTML page (it may have been cut off); try a shorter "
                        "brief or a stronger model in models.default.")
    if 'name="viewport"' not in low:
        page = re.sub(r"(<head[^>]*>)", r'\1\n<meta name="viewport" content="width=device-width, initial-scale=1">', page, count=1, flags=re.I)
    f = config.path("sites", f"{slug(name or brief)}-{uuid.uuid4().hex[:4]}", "index.html")
    f.write_text(page, encoding="utf-8")
    warnings = _page_risks(page)
    return {"saved_to": str(f), "bytes": len(page.encode()), "open_with": f.as_uri(),
            "note": "Saved locally only. This is unreviewed model output: read it before opening or publishing it; "
                    "anything marked [TO FILL] needs your input.", **({"warnings": warnings} if warnings else {})}
