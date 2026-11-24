"""Chrome lifecycle + the per-agent browser Session that implements every tool."""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

from .config import CDP_PORT, MAX_ELEMENTS, PROFILE_DIR

CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

# Tags every visible interactive element with a stable data-bp-ref and describes it in one line.
SNAPSHOT_JS = r"""
(max) => {
  window.__bpNext = window.__bpNext || 1;
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=combobox],[role=option],[role=switch],[role=textbox],[role=searchbox],[contenteditable=""],[contenteditable=true],[onclick]';
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const lines = [];
  let total = 0;
  const walk = root => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
      if (!el.matches(SEL) || el.disabled || !visible(el)) continue;
      total++;
      if (lines.length >= max) continue;
      let ref = el.getAttribute('data-bp-ref');
      if (!ref) { ref = String(window.__bpNext++); el.setAttribute('data-bp-ref', ref); }
      const tag = el.tagName.toLowerCase();
      const kind = el.getAttribute('role') || (tag === 'input' ? 'input:' + (el.type || 'text') : tag === 'a' ? 'link' : tag);
      const label = clean(el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
        || (tag === 'select' ? '' : el.innerText) || el.placeholder || el.title || el.name
        || (el.querySelector && (el.querySelector('img[alt]') || {}).alt));
      let extra = '';
      if (tag === 'input' || tag === 'textarea') {
        if (el.type === 'checkbox' || el.type === 'radio') extra = el.checked ? ' (checked)' : '';
        else if (el.type !== 'password' && el.value) extra = ' value="' + clean(el.value) + '"';
      } else if (tag === 'select') {
        extra = ' value="' + clean(el.selectedOptions[0] && el.selectedOptions[0].text) + '" options=['
          + [...el.options].slice(0, 12).map(o => clean(o.text)).join(' | ') + ']';
      }
      lines.push('[' + ref + '] ' + kind + ' "' + label + '"' + extra);
    }
  };
  walk(document);
  return { lines, total };
}
"""

# Draws a cursor, a ring around the target and a caption, so a person watching the window can follow along.
SPOT_JS = r"""
([ref, caption]) => {
  const el = document.querySelector('[data-bp-ref="' + ref + '"]');
  if (!el) return;
  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = el.getBoundingClientRect();
  let box = document.getElementById('__bp_spot');
  if (!box) {
    box = document.createElement('div');
    box.id = '__bp_spot';
    box.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;border:3px solid #7c3aed;border-radius:6px;'
      + 'box-shadow:0 0 0 4px rgba(124,58,237,.25);transition:all .35s ease;';
    const tag = document.createElement('div');
    tag.style.cssText = 'position:absolute;left:-3px;top:-26px;background:#7c3aed;color:#fff;font:600 12px system-ui;'
      + 'padding:3px 8px;border-radius:5px;white-space:nowrap;';
    box.appendChild(tag);
    const cur = document.createElement('div');
    cur.textContent = '➤';
    cur.style.cssText = 'position:absolute;right:-10px;bottom:-14px;font-size:22px;color:#7c3aed;text-shadow:0 0 3px #fff;';
    box.appendChild(cur);
    document.documentElement.appendChild(box);
  }
  box.firstChild.textContent = caption;
  box.style.left = r.left - 3 + 'px'; box.style.top = r.top - 3 + 'px';
  box.style.width = r.width + 'px'; box.style.height = r.height + 'px';
  clearTimeout(window.__bpSpotTimer);
  window.__bpSpotTimer = setTimeout(() => box.remove(), 4000);
}
"""

RISKY = re.compile(
    r"\b(buy|purchase|pay|place order|checkout|delete|remove|send|transfer|unsubscribe|post|publish|submit|confirm)\b", re.I
)
MUTATING = {"navigate", "go_back", "click", "type", "select", "press", "scroll", "new_tab", "switch_tab", "close_tab", "wait", "upload_file"}

_stdin_lock = None


async def ask_human(prompt: str) -> str | None:
    """Ask on the terminal (serialised across agents). None when no terminal is attached."""
    global _stdin_lock
    if not sys.stdin or not sys.stdin.isatty():
        return None
    _stdin_lock = _stdin_lock or asyncio.Lock()
    async with _stdin_lock:
        try:
            return (await asyncio.to_thread(input, f"\n\x1b[33m? {prompt}\x1b[0m\n> ")).strip()
        except EOFError:  # stdin is the null device, which Windows still reports as a tty
            return None


# ---------------------------------------------------------------- Chrome

def find_chrome() -> str:
    candidates = [
        os.environ.get("BROWSERPILOT_CHROME", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise SystemExit("Chrome not found. Set BROWSERPILOT_CHROME to the browser executable.")


def chrome_info() -> dict | None:
    try:
        with urllib.request.urlopen(CDP_URL + "/json/version", timeout=1) as r:
            return json.load(r)
    except Exception:
        return None


async def stop_chrome(p) -> bool:
    if not chrome_info():
        return False
    browser = await p.chromium.connect_over_cdp(CDP_URL)
    session = await browser.new_browser_cdp_session()
    try:
        await session.send("Browser.close")
    except Exception:
        pass
    for _ in range(40):
        if not chrome_info():
            break
        await asyncio.sleep(0.25)
    return True


async def connect(p, headless: bool):
    """Attach to pilot's Chrome (starting it if needed) and return its default context.

    Chrome runs detached with a dedicated profile, so logins persist and the
    window stays open between runs.
    """
    info = chrome_info()
    if info and ("Headless" in info.get("User-Agent", "")) != headless:
        await stop_chrome(p)
        info = None
    if not info:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        args = [
            find_chrome(),
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1366,900",
        ]
        if headless:
            args.append("--headless=new")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         creationflags=flags, start_new_session=sys.platform != "win32")
        deadline = time.time() + 20
        while not chrome_info():
            if time.time() > deadline:
                raise SystemExit(f"Chrome did not open its debugging port {CDP_PORT}.")
            await asyncio.sleep(0.25)
    browser = await p.chromium.connect_over_cdp(CDP_URL)
    return browser.contexts[0]


# ---------------------------------------------------------------- Session

class ToolError(Exception):
    pass


class Session:
    """One agent's view of the browser: its own tabs inside the shared context."""

    def __init__(self, context, out_dir: Path, auto_yes: bool = False, log=print, visual: bool = True, local_roots=(),
                 confirm=None, ask=None):
        self.context = context
        self.confirm = confirm      # async (label, url) -> bool, set by a host app; never falls back to the terminal
        self.ask = ask              # async (question) -> str | None
        self.visual = visual
        self.local_roots = [Path(r).resolve() for r in local_roots]
        self.out_dir = out_dir
        self.auto_yes = auto_yes
        self.log = log
        self.pages = []
        self.page = None
        self.console = deque(maxlen=100)
        self.network = deque(maxlen=300)
        self.downloads = []

    async def start(self, url: str | None = None):
        await self._adopt(await self.context.new_page())
        if url:
            await self.t_navigate(url)

    async def close(self):
        for page in self.pages:
            try:
                await page.close()
            except Exception:
                pass

    async def _adopt(self, page):
        self.pages.append(page)
        self.page = page
        page.on("popup", lambda popup: asyncio.ensure_future(self._adopt(popup)))
        page.on("close", lambda pg: self._forget(pg))
        page.on("console", lambda m: self.console.append(f"{m.type}: {m.text[:300]}"))
        page.on("response", lambda r: self.network.append(f"{r.status} {r.request.method} {r.url[:200]}"))
        page.on("download", lambda d: asyncio.ensure_future(self._save_download(d)))

    def _forget(self, page):
        if page in self.pages:
            self.pages.remove(page)
        if self.page is page:
            self.page = self.pages[-1] if self.pages else None

    async def _save_download(self, download):
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path = self.out_dir / Path(download.suggested_filename).name
            await download.save_as(path)
            self.downloads.append(str(path))
        except Exception as e:
            self.downloads.append(f"download failed: {e}")

    def _need_page(self):
        if not self.page:
            raise ToolError("No open tab. Use new_tab.")
        return self.page

    def _loc(self, ref):
        try:
            n = int(str(ref).strip("[] "))
        except ValueError:
            raise ToolError(f"'{ref}' is not a valid ref; use the number in [brackets].")
        return self._need_page().locator(f'[data-bp-ref="{n}"]').first

    async def _label(self, loc) -> str:
        if not await loc.count():
            raise ToolError("That ref is no longer on the page. Use the refs from the latest page listing.")
        return await loc.evaluate(
            "el => (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.innerText || el.value || el.placeholder || el.name || el.tagName)"
            ".replace(/\\s+/g, ' ').trim().slice(0, 80)"
        )

    async def _guard(self, label: str):
        """Risky clicks (buy, delete, send...) need a human yes unless --yes was given."""
        if self.auto_yes or not RISKY.search(label):
            return
        if self.confirm is not None:
            try:
                allowed = bool(await self.confirm(label, self.page.url))
            except Exception:
                allowed = False
            if not allowed:
                raise ToolError(f'The user declined clicking "{label}". Do not retry it; continue or finish without it.')
            return
        answer = await ask_human(f'Agent wants to click "{label}" on {self.page.url}. Allow? [y/N]')
        if answer is None:
            raise ToolError(f'Clicking "{label}" needs user approval and no terminal is attached (rerun with --yes to allow).')
        if answer.lower() not in ("y", "yes"):
            raise ToolError(f'The user declined clicking "{label}". Do not retry it; continue or finish without it.')

    async def _spot(self, ref, caption: str):
        """Show the person watching where the agent is acting. Never fails the action itself."""
        if not self.visual:
            return
        try:
            await self._need_page().evaluate(SPOT_JS, [str(int(str(ref).strip("[] "))), caption])
            await asyncio.sleep(0.45)
        except Exception:
            pass

    async def _settle(self):
        try:
            await self._need_page().wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(0.5)

    async def observe(self, max_elements: int = MAX_ELEMENTS) -> str:
        page = self._need_page()
        for attempt in range(3):
            try:
                snap = await page.evaluate(SNAPSHOT_JS, max_elements)
                title = await page.title()
                break
            except Exception:  # navigation in flight
                if attempt == 2:
                    return f"URL: {page.url}\n(page is still loading; call wait and try again)"
                await asyncio.sleep(1)
        head = [f"URL: {page.url}", f"Title: {title}"]
        if len(self.pages) > 1:
            head.append(f"Tabs: {len(self.pages)} open, active index {self.pages.index(page)}")
        if self.downloads:
            head.append("Downloads: " + "; ".join(self.downloads[-3:]))
        shown, total = len(snap["lines"]), snap["total"]
        more = f" (showing {shown} of {total}; use find to locate others)" if total > shown else ""
        head.append(f"Interactive elements{more}:")
        return "\n".join(head + (snap["lines"] or ["(none)"]))

    async def call(self, name: str, args: dict):
        """Run a tool. Returns (text, jpeg_base64 | None); errors come back as text for the model."""
        fn = getattr(self, "t_" + name, None)
        if not fn:
            return f"Error: unknown tool '{name}'.", None
        try:
            out = await fn(**args)
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}", None
        except Exception as e:
            return f"Error: {(str(e).splitlines() or [type(e).__name__])[0][:300]}", None
        image = None
        if isinstance(out, tuple):
            out, image = out
        if name in MUTATING and self.page:
            out += "\n\n" + await self.observe()
        return out, image

    # ------------------------------------------------------------ tools

    def _check_local(self, url: str):
        """file: pages open only from folders the caller listed in local_roots (none by default)."""
        from urllib.parse import unquote, urlsplit
        raw = unquote(urlsplit(url).path)
        path = Path(raw.lstrip("/") if re.match(r"^/[A-Za-z]:", raw) else raw).resolve()
        if not any(path == root or root in path.parents for root in self.local_roots):
            raise ToolError("Only http(s) URLs can be opened (local files are limited to folders the user allowed).")
        if path.suffix.lower() not in (".html", ".htm") or not path.is_file():
            raise ToolError(f"No such page: {path.name}")

    async def t_navigate(self, url: str):
        if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            url = "https://" + url
        if url.lower().startswith("file:"):
            self._check_local(url)
        elif not url.lower().startswith(("http://", "https://", "about:")):
            raise ToolError("Only http(s) URLs can be opened.")
        await self._need_page().goto(url, wait_until="domcontentloaded", timeout=45000)
        await self._settle()
        return f"Opened {url}."

    async def t_go_back(self):
        await self._need_page().go_back(wait_until="domcontentloaded", timeout=30000)
        await self._settle()
        return "Went back."

    async def t_snapshot(self):
        return await self.observe()

    async def t_click(self, ref, double: bool = False):
        loc = self._loc(ref)
        label = await self._label(loc)
        await self._guard(label)
        await self._spot(ref, f"click: {label[:40]}")
        kw = {"click_count": 2} if double else {}
        try:
            await loc.click(timeout=8000, **kw)
        except Exception:
            await loc.click(timeout=5000, force=True, **kw)  # covered by an overlay or still animating
        await self._settle()
        return f'Clicked [{ref}] "{label}".'

    async def t_type(self, ref, text: str, submit: bool = False):
        loc = self._loc(ref)
        label = await self._label(loc)
        await self._spot(ref, f"type: {label[:30]}")
        try:
            if self.visual and len(text) <= 80:     # keystroke by keystroke, so it reads like a person typing
                await loc.click(timeout=5000)
                await loc.fill("", timeout=5000)
                await loc.press_sequentially(text, delay=40, timeout=15000)
            else:
                await loc.fill(text, timeout=8000)
        except Exception:
            await loc.click(timeout=5000)
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.type(text, delay=15)
        if submit:
            await loc.press("Enter")
        await self._settle()
        return f"Typed into [{ref}]" + (" and pressed Enter." if submit else ".")

    async def t_select(self, ref, option: str):
        loc = self._loc(ref)
        label = await self._label(loc)
        await self._spot(ref, f"select: {option[:30]}")
        try:
            await loc.select_option(label=option, timeout=5000)
        except Exception:
            await loc.select_option(value=option, timeout=5000)
        await self._settle()
        return f'Selected "{option}" in [{ref}].'

    async def t_hover(self, ref):
        loc = self._loc(ref)
        label = await self._label(loc)
        await self._spot(ref, f"hover: {label[:40]}")
        await loc.hover(timeout=8000)
        await asyncio.sleep(0.4)
        return f"Hovering [{ref}].\n\n" + await self.observe()

    async def t_press(self, key: str):
        await self._need_page().keyboard.press(key)
        await self._settle()
        return f"Pressed {key}."

    async def t_scroll(self, direction: str = "down"):
        js = {
            "down": "window.scrollBy(0, window.innerHeight * 0.85)",
            "up": "window.scrollBy(0, -window.innerHeight * 0.85)",
            "top": "window.scrollTo(0, 0)",
            "bottom": "window.scrollTo(0, document.body.scrollHeight)",
        }.get(direction)
        if not js:
            raise ToolError("direction must be down, up, top or bottom.")
        await self._need_page().evaluate(js)
        await asyncio.sleep(0.8)  # let lazy content load
        return f"Scrolled {direction}."

    async def t_read_text(self, offset: int = 0, max_chars: int = 6000):
        text = await self._need_page().evaluate("document.body ? document.body.innerText : ''")
        text = re.sub(r"\n\s*\n+", "\n", text)
        chunk = text[offset:offset + max_chars]
        rest = len(text) - offset - len(chunk)
        return chunk + (f"\n…[{rest} more chars; call read_text with offset={offset + len(chunk)}]" if rest > 0 else "")

    async def t_find(self, query: str):
        snap = await self._need_page().evaluate(SNAPSHOT_JS, 3000)
        words = query.lower().split()
        hits = [line for line in snap["lines"] if all(w in line.lower() for w in words)]
        return "\n".join(hits[:60]) or f'No interactive element matches "{query}".'

    async def t_get_links(self, contains: str = ""):
        links = await self._need_page().evaluate(
            "[...document.querySelectorAll('a[href]')].map(a => [a.innerText.replace(/\\s+/g,' ').trim().slice(0,100), a.href])"
        )
        needle = contains.lower()
        seen, out = set(), []
        for text, href in links:
            if href in seen or not href.startswith("http") or (needle and needle not in (text + href).lower()):
                continue
            seen.add(href)
            out.append(f"{text or '(no text)'} -> {href}")
        return "\n".join(out[:100]) or "No matching links."

    async def t_screenshot(self, full_page: bool = False):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"shot-{int(time.time() * 1000)}.jpg"
        data = await self._need_page().screenshot(full_page=full_page, type="jpeg", quality=60)
        path.write_bytes(data)
        return f"Screenshot saved to {path}", base64.b64encode(data).decode()

    async def t_run_js(self, code: str):
        result = await self._need_page().evaluate(code)
        text = json.dumps(result, ensure_ascii=False, default=str)
        return text[:6000] + ("…[truncated]" if len(text) > 6000 else "")

    async def t_new_tab(self, url: str = ""):
        await self._adopt(await self.context.new_page())
        return await self.t_navigate(url) if url else "Opened a new tab."

    async def t_list_tabs(self):
        rows = []
        for i, pg in enumerate(self.pages):
            rows.append(f"{i}{' (active)' if pg is self.page else ''}: {await pg.title()} - {pg.url}")
        return "\n".join(rows) or "No tabs."

    async def t_switch_tab(self, index: int):
        if not 0 <= index < len(self.pages):
            raise ToolError(f"No tab {index}; you have {len(self.pages)}.")
        self.page = self.pages[index]
        await self.page.bring_to_front()
        return f"Switched to tab {index}."

    async def t_close_tab(self, index: int):
        if not 0 <= index < len(self.pages):
            raise ToolError(f"No tab {index}; you have {len(self.pages)}.")
        await self.pages[index].close()
        return f"Closed tab {index}."

    async def t_wait(self, seconds: float = 2, text: str = ""):
        if text:
            await self._need_page().get_by_text(text).first.wait_for(timeout=min(max(seconds, 15), 60) * 1000)
            return f'"{text}" is visible.'
        await asyncio.sleep(min(seconds, 30))
        return f"Waited {seconds}s."

    async def t_upload_file(self, ref, path: str):
        if not os.path.isfile(path):
            raise ToolError(f"File not found: {path}")
        await self._loc(ref).set_input_files(path, timeout=8000)
        return f"Attached {path}."

    async def t_save_file(self, filename: str, content: str):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / Path(filename).name
        path.write_text(content, encoding="utf-8")
        return f"Saved {len(content)} chars to {path}"

    async def t_get_console(self):
        return "\n".join(self.console) or "Console is empty."

    async def t_get_network(self, contains: str = ""):
        rows = [r for r in self.network if contains.lower() in r.lower()]
        return "\n".join(rows[-60:]) or "No matching requests."

    async def t_ask_user(self, question: str):
        if self.ask is not None:
            try:
                answer = await self.ask(question)
            except Exception:
                answer = None
        else:
            answer = await ask_human(question)
        if answer is None:
            return "No human is available right now. Continue without this, or finish and explain what is blocked."
        return f"User replied: {answer}"


def _tool(name, description, props=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": list(required)},
    }}


_REF = {"type": "integer", "description": "Element number from the page listing"}
_S, _I, _B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}

TOOLS = [
    _tool("navigate", "Open a URL in the current tab.", {"url": _S}, ["url"]),
    _tool("go_back", "Go back one page in history."),
    _tool("snapshot", "List the current page's interactive elements again."),
    _tool("click", "Click an element.", {"ref": _REF, "double": _B}, ["ref"]),
    _tool("type", "Replace the contents of an input with text. submit=true presses Enter after.",
          {"ref": _REF, "text": _S, "submit": _B}, ["ref", "text"]),
    _tool("select", "Choose an option in a <select> dropdown by its visible text.", {"ref": _REF, "option": _S}, ["ref", "option"]),
    _tool("hover", "Hover an element to reveal menus or tooltips.", {"ref": _REF}, ["ref"]),
    _tool("press", "Press a key or chord, e.g. Enter, Escape, Tab, Control+A.", {"key": _S}, ["key"]),
    _tool("scroll", "Scroll the page to load more content.", {"direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]}}),
    _tool("read_text", "Read the page's visible text (use this to extract information).", {"offset": _I, "max_chars": _I}),
    _tool("find", "Search all interactive elements on the page by words in their label.", {"query": _S}, ["query"]),
    _tool("get_links", "List links on the page as 'text -> url', optionally filtered by a substring.", {"contains": _S}),
    _tool("screenshot", "Save a screenshot of the page to disk.", {"full_page": _B}),
    _tool("run_js", "Evaluate a JavaScript expression or arrow function in the page and return its JSON result.", {"code": _S}, ["code"]),
    _tool("new_tab", "Open a new tab (optionally at a URL) and switch to it.", {"url": _S}),
    _tool("list_tabs", "List your open tabs."),
    _tool("switch_tab", "Switch to one of your tabs by index.", {"index": _I}, ["index"]),
    _tool("close_tab", "Close one of your tabs by index.", {"index": _I}, ["index"]),
    _tool("wait", "Wait some seconds, or until the given text appears on the page.", {"seconds": {"type": "number"}, "text": _S}),
    _tool("upload_file", "Attach a local file to a file input.", {"ref": _REF, "path": _S}, ["ref", "path"]),
    _tool("save_file", "Save text (CSV, JSON, markdown, notes) to a file in the run's output folder.",
          {"filename": _S, "content": _S}, ["filename", "content"]),
    _tool("get_console", "Read recent browser console messages."),
    _tool("get_network", "Read recent network responses (status, method, URL), optionally filtered.", {"contains": _S}),
    _tool("ask_user", "Ask the human for help: logins, CAPTCHAs, 2FA codes, or a decision only they can make.",
          {"question": _S}, ["question"]),
    _tool("done", "Finish the task. result must contain the complete answer or a report of what was done.",
          {"result": _S}, ["result"]),
]
