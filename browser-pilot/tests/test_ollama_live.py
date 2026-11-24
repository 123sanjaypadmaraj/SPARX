"""Live checks against local Ollama models. Opt-in: `pytest -m ollama`; skipped when Ollama or the model is missing.

Models come from OLLAMA_TEST_MODELS (comma separated, default gpt-oss:latest). The browser test starts its own
headless Chrome on port 9444 with a throwaway profile, so the everyday pilot Chrome is left alone.
"""
import http.server
import json
import os
import threading
import urllib.request

import pytest

import browserpilot.browser as browser
import browserpilot.orchestrator as orch
from browserpilot.config import PROVIDERS, RunConfig
from browserpilot.llm import LLM

pytestmark = [pytest.mark.ollama, pytest.mark.browser]

BASE = PROVIDERS["ollama"][0].removesuffix("/v1")
MODELS = [m.strip() for m in os.environ.get("OLLAMA_TEST_MODELS", "gpt-oss:latest").split(",") if m.strip()]
SECRET = "PILOT-7731"
PAGE = f"""<!doctype html><title>Vault</title><h1>Vault</h1>
<button onclick="document.getElementById('code').textContent='{SECRET}'">Reveal code</button>
<p id="code"></p>"""


def _pulled() -> set:
    try:
        with urllib.request.urlopen(BASE + "/api/tags", timeout=2) as r:
            return {m["name"] for m in json.load(r)["models"]}
    except Exception:
        return set()


@pytest.fixture(params=MODELS)
def model(request):
    if request.param not in _pulled():
        pytest.skip(f"Ollama is not running or {request.param} is not pulled")
    return f"ollama/{request.param}"


@pytest.fixture
def site():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


async def test_model_answers_and_calls_a_tool(model):
    tools = [{"type": "function", "function": {"name": "get_code", "description": "Return the secret code.",
                                               "parameters": {"type": "object", "properties": {}}}}]
    reply = await LLM(model).chat([{"role": "user", "content": "Call the get_code tool."}], tools)
    assert [c.name for c in reply.calls] == ["get_code"]


async def test_pilot_reads_a_page_after_a_click(model, site, tmp_path, monkeypatch):
    port = 9444
    monkeypatch.setattr(browser, "CDP_PORT", port)
    monkeypatch.setattr(browser, "CDP_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(browser, "PROFILE_DIR", tmp_path / "profile")
    cfg = RunConfig(model=model, agents=1, max_steps=12, headless=True, yes=True, url=site, out=tmp_path / "run")
    try:
        final = await orch.run_task("Click the 'Reveal code' button, then report the code that appears.", cfg)
    finally:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            await browser.stop_chrome(p)
    assert SECRET in final
