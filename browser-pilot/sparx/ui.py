"""Terminal UI for `sparx chat`: banner, boxed prompt with slash-command completion, live activity spinner,
markdown replies and approval panels. Built on rich + prompt_toolkit; cli.py falls back to plain I/O without them."""
import json
import os
import sys
import time

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Heading, Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style as PtStyle

from . import config

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

console = Console()

ACCENT = "#4da3ff"
WARM = "#ffd23f"
OK = "#3ddc84"
BAD = "#ff5a2c"
DIM = "grey50"

LOGO = [
    "███████╗██████╗  █████╗ ██████╗ ██╗  ██╗",
    "██╔════╝██╔══██╗██╔══██╗██╔══██╗╚██╗██╔╝",
    "███████╗██████╔╝███████║██████╔╝ ╚███╔╝ ",
    "╚════██║██╔═══╝ ██╔══██║██╔══██╗ ██╔██╗ ",
    "███████║██║     ██║  ██║██║  ██║██╔╝ ██╗",
    "╚══════╝╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝",
]
LOGO_SMALL = ["█▀▀ █▀█ ▄▀█ █▀█ ▀▄▀", "▄▄█ █▀▀ █▀█ █▀▄ ▄▀▄"]
LOGO_COLOURS = ["#4da3ff", "#3ddc84", "#ffd23f", "#ffb700", "#ff7a2c", "#ff3b3b"]
FULL_NAME = "Sanjay Padmaraj's Autonomous Reasoning & Knowledge eXpert"
GRADIENT = ["#4da3ff", "#3ddc84", "#ffd23f", "#ff5a2c", "#ffb700"]

COMMANDS = {
    "/tools": "tool groups and what is loaded", "/load": "load a tool group", "/scope": "switch memory scope",
    "/model": "switch model", "/usage": "tokens, latency, tool calls", "/audit": "recent actions",
    "/undo": "reverse the last reversible action", "/stop": "emergency stop", "/resume": "clear the stop",
    "/new": "fresh conversation", "/clear": "clear the screen", "/help": "show commands", "/exit": "leave",
    "/status": "model, directory, mode, checkpoints", "/cost": "token usage", "/context": "what fills the context window",
    "/memory": "loaded SPARX.md files", "/config": "show or set a setting", "/permissions": "mode and allow/ask/deny rules",
    "/mode": "default | accept_edits | plan | bypass", "/hooks": "configured hooks", "/agents": "custom subagents",
    "/mcp": "MCP servers", "/export": "save the conversation as Markdown", "/resume": "resume a conversation by id",
    "/sessions": "list stored conversations", "/rewind": "restore files changed in the last turn(s)",
    "/add-dir": "let file tools use another folder", "/init": "write SPARX.md for this repo", "/doctor": "check setup",
    "/todos": "the working todo list", "/output-style": "change how answers are written", "/statusline": "custom toolbar command",
    "/skills": "skills and custom commands", "/review": "review uncommitted changes", "/commit": "commit the changes",
    "/test": "run tests and fix failures", "/vim": "toggle vim keys", "/compact": "summarise old messages", "/plan": "toggle plan mode",
}

RISK_STYLE = {"read": OK, "low": OK, "write": WARM, "medium": WARM, "high": BAD, "danger": BAD}


class _Slash(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for cmd, desc in COMMANDS.items():
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text), display_meta=desc)


class _Heading(Heading):
    """Left-aligned headings; rich centres them by default, which looks odd beside the left bar."""

    def __rich_console__(self, console, options):
        self.text.justify = "left"
        if self.tag != "h1":
            yield Text("")
        yield self.text


class _Md(Markdown):
    elements = {**Markdown.elements, "heading_open": _Heading}


def _logo(width: int) -> Group:
    if width >= 48:
        return Group(*[Text(l, style=f"bold {c}") for l, c in zip(LOGO, LOGO_COLOURS)])
    return Group(*[Text(l, style=f"bold {c}") for l, c in zip(LOGO_SMALL, LOGO_COLOURS)])


def banner(name: str, model: str, n_tools: int, scope: str, errors: dict):
    w = console.width
    meta = Text.from_markup(f"[{ACCENT}]{model}[/] [{DIM}]·[/] [{OK}]{n_tools} tools[/] [{DIM}]·[/] [{WARM}]{scope}[/]")
    tips = Table.grid(padding=(0, 2))
    tips.add_column(style=f"bold {ACCENT}", no_wrap=True)
    tips.add_column(style="white")
    for k, v in (("/", "browse commands"), ("/tools", "see tool groups"), ("/model", "switch model"),
                 ("/usage", "tokens and latency"), ("/undo", "reverse last action")):
        tips.add_row(k, v)
    hints = Text("Alt+Enter new line  ·  Ctrl+C interrupt  ·  Ctrl+D exit", style=DIM)
    body = [_logo(w), Text(FULL_NAME, style=DIM, overflow="ellipsis", no_wrap=True), Text(""), meta,
            Text(os.getcwd(), style=DIM, overflow="ellipsis", no_wrap=True), Text(""),
            Rule(style="#ff3b3b"), Text(""), tips, Text(""), hints]
    console.print()
    console.print(Panel(Group(*body), box=box.ROUNDED, border_style="#ff3b3b", padding=(1, 3),
                        title=f"[bold {ACCENT}] {name} [/]", title_align="left", expand=True))
    for mod, err in errors.items():
        console.print(f"  [{BAD}]error: tool module '{mod}' failed to load:[/] {err}")


def say(name: str, text: str, elapsed: float | None = None):
    """Print an assistant message: a header, then the markdown behind a gradient-coloured left bar."""
    foot = f"  [{DIM}]{elapsed:.1f}s[/]" if elapsed is not None else ""
    console.print()
    console.print(f"[bold {GRADIENT[4]}]◆ {name}[/]{foot}")
    width = max(console.width - 4, 20)
    lines = console.render_lines(_Md(text or "(no reply)"), console.options.update(width=width), pad=False)
    step = min(0.012, 0.9 / max(len(lines), 1)) if console.is_terminal else 0
    for i, segs in enumerate(lines):
        bar = Text("▎ ", style=ACCENT)
        row = Text()
        for seg in segs:
            row.append(seg.text, style=seg.style)
        console.print(Text.assemble(bar, row), soft_wrap=True)
        if step:
            time.sleep(step)


def info(text: str):
    console.print()
    for line in text.splitlines() or [""]:
        console.print(Text.assemble(("▎ ", DIM), (line, "white")), soft_wrap=True)


def error(text: str):
    console.print()
    for line in text.splitlines():
        console.print(Text.assemble(("▎ ", BAD), (line, BAD)), soft_wrap=True)


def help_panel():
    t = Table.grid(padding=(0, 3))
    t.add_column(style=f"bold {ACCENT}")
    t.add_column(style="white")
    for cmd, desc in COMMANDS.items():
        t.add_row(cmd, desc)
    console.print()
    console.print(Panel(t, box=box.ROUNDED, border_style=GRADIENT[0], title=f"[bold {ACCENT}]commands[/]", title_align="left"))


def clear():
    console.clear()


class Activity:
    """Live spinner shown while a turn runs. Tool calls scroll above it as they start."""

    def __init__(self):
        self.live: Live | None = None
        self.label = "thinking"
        self.t0 = 0.0
        self.calls = 0

    def _render(self):
        secs = time.time() - self.t0
        extra = f" · {self.calls} tool call{'s' if self.calls != 1 else ''}" if self.calls else ""
        return Spinner("dots", text=Text.from_markup(
            f"[{ACCENT}]{self.label}[/] [{DIM}]({secs:.0f}s{extra} · Ctrl+C to interrupt)[/]"), style=ACCENT)

    def start(self):
        self.t0, self.calls, self.label = time.time(), 0, "thinking"
        self.live = Live(self._render(), console=console, refresh_per_second=12, transient=True,
                         get_renderable=self._render)
        self.live.start()

    def stop(self):
        if self.live:
            self.live.stop()
            self.live = None

    def resume(self):
        if self.live is None:
            self.live = Live(console=console, refresh_per_second=12, transient=True, get_renderable=self._render)
            self.live.start()

    def tool(self, name: str, args: dict):
        self.calls += 1
        self.label = f"running {name}"
        shown = ", ".join(f"{k}={repr(v)[:50]}" for k, v in args.items())
        console.print(Text.assemble(("  ⏺ ", ACCENT), (name, "bold white"), (f"({shown[:110]})", DIM)))

    def elapsed(self) -> float:
        return time.time() - self.t0


activity = Activity()


def approval_panel(tool, args: dict) -> str:
    """Show the approval card and read the answer. Pauses the spinner while it is up."""
    activity.stop()
    risk = getattr(tool, "risk", "?")
    colour = RISK_STYLE.get(str(risk).lower(), WARM)
    shown = json.dumps(args, ensure_ascii=False, indent=1)[:700]
    console.print(Panel(Group(Text(shown, style="white"), Text(""),
                              Text.from_markup(f"[bold {OK}]y[/] allow   [bold {OK}]a[/] always allow (this chat)   [bold {BAD}]n[/] deny  [{DIM}](Enter = deny)[/]")),
                        box=box.HEAVY, border_style=colour, padding=(0, 2),
                        title=f"[bold {colour}]allow {tool.name}?[/]  [{DIM}]risk: {risk}[/]", title_align="left"))
    try:
        ans = console.input(f"[bold {colour}]❯[/] ")
    finally:
        activity.resume()
    return ans.strip().lower()


def make_session(history_path: str, toolbar, on_cycle=None) -> PromptSession:
    kb = KeyBindings()

    @kb.add("s-tab")
    def _cycle(event):          # Shift+Tab: next permission mode
        if on_cycle:
            on_cycle()
        event.app.invalidate()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    style = PtStyle.from_dict({
        "prompt": f"bold {ACCENT}", "bottom-toolbar": "bg:#0b1020 #8a93a8", "bottom-toolbar.text": "bg:#0b1020 #8a93a8",
        "completion-menu.completion": "bg:#141b2e #e6edf7", "completion-menu.completion.current": "bg:#4da3ff #0b1020",
        "completion-menu.meta.completion": "bg:#141b2e #8a93a8", "completion-menu.meta.completion.current": "bg:#4da3ff #0b1020",
    })
    try:
        os.makedirs(os.path.dirname(history_path), exist_ok=True)
        hist = FileHistory(history_path)
    except OSError:
        hist = None
    return PromptSession(history=hist, completer=_Slash(), complete_while_typing=True, key_bindings=kb, style=style,
                         bottom_toolbar=toolbar, multiline=False, enable_history_search=True)


_flash = ("", 0.0)       # (message, expiry) shown in the toolbar after the clipboard changes


def _clipboard_text() -> str:
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetClipboardData.restype = wintypes.HANDLE
    k.GlobalLock.argtypes, k.GlobalLock.restype = [wintypes.HANDLE], ctypes.c_void_p
    k.GlobalUnlock.argtypes = [wintypes.HANDLE]
    if not u.OpenClipboard(None):
        return ""
    try:
        h = u.GetClipboardData(13)      # CF_UNICODETEXT
        if not h:
            return ""
        p = k.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else ""
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def watch_clipboard(session) -> None:
    """Flash 'Copied N characters' in the toolbar whenever the clipboard changes (e.g. copy-on-select in the terminal)."""
    if os.name != "nt":
        return
    import ctypes
    import threading

    def _run():
        global _flash
        seq = ctypes.windll.user32.GetClipboardSequenceNumber()
        shown = False
        while True:
            time.sleep(0.25)
            try:
                now = ctypes.windll.user32.GetClipboardSequenceNumber()
                if now != seq:
                    seq = now
                    n = len(_clipboard_text())
                    if n:
                        _flash = (f"Copied {n} character{'' if n == 1 else 's'} to clipboard", time.time() + 3)
                        shown = True
                        session.app.invalidate()
                elif shown and time.time() > _flash[1]:
                    shown = False
                    session.app.invalidate()
            except Exception:
                return

    threading.Thread(target=_run, daemon=True, name="clipboard-watch").start()


_status_cache = ["", 0.0]


def _custom_status(bot, model: str) -> str:
    """First output line of the `statusline.command` setting (cached for 3 s; it gets JSON on stdin)."""
    import subprocess
    cmd = (config.settings().get("statusline") or {}).get("command", "")
    if not cmd:
        return ""
    if time.time() < _status_cache[1]:
        return _status_cache[0]
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2, encoding="utf-8", errors="replace",
                           input=json.dumps({"model": model, "cwd": os.getcwd(), "scope": bot.scope, "mode": bot.mode}))
        out = (p.stdout or "").strip().splitlines()[:1]
        _status_cache[0] = out[0][:200] if out else ""
    except Exception:
        _status_cache[0] = ""
    _status_cache[1] = time.time() + 3
    return _status_cache[0]


def toolbar_for(bot, model_fn, n_tools):
    def _tb():
        if _flash[0] and time.time() < _flash[1]:
            return HTML(f" <b>{_flash[0]}</b>")
        mode = "" if bot.mode == "default" else f"  │  <b>{bot.mode.replace('_', ' ')}</b>"
        if custom := _custom_status(bot, model_fn()):
            return HTML(" " + custom.replace("&", "&amp;").replace("<", "&lt;") + mode)
        return HTML(f" <b>{model_fn()}</b>  │  scope <b>{bot.scope}</b>  │  {n_tools} tools{mode}  │  "
                    f"<b>/</b> commands  ·  <b>Shift+Tab</b> mode  ·  <b>Alt+Enter</b> newline")
    return _tb


def prompt_text():
    return HTML("<prompt>❯ </prompt>")


def rule():
    console.print(Rule(style="#3b4261"))
