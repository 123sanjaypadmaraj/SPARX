import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from sparx import config
from sparx.tools import creative

from conftest import run_tool


def png(size=(8, 6)):
    buf = io.BytesIO()
    Image.new("RGB", size, "teal").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def net(monkeypatch):
    """Requests the image tool makes land here. net.reply(request) decides the answer."""
    class Net:
        calls = []
        reply = staticmethod(lambda request: httpx.Response(200, content=png()))

    def handler(request):
        Net.calls.append(request)
        return Net.reply(request)
    monkeypatch.setattr(creative, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return Net


async def test_image_uses_the_free_service_and_says_so(ctx, net):
    out = json.loads(await run_tool("image_generate", ctx, prompt="a red kite over hills", size="640x480"))
    (req,) = net.calls
    assert req.url.host == "image.pollinations.ai" and req.url.params["width"] == "640" and "red%20kite" in str(req.url)
    assert out["backend"] == "pollinations" and "third-party" in out["note"] and out["size"] == "8x6"
    assert Path(out["saved_to"]).is_file() and config.HOME in Path(out["saved_to"]).parents
    assert "64 to 2048" in await run_tool("image_generate", ctx, prompt="x", size="9000x10")

    config.set_key("integrations.pollinations", False)
    assert "No image generator is enabled" in await run_tool("image_generate", ctx, prompt="x")
    assert len(net.calls) == 1


async def test_image_prefers_the_local_generator_and_falls_back(ctx, net):
    config.set_key("integrations.sd_url", "http://127.0.0.1:7860/")
    net.reply = lambda r: httpx.Response(500) if r.url.host == "127.0.0.1" else httpx.Response(200, content=png())
    out = json.loads(await run_tool("image_generate", ctx, prompt="a kite"))
    assert [c.url.host for c in net.calls] == ["127.0.0.1", "image.pollinations.ai"]
    assert out["backend"] == "pollinations" and out["earlier_failures"][0].startswith("stable-diffusion:")

    net.reply = lambda r: httpx.Response(200, content=b"not an image")
    assert "Image generation failed" in await run_tool("image_generate", ctx, prompt="a kite")


async def test_diagram_saves_source_and_page(ctx, fake_llm):
    fake_llm.script = ["Here you go:\n```mermaid\nflowchart TD\n  A[\"Start <b>\"] --> B\n```\n", "I cannot draw that."]
    out = json.loads(await run_tool("diagram_create", ctx, description="login <flow>"))
    assert out["mermaid"].startswith("flowchart TD") and Path(out["mermaid_file"]).read_text(encoding="utf-8").startswith("flowchart TD")
    page = Path(out["html_file"]).read_text(encoding="utf-8")
    assert "login &lt;flow&gt;" in page and "Start &lt;b&gt;" in page and "<b>" not in page    # model text is escaped
    assert "did not return Mermaid" in await run_tool("diagram_create", ctx, description="x")


def test_srt_formatting():
    srt = creative.to_srt([{"start": 0, "end": 1.5, "text": "Hello"}, {"start": 2, "end": 2, "text": "  "},
                           {"start": 3661.2, "end": 3661.25, "text": "Bye"}])
    assert srt == "1\n00:00:00,000 --> 00:00:01,500\nHello\n\n2\n01:01:01,200 --> 01:01:01,500\nBye\n"


async def test_subtitles_from_a_transcript(ctx, monkeypatch, tmp_path):
    clip = tmp_path / "talk.wav"
    clip.write_bytes(b"RIFF")
    monkeypatch.setattr(creative.voice, "resolve_file", lambda p: Path(p))
    monkeypatch.setattr(creative.voice, "load_audio", lambda p: "samples")
    said = {"language": "en", "segments": [{"start": 0, "end": 2, "text": "Good morning"}]}
    monkeypatch.setattr(creative.voice, "transcribe", lambda audio, language: said)
    out = await run_tool("subtitles_create", ctx, path=str(clip))
    assert '"cues": 1' in out and "untrusted" in out and "Good morning" in out
    said["segments"] = []
    assert "No speech was recognised" in await run_tool("subtitles_create", ctx, path=str(clip))


async def test_video_edit_without_ffmpeg_and_argument_checks(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(creative.voice, "ffmpeg", lambda: "")
    assert "winget install Gyan.FFmpeg" in await run_tool("video_edit", ctx, action="trim", path="a.mp4", start=0, end=5)

    clip, ran = tmp_path / "a.mp4", []
    clip.write_bytes(b"x")
    monkeypatch.setattr(creative.voice, "ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(creative.voice, "resolve_file", lambda p: Path(p))

    def fake_run(cmd, **kw):
        ran.append(cmd)
        Path(cmd[-1]).write_bytes(b"video")
        return type("R", (), {"returncode": 0, "stderr": b""})()
    monkeypatch.setattr(creative.subprocess, "run", fake_run)
    assert "end after start" in await run_tool("video_edit", ctx, action="trim", path=str(clip), start=5, end=2)
    assert "at least one more clip" in await run_tool("video_edit", ctx, action="concat", path=str(clip))
    assert "needs the .srt path" in await run_tool("video_edit", ctx, action="add_subtitles", path=str(clip))
    assert ran == []
    out = json.loads(await run_tool("video_edit", ctx, action="trim", path=str(clip), start=1, end=4))
    assert ran[0][3:9] == ["error", "-ss", "1", "-to", "4", "-i"] and out["bytes"] == 5 and clip.read_bytes() == b"x"


async def test_writing_tools_save_what_the_model_wrote(ctx, fake_llm):
    fake_llm.script = ["# Launch\n\nWe open on [TO FILL: date].", {"ideas": [{"idea": "Kite club", "why": "cheap"}, {"nope": 1}]},
                       {"title": "Kites", "scenes": [{"seconds": 20, "narration": "Look up."}, "junk", {"seconds": 40}]},
                       {"names": ["Aero"], "taglines": ["Fly"], "palette": [{"name": "Sky", "hex": "#1a2b3c", "use": "primary"},
                                                                           {"name": "Bad", "hex": "blue"}]},
                       {"names": [], "taglines": [], "palette": []}]
    out = json.loads(await run_tool("content_write", ctx, kind="blog", brief="our launch", tone="warm"))
    assert out["words"] == 8 and Path(out["saved_to"]).read_text(encoding="utf-8").startswith("# Launch")
    assert "Tone: warm" in fake_llm.prompts[0][0] and "[TO FILL" in fake_llm.prompts[0][0]
    assert "kind must be one of" in await run_tool("content_write", ctx, kind="novel", brief="x")

    assert json.loads(await run_tool("brainstorm", ctx, topic="clubs", n=99))["ideas"] == [{"idea": "Kite club", "why": "cheap"}]
    script = json.loads(await run_tool("video_script", ctx, topic="kites"))
    assert [s["scene"] for s in script["scenes"]] == [1, 2] and script["scripted_seconds"] == 60
    kit = json.loads(await run_tool("brand_kit", ctx, name="Aero", description="kites"))
    assert kit["palette"] == [{"name": "Sky", "hex": "#1A2B3C", "use": "primary"}]
    assert "not checked against trademarks" in Path(kit["saved_to"]).read_text(encoding="utf-8")
    assert "did not return a usable brand kit" in await run_tool("brand_kit", ctx, name="Aero", description="kites")


async def test_website_is_one_complete_local_file(ctx, fake_llm):
    fake_llm.script = ["```html\n<!doctype html><html><head><title>K</title></head><body>Hi</body></html>\n```",
                       "```html\n<html><body>cut off"]
    out = json.loads(await run_tool("website_generate", ctx, brief="kite club page", name="Kite Club"))
    page = Path(out["saved_to"])
    assert page.name == "index.html" and page.parent.name.startswith("kite-club-") and "Saved locally only" in out["note"]
    assert 'name="viewport"' in page.read_text(encoding="utf-8")
    assert "did not return a complete HTML page" in await run_tool("website_generate", ctx, brief="x")


# ---- audit fixes

async def test_tts_narration_saves_audio_and_says_where_the_text_went(ctx, monkeypatch, tmp_path):
    seen = {}

    def fake_synth(text, chosen, out):
        seen.update(text=text, voice=chosen)
        f = Path(out).with_suffix(seen.get("ext", ".mp3"))
        if seen.get("write", True):
            f.write_bytes(b"audio" * 50)
        return f
    monkeypatch.setattr(creative.voice, "synthesize", fake_synth)
    monkeypatch.setattr(creative.voice, "pick_voice", lambda text, chosen: chosen or "en-GB-RyanNeural")
    out = json.loads(await run_tool("tts_narration", ctx, text="# Hello **world**", voice="en-US-GuyNeural", name="Intro ../x"))
    f = Path(out["saved_to"])
    assert f.suffix == ".mp3" and f.is_file() and config.HOME in f.parents and ".." not in f.name
    assert seen["text"] == "Hello world" and seen["voice"] == "en-US-GuyNeural" and "edge-tts" in out["engine"]
    assert "Microsoft" in out["note"] and "online" in out["note"]
    seen["ext"] = ".wav"
    wav = json.loads(await run_tool("tts_narration", ctx, text="Hello"))
    assert "pyttsx3" in wav["engine"] and "may have been sent" in wav["note"]
    seen["write"] = False
    assert "no audio" in await run_tool("tts_narration", ctx, text="Hello again")
    assert "nothing to narrate" in await run_tool("tts_narration", ctx, text="```x```\n".replace("x", "") * 0 + "   ")


async def test_image_openai_and_failed_fallback_say_the_prompt_left_the_machine(ctx, net, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-123")
    config.set_key("integrations.pollinations", True)
    b64 = __import__("base64").b64encode(png()).decode()
    net.reply = lambda r: (httpx.Response(200, json={"data": [{"b64_json": b64}]}) if r.url.host == "api.openai.com"
                           else httpx.Response(500))
    out = json.loads(await run_tool("image_generate", ctx, prompt="my private plan"))
    assert out["backend"] == "openai" and "OpenAI" in out["note"] and "sk-secret-123" not in json.dumps(out)
    net.reply = lambda r: httpx.Response(200, content=b"not an image")
    msg = await run_tool("image_generate", ctx, prompt="my private plan")
    assert "Image generation failed" in msg and "sk-secret-123" not in msg
    assert "OpenAI" in msg and "Pollinations" in msg and "received the prompt" in msg


async def test_concat_rejects_newline_in_clip_names_and_ffmpeg_timeout_cleans_up(ctx, monkeypatch, tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(creative.voice, "ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(creative.voice, "resolve_file", lambda p: Path(p))
    ran = []
    monkeypatch.setattr(creative.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or (_ for _ in ()).throw(
        creative.subprocess.TimeoutExpired(cmd, 1)))
    assert "line break" in await run_tool("video_edit", ctx, action="concat", path=str(clip), more=[str(tmp_path / "b\nfile 'evil'.mp4")])
    assert ran == []
    msg = await run_tool("video_edit", ctx, action="trim", path=str(clip), start=0, end=2)
    assert "timed out" in msg and not list((config.HOME / "videos").glob("*-trim*"))


async def test_website_flags_scripts_and_external_loads_as_unreviewed(ctx, fake_llm):
    fake_llm.script = ['```html\n<!doctype html><html><head><script src="https://evil.example/x.js"></script></head>'
                       '<body onload="steal()"><script>fetch("//e.example")</script><img src="https://e.example/p.png"></body></html>\n```',
                       "```html\n<!doctype html><html><head><title>K</title></head><body>Hi</body></html>\n```"]
    out = json.loads(await run_tool("website_generate", ctx, brief="page"))
    assert "unreviewed model output" in out["note"] and out["warnings"]
    text = " ".join(out["warnings"])
    assert "script" in text and "external" in text and "onload" in text
    clean = json.loads(await run_tool("website_generate", ctx, brief="page"))
    assert "unreviewed model output" in clean["note"] and "warnings" not in clean


async def test_subtitles_say_when_audio_went_to_a_cloud_service(ctx, monkeypatch, tmp_path):
    clip = tmp_path / "t.wav"
    clip.write_bytes(b"RIFF")
    monkeypatch.setattr(creative.voice, "resolve_file", lambda p: Path(p))
    monkeypatch.setattr(creative.voice, "load_audio", lambda p: "s")
    res = {"language": "en", "backend": "groq", "segments": [{"start": 0, "end": 2, "text": "Hi"}]}
    monkeypatch.setattr(creative.voice, "transcribe", lambda a, l: res)
    assert "groq" in json.loads(_strip(await run_tool("subtitles_create", ctx, path=str(clip))))["note"]
    res["backend"] = "faster-whisper"
    assert "note" not in json.loads(_strip(await run_tool("subtitles_create", ctx, path=str(clip))))


def _strip(text):
    return text[text.index("{"):text.rindex("}") + 1]


async def test_diagram_result_discloses_cdn_script(ctx, fake_llm):
    fake_llm.script = ["```mermaid\nflowchart TD\n  A --> B\n```"]
    out = json.loads(await run_tool("diagram_create", ctx, description="x flow"))
    assert "cdn.jsdelivr.net" in out["note"] and "third party" in out["note"] and "saved locally" in out["note"]
    assert "cdn.jsdelivr.net" in Path(out["html_file"]).read_text(encoding="utf-8")
