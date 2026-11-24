"""Non-interactive mode: `sparx run "prompt"` for scripts and CI, and `sparx sessions`.

Exit codes: 0 done, 1 error, 2 finished but at least one action needed an approval that was not given.
Approvals are denied unless --yes / --permission-mode yes: nobody is there to ask.
"""
import json
import secrets
import sys
import time
from datetime import datetime

from . import config, db, llm, security, skills, tools

FORMATS = ("text", "json", "stream-json")


def sessions(limit: int = 20) -> list[dict]:
    """Stored conversations, newest first."""
    rows = db.q("SELECT conversation AS id, COUNT(*) AS messages, MIN(ts) AS started, MAX(ts) AS last, MAX(id) AS last_id "
                "FROM messages GROUP BY conversation ORDER BY last_id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        first = db.one("SELECT content FROM messages WHERE conversation = ? AND role = 'user' ORDER BY id LIMIT 1", (r["id"],))
        out.append({"id": r["id"], "messages": r["messages"], "started": r["started"], "last": r["last"],
                    "first_prompt": (first["content"] if first else "")[:80].replace("\n", " ")})
    return out


def resolve_conversation(continue_: bool = False, resume: str = "") -> str:
    """Id of the conversation to resume, or '' for a new one. Raises ValueError when there is nothing to resume."""
    if resume:
        ids = [r["conversation"] for r in db.q("SELECT DISTINCT conversation FROM messages WHERE conversation LIKE ?",
                                               (resume.replace("%", "") + "%",))]
        exact = [i for i in ids if i == resume]
        if len(exact) == 1 or len(ids) == 1:
            return exact[0] if exact else ids[0]
        raise ValueError(f"No conversation '{resume}'. `sparx sessions` lists them." if not ids
                         else f"'{resume}' matches {len(ids)} conversations; give more of the id.")
    if continue_:
        row = db.one("SELECT conversation FROM messages ORDER BY id DESC LIMIT 1")
        if not row:
            raise ValueError("There is no earlier conversation to continue.")
        return row["conversation"]
    return ""


def _names(value) -> list[str]:
    return skills.tool_list(",".join(value) if isinstance(value, (list, tuple)) else value) or []


async def execute(prompt: str, *, allowed=None, denied=None, yes: bool = False, max_turns: int | None = None,
                  conversation: str = "", on_event=None) -> dict:
    """Run one prompt to completion and describe what happened. Never raises for model/tool trouble: ok=False instead."""
    from .assistant import Sparx
    started, before = time.time(), dict(llm.USAGE)
    calls, denials, pending = [], [], {}

    def emit(ev: dict):
        if ev.get("type") == "tool_start":
            pending[ev["tool"]] = {"tool": ev["tool"], "args": ev.get("args", {}), "risk": ev.get("risk")}
            calls.append(pending[ev["tool"]])
        elif ev.get("type") == "tool_end" and ev["tool"] in pending:
            pending.pop(ev["tool"])["ok"] = ev.get("ok", True)
        if on_event:
            on_event(ev)

    async def approve(tool, args) -> bool:
        if yes:
            return True
        denials.append({"tool": tool.name, "risk": tool.risk, "args": security.redact(args)})
        return False

    res = {"type": "result", "ok": True, "exit_code": 0, "result": "", "conversation": "", "tool_calls": calls,
           "approvals_denied": denials, "usage": {}, "duration_ms": 0}
    try:
        bot = Sparx(approve=approve, emit=emit)
        bot.conversation = conversation or datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        res["conversation"] = bot.conversation
        bot.max_rounds = max_turns
        tools.load_all()
        allow, deny = _names(allowed), _names(denied)
        skills.restrict(bot.ctx_data, allow, deny)
        for cat, ts in tools.by_category().items():         # the allowed tools must be visible to the model
            if allow and any(skills.matches(p, t.name) for p in allow for t in ts):
                bot.active.add(cat)
        res["result"] = await bot.turn(prompt)
    except llm.FirstRunError as e:
        res.update(ok=False, exit_code=1, error=str(e))
    except (Exception, SystemExit) as e:
        res.update(ok=False, exit_code=1, error=f"{type(e).__name__}: {str(e)[:400]}")
    if res["ok"] and denials:
        res["exit_code"] = 2
    after = llm.USAGE
    res["usage"] = {"calls": after["calls"] - before["calls"],
                    "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
                    "completion_tokens": after["completion_tokens"] - before["completion_tokens"]}
    res["duration_ms"] = int((time.time() - started) * 1000)
    return res


def read_prompt(words: list[str], stdin=None) -> str:
    """The prompt from the command line; '-' (or nothing) reads piped stdin, which is appended to any other words."""
    stdin = stdin if stdin is not None else sys.stdin
    text = " ".join(w for w in words if w != "-").strip()
    if (not words or "-" in words) and stdin and not stdin.isatty():
        piped = stdin.read().strip()
        text = f"{text}\n\n{piped}".strip()
    return text


def main(args, stdin=None, out=None) -> int:
    """Entry for `sparx run`. args: prompt, output_format, max_turns, allowed_tools, deny_tools, yes, permission_mode,
    continue_, resume. Returns the exit code."""
    import asyncio
    out = out or sys.stdout
    err = sys.stderr
    prompt = read_prompt(list(args.prompt), stdin)
    if not prompt:
        print("No prompt: give one as an argument or pipe it in (sparx run -).", file=err)
        return 1
    try:
        conversation = resolve_conversation(args.continue_, args.resume or "")
    except ValueError as e:
        print(str(e), file=err)
        return 1
    yes = args.yes or args.permission_mode == "yes"
    stream = args.output_format == "stream-json"

    def on_event(ev):
        if stream:
            print(json.dumps(ev, ensure_ascii=False, default=str), file=out, flush=True)
    res = asyncio.run(execute(prompt, allowed=args.allowed_tools, denied=args.deny_tools, yes=yes,
                              max_turns=args.max_turns, conversation=conversation, on_event=on_event))
    if args.output_format == "text":
        print(res["result"] if res["ok"] else "", file=out)
    else:
        print(json.dumps(res, ensure_ascii=False, default=str), file=out)
    if not res["ok"]:
        print(f"sparx run failed: {res.get('error')}", file=err)
    elif res["approvals_denied"]:
        names = ", ".join(sorted({d["tool"] for d in res["approvals_denied"]}))
        print(f"Needed approval and did not get it: {names}. Re-run with --yes to allow.", file=err)
    return res["exit_code"]
