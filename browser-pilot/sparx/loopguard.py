"""Loop and failure guard for the agent loop: spots repeated, alternating and repeatedly failing tool calls, and
turns common tool errors into a recovery hint. Thresholds live in settings `limits.loop_*`."""
import hashlib
import json
import re
from collections import deque

DEFAULTS = {"loop_guard": True, "loop_repeat_nudge": 3, "loop_repeat_stop": 6, "loop_fail_nudge": 3,
            "loop_fail_stop": 6, "loop_alternate_nudge": 6, "loop_alternate_stop": 10}

# (pattern on the error text, hint). First match wins.
HINTS = [
    (re.compile(r"old.?string|not found in|no match|did not match|could not find|text to replace", re.I),
     "The file probably changed or your text is stale: re-read the file, then copy the exact current text."),
    (re.compile(r"not recogni[sz]ed as|command not found|no such command|FileNotFoundError.*(exe|cmd)", re.I),
     "The command was not found: check it is installed and on PATH (try `where <cmd>`), or use another tool."),
    (re.compile(r"no such file|cannot find the (file|path)|FileNotFoundError|does not exist", re.I),
     "That path does not exist: list the folder or search for the right path before retrying."),
    (re.compile(r"permission denied|access is denied|PermissionError", re.I),
     "Access was refused: do not retry the same way; pick another location or tell the user."),
    (re.compile(r"timed out|TimeoutError", re.I),
     "It timed out: check whether the action partly happened before retrying, or try something smaller."),
    (re.compile(r"missing required argument|bad arguments|not a JSON object", re.I),
     "Fix the arguments (names, types, required fields) before calling again."),
    (re.compile(r"is not one of your tools", re.I),
     "That tool is not available: use only tools you were given."),
]


def hint(text: str) -> str:
    """Recovery hint for a tool error text, or ''."""
    if not isinstance(text, str) or not text.startswith("Error"):
        return ""
    return next((h for rx, h in HINTS if rx.search(text)), "")


def failed(text: str) -> bool:
    return isinstance(text, str) and text.startswith(("Error", "Not done"))


def _h(x) -> str:
    return hashlib.md5(str(x).encode("utf-8", "replace")).hexdigest()[:12]


class LoopGuard:
    """observe() each finished tool call; returns (level, note) with level 'ok' | 'nudge' | 'stop'."""

    def __init__(self, limits: dict | None = None):
        self.t = {**DEFAULTS, **{k: v for k, v in (limits or {}).items() if k in DEFAULTS}}
        self.calls: deque = deque(maxlen=max(24, int(self.t["loop_alternate_stop"]) + 2))   # (call key, result key)
        self.fails: dict = {}
        self.nudged: set = set()

    def _once(self, kind: str, level: str, note: str):
        """A given kind of nudge is sent once per escalation, not on every following call."""
        if level == "stop" or kind not in self.nudged:
            self.nudged.add(kind)
            return level, note
        return "ok", ""

    def observe(self, name: str, args, result: str) -> tuple[str, str]:
        t = self.t
        if not t["loop_guard"]:
            return "ok", ""
        try:
            key = _h(name + json.dumps(args, sort_keys=True, default=str))
        except Exception:
            key = _h(name + repr(args))
        rkey = _h(result)
        self.calls.append((key, rkey))

        if failed(result):                      # same tool failing the same way, with no success of that tool between
            fk = (name, _h(re.sub(r"\d+", "#", result[:120])))
            self.fails[fk] = self.fails.get(fk, 0) + 1
            n = self.fails[fk]
            if n >= t["loop_fail_stop"]:
                return "stop", f"'{name}' failed the same way {n} times ({result[:120]})"
            if n >= t["loop_fail_nudge"]:
                return self._once(f"fail:{fk}", "nudge", (
                    f"'{name}' has failed the same way {n} times. Stop retrying it unchanged: "
                    "re-read the file or state it works on, try a different approach, or ask the user."))
        else:
            for fk in [k for k in self.fails if k[0] == name]:
                del self.fails[fk]

        streak = 0                              # identical call with identical result, back to back
        for k, r in reversed(self.calls):
            if (k, r) != (key, rkey):
                break
            streak += 1
        if streak >= t["loop_repeat_stop"]:
            return "stop", f"'{name}' was called {streak} times in a row with the same arguments and result"
        if streak >= t["loop_repeat_nudge"]:
            return self._once(f"rep:{key}", "nudge", (
                f"You have called '{name}' with the same arguments {streak} times and got the same result. "
                "Repeating it will not change anything: use the result you have, try a different approach, or ask the user."))

        seq = list(self.calls)                  # A B A B ...: two distinct calls alternating with unchanged results
        for need, level in ((int(t["loop_alternate_stop"]), "stop"), (int(t["loop_alternate_nudge"]), "nudge")):
            tail = seq[-need:]
            if len(tail) == need and len({x for x in tail}) == 2 and tail[0] != tail[1] and \
                    all(tail[i] == tail[i + 2] for i in range(need - 2)):
                if level == "stop":
                    return "stop", f"alternating between two calls for {need} calls without progress"
                return self._once("alt", "nudge", (
                    "You are alternating between the same two calls with no new results. "
                    "Change approach, re-read what you are working on, or ask the user."))
        return "ok", ""
