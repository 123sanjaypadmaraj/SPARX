"""screen_pilot: the screenshot, the vision model and pyautogui are faked."""
import json

import pytest
from PIL import Image

from sparx.tools import screen_pilot, system

from conftest import run_tool as _run


async def run_tool(name, ctx, /, **args):
    out = await _run(name, ctx, **args)
    return json.loads(out[out.index("{"):out.rindex("}") + 1])
from test_system import FakeGui


@pytest.fixture
def pilot(monkeypatch):
    g = FakeGui()
    monkeypatch.setattr(system, "_gui", lambda: g)
    monkeypatch.setattr(screen_pilot, "grab_screen", lambda: Image.new("RGB", (1920, 1080)))
    replies = []

    async def fake(img, question):
        return json.dumps(replies.pop(0))
    monkeypatch.setattr(screen_pilot, "describe_image", fake)
    monkeypatch.setattr(screen_pilot.asyncio, "sleep", lambda s: _noop())
    return g, replies


async def _noop():
    return None


async def test_pilot_clicks_scaled_coordinates_then_finishes(ctx, pilot):
    g, replies = pilot
    replies += [{"action": "click", "x": 640, "y": 360, "label": "Start"}, {"action": "key", "keys": "enter"},
                {"action": "done", "answer": "opened"}]
    out = await run_tool("screen_pilot", ctx, goal="open start")
    assert out["done"] and out["steps"] == 3
    # 1920 wide is shrunk to 1280 for the model, so (640, 360) maps back to (960, 540)
    assert g.calls[0] == ("click", (960, 540), {"clicks": 1, "button": "left"})


async def test_pilot_refuses_sensitive_step_without_approval(ctx, pilot):
    g, replies = pilot

    answers = iter([True, False])           # the first question approves screen_pilot itself, the second the risky step

    async def no(tool, args):
        return next(answers)
    ctx.approve = no
    replies.append({"action": "click", "x": 10, "y": 10, "label": "Delete all", "sensitive": True})
    out = await run_tool("screen_pilot", ctx, goal="tidy up")
    assert not out["done"] and "approval" in out["answer"] and g.calls == []


async def test_pilot_gives_up_after_max_steps(ctx, pilot):
    g, replies = pilot
    replies += [{"action": "wait"}] * 2
    out = await run_tool("screen_pilot", ctx, goal="x", max_steps=2)
    assert not out["done"] and "steps" in out["answer"]
