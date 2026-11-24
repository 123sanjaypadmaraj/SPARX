"""Loads the web HUD in a real headless browser against a real uvicorn server (temp home, scripted model).

Skips, with the reason, when Playwright or every browser (bundled Chromium, Chrome, Edge) is unavailable.
"""
import os
import socket
import threading
import time

import pytest

from sparx import auth, automation, jobs, pending, server
from sparx.tools import REGISTRY, Tool

pytestmark = pytest.mark.browser

BROWSERS = (("bundled Chromium", {}), ("Chrome", {"channel": "chrome"}), ("Edge", {"channel": "msedge"}))
WAIT_MS = int(os.environ.get("SPARX_E2E_WAIT_MS", 15000))     # raise it on a busy machine
# runs before the page's own scripts (Playwright injects it, so the CSP does not apply to it)
CSP_PROBE = """document.addEventListener('securitypolicyviolation', (e) =>
  console.error(`CSP-VIOLATION ${e.violatedDirective} blocked ${e.blockedURI || 'inline'} at ${e.sourceFile}:${e.lineNumber}`));"""


@pytest.fixture
def sent(monkeypatch):
    """Registers t_hud_send (risk 'send', so the user is asked first). Returns the list of recipients it ran for."""
    done = []

    async def send(ctx, to, password=""):
        done.append(to)
        return f"sent to {to}"
    monkeypatch.setitem(REGISTRY, "t_hud_send", Tool(
        "t_hud_send", "Send a test message.", {"to": {"type": "string"}, "password": {"type": "string"}}, ["to"],
        "testing_hud", "send", False, 10, send))
    return done


@pytest.fixture
def hud_url(fake_llm, sent):
    """The HUD served by uvicorn on a free 127.0.0.1 port in a background thread; stopped again afterwards."""
    import uvicorn
    auth._fails.clear()
    app = server.create_app(run_jobs=False)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    # 6s grace: a /api/vitals poll still in flight (about 3s on Windows) finishes instead of being cancelled noisily
    srv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=6))
    thread = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, name="hud-e2e-server", daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not srv.started:
            assert thread.is_alive() and time.time() < deadline, "uvicorn did not start within 15 seconds"
            time.sleep(0.05)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        srv.should_exit = True
        thread.join(10)
        if thread.is_alive():           # still draining: force it, the thread is a daemon so it cannot block exit
            srv.force_exit = True
            thread.join(5)
        sock.close()
        server.CONNS.clear()
        jobs.LISTENERS[:] = [fn for fn in jobs.LISTENERS if getattr(fn, "__self__", None).__class__ is not server.Conn]


@pytest.fixture
def page(hud_url):
    """A page in the first browser that launches. Console errors, page errors and CSP violations land in page.problems."""
    sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as e:
        pytest.skip(f"Playwright could not start: {type(e).__name__}: {str(e)[:200]}")
    browser, why = None, []
    try:
        for label, kwargs in BROWSERS:
            try:
                browser = pw.chromium.launch(headless=True, **kwargs)
                break
            except Exception as e:
                why.append(f"{label}: {str(e).strip().splitlines()[0][:160]}")
        if browser is None:
            pytest.skip("No browser could be launched (not running `playwright install`). " + " | ".join(why))
        print(f"\nHUD e2e browser: {label} {browser.version}")
        context = browser.new_context(viewport={"width": 1366, "height": 800})
        context.set_default_timeout(WAIT_MS)
        context.add_init_script(CSP_PROBE)
        p = context.new_page()
        p.problems = []
        p.on("pageerror", lambda e: p.problems.append(f"pageerror: {e}"))
        p.on("console", lambda m: m.type == "error" and p.problems.append(
            f"console error: {m.text} ({m.location.get('url', '')}:{m.location.get('lineNumber', '')})"))
        yield p
    finally:
        try:
            if browser is not None:
                browser.close()
        finally:
            pw.stop()


def sign_in(page, hud_url):
    """Sign in with a one-time link, as `sparx serve` prints it, and wait for the app to settle. Returns `expect`."""
    from playwright.sync_api import expect
    expect.set_options(timeout=WAIT_MS)
    page.goto(f"{hud_url}/login?code={auth.login_code()}")
    expect(page).to_have_url(f"{hud_url}/")
    expect(page.locator("#gate")).to_be_hidden()
    expect(page.locator("#app")).to_be_visible()
    expect(page.locator("#link")).to_have_text("online")
    expect(page.locator("#log .empty")).to_be_visible()         # the first load of the (empty) conversation has settled
    return expect


def test_hud_in_a_real_browser(page, hud_url, fake_llm, sent):
    fake_llm.script = ["Hello from the **script**.",
                       ("t_hud_send", {"to": "asha@example.com", "password": "hunter2"}), "Message sent."]
    expect = sign_in(page, hud_url)

    # a plain turn: the scripted reply shows up, rendered as markdown
    page.fill("#input", "hello sparx")
    page.click("#send")
    expect(page.locator("#log .msg.user")).to_have_text("hello sparx")
    reply = page.locator("#log .msg.assistant")
    expect(reply).to_have_text("Hello from the script.")
    expect(reply.locator("strong")).to_have_text("script")
    expect(page.locator("#input")).to_have_value("")
    expect(page.locator("#send")).to_be_enabled()

    # a turn that needs approval: the card appears, Allow runs the tool and the turn finishes
    page.fill("#input", "send it")
    page.click("#send")
    card = page.locator("#log .approval")
    expect(card).to_be_visible()
    expect(card.locator("code").first).to_have_text("t_hud_send")
    expect(card.locator(".risk")).to_have_text("send")
    expect(card.locator("pre")).to_contain_text("asha@example.com")
    expect(card.locator("pre")).not_to_contain_text("hunter2")          # secrets are masked before they reach the page
    assert sent == [], "the tool ran before the user allowed it"
    card.get_by_role("button", name="Allow").click()
    expect(page.locator("#log .msg.assistant").last).to_have_text("Message sent.")
    expect(card.locator(".left")).to_have_text("Allowed")
    expect(card.locator("button")).to_have_count(0)
    expect(page.locator("#log .steps .step")).to_contain_text("t_hud_send")
    expect(page.locator("#send")).to_be_enabled()
    expect(page.locator("#cancel")).to_be_hidden()
    assert sent == ["asha@example.com"]

    # the sections open from the rail and load their data
    for view, proof in (("today", "#today-body .section-title"), ("memory", "#memory-body > *"),
                        ("settings", "#settings-body label.field")):
        page.click(f'nav.rail button[data-nav="{view}"]')
        expect(page.locator(f'nav.rail button[data-nav="{view}"]')).to_have_class("on")
        section = page.locator(f'main section[data-view="{view}"]')
        expect(section).to_be_visible()
        expect(page.locator('main section[data-view="chat"]')).to_be_hidden()
        expect(page.locator(proof).first).to_be_visible()
        body = page.locator(f"#{view}-body")
        expect(body).not_to_contain_text("Loading…")
        expect(body).not_to_contain_text("Could not load this")
    expect(page.locator("#today-body")).to_contain_text("Open tasks")
    expect(page.locator("#settings-body")).to_contain_text("You and Sparx")

    # back to the conversation: it is still there and the socket is still up
    page.click('nav.rail button[data-nav="chat"]')
    expect(page.locator("#log .msg.assistant")).to_have_count(2)
    expect(page.locator("#link")).to_have_text("online")

    page.wait_for_timeout(500)          # let late requests (service worker, vitals) report before judging
    assert not page.problems, "The HUD reported errors in the browser:\n" + "\n".join(page.problems)


def test_approvals_panel_runs_a_queued_action(page, hud_url, sent):
    assert pending.queue(REGISTRY["t_hud_send"], {"to": "asha@example.com"}, "daemon")
    expect = sign_in(page, hud_url)
    badge = page.locator("#pending-count")
    expect(badge).to_have_text("1")
    page.click('nav.rail button[data-nav="pending"]')
    card = page.locator("#pending-body .pending-card")
    expect(card).to_have_count(1)
    expect(card.locator("code").first).to_have_text("t_hud_send")
    expect(card.locator(".risk")).to_have_text("send")
    expect(card.locator("pre")).to_contain_text("asha@example.com")
    expect(card).to_contain_text("asked by daemon")
    assert sent == [], "the queued action ran before the user asked for it"
    card.get_by_role("button", name="Run now").click()
    expect(card).to_have_count(0)
    expect(badge).to_be_hidden()
    handled = page.locator("#pending-body .card").filter(has_text="done")
    expect(handled).to_contain_text("t_hud_send")
    expect(handled).to_contain_text("sent to asha@example.com")
    assert sent == ["asha@example.com"]

    page.wait_for_timeout(500)
    assert not page.problems, "The HUD reported errors in the browser:\n" + "\n".join(page.problems)


def settle(page):
    """Let late requests report, then fail on anything the browser complained about (native dialogs included)."""
    page.wait_for_timeout(500)
    assert not page.problems, "The HUD reported errors in the browser:\n" + "\n".join(page.problems)


def no_dialogs(page):
    def seen(d):
        page.problems.append(f"native {d.type} dialog: {d.message}")
        d.dismiss()
    page.on("dialog", seen)


def open_panel(page, expect, view):
    page.click(f'nav.rail button[data-nav="{view}"]')
    expect(page.locator(f'main section[data-view="{view}"]')).to_be_visible()
    body = page.locator(f"#{view}-body")
    expect(body).not_to_contain_text("Loading…")
    expect(body).not_to_contain_text("Could not load this")
    return body


def test_history_export_delete_and_undo(page, hud_url, fake_llm):
    no_dialogs(page)
    fake_llm.script = ["Noted."]
    expect = sign_in(page, hud_url)
    page.fill("#input", "remember the milk")
    page.click("#send")
    expect(page.locator("#log .msg.assistant")).to_have_text("Noted.")

    body = open_panel(page, expect, "history")
    card = body.locator(".card")
    expect(card).to_have_count(1)
    expect(card).to_contain_text("remember the milk")
    with page.expect_download() as got:
        card.get_by_role("button", name="Export Markdown").click()
    name = got.value.suggested_filename
    assert name.startswith("sparx-conversation-") and name.endswith(".md"), name

    card.get_by_role("button", name="Delete").click()
    expect(card.locator(".card-confirm")).to_contain_text("2 messages")
    card.get_by_role("button", name="Yes, delete").click()
    expect(body.locator(".card")).to_have_count(0)
    page.click('nav.rail button[data-nav="chat"]')
    expect(page.locator("#log .empty")).to_be_visible()                 # a fresh conversation, not the deleted one
    expect(page.locator("#log .msg")).to_have_count(0)

    undo = open_panel(page, expect, "undo")
    entry = undo.locator(".card").filter(has_text="Deleted conversation")
    expect(entry).to_have_count(1)
    entry.get_by_role("button", name="Undo").click()
    expect(open_panel(page, expect, "history").locator(".card")).to_contain_text("remember the milk")
    settle(page)


def test_tasks_alerts_activity_and_usage_panels(page, hud_url, fake_llm):
    no_dialogs(page)
    jobs.notify("Water the plants", "They look dry.", "reminder", "You asked for a reminder.")
    fake_llm.script = [("calculate", {"expression": "6*7"}), "It is 42."]
    expect = sign_in(page, hud_url)
    badge = page.locator("#unread")
    expect(badge).to_have_text("1")
    page.fill("#input", "what is 6*7?")
    page.click("#send")
    expect(page.locator("#log .msg.assistant")).to_have_text("It is 42.")

    open_panel(page, expect, "tasks")
    alerts = open_panel(page, expect, "alerts")
    expect(alerts.locator(".card")).to_contain_text("Water the plants")
    expect(alerts.locator(".card")).to_contain_text("Why: You asked for a reminder.")
    page.click("#alerts-read")
    expect(badge).to_be_hidden()
    expect(open_panel(page, expect, "activity").locator(".card code").filter(has_text="calculate").first).to_be_visible()
    usage = open_panel(page, expect, "usage")
    expect(usage).to_contain_text("Model, last 30 days (kept across restarts)")
    expect(usage).to_contain_text("Model, since this server started")
    expect(usage).to_contain_text("calculate")
    settle(page)


def test_memory_forget_and_new_scope_without_native_dialogs(page, hud_url):
    import asyncio
    from sparx import tools
    no_dialogs(page)
    tools.load_all()
    got = []          # Playwright's sync API already runs an event loop in this thread, so the call gets its own
    seed = threading.Thread(target=lambda: got.append(asyncio.run(
        tools.call("remember", {"text": "Prefers metric units"}, tools.Context(session="seed")))))
    seed.start()
    seed.join(30)
    assert got and not got[0].startswith(("Error:", "Not done:")), got
    expect = sign_in(page, hud_url)

    body = open_panel(page, expect, "memory")
    card = body.locator(".card")
    expect(card).to_have_count(1)
    expect(card).to_contain_text("Prefers metric units")
    card.get_by_role("button", name="Forget").click()
    expect(card.locator(".card-confirm")).to_contain_text("Forget this memory?")
    card.get_by_role("button", name="Cancel").click()
    expect(body.locator(".card")).to_contain_text("Prefers metric units")          # Cancel keeps it
    body.locator(".card").get_by_role("button", name="Forget").click()
    body.locator(".card").get_by_role("button", name="Yes, forget").click()
    expect(body.locator(".card")).to_have_count(0)

    field = page.locator("#scope-new")
    expect(field).to_be_hidden()
    page.select_option("#scope", "+")
    expect(field).to_be_focused()
    page.keyboard.press("Escape")
    expect(field).to_be_hidden()
    expect(page.locator("#scope")).to_have_value("personal")
    page.select_option("#scope", "+")
    field.fill("project-x")
    field.press("Enter")
    expect(field).to_be_hidden()
    expect(page.locator("#scope")).to_have_value("project-x")
    settle(page)


def test_pin_gate_locks_and_unlocks(page, hud_url):
    from playwright.sync_api import expect
    expect.set_options(timeout=WAIT_MS)
    no_dialogs(page)
    auth.set_pin("4321")
    page.goto(f"{hud_url}/login?code={auth.login_code()}")
    form, msg = page.locator("#pin-form"), page.locator("#gate-msg")
    expect(form).to_be_visible()
    expect(page.locator("#app")).to_be_hidden()
    page.fill("#pin-input", "0000")
    form.locator('button[type="submit"]').click()
    expect(msg).not_to_be_empty()
    expect(form).to_be_visible()
    expect(page.locator("#app")).to_be_hidden()

    def unlock():
        page.fill("#pin-input", "4321")
        form.locator('button[type="submit"]').click()
        expect(page.locator("#gate")).to_be_hidden()
        expect(page.locator("#app")).to_be_visible()
        expect(page.locator("#link")).to_have_text("online")
    unlock()
    open_panel(page, expect, "settings").get_by_role("button", name="Lock now").click()
    expect(form).to_be_visible()
    unlock()
    # the wrong PIN above is answered with an error status on purpose; only that request may show up as a console error
    page.problems[:] = [p for p in page.problems if "/api/unlock" not in p and "Failed to load resource" not in p]
    settle(page)


def test_automations_panel_approves_a_waiting_run(page, hud_url, sent):
    import asyncio
    from sparx import tools
    no_dialogs(page)
    automation.create("mailer", "daily 08:00", [{"tool": "t_hud_send", "args": {"to": "asha@example.com"}}])
    ctx = tools.Context(agent="automation", approve=None)        # an automatic run: nobody to ask, so the send waits
    ctx.data["automated"] = True
    got = []          # Playwright's sync API already runs an event loop in this thread, so the run gets its own
    seed = threading.Thread(target=lambda: got.append(asyncio.run(
        automation.start(automation.get("mailer"), ctx, trigger="schedule daily 08:00"))))
    seed.start()
    seed.join(30)
    assert got and got[0]["status"] == "waiting_approval" and sent == [], got
    expect = sign_in(page, hud_url)

    badge = page.locator("#automations-count")
    expect(badge).to_have_text("1")
    body = open_panel(page, expect, "automations")
    for title in ("Waiting for you", "Automations", "Recent runs"):
        expect(body.locator(".section-title").filter(has_text=title)).to_have_count(1)
    card = body.locator(".run-waiting")
    expect(card).to_have_count(1)
    expect(card.locator(".card-title")).to_have_text("mailer")
    expect(card.locator("pre")).to_contain_text("step 1: t_hud_send [send]")
    expect(card.locator("pre")).to_contain_text("asha@example.com")
    expect(card).to_contain_text("schedule daily 08:00")
    expect(body.locator(".run-card")).to_have_count(0)
    recipe = body.locator(".auto-card")
    expect(recipe).to_contain_text("daily 08:00 · 1 step · next run")
    expect(recipe.locator(".auto-state")).to_have_text("on")
    assert sent == [], "the step ran before the user approved it"

    card.get_by_role("button", name="Approve and continue").click()
    expect(card).to_have_count(0)
    expect(badge).to_be_hidden()
    ran = body.locator(".run-card")
    expect(ran).to_have_count(1)
    expect(ran).to_contain_text("mailer")
    expect(ran.locator(".chip")).to_have_text("done")
    assert sent == ["asha@example.com"]

    recipe.get_by_role("button", name="Disable").click()
    expect(recipe.locator(".auto-state")).to_have_text("off")
    expect(recipe.get_by_role("button", name="Enable")).to_be_visible()
    assert automation.get("mailer")["enabled"] == 0
    settle(page)


PHONE_VIEWS = ("systems", "today", "tasks", "memory", "alerts", "pending", "automations", "history", "activity", "undo",
               "usage", "settings")


def test_hud_at_phone_width(page, hud_url, sent):
    """At 390 px nothing scrolls sideways, every rail button is reachable, the composer and the approval buttons fit."""
    assert pending.queue(REGISTRY["t_hud_send"], {"to": "asha@example.com"}, "daemon")
    expect = sign_in(page, hud_url)
    page.set_viewport_size({"width": 390, "height": 844})
    width = 390

    def no_sideways_scroll(view):
        over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert over <= 1, f"the {view} view scrolls sideways by {over}px at {width}px width"

    no_sideways_scroll("chat")
    for view in PHONE_VIEWS:
        button = page.locator(f'nav.rail button[data-nav="{view}"]')
        button.scroll_into_view_if_needed()
        box = button.bounding_box()
        assert box and box["x"] >= -1 and box["x"] + box["width"] <= width + 1, f"the {view} button is off screen: {box}"
        assert box["height"] >= 32, f"the {view} button is only {box['height']}px tall"
        button.click()
        expect(page.locator(".systems" if view == "systems" else f'main section[data-view="{view}"]')).to_be_visible()
        page.wait_for_timeout(150)
        no_sideways_scroll(view)

    page.click('nav.rail button[data-nav="chat"]')
    page.focus("#input")
    for sel in ("#input", "#send"):
        box = page.locator(sel).bounding_box()
        assert box and box["x"] >= -1 and box["x"] + box["width"] <= width + 1 and box["y"] + box["height"] <= 844 + 1, \
            f"{sel} is not inside the {width}x844 viewport: {box}"

    page.click('nav.rail button[data-nav="pending"]')
    card = page.locator("#pending-body .pending-card")
    expect(card).to_have_count(1)
    for name in ("Run now", "Dismiss"):
        box = card.get_by_role("button", name=name).bounding_box()
        assert box and box["x"] >= -1 and box["x"] + box["width"] <= width + 1, f"'{name}' is off screen: {box}"
    no_sideways_scroll("pending with a card")
    card.get_by_role("button", name="Run now").click()
    expect(card).to_have_count(0)
    assert sent == ["asha@example.com"]
    settle(page)
