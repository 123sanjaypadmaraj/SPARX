import base64
import importlib.util
import io
import json

import httpx
import numpy as np
import pytest
from PIL import Image

from sparx import config, db, voice
from sparx.tools import REGISTRY, ToolError, audio, vision

from conftest import run_tool
from test_voice import tone, voice_like

RATE = voice.RATE


def body(result: str):
    """The JSON inside an <untrusted> wrapper (or the bare result)."""
    if "<untrusted" in result:
        result = result.split(">\n", 1)[1].rsplit("\n</untrusted>", 1)[0]
    return json.loads(result)


@pytest.fixture
def wav(tmp_path):
    def make(name="standup.wav", samples=None):
        f = tmp_path / name
        f.write_bytes(voice.wav_bytes(tone(3.0) if samples is None else samples))
        return f
    return make


@pytest.fixture
def stt(monkeypatch):
    """A fake speech-to-text backend; stt.calls records (seconds of audio, language)."""
    calls = []
    result = {"text": "We ship the beta on Friday. Priya will write the release notes.", "language": "en",
              "backend": "fake", "segments": [{"start": 0.0, "end": 1.4, "text": "We ship the beta on Friday."},
                                              {"start": 1.6, "end": 3.0, "text": "Priya will write the release notes."}]}

    def transcribe(samples, language=None):
        calls.append((round(len(samples) / RATE, 1), language))
        return json.loads(json.dumps(transcribe.result))
    transcribe.result, transcribe.calls = result, calls
    monkeypatch.setattr(voice, "transcribe", transcribe)
    return transcribe


# ---- audio: transcripts

async def test_transcribe_saves_indexes_and_is_untrusted(ctx, wav, stt):
    raw = await run_tool("audio_transcribe", ctx, path=str(wav()), language="en")
    assert raw.startswith('<untrusted source="tool:audio_transcribe">')
    out = body(raw)
    assert out["transcript_id"] == 1 and out["language"] == "en" and out["duration"] == "00:03"
    assert out["segments"][1] == {"at": "00:01", "text": "Priya will write the release notes."}
    assert stt.calls == [(3.0, "en")]
    saved = open(out["saved_to"], encoding="utf-8").read()
    assert "[00:00] We ship the beta on Friday." in saved and str(config.HOME) in out["saved_to"]

    hits = body(await run_tool("transcript_search", ctx, query="release notes"))
    assert len(hits) == 1 and hits[0]["at"] == "00:01" and hits[0]["file"].endswith("standup.wav")
    assert hits[0]["transcript_id"] == 1
    assert "No match in 1 transcript" in await run_tool("transcript_search", ctx, query="budget")
    assert body(await run_tool("transcript_search", ctx, query='beta" AND (friday*'))[0]["at"] == "00:00"   # no FTS syntax errors


async def test_search_before_anything_is_transcribed(ctx):
    assert "Nothing has been transcribed yet" in await run_tool("transcript_search", ctx, query="hello")
    assert "at least one word" in await run_tool("transcript_search", ctx, query="?!")


async def test_transcribe_errors_are_plain(ctx, tmp_path, monkeypatch, stt, wav):
    assert "File not found" in await run_tool("audio_transcribe", ctx, path=str(tmp_path / "nope.wav"))
    mp3 = tmp_path / "talk.mp3"
    mp3.write_bytes(b"ID3 not really audio")
    monkeypatch.setattr(voice, "ffmpeg", lambda: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    assert "ffmpeg is not on PATH" in await run_tool("audio_transcribe", ctx, path=str(mp3))
    stt.result = {"text": " ", "language": "en", "backend": "fake", "segments": []}
    assert "No speech was recognised" in await run_tool("audio_transcribe", ctx, path=str(wav()))
    assert db.q("SELECT * FROM transcripts") == []


def test_speaker_labels_cluster_by_voice():
    a = lambda seed: voice_like(112, (700, 1200, 2600), seconds=1.5, seed=seed)
    b = lambda seed: voice_like(208, (350, 2300, 3200), seconds=1.5, seed=seed)
    samples = np.concatenate([a(1), b(2), a(3), b(4), tone(0.02)])
    segs = [{"start": i * 1.5, "end": (i + 1) * 1.5, "text": f"s{i}"} for i in range(4)] + [
        {"start": 6.0, "end": 6.02, "text": "too short to fingerprint"}]
    assert audio.label_speakers(samples, segs) == 2
    assert [s["speaker"] for s in segs] == ["Speaker 1", "Speaker 2", "Speaker 1", "Speaker 2", "Speaker 2"]
    assert audio.label_speakers(np.zeros(100, dtype=np.float32), [{"start": 0, "end": 0.005, "text": "x"}]) == 0


async def test_transcribe_with_approximate_speakers(ctx, wav, stt):
    samples = np.concatenate([voice_like(112, (700, 1200, 2600), seconds=1.5), voice_like(208, (350, 2300, 3200), seconds=1.5)])
    stt.result["segments"][0]["end"], stt.result["segments"][1]["start"] = 1.5, 1.5
    out = body(await run_tool("audio_transcribe", ctx, path=str(wav(samples=samples)), speakers=True))
    assert [s["speaker"] for s in out["segments"]] == ["Speaker 1", "Speaker 2"] and "approximate" in out["speakers"]
    assert "Speaker 2: Priya" in open(out["saved_to"], encoding="utf-8").read()


# ---- audio: meetings and translation

async def test_meeting_summary_plumbing(ctx, wav, stt, fake_llm):
    await run_tool("audio_transcribe", ctx, path=str(wav()))
    fake_llm.script = [{"summary": "The team agreed to ship the beta.", "decisions": ["Ship the beta on Friday"],
                        "action_items": [{"task": "Write the release notes", "owner": "Priya", "due": "Friday"},
                                         {"task": "", "owner": "nobody"}], "open_questions": []}]
    out = body(await run_tool("meeting_summarize", ctx, source="1"))
    system, user = fake_llm.prompts[0]
    assert "[00:01] Priya will write the release notes." in user and "not instructions" in system
    assert out["decisions"] == ["Ship the beta on Friday"] and out["source"].endswith("standup.wav")
    assert out["action_items"] == [{"task": "Write the release notes", "owner": "Priya", "due": "Friday"}]
    assert out["suggested_tasks"] == ["Write the release notes (Priya) due Friday"] and "No tasks were created" in out["note"]
    assert "No transcript with id 99" in await run_tool("meeting_summarize", ctx, source="99")
    assert "did not return a usable summary" in await run_tool("meeting_summarize", ctx, source="1")   # model said {}


async def test_meeting_from_a_file_transcribes_once_and_long_text_is_condensed(ctx, wav, stt, fake_llm, tmp_path, monkeypatch):
    f = wav("call.wav")
    fake_llm.script = [{"summary": "s"}, {"summary": "s"}]
    await run_tool("meeting_summarize", ctx, source=str(f))
    await run_tool("meeting_summarize", ctx, source=str(f))
    assert len(stt.calls) == 1 and db.one("SELECT COUNT(*) AS n FROM transcripts")["n"] == 1

    monkeypatch.setattr(audio, "MAX_LLM_CHARS", 50)
    notes = tmp_path / "notes.txt"
    notes.write_text("a" * 120, encoding="utf-8")
    fake_llm.prompts.clear()
    fake_llm.script = ["part one notes", "part two notes", "part three notes", {"summary": "all of it"}]
    assert body(await run_tool("meeting_summarize", ctx, source=str(notes)))["summary"] == "all of it"
    assert len(fake_llm.prompts) == 4 and "Part 3 of 3:\npart three notes" in fake_llm.prompts[3][1]


async def test_translate_text_file_and_microphone(ctx, wav, stt, fake_llm, monkeypatch):
    fake_llm.script = ["नमस्ते दुनिया", "We ship on Friday (kn)", "live translation"]
    out = body(await run_tool("speech_translate", ctx, source="Hello world", target="hi"))
    assert out["translation"] == "नमस्ते दुनिया" and out["original"] == "Hello world" and stt.calls == []
    assert "into Hindi" in fake_llm.prompts[0][0]
    out = body(await run_tool("speech_translate", ctx, source=str(wav()), target="Kannada", language="en"))
    assert out["original"].startswith("We ship the beta") and out["spoken_language"] == "en" and stt.calls == [(3.0, "en")]

    monkeypatch.setattr(voice, "record_seconds", lambda seconds: tone(seconds))
    ctx.asked.clear()
    out = body(await run_tool("live_translate", ctx, target="English", seconds=1))
    assert out["translation"] == "live translation" and stt.calls[-1] == (5.0, None)     # clamped to 5 s
    assert ctx.asked == ["live_translate"]                                                 # the microphone needs approval


# ---- audio: sound detection

def test_sound_classification_rules():
    rng = np.random.default_rng(1)
    quiet = (rng.standard_normal(RATE * 3) * 0.002).astype(np.float32)
    alarm = np.concatenate([quiet, tone(2.0, freq=3100, amp=0.4), quiet])
    assert audio.classify_sound(quiet)["events"] == []
    assert audio.classify_sound(alarm)["events"] == [{"event": "alarm-like tone", "seconds": 2.0}]
    roar = (rng.standard_normal(RATE * 4) * 0.2).astype(np.float32)
    out = audio.classify_sound(np.concatenate([quiet, roar]))
    assert out["events"] == [{"event": "sustained loud noise", "seconds": 4.0}] and out["peak_dbfs"] > -20
    assert audio.classify_sound(np.concatenate([quiet, tone(2.0, freq=300, amp=0.4)]))["events"] == [
        {"event": "sustained loud noise", "seconds": 2.0}]                                 # a low hum is not an alarm
    assert audio.classify_sound(np.concatenate([quiet, tone(0.5, freq=3100, amp=0.4)]))["events"] == []   # one short beep


async def test_sound_detect_tool(ctx, monkeypatch):
    monkeypatch.setattr(voice, "record_seconds", lambda seconds: tone(seconds, freq=3000, amp=0.5))
    out = json.loads(await run_tool("sound_detect", ctx, seconds=6))
    assert out["events"][0]["event"] == "alarm-like tone" and "heuristic" in out["method"] and ctx.asked == ["sound_detect"]


# ---- vision: request building

@pytest.fixture
def picture(tmp_path):
    f = tmp_path / "photo.png"
    Image.new("RGB", (3000, 1500), (200, 30, 30)).save(f)
    return f


@pytest.fixture
def ollama(monkeypatch):
    """Mock Ollama: seen collects the decoded requests; set ollama.reply or ollama.status."""
    class State:
        seen, reply, status = [], "A plain red rectangle.", 200

    def handler(request: httpx.Request):
        State.seen.append((str(request.url), json.loads(request.read())))
        if State.status != 200:
            return httpx.Response(State.status, text="model 'minicpm-v:8b' not found")
        return httpx.Response(200, json={"message": {"role": "assistant", "content": State.reply}})
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(vision, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    State.seen = []
    return State


async def test_image_goes_to_ollama_downscaled(ctx, picture, ollama):
    raw = await run_tool("image_describe", ctx, path=str(picture), question="What colour is it?")
    assert raw.startswith('<untrusted source="tool:image_describe">')
    assert body(raw) == {"answer": "A plain red rectangle.", "model": "ollama/minicpm-v:8b (local)"}
    (url, req), = ollama.seen
    assert url == "http://localhost:11434/api/chat" and req["model"] == "minicpm-v:8b" and req["stream"] is False
    msg, = req["messages"]
    assert msg["role"] == "user" and msg["content"] == "What colour is it?"
    im = Image.open(io.BytesIO(base64.b64decode(msg["images"][0])))
    assert im.format == "JPEG" and im.size == (1280, 640)


async def test_ollama_base_url_and_model_are_configurable(ctx, picture, ollama, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://box:9999/v1")
    config.set_key("integrations.ollama_vision_model", "llava:13b")
    await run_tool("image_describe", ctx, path=str(picture))
    (url, req), = ollama.seen
    assert url == "http://box:9999/api/chat" and req["model"] == "llava:13b"
    assert req["messages"][0]["content"] == "Describe this image in detail."


async def test_vision_errors_say_how_to_fix_it(ctx, picture, ollama, monkeypatch, tmp_path):
    ollama.status = 404
    out = await run_tool("image_describe", ctx, path=str(picture))
    assert out.startswith("Error:") and "ollama pull minicpm-v:8b" in out and "models.vision" in out

    def down(request):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(vision, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(down)))
    out = await run_tool("image_describe", ctx, path=str(picture))
    assert "did not answer" in out and "ollama pull" in out
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    assert "not an image" in await run_tool("image_describe", ctx, path=str(notes))
    ollama.status, ollama.reply = 200, "  "
    monkeypatch.setattr(vision, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"message": {"content": " "}}))))
    assert "returned nothing" in await run_tool("image_describe", ctx, path=str(picture))


async def test_configured_vision_model_is_used_instead_of_ollama(ctx, picture, ollama, fake_llm):
    config.set_key("models.vision", "gemini/gemini-2.5-flash")
    fake_llm.script = ["Two bars, rising."]
    out = body(await run_tool("chart_read", ctx, path=str(picture), question="Which bar is taller?"))
    assert out["answer"] == "Two bars, rising." and out["model"] == "gemini/gemini-2.5-flash" and "approximate" in out["note"]
    assert ollama.seen == []
    text, image = fake_llm.seen[0][0]["content"]
    assert "Markdown table" in text["text"] and text["text"].endswith("Also answer: Which bar is taller?")
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/")


async def test_hosted_model_gets_a_plain_completion(picture, monkeypatch):
    sent = {}

    class Hosted:
        async def _create(self, **kw):
            sent.update(kw)
            msg = type("M", (), {"content": "seen"})
            return type("R", (), {"choices": [type("C", (), {"message": msg})]})
    config.set_key("models.vision", "openai/gpt-4.1-mini")
    monkeypatch.setattr(vision.llm, "get", lambda role="default": Hosted())
    assert await vision.describe_image(picture.read_bytes(), "q") == "seen"
    assert list(sent) == ["messages"]                      # no empty `tools` list, which some providers reject


async def test_ocr_kinds_use_their_prompts(ctx, picture, ollama, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)       # no pytesseract
    ollama.reply = "INVOICE 42"
    assert body(await run_tool("image_ocr", ctx, path=str(picture))) == {"text": "INVOICE 42", "engine": "ollama/minicpm-v:8b (local)"}
    await run_tool("image_ocr", ctx, path=str(picture), kind="handwriting")
    await run_tool("image_ocr", ctx, path=str(picture), kind="document")
    prompts = [req["messages"][0]["content"] for _url, req in ollama.seen]
    assert "Transcribe all the text" in prompts[0] and "[illegible]" in prompts[1] and "Markdown" in prompts[2]
    assert "kind must be" in await run_tool("image_ocr", ctx, path=str(picture), kind="barcode")


async def test_screen_explain_sends_the_screenshot(ctx, ollama, monkeypatch):
    monkeypatch.setattr(vision, "grab_screen", lambda: Image.new("RGB", (1920, 1080), "white"))
    out = body(await run_tool("screen_explain", ctx, question="What is this error?"))
    assert out["screen"] == "1920x1080" and out["answer"] == "A plain red rectangle."
    req = ollama.seen[0][1]
    assert req["messages"][0]["content"] == "What is this error?"
    assert Image.open(io.BytesIO(base64.b64decode(req["messages"][0]["images"][0]))).size == (1280, 720)


# ---- vision: webcam and video

class FakeCamera:
    """Stands in for cv2: VideoCapture hands out frames whose brightness follows `levels`."""
    CAP_DSHOW = 700

    def __init__(self, levels=(40,), opened=True):
        self.levels, self.opened, self.reads, self.released, self.args = list(levels), opened, 0, 0, None

    def VideoCapture(self, *args):
        self.args = args
        return self

    def isOpened(self):
        return self.opened

    def grab(self):
        return True

    def read(self):
        self.reads += 1
        shot = max(0, self.reads - 6)                      # five warm-up reads come first
        return True, np.full((48, 64, 3), self.levels[min(shot, len(self.levels) - 1)], dtype=np.uint8)

    def release(self):
        self.released += 1


async def test_webcam_capture_needs_approval_and_releases_the_camera(ctx, monkeypatch):
    cam = FakeCamera()
    monkeypatch.setattr(vision, "_cv2", lambda: cam)
    out = json.loads(await run_tool("webcam_capture", ctx))
    assert ctx.asked == ["webcam_capture"] and REGISTRY["webcam_describe"].risk == REGISTRY["webcam_watch"].risk == "control"
    assert out["size"] == "64x48" and Image.open(out["saved_to"]).size == (64, 48) and str(config.HOME) in out["saved_to"]
    assert cam.released == 1 and cam.args[0] == 0

    closed = FakeCamera(opened=False)
    monkeypatch.setattr(vision, "_cv2", lambda: closed)
    assert "could not be opened" in await run_tool("webcam_capture", ctx, camera=1)
    assert closed.released == 1                             # released even when it fails


async def test_webcam_is_not_opened_without_approval(monkeypatch):
    from sparx import tools
    cam = FakeCamera()
    monkeypatch.setattr(vision, "_cv2", lambda: cam)
    out = await run_tool("webcam_describe", tools.Context(session="t"))
    assert "Not done" in out and cam.args is None


async def test_webcam_describe_and_watch(ctx, ollama, monkeypatch):
    monkeypatch.setattr(vision.time, "sleep", lambda s: None)
    cam = FakeCamera()
    monkeypatch.setattr(vision, "_cv2", lambda: cam)
    out = body(await run_tool("webcam_describe", ctx))
    assert out["answer"] == "A plain red rectangle." and out["saved_to"].endswith(".jpg") and cam.released == 1
    assert "without identifying anyone" in ollama.seen[0][1]["messages"][0]["content"]

    ollama.seen.clear()
    cam = FakeCamera(levels=(40, 40, 200, 200))             # the scene changes between the 2nd and 3rd frame
    monkeypatch.setattr(vision, "_cv2", lambda: cam)
    out = body(await run_tool("webcam_watch", ctx, seconds=15, interval=5, question="Is anyone there?"))
    assert out["frames"] == 4 and out["changed"] is True and cam.released == 1
    assert out["change_between_frames_percent"][0] == 0 and out["change_between_frames_percent"][1] > 50
    assert [s["at_seconds"] for s in out["seen"]] == [0, 10] and len(ollama.seen) == 2

    still = FakeCamera()
    monkeypatch.setattr(vision, "_cv2", lambda: still)
    out = body(await run_tool("webcam_watch", ctx, seconds=10, interval=5))
    assert out["changed"] is False and len(out["seen"]) == 1


async def test_video_summary_from_a_real_file(ctx, ollama, fake_llm, tmp_path, monkeypatch):
    import cv2
    f = tmp_path / "clip.avi"
    w = cv2.VideoWriter(str(f), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (64, 48))
    for i in range(40):
        w.write(np.full((48, 64, 3), i * 6, dtype=np.uint8))
    w.release()
    monkeypatch.setattr(voice, "load_audio", lambda src, rate=RATE: tone(1.0))
    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {"text": "welcome to the demo"})
    fake_llm.script = ["A screen that slowly brightens while someone welcomes the viewer."]
    out = body(await run_tool("video_summarize", ctx, path=str(f), max_frames=4))
    assert out["summary"].startswith("A screen that slowly brightens") and out["duration_seconds"] == 4.0
    assert [s["at_seconds"] for s in out["frames"]] == [0.5, 1.5, 2.5, 3.5] and len(ollama.seen) == 4
    assert out["audio"] == "transcribed"
    system, user = fake_llm.prompts[0]
    assert "[0.5s] A plain red rectangle." in user and "welcome to the demo" in user

    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: (_ for _ in ()).throw(ToolError(voice.STT_HELP)))
    fake_llm.script = ["Frames only."]
    out = body(await run_tool("video_summarize", ctx, path=str(f), max_frames=2))
    assert out["summary"] == "Frames only." and out["audio"].startswith("not transcribed: Speech-to-text is not set up")
    notes = tmp_path / "notes.txt"
    notes.write_text("x")
    assert "could not be read as a video" in await run_tool("video_summarize", ctx, path=str(notes))


# ---- audit: privacy of captures

async def test_screenshot_needs_approval_and_says_when_it_left_the_machine(ctx, monkeypatch, fake_llm):
    assert REGISTRY["screen_explain"].risk == "control"
    monkeypatch.setattr(vision, "grab_screen", lambda: Image.new("RGB", (64, 48), "white"))
    config.set_key("models.vision", "gemini/gemini-2.5-flash")
    fake_llm.script = ["A white screen."]
    out = body(await run_tool("screen_explain", ctx))
    assert ctx.asked == ["screen_explain"] and "sent to the hosted vision model gemini/gemini-2.5-flash" in out["privacy"]


async def test_local_vision_makes_no_hosted_claim(ctx, picture, ollama):
    assert "privacy" not in body(await run_tool("image_describe", ctx, path=str(picture)))


async def test_old_webcam_photos_are_deleted(ctx, monkeypatch):
    import os
    import time
    monkeypatch.setattr(vision, "_cv2", lambda: FakeCamera())
    old = config.path("captures", "webcam-20200101-000000-aaaa.jpg")
    old.write_bytes(b"x")
    stamp = time.time() - 30 * 86400
    os.utime(old, (stamp, stamp))
    other = config.path("captures", "mine.jpg")
    other.write_bytes(b"x")
    out = json.loads(await run_tool("webcam_capture", ctx))
    assert not old.exists() and other.exists() and os.path.exists(out["saved_to"]) and "7 days" in out["note"]


async def test_hosted_transcription_is_disclosed(ctx, wav, stt):
    stt.result["backend"] = "openai"
    out = body(await run_tool("audio_transcribe", ctx, path=str(wav())))
    assert "sent to the hosted service openai" in out["privacy"]
    stt.result["backend"] = "faster-whisper"
    assert "privacy" not in body(await run_tool("audio_transcribe", ctx, path=str(wav("b.wav"))))
