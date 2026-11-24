"""sparx: chat, one-shot ask, web UI, voice, background daemon, and admin commands."""
import argparse
import asyncio
import json
import os
import sys

from . import config, hooks, security, skills, slash, tools

try:
    from . import ui
except ImportError:      # rich / prompt_toolkit missing: plain terminal
    ui = None

HELP = """/tools              tool groups and what is loaded      /load GROUP     load a tool group
/scope NAME         switch memory scope (personal, work, study, or a project name)
/model [SPEC]       show models / switch (e.g. groq, gemini, ollama/gpt-oss:latest)  /models  pick from all with arrow keys
/usage              tokens (this session and the last 30 days), latency and tool calls
/audit [N]          recent actions                      /undo           reverse the last reversible action
/stop               emergency stop                      /resume         clear it
/plan               toggle plan mode (read-only until you approve a plan)
/compact            summarise old messages now to free context
/new                fresh conversation (memory is kept) /exit      Ctrl+C stops a running turn; twice at the prompt exits
/status /context /cost      where you are, what fills the context, token use
/mode [MODE]        default | accept_edits | plan | bypass   (Shift+Tab cycles)
/permissions [allow|ask|deny|rm RULE]   rules such as shell_run(git status*)
/rewind [N|list]    restore files changed in the last N turns   /export [FILE]   save the chat as Markdown
/resume ID, /sessions   switch to a stored conversation        /hooks /agents /mcp /skills /memory /config
/review /commit /test   canned jobs                           /output-style [NAME]   /statusline [CMD]   /add-dir PATH
@file attaches a file, !cmd runs a shell command, #note appends to SPARX.md"""


async def approve_in_terminal(tool, args) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        return False
    if ui:
        ans = await asyncio.to_thread(ui.approval_panel, tool, security.redact(args))
        return "always" if ans in ("a", "always") else ans in ("y", "yes")
    shown = json.dumps(security.redact(args), ensure_ascii=False)
    prompt = f"\n\x1b[33mAllow {tool.name} [{tool.risk}]\x1b[0m {shown[:600]}\n  y = yes, a = always this chat, anything else = no > "
    try:
        ans = (await asyncio.to_thread(input, prompt)).strip().lower()
        return "always" if ans in ("a", "always") else ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _show_event(ev: dict):
    if ev.get("type") == "tool_start" and ui and ui.activity.live is not None:
        ui.activity.tool(ev["tool"], ev["args"])
    elif ev.get("type") == "tool_start":
        args = ", ".join(f"{k}={repr(v)[:60]}" for k, v in ev["args"].items())
        print(f"\x1b[2m· {ev['tool']}({args})\x1b[0m", flush=True)


class Interrupted(Exception):
    pass


async def interruptible(coro):
    """Await coro; Ctrl+C meanwhile cancels just that work and raises Interrupted instead of ending the program."""
    import signal
    import threading
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro)
    old = None
    if threading.current_thread() is threading.main_thread():
        old = signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(task.cancel))
    try:
        return await task
    except asyncio.CancelledError:
        cur = asyncio.current_task()
        if task.cancelled() and not (cur and cur.cancelling()):
            raise Interrupted() from None
        raise
    finally:
        if old is not None:
            signal.signal(signal.SIGINT, old)


async def _pick_model() -> str:
    from . import llm, models
    chosen = await models.pick(models.available(refresh=True), llm.spec_for())
    if not chosen:
        return f"Unchanged: {llm.spec_for()}"
    config.set_key("models.default", chosen)
    return f"Using {llm.spec_for()}"


def _command(bot, line: str) -> str | None:
    from . import llm, undo
    cmd, _, arg = line[1:].partition(" ")
    arg = arg.strip()
    if cmd in ("exit", "quit", "q"):
        return None
    if cmd == "tools":
        return "\n".join(f"{'*' if c in bot.active else ' '} {c:<14} {len(ts):>3}  {tools.CATEGORIES.get(c, '')}"
                         for c, ts in sorted(tools.by_category().items()))
    if cmd == "load" and arg:
        bot.active.add(arg)
        return f"Loaded {arg}."
    if cmd == "scope":
        if arg:
            bot.set_scope(arg)
        return f"Memory scope: {bot.scope}"
    if cmd == "models":
        return _pick_model()
    if cmd == "model":
        from . import models
        if arg:
            if arg.partition("/")[0] not in config.bp.PROVIDERS:
                return f"Unknown provider in '{arg}'. Use provider/model, e.g. groq/openai/gpt-oss-120b. /models lets you pick."
            config.set_key("models.default", arg)
            return f"Using {llm.spec_for()}"
        return (f"Using {llm.spec_for()}\n\n{models.summary(models.available(refresh=True))}"
                "\n\n/models to browse and pick")
    if cmd == "usage":
        return json.dumps({"llm": llm.USAGE, "last_30_days": llm.usage_history(30)["totals"],
                           "tools": bot.last_ctx.stats if bot.last_ctx else {}}, indent=1)
    if cmd == "audit":
        rows = security.audit_log(int(arg) if arg.isdigit() else 15)
        return "\n".join(f"{r['ts']} {'ok ' if r['ok'] else 'ERR'} {r['tool']:<22} {r['args'][:70]}" for r in rows) or "Empty."
    if cmd == "undo":
        return undo.run()
    if cmd == "stop":
        security.request_stop()
        return "Emergency stop set."
    if cmd == "resume" and arg:
        return slash.run(bot, "resume", arg)
    if cmd == "resume":
        security.clear_stop()
        return "Stop cleared."
    if cmd == "compact":
        return bot.compact(force=True)          # a coroutine; the chat loops await it
    if cmd == "plan":
        from . import planmode
        now = not (bot.plan_mode if bot.plan_mode is not None else planmode.active(tools.Context()))
        bot.plan_mode = now
        return "Plan mode ON: only read-only tools run until you approve a plan." if now else "Plan mode off."
    if cmd == "new":
        bot.new_conversation()
        return "Started a fresh conversation."
    if cmd == "sessions":
        return slash.run(bot, "resume", "")
    if cmd in slash.NAMES and cmd not in slash.PROMPTS:
        return slash.run(bot, cmd, arg)         # a coroutine; the chat loops await it
    return HELP


def _prepare(bot, raw: str) -> tuple[str | None, str]:
    """(line for the chat loop or None when it was fully handled, text to show). Runs the @file / !cmd / #note
    shortcuts and canned prompts first, then expands a custom /command."""
    prompt, shown = slash.preprocess(bot, raw)
    if prompt is None:
        return None, shown
    return skills.prepare(bot, prompt), shown


async def chat():
    from .assistant import Sparx
    from . import llm
    bot = Sparx(approve=approve_in_terminal, emit=_show_event)
    name = config.settings()["name"]
    if ui and sys.stdin and sys.stdin.isatty():
        return await _chat_rich(bot, name, llm)
    print(f"{name} · {llm.spec_for()} · {len(tools.REGISTRY)} tools · /help for commands")
    for mod, err in tools.LOAD_ERRORS.items():
        print(f"\x1b[31m  tool module '{mod}' failed to load: {err}\x1b[0m")
    try:
        from .tools import sparx
        print(f"\n\x1b[32m{name}>\x1b[0m {sparx.greeting()}")
    except Exception:
        pass
    while True:
        try:
            line = (await asyncio.to_thread(input, "\n\x1b[36myou>\x1b[0m ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        line, shown = _prepare(bot, line)
        if line is None:
            print(shown)
            continue
        if shown:
            print(f"\x1b[2m{shown}\x1b[0m")
        if line.startswith("/"):
            out = _command(bot, line)
            if asyncio.iscoroutine(out):        # /compact is async
                out = await out
            if out is None:
                break
            print(out)
            continue
        try:
            print(f"\n\x1b[32m{name}>\x1b[0m {await bot.turn(line)}")
        except KeyboardInterrupt:
            print("(interrupted)")
        except llm.FirstRunError as e:
            print(f"\x1b[31m{e}\x1b[0m")
        except (Exception, SystemExit) as e:
            print(f"\x1b[31m{type(e).__name__}: {str(e)[:400]}\x1b[0m\n(/model to switch provider)")


async def _chat_rich(bot, name: str, llm):
    ui.banner(name, llm.spec_for(), len(tools.REGISTRY), bot.scope, tools.LOAD_ERRORS)
    try:
        from .tools import sparx
        ui.say(name, sparx.greeting())
    except Exception:
        pass
    hist = str(config.path("chat_history"))
    session = ui.make_session(hist, ui.toolbar_for(bot, llm.spec_for, len(tools.REGISTRY)), on_cycle=bot.cycle_mode)
    ui.watch_clipboard(session)
    hooks.session_start_sync()
    armed = False
    while True:
        try:
            ui.rule()
            line = (await session.prompt_async(ui.prompt_text())).strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            if armed:
                break
            armed = True
            ui.console.print("[grey50]  press Ctrl+C again to exit[/]")
            continue
        armed = False
        if not line:
            continue
        line, shown = _prepare(bot, line)
        if line is None:
            ui.info(shown)
            continue
        if shown:
            ui.console.print(f"[grey50]{shown}[/]")
        if line.startswith("/"):
            if line == "/vim":
                from prompt_toolkit.enums import EditingMode
                vi = session.app.editing_mode != EditingMode.VI
                session.app.editing_mode = EditingMode.VI if vi else EditingMode.EMACS
                ui.info("Vim keys on." if vi else "Vim keys off.")
                continue
            if line in ("/clear", "/cls"):
                ui.clear()
                continue
            if line in ("/help", "/?"):
                ui.help_panel()
                continue
            out = _command(bot, line)
            if asyncio.iscoroutine(out):        # /compact is async
                out = await out
            if out is None:
                break
            ui.info(out)
            continue
        ui.activity.start()
        try:
            reply = await interruptible(bot.turn(line))
            took = ui.activity.elapsed()
            ui.activity.stop()
            ui.say(name, reply, took)
        except (KeyboardInterrupt, Interrupted):
            ui.activity.stop()
            ui.console.print("[grey50]  interrupted; tell me what to do instead[/]")
        except llm.FirstRunError as e:
            ui.activity.stop()
            ui.error(str(e))
        except (Exception, SystemExit) as e:
            ui.activity.stop()
            ui.error(f"{type(e).__name__}: {str(e)[:400]}\n(/model to switch provider)")
        finally:
            ui.activity.stop()


async def ask(text: str, yes: bool, plan: bool = False) -> str:
    from .assistant import Sparx

    async def approve_all(tool, args):
        return True
    from . import llm
    try:
        bot = Sparx(approve=approve_all if yes else approve_in_terminal, emit=_show_event)
        if plan:
            bot.plan_mode = True
        return await bot.turn(text)
    except llm.FirstRunError as e:
        sys.exit(str(e))


def _ollama_models(base: str, timeout: float = 2) -> list[str]:
    """Names of the models the local Ollama has (its /api/tags). Raises OSError/httpx errors when it is not reachable."""
    import httpx
    r = httpx.get(base.removesuffix("/v1").rstrip("/") + "/api/tags", timeout=timeout)
    r.raise_for_status()
    return [m.get("name", "") for m in r.json().get("models", [])]


def _model_check(spec: str) -> list[str]:
    """What to tell the user about the model in use, without calling it."""
    import os
    from . import llm
    out = []
    wanted = config.settings()["models"]["default"] or os.environ.get("SPARX_MODEL") or config.bp.DEFAULT_MODEL
    want_key = config.bp.PROVIDERS.get(wanted.partition("/")[0], (None, None, None))[1]
    if spec == config.default_model() and wanted != spec and want_key:
        out.append(f"  fallback   {wanted} needs {want_key}, which is not set, so the local model above is in use; "
                   f"add {want_key}=<your key> to {config.HOME / '.env'}")
    provider, _, model = spec.partition("/")
    key_env = config.bp.PROVIDERS.get(provider, (None, None, None))[1]
    if key_env and provider != "custom":
        out.append(f"model check ok ({key_env} is set; no call made)" if os.environ.get(key_env) else llm.missing_key(key_env))
    elif provider == "ollama":
        model = model or config.bp.PROVIDERS["ollama"][2]
        try:
            have = _ollama_models(config.bp.PROVIDERS["ollama"][0])
        except Exception:
            out.append(llm.OLLAMA_DOWN)
        else:
            out.append("model check ok" if model in have or (":" not in model and model + ":latest" in have)
                       else llm.not_pulled(model))
    return out


def doctor() -> str:
    import importlib.util
    import os
    from . import llm
    tools.load_all()
    lines = [f"home        {config.HOME}", f"model       {llm.spec_for()}",
             f"tools       {len(tools.REGISTRY)} in {len(tools.by_category())} groups"]
    for mod, err in tools.LOAD_ERRORS.items():
        lines.append(f"  BROKEN    {mod}: {err}")
    for bad in config.problems():
        lines.append(f"  IGNORED   settings.json {bad}")
    lines[2:2] = _model_check(llm.spec_for())
    lines.append("providers   " + (", ".join(
        n for n, (_b, k, _d) in config.bp.PROVIDERS.items() if k and os.environ.get(k)) or "(no API keys set)"))
    optional = {"docx": "Word files", "pptx": "PowerPoint files", "openpyxl": "Excel files", "pypdf": "PDF reading",
                "faster_whisper": "speech-to-text", "edge_tts": "natural voices", "pyttsx3": "offline voice",
                "sounddevice": "microphone", "cv2": "webcam", "mss": "screenshots", "pyautogui": "desktop control",
                "pytesseract": "OCR", "paho": "MQTT devices", "playwright": "browser agents"}
    for mod, what in optional.items():
        lines.append(f"  {'ok     ' if importlib.util.find_spec(mod) else 'missing'} {mod:<15} {what}")
    try:                                    # fpdf (1.x) and fpdf2 share the import name, so the version matters
        import fpdf
        old = int(str(getattr(fpdf, "__version__", "1")).split(".")[0]) < 2
        lines.append(f"  {'old    ' if old else 'ok     '} {'fpdf':<15} PDF writing" + (
            " (old: no Unicode, no tables; pip uninstall fpdf, then pip install fpdf2)" if old else ""))
    except Exception:
        lines.append(f"  missing {'fpdf':<15} PDF writing (pip install fpdf2)")
    lines.append(f"stop flag  {'SET' if security.stop_requested() else 'clear'}")
    from . import joblock
    lines.append(f"jobs run by {joblock.describe()}")
    try:                                    # the phone: is adb there, and does the phone used before still answer
        from .tools import phone
        phone._exe()
        lines.append(f"phone      {'adb found; a phone was set up before (sparx ask to use it)' if phone.known() else 'adb found; no phone set up yet (docs: Controlling your PC and phone)'}")
    except tools.ToolError as e:
        lines.append(f"phone      {str(e).split('. ')[0][:110]}")
    return "\n".join(lines)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="sparx", description="SPARX: Sanjay Padmaraj's Autonomous Reasoning & Knowledge eXpert.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="talk to Sparx in the terminal (default)")
    a = sub.add_parser("ask", help="one question, one answer")
    a.add_argument("text", nargs="+")
    a.add_argument("-y", "--yes", action="store_true", help="approve every action without asking")
    a.add_argument("--plan", action="store_true", help="plan mode: only read-only tools run; the answer is a plan")
    r = sub.add_parser("run", help="non-interactive run for scripts and CI (exit 0 ok, 1 error, 2 needed approval)")
    r.add_argument("prompt", nargs="*", help="the prompt; '-' or nothing reads piped stdin")
    r.add_argument("--output-format", choices=("text", "json", "stream-json"), default="text")
    r.add_argument("--max-turns", type=int, help="most model rounds before it must summarise")
    r.add_argument("--allowed-tools", help="comma-separated tool names/globs; nothing else may run")
    r.add_argument("--deny-tools", help="comma-separated tool names/globs that may not run")
    r.add_argument("--permission-mode", choices=("deny", "yes"), default="deny",
                   help="deny (default): actions needing approval are refused; yes: approve them")
    r.add_argument("-y", "--yes", action="store_true", help="same as --permission-mode yes")
    r.add_argument("--continue", dest="continue_", action="store_true", help="resume the latest conversation")
    r.add_argument("--resume", metavar="ID", help="resume a conversation from `sparx sessions`")
    ss = sub.add_parser("sessions", help="list stored conversations")
    ss.add_argument("-n", type=int, default=20)
    s = sub.add_parser("serve", help="web UI (chat, voice, dashboards) for this PC and your phone")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    sub.add_parser("voice", help="hands-free voice mode with wake word")
    sub.add_parser("telegram", help="control Sparx from your Telegram chat (needs a bot token and chat id)")
    sub.add_parser("daemon", help="background: reminders, briefings, monitors, automations")
    t = sub.add_parser("team", help="run a goal through the multi-agent team")
    t.add_argument("goal", nargs="+")
    sub.add_parser("tools", help="list every tool")
    au = sub.add_parser("audit", help="recent actions")
    au.add_argument("-n", type=int, default=30)
    sub.add_parser("stop", help="emergency stop: halt all agents")
    sub.add_parser("resume", help="clear the emergency stop")
    i = sub.add_parser("init", help="write a SPARX.md (project guidance) for the current repo")
    i.add_argument("--force", action="store_true")
    c = sub.add_parser("config", help="show settings, or set one: sparx config permissions.send deny")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    sub.add_parser("doctor", help="check models, tools and optional dependencies")
    sub.add_parser("backup", help="copy the database and settings into ~/.sparx/backups now (checked, keeps the newest 7)")
    sub.add_parser("models", help="list the models you can use: Ollama, hosted providers with a key, the Kaggle notebook")
    k = sub.add_parser("kaggle-sync", help="write the current Kaggle server URL and token into ~/.sparx/.env")
    k.add_argument("gist", nargs="?", help="gist id (default: SPARX_KAGGLE_GIST from .env or the environment)")
    k.add_argument("--watch", action="store_true", help="keep checking, and start a new Kaggle session when none is up")
    k.add_argument("--now", action="store_true", help="start a session if none answers and wait for it")
    k.add_argument("--autostart", choices=("on", "off"), help="start --watch at every Windows sign-in, or stop doing so")
    args = p.parse_args()

    cmd = args.cmd or "chat"
    if cmd in ("chat", "serve", "voice", "telegram", "daemon"):
        from . import kaggle_sync
        kaggle_sync.start_background()      # loads the remote model server by itself when SPARX_KAGGLE_GIST is set
    if cmd == "chat":
        asyncio.run(chat())
    elif cmd == "run":
        from . import headless
        sys.exit(headless.main(args))
    elif cmd == "sessions":
        from . import headless
        for s in headless.sessions(args.n):
            print(f"{s['id']:<24} {s['messages']:>4} msgs  {s['last']}  {s['first_prompt']}")
    elif cmd == "ask":
        print(asyncio.run(ask(" ".join(args.text), args.yes, args.plan)))
    elif cmd == "serve":
        from . import server
        srv = config.settings()["server"]
        server.serve(args.host or srv["host"], args.port or srv["port"])
    elif cmd == "voice":
        from . import voice
        from .assistant import Sparx
        asyncio.run(voice.voice_loop(Sparx(approve=approve_in_terminal, emit=_show_event)))
    elif cmd == "telegram":
        from . import telegram_bot
        asyncio.run(telegram_bot.main())
    elif cmd == "daemon":
        from . import daemon
        asyncio.run(daemon.main())
    elif cmd == "team":
        from .agents import team
        tools.load_all()
        ctx = tools.Context(agent="orchestrator", approve=approve_in_terminal, emit=_show_event)
        result = asyncio.run(team.run_team(" ".join(args.goal), ctx))
        print(result.get("answer") or json.dumps(result, indent=1, default=str))
    elif cmd == "tools":
        tools.load_all()
        for cat, ts in sorted(tools.by_category().items()):
            print(f"\n{cat}: {tools.CATEGORIES.get(cat, '')}")
            for tl in ts:
                print(f"  {tl.name:<28} [{tl.risk:<7}] {tl.description[:90]}")
    elif cmd == "audit":
        for r in security.audit_log(args.n)[::-1]:
            print(f"{r['ts']} {'ok ' if r['ok'] else 'ERR'} {r['agent'] or '':<12} {r['tool']:<24} {r['args'][:80]}")
    elif cmd == "backup":
        from .tools import maintenance
        try:
            print(maintenance.backup()["path"])
        except tools.ToolError as e:
            sys.exit(f"Error: {e}")
    elif cmd == "models":
        from . import models
        print(models.text(models.available(refresh=True)))
        print("\nUse one: sparx config models.default <spec>")
    elif cmd == "kaggle-sync":
        from . import kaggle_sync
        if args.autostart:
            sys.exit(kaggle_sync.autostart(args.autostart == "on"))
        gist = args.gist or config.secret("SPARX_KAGGLE_GIST")
        if not gist:
            sys.exit("Give a gist id, or set SPARX_KAGGLE_GIST in " + str(config.HOME / ".env"))
        kernel = config.secret("SPARX_KAGGLE_KERNEL")
        try:
            if args.watch:
                kaggle_sync.watch(gist, kernel)
            elif args.now:
                sys.exit(0 if kaggle_sync.ensure(gist, kernel, wait=kaggle_sync.BOOT_WAIT) else "no model server came up")
            else:
                print(kaggle_sync.sync(gist))
        except (RuntimeError, KeyboardInterrupt) as e:
            sys.exit(str(e))
    elif cmd == "stop":
        security.request_stop()
        print("Emergency stop set. Every Sparx agent halts at its next step.")
    elif cmd == "resume":
        security.clear_stop()
        print("Stop cleared.")
    elif cmd == "init":
        from . import projectdocs
        print(projectdocs.init(".", args.force))
    elif cmd == "config":
        if args.key and args.value is not None:
            try:
                print(f"{args.key} = {json.dumps(config.set_key(args.key, args.value))}")
            except ValueError as e:
                sys.exit(f"Not saved. {e}")
        else:
            cur = config.settings()
            for part in (args.key or "").split("."):
                cur = cur.get(part, {}) if part else cur
            print(json.dumps(cur, indent=2, ensure_ascii=False))
    elif cmd == "doctor":
        print(doctor())


if __name__ == "__main__":
    main()
