"""Keyword check that each group's one-line catalogue entry still carries the words people say. No model involved."""
import re

import pytest

from sparx import tools

STOP = set("the and for my me this that what how much did about on in to of a an is do you i its it's with from at today tomorrow".split())

CASES = [
    ("turn the volume down", "pc"), ("turn on Bluetooth", "pc"), ("dim the living room lights", "home"),
    ("remind me to pay rent tomorrow", "productivity"), ("what's on my calendar today", "productivity"),
    ("how much did I spend on food this month", "finance"), ("send an email to Ravi", "comms"),
    ("message Priya on WhatsApp", "comms"), ("search the web for monsoon news", "web"),
    ("watch this page for price changes", "web"), ("make a PowerPoint about solar energy", "documents"),
    ("read the text in this photo", "vision"), ("transcribe this meeting recording", "audio"),
    ("quiz me on photosynthesis", "learning"), ("log a workout", "health"), ("did I take my medication", "health"),
    ("run the tests and fix the failing one", "coding"), ("open Spotify on my phone", "phone"),
    ("install VLC", "desktop"), ("find my resume pdf", "files"), ("what do you remember about me", "memory"),
    ("draw a diagram of the system", "creative"), ("take a screenshot", "system"),
    ("what does my lease document say about the deposit", "knowledge"),
]


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3 and w not in STOP]


def same(a: str, b: str) -> bool:
    return a == b or (len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5])


def scores(request: str) -> dict[str, int]:
    tools.load_all()
    out = {}
    for name, desc in tools.CATEGORIES.items():
        if desc == "test":
            continue
        group = words(f"{name} {desc}")
        out[name] = sum(any(same(w, g) for g in group) for w in words(request))
    return out


def test_every_expected_group_exists():
    tools.load_all()
    missing = sorted({g for _, g in CASES} - set(tools.CATEGORIES))
    assert not missing, f"renamed or removed groups: {missing}"


@pytest.mark.parametrize("request_text,group", CASES, ids=[c[0] for c in CASES])
def test_request_reaches_its_group(request_text, group):
    s = scores(request_text)
    top = sorted(s.items(), key=lambda kv: -kv[1])[:3]
    assert s[group] > 0 and s[group] == max(s.values()), f"{request_text!r} expected {group}; top three: {top}"
