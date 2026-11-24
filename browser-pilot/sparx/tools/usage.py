"""Read-only report of persisted LLM token usage."""
from .. import llm
from . import I, tool


@tool("usage_report", "Model usage over the last N days (default 30): calls and tokens per day and model, with totals. "
      "Use for 'how many tokens have I used'. Counts are only what the provider reported.",
      {"days": {**I, "description": "how many days back, default 30"}}, category="core")
def usage_report(ctx, days: int = 30):
    h = llm.usage_history(days)
    t = h["totals"]
    if not h["rows"]:
        return f"No model calls recorded in the last {h['days']} days."
    h["note"] = ("Tokens are only what the provider reported. "
                 + (f"{t['unreported_calls']} call(s) came back with no token counts, so their tokens are unknown "
                    "(stored as 0), not estimated." if t["unreported_calls"] else "Every recorded call reported counts."))
    return h
