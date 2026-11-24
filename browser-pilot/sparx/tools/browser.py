"""Browser: hand a task to browser-pilot's agents, which drive the user's real Chrome."""
import asyncio
import dataclasses
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import browserpilot.config as bp

from .. import config, security, undo
from . import B, I, O, REGISTRY, S, ToolError, category, tool
from .web import check_url

category("browser", "Drive the user's own Chrome (signed in as them) with autonomous agents; saved workflows")

_running: set[asyncio.Task] = set()
_stopped: set[asyncio.Task] = set()


def _workflows() -> dict:
    try:
        return json.loads(bp.WORKFLOWS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_workflows(data: dict):
    bp.WORKFLOWS.parent.mkdir(parents=True, exist_ok=True)
    bp.WORKFLOWS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


async def _run(ctx, task: str, agents: int = 1, url: str = "") -> str:
    try:
        from browserpilot.orchestrator import run_task
    except ImportError as e:
        raise ToolError(f"The browser agent needs Playwright: pip install playwright ({e}).")
    pages = config.path("browser_pages", mkdir=False)
    if url.lower().startswith("file:"):      # only .html pages Sparx itself wrote into ~/.sparx/browser_pages
        from urllib.parse import unquote, urlsplit
        raw = unquote(urlsplit(url).path)
        target = Path(raw.lstrip("/") if re.match(r"^/[A-Za-z]:", raw) else raw).resolve()
        if pages.resolve() not in target.parents or target.suffix.lower() not in (".html", ".htm") or not target.is_file():
            raise ToolError(f"Only .html pages inside {pages} can be opened as local files.")
    elif url:
        await check_url(url)      # same private-address check as the web tools
    s = config.settings()
    cfg = bp.RunConfig(model=config.default_model(), agents=max(1, min(int(agents or 1), s["limits"]["max_agents"])),
                       url=url or None, yes=False,     # keeps browser-pilot's own guard on buy / pay / send clicks
                       headless=bool((s["integrations"].get("browser") or {}).get("headless")),
                       local_roots=(pages,),
                       out=config.path("browser_runs", f"{datetime.now():%Y%m%d-%H%M%S}-{ctx.session}"))

    async def confirm(label: str, page_url: str) -> bool:
        # A buy, pay or send click goes to the same approval prompt as any other risky tool, never to the server console.
        if ctx.approve is None or ctx.data.get("automated") or security.stop_requested():
            return False
        click = dataclasses.replace(REGISTRY["browse"], name="browser_click", risk="send")      # shown to the user as a send-risk action
        return bool(await ctx.approve(click, {"browser_click": label, "page": page_url, "task": task[:200]}))
    cfg.confirm = confirm       # cfg.ask stays None: nobody can type an answer into a Sparx browser task

    async def guarded():
        try:
            return await run_task(task, cfg)
        except SystemExit as e:   # browser-pilot reports setup problems (no API key, no Chrome) this way
            raise ToolError(str(e))
    job = asyncio.ensure_future(guarded())
    _running.add(job)
    try:
        while not job.done():
            await asyncio.wait({job}, timeout=2)
            if not job.done() and security.stop_requested():
                _stopped.add(job)
                job.cancel()
        return job.result()
    except asyncio.CancelledError:
        if job in _stopped:
            raise ToolError("The browser task was stopped before it finished. Pages it had already changed stay changed.")
        job.cancel()      # this tool call itself was cancelled (timeout): take the browser agents down with it
        raise
    finally:
        _running.discard(job)
        _stopped.discard(job)


@tool("browse", "Give a task to browser agents that control the user's real Chrome, signed in as them, and get their "
      "report. Use for sites that need a login or clicking through pages; plain reading is faster with web_fetch. "
      "Describe the whole task with every detail; agents plan their own steps. Clicks on buttons labelled buy / pay / "
      "send / delete / submit and similar are put to the user for approval and refused when nobody can approve "
      "(background runs); other clicks and typing are NOT confirmed. Agents cannot ask the user questions. Can take minutes.",
      {"task": S, "agents": {**I, "description": "parallel agents for independent parts; 1 for one sequential flow"},
       "url": {**S, "description": "optional page to start on"}}, ["task"],
      category="browser", risk="control", untrusted=True, timeout=900)
async def browse(ctx, task: str, agents: int = 1, url: str = ""):
    return await _run(ctx, task, agents, url)


@tool("browser_stop", "Stop every browser task that is running right now.", category="browser", risk="write")
def browser_stop(ctx):
    jobs_ = [j for j in _running if not j.done()]
    for j in jobs_:
        _stopped.add(j)
        j.get_loop().call_soon_threadsafe(j.cancel)
    return f"Stopping {len(jobs_)} browser task(s)." if jobs_ else "No browser task is running."


@tool("browser_workflow_save", "Save a reusable browser task under a name. Write {placeholders} for the parts that "
      "change between runs. Shared with the `pilot` command's workflows.",
      {"name": S, "task": S, "replace": {**B, "description": "true to overwrite an existing workflow"}},
      ["name", "task"], category="browser", risk="write")
def browser_workflow_save(ctx, name: str, task: str, replace: bool = False):
    if ctx.approve is None or ctx.data.get("automated"):
        raise ToolError("Browser workflows are replayed later in the user's signed-in Chrome, so they are only saved "
                        "when the user is present to approve them.")
    if hits := security.scan_injection(task) + security.scan_injection(name):
        raise ToolError(f"Not saved: the task text looks like instructions aimed at the assistant ({'; '.join(hits[:3])}).")
    data = _workflows()
    if name in data and not replace:
        raise ToolError(f"A workflow named '{name}' already exists. Pass replace=true to overwrite it.")
    undo.record(ctx, "browser_workflow", f"Saved browser workflow '{name}'", {"name": name, "old": data.get(name)})
    data[name] = {"task": task, "flags": data.get(name, {}).get("flags", [])}
    _write_workflows(data)
    return {"saved": name, "placeholders": sorted(set(re.findall(r"\{(\w+)\}", task)))}


@undo.handler("browser_workflow")
def _undo_workflow(d):
    data = _workflows()
    if d["old"] is None:
        data.pop(d["name"], None)
    else:
        data[d["name"]] = d["old"]
    _write_workflows(data)


@tool("browser_workflow_list", "Saved browser workflows and the placeholders each one needs.", category="browser")
def browser_workflow_list(ctx):
    return [{"name": n, "task": w.get("task", ""), "placeholders": sorted(set(re.findall(r"\{(\w+)\}", w.get("task", ""))))}
            for n, w in _workflows().items()] or "No saved workflows."


@tool("browser_workflow_run", "Run a saved browser workflow, filling its placeholders.",
      {"name": S, "variables": {**O, "description": "placeholder values, e.g. {\"city\": \"Mysuru\"}"},
       "confirm": {**S, "description": "the confirm value the first call's error gave, after the user saw the task"}},
      ["name"],
      category="browser", risk="control", untrusted=True, timeout=900)
async def browser_workflow_run(ctx, name: str, variables: dict | None = None, confirm: str = ""):
    wf = _workflows().get(name)
    if not wf:
        raise ToolError(f"No workflow named '{name}'. Saved: {', '.join(_workflows()) or 'none'}.")
    code = hashlib.sha256(wf["task"].encode()).hexdigest()[:12]
    if confirm.strip() != code:
        raise ToolError(f"Nothing was run. Workflow '{name}' as saved now says: {wf['task'][:1500]!r}. To run exactly "
                        f"this, call browser_workflow_run again with confirm='{code}'.")
    values = {k: str(v) for k, v in (variables or {}).items()}
    for k, v in values.items():
        if len(v) > 500:
            raise ToolError(f"The value for '{k}' is longer than 500 characters.")
        if security.scan_injection(v):
            raise ToolError(f"The value for '{k}' looks like instructions aimed at the assistant; not run.")
    task = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), wf["task"])
    if missing := sorted(set(re.findall(r"\{(\w+)\}", task)) - set(values)):
        raise ToolError(f"Missing values for: {', '.join(missing)}. Ask the user.")
    flags = wf.get("flags") or []          # saved by `pilot`; only the agent count is honoured, never --yes
    agents = next((int(flags[i + 1]) for i, f in enumerate(flags[:-1]) if f == "--agents" and flags[i + 1].isdigit()), 1)
    return await _run(ctx, task, agents)
