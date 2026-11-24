"""pilot: drive your Chrome with any LLM.

  pilot "find the 3 cheapest flights from BLR to DEL next Friday"
  pilot -m ollama/gpt-oss:latest --agents 1 "summarise the front page of news.ycombinator.com"
  pilot save prices "compare the price of {item} on amazon.in and flipkart.com" --agents 2
  pilot run prices item="sony wh-1000xm5"
  pilot chat                              personal assistant with memory; browses for you when needed
  pilot ask "what's on my calendar today?"   one assistant turn, no prompt loop
  pilot login https://mail.google.com     sign in once; the profile is reused by every run
  pilot list | rm NAME | models | stop
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from .config import DEFAULT_MODEL, HOME, PROVIDERS, WORKFLOWS, RunConfig


def _options() -> argparse.ArgumentParser:
    o = argparse.ArgumentParser(add_help=False)
    o.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"provider/model (default {DEFAULT_MODEL})")
    o.add_argument("--planner-model", help="model for splitting the task and merging results (default: same as --model)")
    o.add_argument("-a", "--agents", type=int, default=4, help="max parallel agents; 1 disables planning (default 4)")
    o.add_argument("--max-steps", type=int, default=30, help="tool calls allowed per agent (default 30)")
    o.add_argument("--url", help="page to start on (single-agent runs)")
    o.add_argument("--headless", action="store_true", help="run Chrome without a window")
    o.add_argument("-y", "--yes", action="store_true", help="don't ask before risky clicks (buy, delete, send, ...)")
    o.add_argument("--vision", action="store_true", help="send screenshots to the model (needs a vision model)")
    o.add_argument("--keep-tabs", action="store_true", help="leave the agents' tabs open when done")
    o.add_argument("--out", type=Path, help="folder for screenshots, saved files and result.json")
    return o


def _config(ns) -> RunConfig:
    return RunConfig(model=ns.model, planner_model=ns.planner_model, agents=ns.agents, max_steps=ns.max_steps,
                     headless=ns.headless, yes=ns.yes, vision=ns.vision, url=ns.url, out=ns.out, keep_tabs=ns.keep_tabs)


def _load_workflows() -> dict:
    return json.loads(WORKFLOWS.read_text(encoding="utf-8")) if WORKFLOWS.is_file() else {}


def _save_workflows(data: dict):
    HOME.mkdir(parents=True, exist_ok=True)
    WORKFLOWS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _execute(task: str, cfg: RunConfig):
    from .orchestrator import run_task
    final = asyncio.run(run_task(task, cfg))
    print("\n" + final)


def _do(argv):
    p = argparse.ArgumentParser(prog="pilot", parents=[_options()], description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("task", nargs="+", help="what to do, in plain language")
    ns = p.parse_args(argv)
    _execute(" ".join(ns.task), _config(ns))


def _save(argv):
    if len(argv) < 2:
        raise SystemExit('usage: pilot save NAME "task with optional {placeholders}" [options]')
    name, task, flags = argv[0], argv[1], argv[2:]
    _options().parse_args(flags)  # validate now rather than at run time
    data = _load_workflows()
    data[name] = {"task": task, "flags": flags}
    _save_workflows(data)
    print(f"Saved '{name}'. Run it with: pilot run {name}")


def _run(argv):
    if not argv:
        raise SystemExit("usage: pilot run NAME [key=value ...] [options]")
    wf = _load_workflows().get(argv[0])
    if not wf:
        raise SystemExit(f"No workflow named '{argv[0]}'. See: pilot list")
    variables, flags = {}, []
    for tok in argv[1:]:
        m = re.match(r"^([A-Za-z_]\w*)=(.*)$", tok)
        if m and not flags:
            variables[m.group(1)] = m.group(2)
        else:
            flags.append(tok)
    task = re.sub(r"\{(\w+)\}", lambda m: variables.get(m.group(1), m.group(0)), wf["task"])
    missing = sorted(set(re.findall(r"\{(\w+)\}", task)))
    if missing:
        raise SystemExit("Missing values: " + " ".join(f"{k}=..." for k in missing))
    ns = _options().parse_args(wf["flags"] + flags)  # run-time flags override saved ones
    _execute(task, _config(ns))


def _list(_argv):
    data = _load_workflows()
    if not data:
        print('No saved workflows. Create one with: pilot save NAME "task"')
    for name, wf in data.items():
        print(f"{name}: {wf['task']}" + (f"   [{' '.join(wf['flags'])}]" if wf["flags"] else ""))


def _rm(argv):
    data = _load_workflows()
    if not argv or argv[0] not in data:
        raise SystemExit("usage: pilot rm NAME (see: pilot list)")
    del data[argv[0]]
    _save_workflows(data)
    print(f"Removed '{argv[0]}'.")


def _chat(argv):
    from .assistant import chat
    asyncio.run(chat(_config(_options().parse_args(argv))))


def _ask(argv):
    from .assistant import ask
    p = argparse.ArgumentParser(prog="pilot ask", parents=[_options()])
    p.add_argument("question", nargs="+")
    ns = p.parse_args(argv)
    print(asyncio.run(ask(_config(ns), " ".join(ns.question))))


def _login(argv):
    from playwright.async_api import async_playwright
    from .browser import connect

    async def go():
        async with async_playwright() as p:
            context = await connect(p, headless=False)
            page = await context.new_page()
            if argv:
                await page.goto(argv[0] if "://" in argv[0] else "https://" + argv[0])
    asyncio.run(go())
    print("Chrome is open with pilot's own profile. Sign in to the sites you need; they stay signed in for future runs.")


def _stop(_argv):
    from playwright.async_api import async_playwright
    from .browser import stop_chrome

    async def go():
        async with async_playwright() as p:
            return await stop_chrome(p)
    print("Closed pilot's Chrome." if asyncio.run(go()) else "Pilot's Chrome is not running.")


def _models(_argv):
    print(f"Default: {DEFAULT_MODEL}  (change with -m or BROWSERPILOT_MODEL)\n")
    for name, (base, key_env, default) in PROVIDERS.items():
        state = "no key needed" if not key_env else f"{key_env} {'set' if os.environ.get(key_env) else 'NOT set'}"
        print(f"{name + '/' + (default or '<model>'):<34} {state:<28} {base or '(set BROWSERPILOT_BASE_URL)'}")
    print(f"\nKeys can live in {HOME / '.env'} as KEY=value lines.")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.platform == "win32":
        os.system("")  # enables ANSI colours in older Windows consoles
    os.environ.setdefault("NODE_OPTIONS", "--no-deprecation")  # quiet Playwright's node driver
    argv = sys.argv[1:]
    handlers = {"save": _save, "run": _run, "list": _list, "rm": _rm, "login": _login, "stop": _stop, "models": _models,
                "chat": _chat, "ask": _ask}
    try:
        if argv and argv[0] in handlers:
            handlers[argv[0]](argv[1:])
        else:
            _do(argv)
    except KeyboardInterrupt:
        raise SystemExit("\nInterrupted.")


if __name__ == "__main__":
    main()
