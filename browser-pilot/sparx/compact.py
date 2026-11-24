"""Context compaction: old messages become a running summary instead of being cut off.

Two levels. `Sparx.compact()` summarises a conversation's oldest stored messages into a per-conversation summary row;
`shrink()` does the same inside one agent turn, first replacing old tool outputs with one-line placeholders.
A failed summary call never raises: callers fall back to plain truncation."""
from . import config, db, llm

db.schema("""CREATE TABLE IF NOT EXISTS conv_summary (
    conversation TEXT PRIMARY KEY, summary TEXT NOT NULL, upto_id INTEGER NOT NULL DEFAULT 0, ts TEXT NOT NULL);""")

KEEP_RECENT = 6             # messages kept verbatim after a compaction
THRESHOLD = 0.7             # compact when history uses this share of the budget
CHUNK_CHARS = 24000         # most text sent to the summariser in one call
MSG_CHARS = 2500            # per message cap in the summariser's input
KEEP_TOOL_RESULTS = 4       # newest tool outputs left intact when pruning
PRUNE_MIN = 300             # tool outputs shorter than this are not worth replacing

SYSTEM = ("You compress a conversation between a user and their assistant Sparx into a running summary that replaces "
          "the old messages. Write terse bullet points. You MUST preserve: decisions made and why; every file path, "
          "command, URL, name and number that may matter later; open todos and unfinished work; errors and how they "
          "were or were not resolved; the user's stated preferences and constraints. Drop pleasantries and repetition. "
          "Merge the new messages into the existing summary and keep it under 400 words. Output only the summary.")


def get_summary(conversation: str) -> tuple[str, int]:
    row = db.one("SELECT summary, upto_id FROM conv_summary WHERE conversation = ?", (conversation,))
    return (row["summary"], row["upto_id"]) if row else ("", 0)


def _render(messages: list[dict]) -> str:
    out = []
    for m in messages:
        text = m.get("content") or ""
        if not isinstance(text, str):
            text = str(text)
        if m.get("tool_calls"):
            names = ", ".join((c.get("function") or {}).get("name", "?") for c in m["tool_calls"])
            text = f"{text}\n[called tools: {names}]".strip()
        if len(text) > MSG_CHARS:
            text = text[:MSG_CHARS * 2 // 3] + "\n[... cut]\n" + text[-MSG_CHARS // 3:]
        out.append(f"{m.get('role', '?').upper()}: {text}")
    return "\n\n".join(out)


async def summarise(old_summary: str, messages: list[dict]) -> str:
    """Fold `messages` into `old_summary`. Raises if the model call fails or returns nothing."""
    summary, chunk, size = old_summary, [], 0
    batches = []
    for m in messages:
        n = min(len(m.get("content") or ""), MSG_CHARS) + 40
        if chunk and size + n > CHUNK_CHARS:
            batches.append(chunk)
            chunk, size = [], 0
        chunk.append(m)
        size += n
    if chunk:
        batches.append(chunk)
    for batch in batches:
        user = f"Existing summary:\n{summary or '(none yet)'}\n\nNew messages to merge in:\n{_render(batch)}"
        summary = (await llm.complete(SYSTEM, user, role="fast") or "").strip()
        if not summary:
            raise ValueError("empty summary")
    return summary


def should_compact(chars: int) -> bool:
    return chars > int(config.settings()["limits"]["max_history_chars"]) * THRESHOLD


async def compact_conversation(conversation: str, force: bool = False, keep: int = KEEP_RECENT) -> str:
    """Summarise the stored messages older than the last `keep` into the conversation's summary.
    Returns a one-line status. Never raises."""
    try:
        old, upto = get_summary(conversation)
        rows = db.q("SELECT id, role, content FROM messages WHERE conversation = ? AND id > ? ORDER BY id",
                    (conversation, upto))
        if not rows:
            return "Nothing to compact."
        if not force and not should_compact(sum(len(r["content"]) for r in rows)):
            return "History is within budget."
        cut = max(len(rows) - keep, 0)
        while cut < len(rows) and rows[cut]["role"] != "user":      # the kept part starts with a user message
            cut += 1
        if cut == 0 and force:
            cut = len(rows)
        if cut == 0:
            return "Too little history to compact."
        from . import hooks
        await hooks.fire_info("pre_compact", type("C", (), {"session": conversation, "agent": "sparx"})(),
                              messages=cut, forced=force)
        summary = await summarise(old, rows[:cut])
        db.x("INSERT INTO conv_summary (conversation, summary, upto_id, ts) VALUES (?,?,?,?) ON CONFLICT(conversation) "
             "DO UPDATE SET summary = excluded.summary, upto_id = excluded.upto_id, ts = excluded.ts",
             (conversation, summary, rows[cut - 1]["id"], db.now()))
        return f"Compacted {cut} messages into a {len(summary)}-character summary; {len(rows) - cut} kept verbatim."
    except Exception as e:
        return f"Could not compact ({type(e).__name__}: {str(e)[:200]}); falling back to truncation."


def _size(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or "")) for m in messages)


def prune_tool_outputs(messages: list[dict], keep: int = KEEP_TOOL_RESULTS) -> int:
    """Replace all but the newest `keep` long tool outputs with a one-line placeholder, in place. Returns how many."""
    idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    n = 0
    for i in idx[:-keep] if keep else idx:
        text = str(messages[i].get("content") or "")
        if len(text) >= PRUNE_MIN:
            first = text.strip().splitlines()[0][:80] if text.strip() else ""
            messages[i] = {**messages[i], "content": f"[old tool output removed: {len(text)} characters; began: {first}]"}
            n += 1
    return n


async def shrink(messages: list[dict], budget: int | None = None) -> list[dict]:
    """Keep one agent turn's messages under `budget` characters (limits.max_turn_chars, default 60000).
    Prunes old tool outputs first; if still over, summarises everything between the first two messages and the recent
    tail into one message. Returns the list to use (the input when nothing was needed or the summary failed)."""
    budget = budget or int(config.settings()["limits"].get("max_turn_chars", 60000))
    if _size(messages) <= budget * THRESHOLD:
        return messages
    messages = list(messages)
    prune_tool_outputs(messages)
    if _size(messages) <= budget * THRESHOLD or len(messages) < 8:
        return messages
    head = 2 if messages[1].get("role") == "user" else 1       # system prompt + the original task stay
    cut = len(messages) - 6
    while cut > head and messages[cut].get("role") == "tool":   # never split a tool call from its results
        cut -= 1
    if cut <= head:
        return messages
    try:
        summary = await summarise("", messages[head:cut])
    except Exception:
        return messages
    note = {"role": "user", "content": "[Summary of earlier steps in this task, replacing the old messages]\n" + summary}
    return [*messages[:head], note, *messages[cut:]]
