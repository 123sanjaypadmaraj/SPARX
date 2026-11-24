import base64
import importlib.util
import json
import subprocess
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from sparx import config, voice
from sparx.tools import ToolError

from conftest import run_tool

RATE = voice.RATE


def tone(seconds, freq=220.0, amp=0.3, rate=RATE):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds, noise=0.001, rate=RATE):
    return (np.random.default_rng(0).standard_normal(int(seconds * rate)) * noise).astype(np.float32)


def voice_like(f0, formants, seconds=2.0, seed=0):
    """A harmonic source shaped by formant peaks: a crude stand-in for one person's voice."""
    t = np.arange(int(seconds * RATE)) / RATE
    x = np.zeros_like(t)
    for h in range(1, int(7000 / f0)):
        f = h * f0
        x += sum(np.exp(-((f - c) / 180.0) ** 2) for c in formants) * np.sin(2 * np.pi * f * t) + 0.002 * np.sin(2 * np.pi * f * t)
    x += np.random.default_rng(seed).standard_normal(len(t)) * 0.01
    return (0.3 * x / np.abs(x).max()).astype(np.float32)


class FakeStream:
    """Stands in for sounddevice.InputStream: plays back a prepared signal, then silence."""

    def __init__(self, signal):
        self.signal, self.pos = signal, 0

    def __call__(self, **kw):
        self.kw = kw
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        out = np.zeros(n, dtype=np.float32)
        part = self.signal[self.pos:self.pos + n]
        out[:len(part)] = part
        self.pos += n
        return out.reshape(-1, 1), False


class FakeSD:
    def __init__(self, signal=None):
        self.InputStream = FakeStream(signal if signal is not None else np.zeros(0, dtype=np.float32))
        self.played = []

    def play(self, samples, rate):
        self.played.append((len(samples), rate))

    def stop(self):
        pass


@pytest.fixture(autouse=True)
def no_sarvam(monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)


@pytest.fixture
def no_stt(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    for env in ("GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(env, raising=False)


# ---- VAD

def test_vad_finds_the_tone_between_silences():
    audio = np.concatenate([silence(1.0), tone(1.2), silence(1.5)])
    (start, end), = voice.segment(audio)
    assert abs(start - 1.0) < 0.1 and abs(end - 2.2) < 0.1


def test_vad_ignores_steady_noise_and_adapts_its_floor():
    vad = voice.VAD()
    noise = silence(3.0, noise=0.004)
    assert voice.segment(noise) == []
    for i in range(0, len(noise) - vad.frame, vad.frame):
        vad.feed(noise[i:i + vad.frame])
    assert 0.003 < vad.floor < 0.006 and vad.threshold > vad.min_rms


def test_vad_hangover_bridges_a_short_pause():
    audio = np.concatenate([silence(0.5), tone(0.5), silence(0.3), tone(0.5), silence(1.5)])
    assert len(voice.segment(audio)) == 1
    assert len(voice.segment(audio, hangover_ms=150)) == 2


def test_record_utterance_returns_the_speech_and_fires_barge_in(monkeypatch):
    sd = FakeSD(np.concatenate([silence(0.6), tone(1.0), silence(2.0)]))
    monkeypatch.setattr(voice, "_sd", lambda: sd)
    voice._abort.clear()
    started = []
    audio = voice.record_utterance(on_start=lambda: started.append(1))
    assert started == [1] and 0.9 < len(audio) / RATE < 2.2
    assert sd.InputStream.kw["samplerate"] == RATE and sd.InputStream.kw["channels"] == 1


def test_record_utterance_gives_up_when_nobody_speaks(monkeypatch):
    monkeypatch.setattr(voice, "_sd", lambda: FakeSD(silence(1.0)))
    voice._abort.clear()
    assert voice.record_utterance(wait_seconds=2) is None


def test_threshold_is_raised_while_sparx_is_speaking(monkeypatch):
    monkeypatch.setattr(voice, "_sd", lambda: FakeSD(np.concatenate([silence(0.5), tone(1.0, amp=0.015), silence(1.5)])))
    voice._abort.clear()
    assert voice.record_utterance(wait_seconds=4) is not None       # quiet speech is heard normally
    monkeypatch.setattr(voice, "_sd", lambda: FakeSD(np.concatenate([silence(0.5), tone(1.0, amp=0.015), silence(1.5)])))
    voice._speaking.set()
    try:
        assert voice.record_utterance(wait_seconds=4) is None       # but not over Sparx's own voice
    finally:
        voice._speaking.clear()


# ---- wake word, yes/no, conversation mode

@pytest.mark.parametrize("heard,rest", [
    ("Hey Sparx, what's the weather?", "what's the weather?"),
    ("hey spark set a timer", "set a timer"),
    ("Hey, Sparx.", ""),
    ("Heysparx open the notes", "open the notes"),
    ("हे स्पार्क्स समय क्या है", "समय क्या है"),
])
def test_wake_word_matches_fuzzily(heard, rest):
    assert voice.match_wake(heard, "hey sparx") == (True, rest)


@pytest.mark.parametrize("heard", ["what is the weather today", "hello everyone", "", "they know that already"])
def test_wake_word_rejects_other_speech(heard):
    assert voice.match_wake(heard, "hey sparx") == (False, "")


def test_wake_word_comes_from_settings():
    config.set_key("voice.wake_word", "computer")
    assert voice.match_wake("Computer, lights on") == (True, "lights on")
    assert voice.match_wake("hey sparx lights on")[0] is False


def test_yes_no():
    assert voice.yes_no("Yes, go ahead.") is True and voice.yes_no("okay") is True and voice.yes_no("हाँ") is True
    assert voice.yes_no("No.") is False and voice.yes_no("yes... no, cancel") is False and voice.yes_no("ಬೇಡ") is False
    assert voice.yes_no("maybe later") is None


def test_follow_up_window():
    c = voice.Conversation("hey sparx", window=8)
    assert c.route("what time is it", 100) == ("ignore", "")                 # no wake word, nothing open
    assert c.route("hey sparx what time is it", 100) == ("command", "what time is it")
    c.replied(110)
    assert c.route("and tomorrow?", 117) == ("command", "and tomorrow?")     # inside the window
    assert c.route("and tomorrow?", 119) == ("ignore", "")                   # window over
    c.extend(115)                                                            # the reply finished playing at 115
    assert c.route("and tomorrow?", 119)[0] == "command"
    assert c.route("hey sparx", 500) == ("prompt", "")


def test_follow_up_ends_and_mute_controls():
    c = voice.Conversation("hey sparx", window=8)
    c.replied(10)
    assert c.route("Thank you, that's all.", 12) == ("end", "")
    c.extend(13)
    assert c.route("one more thing", 13) == ("ignore", "")                   # closed until the wake word
    assert c.route("hey sparx go silent", 14) == ("mute", "") and c.muted
    c.replied(15)
    assert c.route("unmute", 16) == ("unmute", "") and not c.muted
    assert c.route("stop listening", 17) == ("end", "")
    assert c.route("hey sparx stop", 18) == ("hush", "") and c.route("hey sparx goodbye", 18) == ("quit", "")


# ---- text to speech

def test_sentence_chunking():
    chunks = voice.chunk_sentences("The first sentence is here. A second one follows it! And a third? " + "word " * 80)
    assert chunks[0] == "The first sentence is here."
    assert chunks[1] == "A second one follows it!"
    assert all(len(c) <= 221 for c in chunks) and len(chunks) >= 5
    assert voice.chunk_sentences("Hi. Ok. Sure.") == ["Hi. Ok. Sure."]      # tiny fragments are merged
    assert voice.chunk_sentences("  ") == []


def test_markdown_is_cleaned_for_speech():
    out = voice.speakable("## Plan 🚀\n**Bold** and _italic_ with `code_name`.\n- see [the docs](https://x.io/a_b) or https://y.io/z.\n"
                          "```python\nprint('hi')\n```\nDone ✅")
    assert out.splitlines() == ["Plan", "Bold and italic with code name.", "see the docs or link.",
                                "I have put the code on screen.", "Done"]
    assert "print" not in out and "http" not in out and "*" not in out


def test_numbers_and_units_are_spoken_naturally():
    out = voice.speakable("It is 5km away, 30°C, 20% off, ₹1,200 or $5.50, takes 5-10 mins at 60 km/h.")
    assert out == ("It is 5 kilometres away, 30 degrees Celsius, 20 percent off, 1,200 rupees or 5.50 dollars, "
                   "takes 5 to 10 minutes at 60 kilometres per hour.")


def test_voice_follows_the_language_of_the_text():
    config.set_key("personality", "casual")          # the sparx default has its own English voice (next test)
    assert voice.pick_voice("Hello there") == "en-IN-NeerjaNeural"
    assert voice.pick_voice("नमस्ते, आप कैसे हैं?") == "hi-IN-SwaraNeural"
    assert voice.pick_voice("ನಮಸ್ಕಾರ, ಹೇಗಿದ್ದೀರಿ?") == "kn-IN-SapnaNeural"
    assert voice.pick_voice("Hello", "en-US-GuyNeural") == "en-US-GuyNeural"
    config.set_key("voice.voice", "hi-IN-MadhurNeural")
    assert voice.pick_voice("नमस्ते दुनिया") == "hi-IN-MadhurNeural" and voice.pick_voice("Hello") == "en-IN-NeerjaNeural"


def test_sparx_persona_voice_rule():
    config.set_key("personality", "casual")
    assert voice.speech_rate() == "+0%"
    config.set_key("personality", "sparx")
    assert voice.pick_voice("Good evening, sir.") == "en-GB-RyanNeural" and voice.speech_rate() == "-4%"
    assert voice.pick_voice("नमस्ते, आप कैसे हैं?") == "hi-IN-SwaraNeural"       # other languages keep their voices
    assert voice.pick_voice("ನಮಸ್ಕಾರ, ಹೇಗಿದ್ದೀರಿ?") == "kn-IN-SapnaNeural"
    config.set_key("voice.voice", "en-US-AriaNeural")                           # an explicit choice wins
    assert voice.pick_voice("Good evening, sir.") == "en-US-AriaNeural"


def test_synthesize_uses_edge_tts_with_voice_and_rate(monkeypatch, tmp_path):
    import edge_tts
    made = []

    class Communicate:
        def __init__(self, text, voice, rate="+0%"):
            made.append((text, voice, rate))

        async def save(self, path):
            Path(path).write_bytes(b"mp3")
    monkeypatch.setattr(edge_tts, "Communicate", Communicate)
    config.set_key("personality", "sparx")
    assert voice.synthesize("At once.", out=tmp_path / "a").suffix == ".mp3"
    assert made == [("At once.", "en-GB-RyanNeural", "-4%")]


def test_synthesize_falls_back_to_the_offline_voice(monkeypatch, tmp_path):
    import edge_tts

    class Down:
        def __init__(self, *a, **kw):
            raise OSError("no network")
    monkeypatch.setattr(edge_tts, "Communicate", Down)
    monkeypatch.setattr(voice, "_synth_offline", lambda text, out: out)
    assert voice.synthesize("Hello.", out=tmp_path / "a").suffix == ".wav"


@pytest.fixture
def spoken(monkeypatch):
    """Replaces synthesis and playback: speak() runs for real, 'playing' records the sentence instead."""
    log = []
    monkeypatch.setattr(voice, "synthesize", lambda text, v=None, out=None, offline=False: Path(text))
    monkeypatch.setattr(voice, "play", lambda f: log.append(str(f)))
    monkeypatch.setattr(voice, "play_earcon", lambda kind: log.append(f"<{kind}>"))
    voice._interject.clear()
    return log


def test_speak_plays_sentences_in_order(spoken):
    assert voice.speak("This is the first sentence, quite long. This is the second sentence, also long.") is True
    assert spoken == ["This is the first sentence, quite long.", "This is the second sentence, also long."]
    assert not voice.speaking()


def test_stop_speaking_interrupts_between_sentences(spoken, monkeypatch):
    def play(f):
        spoken.append(str(f))
        voice.stop_speaking()
    monkeypatch.setattr(voice, "play", play)
    assert voice.speak("This is the first sentence, quite long. This is the second sentence, also long.") is False
    assert spoken == ["This is the first sentence, quite long."]


def test_playback_stops_when_interrupted(monkeypatch):
    sd = FakeSD()
    monkeypatch.setattr(voice, "_sd", lambda: sd)
    voice._stop.set()
    start = time.monotonic()
    voice._play_samples(np.zeros(24000 * 5, dtype=np.float32))     # five seconds of audio
    assert time.monotonic() - start < 1 and sd.played == [(120000, 24000)]
    voice._stop.clear()


def test_notification_priority_rules():
    assert voice.notification_action({"kind": "alert"}) == "interrupt"
    assert voice.notification_action({"kind": "reminder"}) == "queue"
    assert voice.notification_action({"kind": "info"}) == "skip"
    assert voice.announce({"kind": "info", "title": "t", "body": "b"}) is None
    config.set_key("voice.speak_all_notifications", True)
    assert voice.notification_action({"kind": "info"}) == "queue"


def test_reminder_waits_for_the_sentence_and_alert_interrupts(spoken, monkeypatch):
    threads = []

    def play(f):
        spoken.append(str(f))
        if str(f).startswith("Sentence one"):
            threads.append(voice.announce({"kind": "reminder", "title": "Reminder", "body": "call the dentist today"}))
            time.sleep(0.2)
    monkeypatch.setattr(voice, "play", play)
    assert voice.speak("Sentence one is being spoken now. Sentence two comes right after it.") is True
    threads[0].join(5)
    assert spoken == ["Sentence one is being spoken now.", "Reminder. call the dentist today",
                      "Sentence two comes right after it."]

    spoken.clear()

    def play_alert(f):
        spoken.append(str(f))
        if str(f).startswith("Sentence one"):
            threads.append(voice.announce({"kind": "alert", "title": "Smoke alarm", "body": "in the kitchen, check it now"}))
            time.sleep(0.3)
    monkeypatch.setattr(voice, "play", play_alert)
    assert voice.speak("Sentence one is being spoken now. Sentence two comes right after it.") is False
    threads[1].join(5)
    assert spoken == ["Sentence one is being spoken now.", "<alert>", "Smoke alarm. in the kitchen, check it now"]


def test_earcons_are_short_distinct_and_optional(monkeypatch):
    wake, done, alert = (voice.earcon(k) for k in ("wake", "done", "alert"))
    assert all(0.1 < len(e) / 24000 < 0.6 and np.abs(e).max() <= 0.5 for e in (wake, done, alert))

    def peak(x):
        return np.fft.rfftfreq(len(x), 1 / 24000)[np.argmax(np.abs(np.fft.rfft(x)))]
    half = len(wake) // 2
    assert peak(wake[:half]) < peak(wake[half:]) and peak(done[:half]) > peak(done[half:])   # rising, falling
    assert peak(alert) < 300
    sd = FakeSD()
    monkeypatch.setattr(voice, "_sd", lambda: sd)
    config.set_key("voice.earcons", False)
    voice.play_earcon("wake")
    assert sd.played == []
    config.set_key("voice.earcons", True)
    voice.play_earcon("wake")
    assert len(sd.played) == 1


# ---- speech to text

def test_backend_order(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object())
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert voice.stt_backends() == ["faster-whisper", "groq", "openai"]
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    monkeypatch.delenv("GROQ_API_KEY")
    assert voice.stt_backends() == ["openai"]


def test_no_backend_explains_both_ways(no_stt):
    with pytest.raises(ToolError) as e:
        voice.transcribe(tone(1.0))
    assert "pip install faster-whisper" in str(e.value) and "GROQ_API_KEY" in str(e.value)


def test_local_failure_falls_through_to_groq(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object())
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(voice, "_stt_local", lambda audio, language: (_ for _ in ()).throw(RuntimeError("no model")))
    seen = {}

    def handler(request: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], request.read()
        return httpx.Response(200, json={"text": " namaste duniya ", "language": "hindi",
                                         "segments": [{"start": 0.0, "end": 1.2, "text": " namaste duniya"}]})
    monkeypatch.setattr(voice, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    out = voice.transcribe(tone(1.0), language="hi")
    assert out == {"text": "namaste duniya", "language": "hi", "backend": "groq",
                   "segments": [{"start": 0.0, "end": 1.2, "text": "namaste duniya"}]}
    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions" and seen["auth"] == "Bearer gsk-test"
    assert b"whisper-large-v3" in seen["body"] and b'name="language"\r\n\r\nhi' in seen["body"] and b"RIFF" in seen["body"]


def test_all_backends_failing_reports_each(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object())
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(voice, "_stt_local", lambda audio, language: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(ToolError, match="faster-whisper: RuntimeError: boom"):
        voice.transcribe(tone(0.5))


def test_sarvam_leads_for_tamil_but_not_english(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object())
    monkeypatch.setenv("SARVAM_API_KEY", "s")
    monkeypatch.setattr(voice, "_stt_local", lambda audio, language: {"text": "local", "language": "en", "segments": []})
    monkeypatch.setattr(voice, "_stt_sarvam", lambda audio, language: {"text": "sarvam", "language": "ta", "segments": []})
    assert voice.transcribe(tone(0.5), "ta")["backend"] == "sarvam"
    assert voice.transcribe(tone(0.5), "en")["backend"] == "faster-whisper"


def test_detect_language_by_script():
    assert voice.detect_language("வணக்கம் நண்பா") == "ta"
    assert voice.detect_language("hello there") == "en"


def test_synthesize_prefers_sarvam_unless_a_voice_is_named(monkeypatch, tmp_path):
    monkeypatch.setenv("SARVAM_API_KEY", "s")
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"audios": [base64.b64encode(voice.wav_bytes(tone(0.3), 16000)).decode()]})
    monkeypatch.setattr(voice, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    out = voice.synthesize("வணக்கம்", out=tmp_path / "a")
    assert out.suffix == ".wav" and out.stat().st_size > 100
    assert seen["body"]["target_language_code"] == "ta-IN" and seen["body"]["model"] == "bulbul:v3"


def test_wav_roundtrip(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(voice.wav_bytes(tone(0.5, rate=8000), 8000))
    x = voice.load_audio(f)                      # resampled to 16 kHz
    assert abs(len(x) - 8000) <= 2 and 0.25 < np.abs(x).max() < 0.31


# ---- speaker identification

def test_speaker_embedding_similarity():
    a1, a2 = voice_like(110, (700, 1200, 2600), seed=1), voice_like(118, (700, 1200, 2600), seed=2)
    b = voice_like(210, (350, 2300, 3200), seed=3)
    ea1, ea2, eb = (voice.embed_voice(x) for x in (a1, a2, b))
    assert ea1.shape == (24,)
    assert voice.similarity(ea1, ea2) > voice.similarity(ea1, eb) + 0.02
    assert voice.similarity(ea1, ea1) == pytest.approx(1.0)


def test_enrol_and_identify():
    a, b = voice_like(110, (700, 1200, 2600), seed=1), voice_like(210, (350, 2300, 3200), seed=3)
    with pytest.raises(ToolError, match="Nobody is enrolled"):
        voice.identify_speaker(a)
    voice.enrol_speaker("Asha", [a])
    assert voice.enrol_speaker("Ravi", [b])["known_speakers"] == ["Asha", "Ravi"]
    assert config.path("voice", "speakers.json").is_file()
    assert voice.identify_speaker(voice_like(114, (700, 1200, 2600), seed=7))["speaker"] == "Asha"
    assert voice.identify_speaker(voice_like(205, (350, 2300, 3200), seed=8))["speaker"] == "Ravi"
    with pytest.raises(ToolError, match="Too little audio"):
        voice.embed_voice(tone(0.01))


# ---- tools

async def test_say_and_listen_tools(ctx, spoken, monkeypatch):
    assert await run_tool("say", ctx, text="Hello from the test suite, speaking now.") == "Spoken."
    assert spoken == ["Hello from the test suite, speaking now."]
    monkeypatch.setattr(voice, "record_utterance", lambda *a, **kw: tone(1.0))
    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {
        "text": "buy milk", "language": "en", "segments": [], "backend": "fake"})
    out = await run_tool("listen", ctx)
    assert '<untrusted source="tool:listen">' in out and "buy milk" in out


async def test_speaker_enrol_needs_consent(ctx, tmp_path):
    f = tmp_path / "asha.wav"
    f.write_bytes(voice.wav_bytes(voice_like(110, (700, 1200, 2600))))
    assert "explicit request" in await run_tool("speaker_enrol", ctx, name="Asha", consent=False, paths=[str(f)])
    assert '"enrolled": "Asha"' in await run_tool("speaker_enrol", ctx, name="Asha", consent=True, paths=[str(f)])
    assert '"speaker": "Asha"' in await run_tool("speaker_identify", ctx, path=str(f))
    assert "Deleted" in await run_tool("speaker_forget", ctx, name="Asha") and ctx.asked[-1] == "speaker_forget"


# ---- the loop

class FakeSparx:
    def __init__(self):
        self.approve, self.turns = "terminal", []

    async def turn(self, text):
        self.turns.append(text)
        return f"Answer to {text}."


async def test_voice_loop_wake_follow_up_and_exit(monkeypatch, spoken):
    heard = iter(["some chatter in the room", "hey sparx what time is it", "and tomorrow", "that's all",
                  "this is ignored now", "hey sparx goodbye"])
    monkeypatch.setattr(voice, "stt_backends", lambda: ["fake"])
    monkeypatch.setattr(voice, "record_utterance", lambda *a, **kw: tone(0.2))
    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {"text": next(heard), "language": "en"})
    monkeypatch.setattr(voice, "_greeting", lambda: "Online and ready.")
    bot = FakeSparx()
    await voice.voice_loop(bot)
    for _ in range(100):                      # replies are spoken on their own threads
        if not voice.speaking() and threading.active_count() >= 1 and "Goodbye." in spoken:
            break
        time.sleep(0.02)
    assert bot.turns == ["what time is it", "and tomorrow"]
    assert bot.approve == "terminal"          # the spoken approver is removed again
    assert spoken[0] == "Online and ready." and "Goodbye." in spoken and "<done>" in spoken
    assert voice.jobs.LISTENERS == []


async def test_spoken_approval(monkeypatch, spoken):
    answers = iter(["hmm what", "yes please"])
    monkeypatch.setattr(voice, "record_utterance", lambda *a, **kw: tone(0.2))
    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {"text": next(answers)})

    class T:
        name, risk = "calendar_add", "control"
    assert await voice.spoken_approver()(T, {"to": "a@b.c", "password": "hunter2"}) is True
    retry = spoken.index("Please say yes or no.")            # the request itself is spoken as several sentences
    request = " ".join(spoken[:retry])
    assert retry >= 1 and "Say yes to allow" in request and "calendar add" in request and "hunter2" not in request
    assert spoken.count("Please say yes or no.") == 1
    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {"text": "no"})
    assert await voice.spoken_approver()(T, {}) is False


# ---- audit: tools without a test, approval, echo, limits

async def test_voice_list_tool_with_both_engines_faked(ctx, monkeypatch):
    import sys
    import types

    async def edge_voices():
        return [{"ShortName": "en-IN-NeerjaNeural", "Locale": "en-IN", "Gender": "Female"},
                {"ShortName": "hi-IN-SwaraNeural", "Locale": "hi-IN", "Gender": "Female"}]
    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(list_voices=edge_voices))
    eng = types.SimpleNamespace(getProperty=lambda k: [types.SimpleNamespace(name="Microsoft David")])
    monkeypatch.setitem(sys.modules, "pyttsx3", types.SimpleNamespace(init=lambda: eng))
    out = json.loads(await run_tool("voice_list", ctx))
    assert [v["name"] for v in out] == ["en-IN-NeerjaNeural", "hi-IN-SwaraNeural", "Microsoft David"]
    assert out[2]["engine"] == "pyttsx3 (offline)"
    only = json.loads(await run_tool("voice_list", ctx, language="hi"))
    assert [v["name"] for v in only] == ["hi-IN-SwaraNeural"]

    async def down():
        raise OSError("offline")
    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(list_voices=down))
    monkeypatch.setitem(sys.modules, "pyttsx3", types.SimpleNamespace(init=lambda: 1 / 0))
    assert "No voices found" in await run_tool("voice_list", ctx)


async def test_spoken_yes_is_not_enough_for_risky_tools(monkeypatch, spoken):
    monkeypatch.setattr(voice, "record_utterance", lambda *a, **kw: tone(0.2))
    for risk in ("send", "delete", "spend", "exec"):
        T = type("T", (), {"name": "x_tool", "risk": risk})
        monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {"text": "yes yeah sure ok"})
        assert await voice.spoken_approver()(T, {}) is False, risk
    T = type("T", (), {"name": "x_tool", "risk": "send"})
    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {"text": "yes, confirm"})
    assert await voice.spoken_approver()(T, {}) is True
    assert any("Say confirm to allow" in s for s in spoken)


def test_own_voice_is_not_a_command():
    said = "Your meeting with Priya is at three o'clock tomorrow."
    assert voice.is_own_speech("your meeting with priya is at three", said)
    assert not voice.is_own_speech("what is the weather tomorrow", said)
    assert not voice.is_own_speech("yes", said)


async def test_voice_loop_ignores_its_own_echo(monkeypatch, spoken):
    heard = iter(["hey sparx what is my schedule", "your schedule has the standup at nine", "hey sparx goodbye"])
    monkeypatch.setattr(voice, "stt_backends", lambda: ["fake"])
    monkeypatch.setattr(voice, "record_utterance", lambda *a, **kw: tone(0.2))
    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {"text": next(heard), "language": "en"})
    monkeypatch.setattr(voice, "_greeting", lambda: "Online and ready.")

    class Bot(FakeSparx):
        async def turn(self, text):
            self.turns.append(text)
            return "Your schedule has the standup at nine, then lunch."
    bot = Bot()
    await voice.voice_loop(bot)
    assert bot.turns == ["what is my schedule"]


def test_ffmpeg_has_a_timeout_and_failure_is_an_error(monkeypatch, tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    monkeypatch.setattr(voice, "ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(voice.subprocess, "run", run)
    with pytest.raises(ToolError, match="stopped"):
        voice.load_audio(f)
    assert seen["timeout"] == voice.FFMPEG_TIMEOUT


async def test_listen_wait_is_bounded_and_says_when_audio_left_the_machine(ctx, monkeypatch):
    waits = []
    monkeypatch.setattr(voice, "record_utterance", lambda m, w=None, *a: waits.append(w) or tone(1.0))
    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {
        "text": "hello", "language": "en", "segments": [], "backend": "groq"})
    out = await run_tool("listen", ctx, wait_seconds=10 ** 9)
    assert waits == [45] and "sent to the hosted service groq" in out
    monkeypatch.setattr(voice, "transcribe", lambda audio, language=None: {
        "text": "hello", "language": "en", "segments": [], "backend": "faster-whisper"})
    assert "privacy" not in await run_tool("listen", ctx)


def test_microphone_tools_are_not_silent():
    from sparx import tools
    tools.load_all()
    for name in ("listen", "speaker_enrol", "speaker_identify", "live_translate", "sound_detect"):
        assert tools.REGISTRY[name].risk == "control", name
