"""Tool groups: what a fresh conversation sends the model, loading a group, and the budget that unloads idle ones."""
import json

import pytest

from sparx import agent as agent_mod
from sparx import config, db, tools
from sparx.agent import Agent
from sparx.assistant import Sparx

FILL = "x" * 3000           # makes each test group cost a little over 3,000 characters of schema


def _size(schemas) -> int:
    return len(json.dumps(schemas, ensure_ascii=False))


@pytest.fixture
def groups():
    """Four throwaway groups (tg_a .. tg_d), one tool each, removed again so no other test sees them."""
    tools.load_all()
    names = ["tg_a", "tg_b", "tg_c", "tg_d"]
    for g in names:
        tools.category(g, f"Test group {g}")
        tools.tool(f"{g}_echo", f"Echo for {g}. {FILL}", {"text": tools.S}, category=g)(lambda ctx, text="": f"echo {text}")
    yield names
    for g in names:
        tools.REGISTRY.pop(f"{g}_echo", None)
        tools.CATEGORIES.pop(g, None)


@pytest.fixture
def sent(fake_llm, monkeypatch):
    """Records the tool schemas of every model call: sent[i] is the list of tool names, sent.chars[i] their size."""
    class Names(list):                    # a plain list cannot take attributes
        chars: list

    names, inner = Names(), fake_llm.chat
    names.chars = []

    async def chat(messages, tool_schemas):
        names.append([s["function"]["name"] for s in tool_schemas])
        names.chars.append(_size(tool_schemas))
        return await inner(messages, tool_schemas)
    monkeypatch.setattr(fake_llm, "chat", chat)
    return names


def _results(messages) -> list[str]:
    return [m["content"] for m in messages if m.get("role") == "tool"]


def _fit(extra: int):
    """Set the schema budget to the always-on groups plus `extra` characters."""
    base = _size(tools.schemas((), config.settings()["always_on"]))
    config.set_key("limits.max_history_chars", (base + extra) // agent_mod.SCHEMA_BUDGET_FACTOR)
    assert base < agent_mod.schema_budget() <= base + extra


# ---- measurements, pinned loosely so growth is noticed (measured: 62 tools, 27,743 + 7,245 characters, 27 groups)

def test_a_fresh_conversation_sends_only_the_always_on_groups_within_the_pinned_size(sent):
    bot = Sparx()
    assert bot.active == set(config.settings()["always_on"]) == {"core", "memory", "productivity", "sparx"}
    schemas = tools.schemas((), bot.active)
    assert {tools.REGISTRY[s["function"]["name"]].category for s in schemas} == bot.active
    assert len(schemas) <= 70, "more tools are shown by default than the pinned 70 (measured 62)"
    assert _size(schemas) <= 31_000, "default tool schema grew past the pinned 31,000 characters (measured 27,743)"
    system = bot.system("hello")
    assert len(system) <= 8_500, "system prompt grew past the pinned 8,500 characters (measured 7,245)"
    assert _size(schemas) + len(system) <= 39_000                  # measured 34,988
    assert _size(schemas) <= agent_mod.schema_budget()             # the default set is inside its own budget


async def test_the_first_model_call_carries_exactly_the_measured_default(fake_llm, sent):
    fake_llm.script = ["Hello."]
    bot = Sparx()
    await bot.turn("hi")
    assert sorted(sent[0]) == sorted(s["function"]["name"] for s in tools.schemas((), config.settings()["always_on"]))
    assert fake_llm.seen[0][0]["role"] == "system" and fake_llm.seen[0][-1] == {"role": "user", "content": "hi"}


def test_tool_descriptions_stay_short():
    tools.load_all()
    real = [t for t in tools.REGISTRY.values() if not t.category.startswith(("tg_", "test", "tt"))]
    longest = max(real, key=lambda t: len(t.description))
    assert len(longest.description) <= 900, f"{longest.name} has a {len(longest.description)}-character description"
    assert sum(len(t.description) > 300 for t in real) <= 24       # measured 20
    assert _size([t.schema() for t in real]) <= 200_000            # every group loaded at once: measured 181,251


def test_the_prompt_lists_every_group_with_its_registry_description(groups):
    bot = Sparx()
    lines = {l.split(":")[0].lstrip("- ").rstrip("*"): l for l in bot.system().splitlines() if l.startswith("- ")}
    known = tools.by_category()
    for g in ("phone", "pc", "desktop", "sparx", "automation", "proactive", "plugins", "core", "web", "files"):
        assert g in known, g
    for g, ts in known.items():
        assert tools.CATEGORIES.get(g), f"group '{g}' has no category() description"
        assert tools.CATEGORIES[g] in lines[g] and f"({len(ts)} tools)" in lines[g], g
        assert lines[g].startswith(f"- {g}*:") == (g in bot.active), g
    assert "tg_a" in lines and "load_tools" in bot.system()        # built from the registry, so a new group cannot be missed
    assert sum(len(l) + 1 for g, l in lines.items() if not g.startswith("tg_")) <= 5_500     # measured 4,443


# ---- loading a group

async def test_load_tools_shows_the_group_in_the_next_call_and_in_the_next_turn(fake_llm, sent, groups):
    fake_llm.script = [("load_tools", {"groups": ["tg_a"]}), ("tg_a_echo", {"text": "one"}), "Done.", "Still here."]
    bot = Sparx()
    await bot.turn("use group a")
    assert "tg_a_echo" not in sent[0] and "tg_a_echo" in sent[1] and "tg_a_echo" in sent[2]
    assert "tg_a_echo" in _results(fake_llm.seen[1])[0]            # load_tools answered with the tool names
    assert _results(fake_llm.seen[2])[1] == "echo one"             # a loaded tool's result carries no note
    await bot.turn("and again")
    assert "tg_a_echo" in sent[3] and "- tg_a*:" in bot.system()


async def test_calling_a_tool_from_an_unloaded_group_runs_it_and_loads_the_group(fake_llm, sent, groups):
    fake_llm.script = [("tg_a_echo", {"text": "hi"}), "Done."]
    bot = Sparx()
    await bot.turn("echo hi")
    result = _results(fake_llm.seen[1])[0]
    assert result.startswith("echo hi") and "unknown tool" not in result
    assert '"tg_a"' in result and "loaded now" in result           # the model is told which group it was in
    assert "tg_a" in bot.active and "tg_a_echo" not in sent[0] and "tg_a_echo" in sent[1]


async def test_a_tool_that_does_not_exist_is_still_an_unknown_tool(fake_llm, sent):
    fake_llm.script = [("tg_no_such_tool", {}), "Sorry."]
    bot = Sparx()
    await bot.turn("go")
    assert _results(fake_llm.seen[1]) == ["Error: unknown tool 'tg_no_such_tool'."]
    assert bot.active == set(config.settings()["always_on"])


async def test_auto_loading_asks_first_exactly_as_before(fake_llm, sent, groups):
    """The call goes through the registry as it always did: a tool that needs approval is still not run."""
    ran = []
    tools.tool("tg_a_send", "Send something.", category="tg_a", risk="send")(lambda ctx: ran.append(1))
    try:
        fake_llm.script = [("tg_a_send", {}), "It needs your approval."]
        bot = Sparx()                                                # no approver: nobody to ask
        await bot.turn("send it")
        assert _results(fake_llm.seen[1])[0].startswith("Not done: 'tg_a_send'") and not ran
    finally:
        tools.REGISTRY.pop("tg_a_send", None)


async def test_agents_that_cannot_load_groups_are_not_widened(ctx, fake_llm, sent, groups):
    fake_llm.script = [("tg_a_echo", {"text": "x"}), "ok", ("tg_a_echo", {"text": "y"}), "ok"]
    await Agent("a", "sys", tool_names=["calculate"], strict=True).run("go", ctx)
    assert _results(fake_llm.seen[1]) == ["Error: 'tg_a_echo' is not one of your tools."]
    await Agent("a", "sys", tool_names=["calculate"]).run("go", ctx)     # not strict, but no load_tools either
    assert _results(fake_llm.seen[3]) == ["echo y"]
    assert not ctx.active and sent[1] == sent[3] == ["calculate"]


# ---- the budget

async def test_idle_groups_are_unloaded_when_the_schema_passes_the_budget(fake_llm, sent, groups):
    bot = Sparx()
    _fit(7000)                                                      # room for two test groups, not three
    always = set(config.settings()["always_on"])
    fake_llm.script = [("load_tools", {"groups": ["tg_a"]}), ("tg_a_echo", {}), "one",
                       ("load_tools", {"groups": ["tg_b"]}), ("tg_b_echo", {}), "two",
                       ("tg_a_echo", {}), "three",
                       ("load_tools", {"groups": ["tg_c"]}), "four", "five"]
    for text in ("first", "second", "third"):
        await bot.turn(text)
    assert bot.active == always | {"tg_a", "tg_b"}                 # inside the budget: nothing is unloaded
    assert not any("unloaded" in r for m in fake_llm.seen for r in _results(m))

    await bot.turn("fourth")                                        # tg_b is the one used longest ago
    assert bot.active == always | {"tg_a", "tg_c"}
    note = _results(fake_llm.seen[-1])[-1].splitlines()[-1]
    assert "tg_b" in note and "unloaded" in note and "load_tools" in note and "tg_a" not in note
    assert "tg_b_echo" not in sent[-1] and "tg_c_echo" in sent[-1] and "tg_a_echo" in sent[-1]
    assert sent.chars[-1] <= agent_mod.schema_budget()
    await bot.turn("fifth")
    assert bot.active == always | {"tg_a", "tg_c"} and "- tg_b:" in bot.system()


async def test_groups_used_in_this_turn_and_always_on_groups_are_never_unloaded(fake_llm, sent, groups):
    bot = Sparx()
    config.set_key("limits.max_history_chars", 100)                 # a budget nothing fits in
    always = set(config.settings()["always_on"])
    fake_llm.script = [("load_tools", {"groups": ["tg_a", "tg_b"]}), ("tg_a_echo", {}), ("tg_c_echo", {}), "one", "two"]
    await bot.turn("first")
    assert bot.active == always | {"tg_a", "tg_b", "tg_c"}
    assert {"tg_a_echo", "tg_b_echo", "tg_c_echo", "load_tools", "remember"} <= set(sent[3])
    assert not any("unloaded" in r for r in _results(fake_llm.seen[3]))

    await bot.turn("second")                                        # a new turn: they are idle now
    assert bot.active == always and {"load_tools", "remember"} <= set(sent[4]) and "tg_a_echo" not in sent[4]
    first = fake_llm.seen[4]
    assert first[-1]["role"] == "user" and first[-1]["content"].startswith("second\n")
    note = first[-1]["content"].splitlines()[-1]
    assert all(g in note for g in ("tg_a", "tg_b", "tg_c")) and "load_tools" in note
    assert "- tg_a:" in first[0]["content"] and "- core*:" in first[0]["content"]      # the catalogue agrees
    stored = [r["content"] for r in db.q("SELECT content FROM messages WHERE role = 'user' ORDER BY id")]
    assert stored == ["first", "second"]                            # the note is in the prompt copy only


async def test_a_long_conversation_does_not_accumulate_every_group(fake_llm, sent):
    bot = Sparx()
    names = sorted(set(tools.by_category()) - set(config.settings()["always_on"]))
    batches = [names[i:i + 2] for i in range(0, len(names), 2)]
    fake_llm.script = [x for b in batches for x in (("load_tools", {"groups": b}), "ok")]
    budget, starts = agent_mod.schema_budget(), []
    for b in batches:
        starts.append(len(sent))
        await bot.turn(f"load {b}")
    assert budget == 3 * 24000
    assert max(sent.chars[i] for i in starts) <= budget            # what each turn opens with
    assert _size(tools.schemas((), bot.active)) <= budget + max(
        _size(tools.schemas((), b)) for b in batches)               # only the newest batch may sit on top
    assert set(config.settings()["always_on"]) <= bot.active and set(batches[-1]) <= bot.active


def test_the_history_budget_is_a_promise_about_history_only():
    """limits.max_history_chars bounds the conversation history. The system prompt and the tool schemas are sent on
    top of it and have their own bound (schema_budget), so a prompt opens with at most the sum pinned here."""
    bot = Sparx()
    limit = config.settings()["limits"]["max_history_chars"]
    rows = [(bot.conversation, "assistant" if i % 2 else "user", "m" * 5000, db.now(), "personal") for i in range(30)]
    db.many("INSERT INTO messages (conversation, role, content, ts, scope) VALUES (?,?,?,?,?)", rows)
    assert sum(len(m["content"]) for m in bot.history()) <= limit
    assert agent_mod.schema_budget() == agent_mod.SCHEMA_BUDGET_FACTOR * limit
    assert limit + agent_mod.schema_budget() + len(bot.system()) <= 105_000
