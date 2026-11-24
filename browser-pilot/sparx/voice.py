"""Hands-free voice: speech-to-text, text-to-speech with barge-in, VAD, wake word, speaker id, and the voice loop.

What this is, honestly:
- Wake word: there is no always-on low-power keyword model here. The microphone is open while `sparx voice` runs,
  an energy VAD cuts out one utterance at a time, each utterance is transcribed, and the transcript is fuzzy-matched
  against the wake word. Everything said near the mic is therefore sent to the speech-to-text backend
  (local faster-whisper stays on this PC; Groq/OpenAI do not).
- Barge-in: Sparx stops talking when the VAD hears speech. On open speakers it can hear itself; the threshold is
  raised while it talks, but a headset works best. `sparx config voice.barge_in false` turns it off.
- Speaker identification is a mean/variance MFCC fingerprint compared by cosine similarity. It tells a few enrolled
  household voices apart in a quiet room; it is not biometric security and must never gate anything sensitive.
- Small Whisper models are weak at Hindi and Kannada; set voice.stt_model to "small"/"medium", or use Groq.
"""
import asyncio
import base64
import difflib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np

from . import config, db, jobs, security
from .tools import A, B, I, S, ToolError, tool

RATE = 16000
VOICES = {"en": "en-IN-NeerjaNeural", "ta": "ta-IN-PallaviNeural", "hi": "hi-IN-SwaraNeural",
          "kn": "kn-IN-SapnaNeural", "te": "te-IN-ShrutiNeural", "ml": "ml-IN-SobhanaNeural",
          "bn": "bn-IN-TanishaaNeural", "gu": "gu-IN-DhwaniNeural"}
SPARX_VOICE = "en-GB-RyanNeural"   # English voice of the "sparx" personality unless the user picked one
LANGUAGES = {"english": "en", "tamil": "ta", "hindi": "hi", "kannada": "kn", "telugu": "te", "malayalam": "ml",
             "bengali": "bn", "gujarati": "gu"}
SCRIPTS = {"ta": "஀-௿", "hi": "ऀ-ॿ", "kn": "ಀ-೿", "te": "ఀ-౿", "ml": "ഀ-ൿ",
           "bn": "ঀ-৿", "gu": "઀-૿"}   # Unicode block each language is written in
STT_API = {  # name: (key env, base url, model)
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "whisper-large-v3"),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "whisper-1"),
}
STT_HELP = ("Speech-to-text is not set up. Enable it one of two ways: (1) local and private: "
            "`pip install faster-whisper`; (2) hosted: put GROQ_API_KEY (free tier, whisper-large-v3) or "
            "OPENAI_API_KEY in ~/.sparx/.env.")
API_CHUNK = 600 * RATE     # hosted endpoints cap uploads at 25 MB; 10 minutes of 16 kHz mono WAV is 19 MB
FOLLOW_UP = 8.0            # seconds after a reply during which no wake word is needed
BARGE_BOOST = 4.0          # VAD threshold multiplier while Sparx itself is talking
FFMPEG_TIMEOUT = 600       # seconds before a stuck ffmpeg is killed
SPEAKER_MATCH = 0.35       # centred cosine similarity needed to name a speaker


# ---- audio files and arrays

def resolve_file(path: str) -> Path:
    """An existing file inside the user's home, Sparx's home or an allowed root."""
    p = Path(str(path)).expanduser().resolve()
    roots = [Path.home(), config.HOME, Path(tempfile.gettempdir()), *map(Path, config.settings()["allowed_roots"])]
    if not any(p.is_relative_to(Path(r).resolve()) for r in roots):
        raise ToolError(f"{p} is outside the folders Sparx may read (add one with `sparx config allowed_roots`).")
    if why := security.secret_path(p) or security.secret_path(path) or security.control_path(p) or security.control_path(path):
        raise ToolError(f"Refused: {p} is off limits because {why}.")
    if not p.is_file():
        raise ToolError(f"File not found: {p}")
    return p


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def wav_bytes(samples: np.ndarray, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _read_wav(path: Path, rate: int) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        width, channels, src = w.getsampwidth(), w.getnchannels(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width not in (1, 2, 4):
        raise wave.Error(f"unsupported sample width {width}")
    x = np.frombuffer(raw, {1: "u1", 2: "<i2", 4: "<i4"}[width]).astype(np.float32)
    x = (x - 128) / 128 if width == 1 else x / float(2 ** (8 * width - 1))
    x = x.reshape(-1, channels).mean(axis=1)
    if src != rate and len(x):
        x = np.interp(np.arange(0, len(x), src / rate), np.arange(len(x)), x)
    return x.astype(np.float32)


def load_audio(src, rate: int = RATE) -> np.ndarray:
    """Mono float32 samples at `rate` from an array or any audio/video file (WAV natively, the rest via ffmpeg)."""
    if isinstance(src, np.ndarray):
        return src.astype(np.float32).reshape(-1)
    path = Path(str(src))
    if path.suffix.lower() == ".wav":
        try:
            return _read_wav(path, rate)
        except (wave.Error, EOFError):
            pass  # compressed or float WAV: let a real decoder handle it
    if ffmpeg():
        try:
            r = subprocess.run([ffmpeg(), "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
                               capture_output=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ToolError(f"ffmpeg took longer than {FFMPEG_TIMEOUT} s to read {path.name} and was stopped.")
        if r.returncode or not r.stdout:
            raise ToolError(f"ffmpeg could not read audio from {path.name}: {r.stderr.decode(errors='replace')[-300:]}")
        return np.frombuffer(r.stdout, "<i2").astype(np.float32) / 32768
    if importlib.util.find_spec("faster_whisper"):
        from faster_whisper.audio import decode_audio
        return decode_audio(str(path), sampling_rate=rate)
    raise ToolError(f"Cannot decode {path.suffix or 'this'} audio: ffmpeg is not on PATH. Install it "
                    "(`winget install Gyan.FFmpeg`) or give a .wav file.")


# ---- speech to text

def stt_backends() -> list[str]:
    """Usable speech-to-text backends, in the order they are tried."""
    local = ["faster-whisper"] if importlib.util.find_spec("faster_whisper") else []
    sarvam = ["sarvam"] if config.secret("SARVAM_API_KEY") else []
    return local + sarvam + [name for name, (env, _b, _m) in STT_API.items() if config.secret(env)]


_whisper = None


def _stt_local(audio, language):
    global _whisper
    from faster_whisper import WhisperModel
    name = config.settings()["voice"]["stt_model"]
    if _whisper is None or _whisper[0] != name:   # first use downloads the model from Hugging Face
        _whisper = (name, WhisperModel(name, device="cpu", compute_type="int8"))
    segs, info = _whisper[1].transcribe(audio if isinstance(audio, np.ndarray) else str(audio),
                                        language=language, vad_filter=True)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs]
    return {"text": " ".join(s["text"] for s in segs), "language": info.language, "segments": segs}


def _http() -> httpx.Client:
    return httpx.Client(timeout=120)


def _stt_api(name, audio, language):
    env, base, model = STT_API[name]
    samples = load_audio(audio)
    texts, segs, lang = [], [], ""
    with _http() as client:
        for off in range(0, len(samples), API_CHUNK):
            r = client.post(f"{base}/audio/transcriptions", headers={"Authorization": f"Bearer {config.secret(env)}"},
                            files={"file": ("audio.wav", wav_bytes(samples[off:off + API_CHUNK]), "audio/wav")},
                            data={"model": model, "response_format": "verbose_json",
                                  **({"language": language} if language else {})})
            r.raise_for_status()
            j, t0 = r.json(), off / RATE
            texts.append((j.get("text") or "").strip())
            lang = lang or LANGUAGES.get(str(j.get("language", "")).lower(), j.get("language") or "")
            segs += [{"start": round(t0 + s["start"], 2), "end": round(t0 + s["end"], 2), "text": s["text"].strip()}
                     for s in j.get("segments") or []]
    return {"text": " ".join(t for t in texts if t), "language": lang or language or "", "segments": segs}


SARVAM_CHUNK = 25 * RATE   # the Sarvam REST endpoint accepts at most 30 s per request
SARVAM_LANGS = {c: f"{c}-IN" for c in VOICES}


def _stt_sarvam(audio, language):
    samples = load_audio(audio)
    texts, lang = [], ""
    with _http() as client:
        for off in range(0, len(samples), SARVAM_CHUNK):
            r = client.post("https://api.sarvam.ai/speech-to-text",
                            headers={"api-subscription-key": config.secret("SARVAM_API_KEY")},
                            files={"file": ("audio.wav", wav_bytes(samples[off:off + SARVAM_CHUNK]), "audio/wav")},
                            data={"model": "saarika:v2.5", "language_code": SARVAM_LANGS.get(language or "", "unknown")})
            r.raise_for_status()
            j = r.json()
            texts.append((j.get("transcript") or "").strip())
            lang = lang or str(j.get("language_code") or "").split("-")[0]
    return {"text": " ".join(t for t in texts if t), "language": lang or language or "", "segments": []}


def left_machine(backend: str) -> bool:
    """True when this speech-to-text backend sent the audio to a hosted service."""
    return backend != "faster-whisper"


def transcribe(audio, language: str | None = None) -> dict:
    """Speech to text for a file path or a 16 kHz mono float array.

    Returns {"text", "language", "segments": [{"start", "end", "text"}], "backend"}. `language` is a hint
    (en, hi, kn, ...); None auto-detects. Backends are tried in stt_backends() order; a failing one falls through.
    """
    backends = stt_backends()
    if not backends:
        raise ToolError(STT_HELP)
    if language != "en" and "sarvam" in backends:   # small local Whisper is weak at Tamil, Hindi and the like
        backends = ["sarvam"] + [b for b in backends if b != "sarvam"]
    errors = []
    for name in backends:
        try:
            if name == "faster-whisper":
                out = _stt_local(audio, language or None)
            elif name == "sarvam":
                out = _stt_sarvam(audio, language)
            else:
                out = _stt_api(name, audio, language)
            return {**out, "backend": name}
        except ToolError:
            raise
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
    raise ToolError("Speech-to-text failed. " + " | ".join(errors))


# ---- voice activity detection and recording

class VAD:
    """Energy VAD with an adaptive noise floor. feed() one frame at a time; it returns "start", "end" or None."""

    def __init__(self, rate: int = RATE, frame_ms: int = 30, ratio: float = 3.0, min_rms: float = 0.006,
                 start_ms: int = 90, hangover_ms: int = 750):
        self.frame = rate * frame_ms // 1000
        self.ratio, self.min_rms, self.boost = ratio, min_rms, 1.0
        self.start_frames, self.hang_frames = max(1, start_ms // frame_ms), max(1, hangover_ms // frame_ms)
        self.floor, self.active, self.run, self.quiet = min_rms / ratio, False, 0, 0

    @property
    def threshold(self) -> float:
        return max(self.min_rms, self.floor * self.ratio) * self.boost

    def feed(self, frame: np.ndarray) -> str | None:
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64)))) if len(frame) else 0.0
        loud = rms > self.threshold
        if self.active:
            self.quiet = 0 if loud else self.quiet + 1
            self.floor += 0.002 * (rms - self.floor)   # creep up, so a new steady noise cannot hold speech open forever
            if self.quiet >= self.hang_frames:
                self.active, self.run = False, 0
                return "end"
            return None
        self.run = self.run + 1 if loud else 0
        self.floor += (0.01 if loud else 0.05) * (rms - self.floor)
        if self.run >= self.start_frames:
            self.active, self.quiet = True, 0
            return "start"
        return None


def segment(samples: np.ndarray, rate: int = RATE, **vad_args) -> list[tuple[float, float]]:
    """(start, end) seconds of each stretch of speech/sound in a recording."""
    vad = VAD(rate, **vad_args)
    n, out, start = vad.frame, [], None
    for i in range(0, len(samples) - n + 1, n):
        ev = vad.feed(samples[i:i + n])
        if ev == "start":
            start = max(0, i - (vad.start_frames - 1) * n)
        elif ev == "end":
            out.append((start / rate, (i + n - vad.hang_frames * n) / rate))
            start = None
    if start is not None:
        out.append((start / rate, len(samples) / rate))
    return out


def _sd():
    try:
        import sounddevice
        return sounddevice
    except (ImportError, OSError) as e:
        raise ToolError(f"Microphone and speaker access needs sounddevice with PortAudio (pip install sounddevice): {e}")


_abort = threading.Event()       # set to make any recording in progress return


def record_utterance(max_seconds: float = 20, wait_seconds: float | None = None, on_start=None) -> np.ndarray | None:
    """Block until one utterance has been spoken and return it (None if nobody spoke within wait_seconds)."""
    vad = VAD()
    n, pre, chunks, seen = vad.frame, deque(maxlen=10), [], 0
    with _sd().InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=n) as stream:
        while not _abort.is_set():
            frame = np.asarray(stream.read(n)[0], dtype=np.float32).reshape(-1)
            seen += 1
            vad.boost = BARGE_BOOST if _speaking.is_set() else 1.0
            ev = vad.feed(frame)
            if ev == "start":
                chunks, seen = [*pre, frame], 0
                if on_start:
                    on_start()
            elif vad.active or ev == "end":
                chunks.append(frame)
            else:
                pre.append(frame)
            if ev == "end" or (vad.active and seen * n / RATE > max_seconds):
                return np.concatenate(chunks)
            if not vad.active and wait_seconds and seen * n / RATE > wait_seconds:
                return None
    return None


def record_seconds(seconds: float) -> np.ndarray:
    sd = _sd()
    audio = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="float32")
    sd.wait()
    return np.asarray(audio, dtype=np.float32).reshape(-1)


# ---- text to speech

_stop = threading.Event()
_speaking = threading.Event()
_speak_lock = threading.Lock()
_interject: deque = deque()   # reminders waiting for the sentence being spoken to end
_last_spoke = 0.0

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
_UNITS = {"km/h": "kilometres per hour", "kmph": "kilometres per hour", "km": "kilometres", "kg": "kilograms",
          "cm": "centimetres", "mm": "millimetres", "ml": "millilitres", "GB": "gigabytes", "MB": "megabytes",
          "KB": "kilobytes", "ms": "milliseconds", "°C": "degrees Celsius", "°F": "degrees Fahrenheit",
          "%": "percent", "mins": "minutes", "min": "minutes", "hrs": "hours", "hr": "hours"}
_UNIT_RE = re.compile(r"(\d)\s?(" + "|".join(map(re.escape, sorted(_UNITS, key=len, reverse=True))) + r")(?![A-Za-z])")
_MONEY = {"₹": "rupees", "Rs.": "rupees", "Rs": "rupees", "$": "dollars", "€": "euros", "£": "pounds"}
_MONEY_RE = re.compile("(" + "|".join(map(re.escape, _MONEY)) + r")\s?(\d[\d,]*(?:\.\d+)?)(\s(?:lakh|crore|thousand|million|billion))?")


def speakable(text: str) -> str:
    """Text as it should be said aloud: no Markdown, code, URLs or emoji; simple numbers and units spelled out."""
    t = re.sub(r"```.*?(```|\Z)", "\nI have put the code on screen.\n", text or "", flags=re.S)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)                     # [label](url) -> label
    t = re.sub(r"(?:https?://|www\.)[^\s<>]*[^\s<>.,;:!?)]", "link", t)
    t = re.sub(r"^\s*\|?[\s:|-]*-{3,}[\s:|-]*$", "", t, flags=re.M)      # table rules and horizontal lines
    t = re.sub(r"^\s*(#{1,6}|>|[-*+•])\s+", "", t, flags=re.M)           # headings, quotes, bullets
    t = re.sub(r"[ \t]*\|[ \t]*", ", ", re.sub(r"^[ \t]*\|[ \t]*|[ \t]*\|[ \t]*$", "", t, flags=re.M))   # table rows
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\*+|~~", "", t).replace("_", " ")
    t = _EMOJI.sub("", t)
    t = _MONEY_RE.sub(lambda m: f"{m.group(2)}{m.group(3) or ''} {_MONEY[m.group(1)]}", t)
    t = _UNIT_RE.sub(lambda m: f"{m.group(1)} {_UNITS[m.group(2)]}", t)
    t = re.sub(r"(?<=\d)\s?[-–]\s?(?=\d)", " to ", t)                    # 5-10 -> 5 to 10
    t = re.sub(r"^[ \t,]+|[ \t,]+$", "", t, flags=re.M)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def chunk_sentences(text: str, max_chars: int = 220) -> list[str]:
    """Split text into sentence-sized spoken pieces so the first one can be synthesised and played at once."""
    out = []
    for part in re.split(r"(?<=[.!?।])\s+|\n+", speakable(text)):
        part = part.strip()
        while len(part) > max_chars:   # a very long sentence: break at the last comma or space that fits
            cut = max(part.rfind(", ", 0, max_chars), part.rfind(" ", 0, max_chars))
            cut = cut if cut > 0 else max_chars
            out.append(part[:cut + 1].strip())
            part = part[cut + 1:].strip()
        if part:
            if out and len(out[-1]) < 16 and len(out[-1]) + len(part) < max_chars:   # "Hi." is not worth a request
                out[-1] += " " + part
            else:
                out.append(part)
    return out


def detect_language(text: str) -> str:
    """en, ta, hi, kn, ... from the script the text is written in."""
    counts = {code: len(re.findall(f"[{block}]", text)) for code, block in SCRIPTS.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] > len(text) * 0.2 else "en"


def pick_voice(text: str, voice: str | None = None) -> str:
    """Explicit voice, else the user's configured voice when it speaks the text's language, else the default for it.

    With personality "sparx" and the voice setting left at its default, English is spoken by SPARX_VOICE.
    """
    if voice:
        return voice
    lang, s = detect_language(text), config.settings()
    mine = s["voice"]["voice"]
    if lang == "en" and s["personality"] == "sparx" and mine == config.DEFAULTS["voice"]["voice"]:
        return SPARX_VOICE
    return mine if mine.lower().startswith(lang) else VOICES[lang]


def speech_rate() -> str:
    """edge-tts rate: slightly measured for the sparx persona."""
    return "-4%" if config.settings()["personality"] == "sparx" else "+0%"


def _run(coro):
    """Run a coroutine to completion from sync code, whether or not this thread already has an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _synth_offline(text: str, out: Path) -> Path:
    try:
        import pyttsx3
    except ImportError:
        raise ToolError("No speech voice available: edge-tts could not be reached and pyttsx3 is not installed "
                        "(pip install pyttsx3).")
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.ole32.CoInitialize(None)   # SAPI needs COM on whichever thread speaks
    engine = pyttsx3.init()
    engine.save_to_file(text, str(out))
    engine.runAndWait()
    engine.stop()
    if not out.is_file() or out.stat().st_size < 100:
        raise ToolError("The offline voice (pyttsx3) produced no audio.")
    return out


def _synth_sarvam(text: str, target: Path) -> Path:
    code = SARVAM_LANGS[detect_language(text)]
    parts = []
    with _http() as client:
        for piece in chunk_sentences(text, 500):
            r = client.post("https://api.sarvam.ai/text-to-speech",
                            headers={"api-subscription-key": config.secret("SARVAM_API_KEY")},
                            json={"text": piece, "target_language_code": code, "model": "bulbul:v3"})
            r.raise_for_status()
            parts.append(base64.b64decode(r.json()["audios"][0]))
    with wave.open(str(target), "wb") as w:
        for i, raw in enumerate(parts):
            with wave.open(io.BytesIO(raw)) as src:
                if i == 0:
                    w.setparams(src.getparams())
                w.writeframes(src.readframes(src.getnframes()))
    return target


def synthesize(text: str, voice: str | None = None, out: Path | None = None, offline: bool = False) -> Path:
    """Text to an audio file: Sarvam WAV (when keyed and no edge voice was asked for), edge-tts MP3, or a pyttsx3 WAV
    when those fail or offline=True."""
    stem = Path(out).with_suffix("") if out else Path(tempfile.mkdtemp(prefix="sparx-tts-")) / "speech"
    if not offline and not voice and config.secret("SARVAM_API_KEY"):
        try:
            return _synth_sarvam(text, stem.with_suffix(".wav"))
        except Exception:
            pass
    if not offline:
        try:
            import edge_tts
            target = stem.with_suffix(".mp3")
            _run(edge_tts.Communicate(text, pick_voice(text, voice), rate=speech_rate()).save(str(target)))
            if target.stat().st_size > 0:
                return target
        except Exception:
            pass
    return _synth_offline(text, stem.with_suffix(".wav"))


def _play_mci(path: Path):
    """Windows-native playback for when nothing can decode the file into samples."""
    import ctypes
    mci = ctypes.windll.winmm.mciSendStringW
    buf = ctypes.create_unicode_buffer(64)
    if mci(f'open "{path}" alias sparx_tts', None, 0, None):
        raise ToolError(f"Windows could not play {path.name}.")
    try:
        mci("play sparx_tts", None, 0, None)
        while not _stop.is_set():
            mci("status sparx_tts mode", buf, 64, None)
            if buf.value != "playing":
                break
            time.sleep(0.03)
    finally:
        mci("close sparx_tts", None, 0, None)


def _play_samples(samples: np.ndarray, rate: int = 24000, interruptible: bool = True):
    sd = _sd()
    sd.play(samples, rate)
    end = time.monotonic() + len(samples) / rate + 0.15
    while time.monotonic() < end and not (interruptible and _stop.is_set()):
        time.sleep(0.02)
    sd.stop()


def play(path: Path):
    """Play an audio file and return when it ends or stop_speaking() is called."""
    try:
        samples = load_audio(path, 24000)
    except ToolError:
        if sys.platform != "win32":
            raise
        return _play_mci(Path(path))
    _play_samples(samples)


def earcon(kind: str, rate: int = 24000) -> np.ndarray:
    """A short generated cue: "wake" rises, "done" falls, "alert" is two low pulses."""
    notes = {"wake": (660, 880), "done": (880, 587), "alert": (233, 233)}[kind]
    n = int(rate * (0.16 if kind == "alert" else 0.11))
    t = np.arange(n) / rate
    env = np.minimum(1, np.minimum(t, t[::-1]) / 0.015)   # 15 ms fades, so there are no clicks
    gap = np.zeros(int(rate * (0.09 if kind == "alert" else 0.02)), dtype=np.float32)
    tones = [(0.22 * env * (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t))).astype(np.float32)
             for f in notes]
    return np.concatenate([tones[0], gap, tones[1]])


def play_earcon(kind: str):
    """Play a cue unless voice.earcons is off. A missing sound device is not worth failing over."""
    if config.settings()["voice"].get("earcons", True):
        try:
            _play_samples(earcon(kind), interruptible=False)
        except Exception:
            pass


def speak(text: str, voice: str | None = None, cue: str | None = None) -> bool:
    """Say text aloud (blocking). False if it was interrupted by stop_speaking().

    Sentences are synthesised one ahead of playback, so sound starts after the first sentence is ready.
    Reminders queued by announce() are slipped in between two sentences. `cue` plays an earcon first.
    """
    global _last_spoke
    chunks = chunk_sentences(text)
    if not chunks:
        return True
    with _speak_lock:
        _stop.clear()
        _speaking.set()
        tmp = Path(tempfile.mkdtemp(prefix="sparx-tts-"))
        state = {"offline": False}

        def make(name, chunk, v=voice):
            if _stop.is_set():
                return None
            f = synthesize(chunk, v, tmp / str(name), state["offline"])
            state["offline"] = f.suffix == ".wav"   # edge-tts is down: do not wait for it again
            return f
        try:
            if cue:
                play_earcon(cue)
            with ThreadPoolExecutor(1) as pool:
                for fut in [pool.submit(make, i, c) for i, c in enumerate(chunks)]:
                    f = fut.result()
                    if f is None or _stop.is_set():
                        break
                    play(f)
                    while _interject and not _stop.is_set():
                        for j, c in enumerate(chunk_sentences(_interject.popleft())):
                            f = pool.submit(make, f"r{j}", c, None).result()
                            if f:
                                play(f)
            return not _stop.is_set()
        finally:
            _speaking.clear()
            _last_spoke = time.monotonic()
            shutil.rmtree(tmp, ignore_errors=True)


def stop_speaking():
    """Cut off whatever is being spoken right now (barge-in)."""
    _stop.set()


def speaking() -> bool:
    return _speaking.is_set()


def notification_action(n: dict) -> str:
    """How a notification is voiced: "interrupt" (alerts), "queue" (reminders: after the current sentence), "skip"."""
    if n.get("kind") == "alert":
        return "interrupt"
    if n.get("kind") == "reminder" or config.settings()["voice"].get("speak_all_notifications"):
        return "queue"
    return "skip"


def announce(n: dict) -> threading.Thread | None:
    """Speak a notification according to notification_action(), on its own thread (returned so callers can join)."""
    action, text = notification_action(n), f"{n.get('title', '')}. {n.get('body', '')}"
    if action == "skip":
        return None

    def run():
        if action == "interrupt":
            stop_speaking()
            speak(text, cue="alert")
            return
        _interject.append(text)
        while speaking():
            time.sleep(0.05)
        try:
            _interject.remove(text)   # still there: nothing was being said, or it was cut off before reaching it
        except ValueError:
            return                    # the speech in progress said it between two sentences
        speak(text)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def list_voices(language: str = "") -> list[dict]:
    """Available voices: edge-tts neural voices when online, plus the offline voices installed on this PC."""
    out = []
    try:
        import edge_tts
        out = [{"name": v["ShortName"], "locale": v["Locale"], "gender": v.get("Gender", ""), "engine": "edge-tts"}
               for v in _run(edge_tts.list_voices())]
    except Exception:
        pass
    try:
        import pyttsx3
        out += [{"name": v.name, "locale": "", "gender": "", "engine": "pyttsx3 (offline)"}
                for v in pyttsx3.init().getProperty("voices")]
    except Exception:
        pass
    if language:
        out = [v for v in out if v["locale"].lower().startswith(language.lower())]
    return out


# ---- wake word and yes/no

WAKE_ALIASES = {"hey sparx": ("हे स्पार्क्स", "हेय स्पार्क्स", "ಹೇ ಸ್ಪಾರ್ಕ್ಸ್", "ಹೇ ಸ್ಪಾರ್ಕ್ಸ")}   # how Whisper writes it in hi / kn
YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "allow", "approve", "approved", "confirm", "proceed",
       "haan", "han", "ha", "हाँ", "हां", "जी", "ठीक", "howdu", "haudu", "ಹೌದು", "ಸರಿ"}
NO = {"no", "nope", "nah", "deny", "denied", "cancel", "stop", "dont", "don't", "never", "nahi", "nahin", "नहीं",
      "मत", "illa", "beda", "ಇಲ್ಲ", "ಬೇಡ"}


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", (text or "").lower())


def match_wake(text: str, wake: str | None = None, cutoff: float = 0.72) -> tuple[bool, str]:
    """Does the transcript start with the wake word (fuzzily)? Returns (matched, the rest of the utterance)."""
    wake = (wake or config.settings()["voice"]["wake_word"]).lower().strip()
    raw = (text or "").split()
    heard = [" ".join(_words(w)) for w in raw]
    best = (0.0, 0)
    for phrase in (wake, *WAKE_ALIASES.get(wake, ())):
        target = " ".join(_words(phrase))
        size = len(phrase.split())
        for n in (size, size + 1, size - 1):   # "hey sparx" can come back as "hey, spar x" or "heysparx"
            if 0 < n <= len(heard):
                got = " ".join(w for w in heard[:n] if w)
                score = difflib.SequenceMatcher(None, got, target).ratio()
                if score >= cutoff and score > best[0]:
                    best = (score, n)
    if best[1]:
        return True, " ".join(raw[best[1]:]).lstrip(" ,.!?-")
    return False, ""


def yes_no(text: str) -> bool | None:
    """True / False for a spoken yes / no in English, Hindi or Kannada; None when it is neither. No wins a tie."""
    words = set(_words(text)) | set((text or "").lower().split())
    if "go ahead" in (text or "").lower():
        words.add("yes")
    return False if words & NO else True if words & YES else None


# ---- speaker identification (consent-based)

def embed_voice(samples: np.ndarray, rate: int = RATE) -> np.ndarray:
    """A 24-number voice fingerprint: mean and spread of 12 MFCCs over the voiced frames."""
    x = load_audio(samples, rate).astype(np.float64)
    n, hop, nfft, mels = rate * 25 // 1000, rate * 10 // 1000, 512, 26
    if len(x) < n * 4:
        raise ToolError("Too little audio to recognise a voice; a few seconds of speech are needed.")
    x = np.append(x[0], x[1:] - 0.97 * x[:-1])
    frames = x[np.arange(n)[None, :] + np.arange(0, len(x) - n + 1, hop)[:, None]] * np.hamming(n)
    power = np.abs(np.fft.rfft(frames, nfft)) ** 2
    energy = power.sum(axis=1)
    power = power[energy > 0.05 * np.percentile(energy, 95)]
    if len(power) < 4:
        raise ToolError("That recording is silent.")
    mel = lambda hz: 2595 * np.log10(1 + hz / 700)
    edges = 700 * (10 ** (np.linspace(mel(60), mel(rate / 2), mels + 2) / 2595) - 1)
    bins = np.fft.rfftfreq(nfft, 1 / rate)
    bank = np.maximum(0, np.minimum((bins - edges[:-2, None]) / (edges[1:-1, None] - edges[:-2, None]),
                                    (edges[2:, None] - bins) / (edges[2:, None] - edges[1:-1, None])))
    logmel = np.log(power @ bank.T + 1e-10)
    dct = np.cos(np.pi / mels * (np.arange(mels) + 0.5)[None, :] * np.arange(1, 13)[:, None])
    mfcc = logmel @ dct.T
    return np.concatenate([mfcc.mean(axis=0), mfcc.std(axis=0)])


def similarity(a: np.ndarray, b: np.ndarray, centre: np.ndarray | None = None) -> float:
    """Cosine similarity of two fingerprints, optionally after removing what all enrolled voices share."""
    a, b = (np.asarray(v, dtype=np.float64) - (0 if centre is None else centre) for v in (a, b))
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def _speakers() -> dict:
    try:
        return json.loads(config.path("voice", "speakers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def enrol_speaker(name: str, samples: list) -> dict:
    """Store a voice fingerprint for `name` from recordings (paths or arrays). Only the 24 numbers are kept, no audio.

    Call this only when the person asked to be enrolled. Accuracy is limited (see the module docstring).
    """
    if not samples:
        raise ToolError("Enrolment needs at least one recording of the person speaking.")
    known = _speakers()
    known[name] = {"embedding": np.mean([embed_voice(s) for s in samples], axis=0).round(4).tolist(),
                   "samples": len(samples), "enrolled": db.now()}
    config.path("voice", "speakers.json").write_text(json.dumps(known, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"enrolled": name, "samples": len(samples), "known_speakers": sorted(known)}


def identify_speaker(audio) -> dict:
    """Best guess at which enrolled speaker is talking: {"speaker" (None if unsure), "confidence", "scores"}."""
    known = _speakers()
    if not known:
        raise ToolError("Nobody is enrolled. Someone must ask to be enrolled first (speaker_enrol).")
    e = embed_voice(audio)
    vecs = {k: np.array(v["embedding"]) for k, v in known.items()}
    # with several voices enrolled, compare what distinguishes them; with one, only raw closeness is available
    centre = np.mean(list(vecs.values()), axis=0) if len(vecs) > 1 else None
    scores = {k: round(similarity(e, v, centre), 3) for k, v in vecs.items()}
    best = max(scores, key=scores.get)
    ok = scores[best] >= (SPEAKER_MATCH if centre is not None else 0.9)
    return {"speaker": best if ok else None, "confidence": scores[best], "scores": scores,
            "note": "Approximate voice match; not suitable for security decisions."}


# ---- the voice loop

END_FOLLOW_UP = ("thats all", "that is all", "thatll be all", "that will be all", "stop listening")
MUTE, UNMUTE = ("mute", "go silent", "be silent", "silent mode"), ("unmute", "speak again", "you can speak")
HUSH = ("stop", "quiet", "be quiet", "cancel", "never mind", "nevermind")
QUIT = ("goodbye", "good bye", "exit", "quit", "exit voice mode")


class Conversation:
    """Decides who an utterance is for: wake word, the follow-up window after a reply, and the spoken controls.

    Pure logic (the clock is passed in). route() returns (action, command) with action one of:
    ignore | prompt (wake word alone) | command | end (close the follow-up) | mute | unmute | hush | quit.
    """

    def __init__(self, wake: str, window: float = FOLLOW_UP):
        self.wake, self.window, self.muted, self.closed, self.last = wake, window, False, True, 0.0

    def replied(self, now: float):
        """Sparx just answered: the follow-up window runs from here."""
        self.closed, self.last = False, now

    def extend(self, now: float):
        """Speech ended later than replied() knew; never reopens a window the user closed."""
        if not self.closed:
            self.last = max(self.last, now)

    def open(self, now: float) -> bool:
        return not self.closed and now - self.last <= self.window

    def route(self, text: str, now: float) -> tuple[str, str]:
        woke, rest = match_wake(text, self.wake)
        if not woke and not self.open(now):
            return "ignore", ""
        said = rest if woke else text.strip()
        key = " ".join(_words(said.replace("'", "").replace("’", "")))
        if not key:
            return ("prompt" if woke else "ignore"), ""
        if key.endswith(END_FOLLOW_UP):
            self.closed = True
            return "end", ""
        if key in MUTE or key in UNMUTE:
            self.muted = key in MUTE
            return ("mute" if self.muted else "unmute"), ""
        if key in HUSH:
            return "hush", ""
        if key in QUIT:
            return "quit", ""
        return "command", said


def is_own_speech(heard: str, said: str) -> bool:
    """Is this transcript (mostly) what Sparx itself just said, picked up again by the microphone?"""
    words, known = _words(heard), set(_words(said))
    return len(words) >= 3 and sum(w in known for w in words) / len(words) >= 0.8


def _brief(args: dict) -> str:
    parts = [f"{k.replace('_', ' ')} {str(v)[:60]}" for k, v in security.redact(args).items() if v not in ("", None)]
    return ", ".join(parts[:3])


STRICT_RISKS = ("send", "delete", "spend", "exec")


def spoken_approver(language: str | None = None):
    """An approver for Sparx.approve that reads the request aloud and listens for yes or no (default: no)."""
    async def approve(t, args) -> bool:
        ask = f"I need your permission to {t.name.replace('_', ' ')}"
        detail = _brief(args)
        strict = getattr(t, "risk", "") in STRICT_RISKS   # anyone in the room can say "yes": these need "confirm"
        word = "confirm" if strict else "yes"
        prompt = f"{ask}: {detail}. Say {word} to allow, or no." if detail else f"{ask}. Say {word} to allow, or no."
        print(f"\x1b[33m{prompt}\x1b[0m", flush=True)
        for attempt in range(2):
            await asyncio.to_thread(speak, prompt if attempt == 0 else f"Please say {word} or no.")
            audio = await asyncio.to_thread(record_utterance, 6, 10)
            if audio is None:
                continue
            heard = (await asyncio.to_thread(transcribe, audio, language))["text"]
            verdict = yes_no(heard)
            if verdict and strict and "confirm" not in _words(heard):
                verdict = None
            if verdict is not None:
                return verdict
        await asyncio.to_thread(speak, "I did not hear a yes, so I will not do it.")
        return False
    return approve


def _greeting() -> str:
    try:
        from .tools import sparx
        return str(sparx.greeting()) or "Online and ready."
    except Exception:
        return "Online and ready."


async def voice_loop(sparx):
    """Greeting, then wake word -> listen -> sparx.turn -> speak, until Ctrl+C or "goodbye".

    After each reply the conversation stays open for voice.follow_up_seconds without the wake word.
    See the module docstring for what the wake word really is.
    """
    s = config.settings()
    v, name = s["voice"], s["name"]
    hint = s["language"] if s["language"] in VOICES and s["language"] != "en" else None   # None = auto-detect
    barge = v.get("barge_in", True)
    if not stt_backends():
        print(STT_HELP)
        return
    conv = Conversation(v["wake_word"], float(v.get("follow_up_seconds", FOLLOW_UP)))
    spoken, old_approve, last_answer = set(), sparx.approve, ""
    last_note = (db.one("SELECT MAX(id) AS m FROM notifications") or {}).get("m") or 0

    def on_note(n: dict):
        if n.get("id") not in spoken:
            spoken.add(n.get("id"))
            announce(n)

    async def other_processes():   # reminders raised by `sparx daemon` land in the table, not in our LISTENERS
        nonlocal last_note
        while True:
            await asyncio.sleep(5)
            for n in db.q("SELECT * FROM notifications WHERE id > ? ORDER BY id", (last_note,)):
                last_note = n["id"]
                on_note(n)

    async def hear(wait=None, interruptible=False):
        audio = await asyncio.to_thread(record_utterance, 20, wait, stop_speaking if interruptible else None)
        return None if audio is None else await asyncio.to_thread(transcribe, audio, hint)

    async def tell(text: str):
        await asyncio.to_thread(speak, text)

    _abort.clear()
    sparx.approve = spoken_approver(hint)
    jobs.LISTENERS.append(on_note)
    poller = asyncio.create_task(other_processes())
    print(f"{name} is listening. Say \"{v['wake_word']}\" then your request. Ctrl+C to quit.", flush=True)
    try:
        await tell(_greeting())
        while not security.stop_requested():
            if speaking() and not barge:
                await asyncio.sleep(0.1)
                continue
            try:
                heard = await hear(interruptible=barge)
            except ToolError as e:
                print(f"\x1b[31m{e}\x1b[0m", flush=True)
                await asyncio.sleep(2)
                continue
            text = (heard or {}).get("text", "").strip()
            if not text:
                continue
            if last_answer and not match_wake(text, conv.wake)[0] and is_own_speech(text, last_answer):
                continue   # Sparx's own voice coming back through the microphone is not a command
            now = time.monotonic()
            conv.extend(now if speaking() else _last_spoke)
            action, command = conv.route(text, now)
            if action == "ignore":
                continue
            stop_speaking()
            if action == "prompt":   # the wake word on its own: cue, then take the next utterance as the request
                await asyncio.to_thread(play_earcon, "wake")
                more = ((await hear(wait=8)) or {}).get("text", "").strip()
                action, command = conv.route(f"{v['wake_word']} {more}", now) if more else ("ignore", "")
            if action in ("ignore", "hush", "prompt"):
                continue   # for "hush", barge-in has already silenced the reply
            await asyncio.to_thread(play_earcon, "done")
            print(f"\n\x1b[36myou>\x1b[0m {command or text}", flush=True)
            if action == "quit":
                await tell("Goodbye.")
                break
            if action in ("end", "mute", "unmute"):
                if action != "mute":
                    await tell("Very good." if action == "end" else "Voice back on.")
                    if action == "end":
                        conv.closed = True
                continue
            try:
                answer = await sparx.turn(command)
            except security.Stopped as e:
                answer = str(e)
            except Exception as e:
                answer = f"Sorry, that failed: {type(e).__name__}."
                print(f"\x1b[31m{type(e).__name__}: {str(e)[:400]}\x1b[0m", flush=True)
            print(f"\x1b[32m{name}>\x1b[0m {answer}", flush=True)
            last_answer = speakable(answer)
            conv.replied(time.monotonic())
            if v.get("speak_replies", True) and not conv.muted:
                # not awaited: the loop goes straight back to listening, which is what makes barge-in work
                threading.Thread(target=speak, args=(answer,), daemon=True).start()
                await asyncio.sleep(0.05)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        _abort.set()
        stop_speaking()
        poller.cancel()
        jobs.LISTENERS.remove(on_note)
        sparx.approve = old_approve
        print("\nVoice mode off.", flush=True)


# ---- tools (group "audio"; the category is declared in sparx/tools/audio.py, which imports this module)

@tool("say", "Speak text aloud through the PC speakers (natural edge-tts voice, offline voice as fallback). "
      "Use it when the user asks to hear something read out.",
      {"text": S, "voice": {**S, "description": "voice name from voice_list; default follows the text's language"}},
      ["text"], category="audio", risk="write", timeout=300)
def say(ctx, text: str, voice: str = ""):
    return "Spoken." if speak(text, voice or None) else "Interrupted before the end."


@tool("listen", "Record one spoken utterance from the microphone and return its transcript. "
      "Use it when the user wants to dictate or answer by voice.",
      {"language": {**S, "description": "hint: en, hi, kn; default auto-detect"},
       "wait_seconds": {**I, "description": "give up if nobody speaks for this long (default 10)"}},
      category="audio", risk="control", untrusted=True, timeout=90)
def listen(ctx, language: str = "", wait_seconds: int = 10):
    audio = record_utterance(30, min(max(int(wait_seconds), 1), 45))   # the tool timeout cannot stop the microphone
    if audio is None:
        return {"text": "", "note": "Nobody spoke."}
    out = transcribe(audio, language or None)
    res = {"text": out["text"], "language": out["language"], "backend": out["backend"]}
    if left_machine(out["backend"]):
        res["privacy"] = f"This audio was sent to the hosted service {out['backend']} for transcription."
    return res


@tool("voice_list", "List the voices Sparx can speak with, optionally for one locale (e.g. 'en-IN', 'hi', 'kn'). "
      "Set one with settings_set voice.voice.", {"language": S}, category="audio")
def voice_list(ctx, language: str = ""):
    return list_voices(language) or "No voices found (edge-tts needs internet; pyttsx3 is the offline fallback)."


def _samples_or_mic(paths, seconds: int) -> list:
    return [resolve_file(p) for p in paths] if paths else [record_seconds(min(max(seconds, 3), 30))]


@tool("speaker_enrol", "Remember a person's voice so speaker_identify can name them later. Only when that person "
      "explicitly asked to be enrolled. Stores a small numeric fingerprint, no audio. Accuracy is limited: fine for "
      "telling a few household voices apart, never for security.",
      {"name": S, "consent": {**B, "description": "true only if this person asked to be enrolled"},
       "paths": {**A, "description": "recordings of them speaking; empty = record from the microphone now"},
       "seconds": {**I, "description": "microphone recording length, default 8"}},
      ["name", "consent"], category="audio", risk="control", timeout=120)
def speaker_enrol(ctx, name: str, consent: bool, paths: list | None = None, seconds: int = 8):
    if consent is not True:
        raise ToolError("Not enrolled: voice enrolment needs the person's explicit request.")
    return enrol_speaker(name.strip(), _samples_or_mic(paths, seconds))


@tool("speaker_identify", "Guess which enrolled person is speaking, from a recording or a few seconds of microphone. "
      "Approximate (simple spectral fingerprint); treat the answer as a hint.",
      {"path": {**S, "description": "audio file; empty = record from the microphone now"}, "seconds": I},
      category="audio", risk="control", timeout=120)
def speaker_identify(ctx, path: str = "", seconds: int = 5):
    return identify_speaker(_samples_or_mic([path] if path else [], seconds)[0])


@tool("speaker_forget", "Permanently delete an enrolled voice fingerprint.", {"name": S}, ["name"],
      category="audio", risk="delete")
def speaker_forget(ctx, name: str):
    known = _speakers()
    if known.pop(name, None) is None:
        raise ToolError(f"No enrolled speaker called '{name}'. Enrolled: {', '.join(sorted(known)) or 'nobody'}.")
    config.path("voice", "speakers.json").write_text(json.dumps(known, indent=1, ensure_ascii=False), encoding="utf-8")
    return f"Deleted the voice fingerprint for {name}."
