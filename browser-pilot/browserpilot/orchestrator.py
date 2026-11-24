"""Plan -> run workers in parallel tabs -> merge their results."""
import asyncio
import json
import time

from playwright.async_api import async_playwright

from .agent import Agent
from .browser import Session, connect
from .config import RUNS_DIR, RunConfig
from .llm import LLM, extract_json

PLAN_SYSTEM = """You split a browser-automation task into subtasks for worker agents that run in parallel. Each worker has its own browser tab (sharing the user's logins) and cannot see the other workers or the original task.

Reply with ONLY JSON: {"subtasks": [{"task": "...", "url": "https://start-page or empty string"}]}

Rules:
- Split only when the parts are independent, such as different sites, items, or pages to research or process. Otherwise return exactly one subtask that repeats the original task word for word.
- At most N_MAX subtasks.
- Each subtask must be fully self-contained: repeat every detail, name, and constraint the worker needs, and say exactly what to report back.
- Never split one sequential flow (for example filling a form and then submitting it) across workers."""

MERGE_SYSTEM = """Several browser agents each completed part of a user's task. Combine their reports into one final answer to the original task. Use only information in the reports, keep concrete data (numbers, names, URLs), and say plainly if any part failed or is missing."""

COLORS = [36, 35, 32, 34, 33, 31]


def _logger(index: int, total: int):
    if total == 1:
        return lambda msg: print(f"\x1b[2m{msg}\x1b[0m", flush=True)
    color = COLORS[index % len(COLORS)]
    return lambda msg: print(f"\x1b[{color}m[A{index + 1}]\x1b[0m \x1b[2m{msg}\x1b[0m", flush=True)


async def plan(llm: LLM, task: str, max_agents: int) -> list[dict]:
    fallback = [{"task": task, "url": ""}]
    try:
        obj = extract_json(await llm.text(PLAN_SYSTEM.replace("N_MAX", str(max_agents)), task))
    except Exception as e:
        print(f"planner failed ({e}); running as one agent")
        return fallback
    subtasks = [
        {"task": s["task"].strip(), "url": str(s.get("url") or "").strip()}
        for s in (obj or {}).get("subtasks", [])
        if isinstance(s, dict) and isinstance(s.get("task"), str) and s["task"].strip()
    ]
    return subtasks[:max_agents] or fallback


async def run_task(task: str, cfg: RunConfig) -> str:
    out_dir = cfg.out or RUNS_DIR / time.strftime("%Y%m%d-%H%M%S")
    llm = LLM(cfg.model, concurrency=max(cfg.agents, 1))
    planner = LLM(cfg.planner_model) if cfg.planner_model else llm

    subtasks = [{"task": task, "url": ""}]
    if cfg.agents > 1:
        subtasks = await plan(planner, task, cfg.agents)
    if cfg.url and not subtasks[0]["url"] and len(subtasks) == 1:
        subtasks[0]["url"] = cfg.url
    total = len(subtasks)
    if total > 1:
        print(f"Split into {total} parallel agents:")
        for i, s in enumerate(subtasks):
            print(f"  A{i + 1}: {s['task'][:140]}")

    async with async_playwright() as p:
        context = await connect(p, cfg.headless)

        async def worker(i: int, sub: dict) -> str:
            log = _logger(i, total)
            session = Session(context, out_dir, auto_yes=cfg.yes, log=log, visual=not cfg.headless, local_roots=cfg.local_roots,
                              confirm=cfg.confirm, ask=cfg.ask)
            try:
                await session.start()
                if sub["url"]:
                    text, _ = await session.call("navigate", {"url": sub["url"]})
                    if text.startswith("Error:"):
                        log(text[:200])
                agent = Agent(f"A{i + 1}", llm, session, cfg.max_steps, cfg.vision, log)
                result = await agent.run(sub["task"])
                log("finished")
                return result
            except Exception as e:
                log(f"failed: {e}")
                return f"FAILED: {type(e).__name__}: {str(e)[:500]}"
            finally:
                if not cfg.keep_tabs:
                    await session.close()

        results = await asyncio.gather(*(worker(i, s) for i, s in enumerate(subtasks)))

    if total == 1:
        final = results[0]
    else:
        reports = "\n\n".join(f"### Agent {i + 1}\nSubtask: {s['task']}\nReport:\n{r}" for i, (s, r) in enumerate(zip(subtasks, results)))
        try:
            final = await planner.text(MERGE_SYSTEM, f"ORIGINAL TASK: {task}\n\n{reports}")
        except Exception as e:
            final = f"(could not merge reports: {e})\n\n{reports}"

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(json.dumps(
        {"task": task, "model": cfg.model, "subtasks": subtasks, "results": results, "final": final},
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\x1b[2mrun saved to {out_dir}\x1b[0m")
    return final
