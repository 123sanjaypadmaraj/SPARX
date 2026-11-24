import pytest

from sparx import compact, config, db
from sparx.assistant import Sparx


def add(conv, n, size=1100):
    rows = [(conv, "user" if i % 2 == 0 else "assistant", f"m{i} " + "x" * size, db.now(), "personal") for i in range(n)]
    db.many("INSERT INTO messages (conversation, role, content, ts, scope) VALUES (?,?,?,?,?)", rows)


async def test_under_threshold_does_nothing(fake_llm):
    bot = Sparx()
    add(bot.conversation, 4, 100)
    assert "within budget" in await bot.compact()
    assert fake_llm.prompts == [] and compact.get_summary(bot.conversation) == ("", 0)


async def test_compaction_stores_summary_and_keeps_recent(fake_llm):
    bot = Sparx()
    add(bot.conversation, 20)          # 22000 chars > 70% of 24000
    fake_llm.script = ["- decided X in C:/a.py"]
    out = await bot.compact()
    assert out.startswith("Compacted")
    summary, upto = compact.get_summary(bot.conversation)
    assert summary == "- decided X in C:/a.py" and upto > 0
    system, user = fake_llm.prompts[0]
    assert "file path" in system and "open todos" in system and "errors" in system
    hist = bot.history()
    assert hist[0]["role"] == "user" and hist[0]["content"].startswith("m1")        # recent kept verbatim
    assert "m0 " not in " ".join(m["content"] for m in hist)
    assert "decided X" in bot.system("hi")


async def test_summary_is_rolled_forward(fake_llm):
    bot = Sparx()
    add(bot.conversation, 20)
    fake_llm.script = ["S1", "S2"]
    await bot.compact()
    add(bot.conversation, 20)
    await bot.compact()
    assert compact.get_summary(bot.conversation)[0] == "S2"
    assert "S1" in fake_llm.prompts[1][1]


async def test_failure_falls_back_to_truncation(fake_llm, monkeypatch):
    bot = Sparx()
    add(bot.conversation, 20)

    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(compact.llm, "complete", boom)
    assert "falling back" in await bot.compact()
    assert compact.get_summary(bot.conversation) == ("", 0)
    budget = config.settings()["limits"]["max_history_chars"]
    assert sum(len(m["content"]) for m in bot.history()) <= budget


async def test_turn_survives_failed_summary(fake_llm, monkeypatch):
    bot = Sparx()
    add(bot.conversation, 20)

    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(compact.llm, "complete", boom)
    fake_llm.script = ["ok"]
    assert await bot.turn("hello") == "ok"


def test_prune_old_tool_outputs():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for i in range(7):
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f"}}]},
                 {"role": "tool", "content": f"R{i} " + "y" * 1000}]
    assert compact.prune_tool_outputs(msgs, keep=2) == 5
    tool = [m["content"] for m in msgs if m["role"] == "tool"]
    assert tool[0].startswith("[old tool output removed") and "R0" in tool[0] and len(tool[0]) < 150
    assert tool[-1].startswith("R6") and tool[-2].startswith("R5")


async def test_shrink_prunes_first_then_summarises(fake_llm):
    def build(n):
        m = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
        for i in range(n):
            m += [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f"}}]},
                  {"role": "tool", "tool_call_id": str(i), "content": "z" * 2000}]
        return m
    small = build(3)
    assert await compact.shrink(small, budget=100000) is small
    pruned = await compact.shrink(build(10), budget=26000)          # 40000 chars; pruning is enough
    assert fake_llm.prompts == [] and sum("removed" in str(m["content"]) for m in pruned) == 6
    fake_llm.script = ["SUMMARY"]
    big = await compact.shrink(build(10), budget=300)               # pruning cannot be enough
    assert big[1]["content"] == "task" and "SUMMARY" in big[2]["content"]
    assert big[3]["role"] != "tool" and len(big) < 22


async def test_shrink_failure_keeps_messages(fake_llm, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(compact.llm, "complete", boom)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for i in range(10):
        msgs += [{"role": "assistant", "content": "a" * 500}, {"role": "user", "content": "u" * 500}]
    out = await compact.shrink(msgs, budget=300)
    assert len(out) == len(msgs)
