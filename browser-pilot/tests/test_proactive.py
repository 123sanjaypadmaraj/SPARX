import json
from datetime import datetime, timedelta

import pytest

from sparx import assistant, config, daemon, db, jobs, proactive, security, tools  # noqa: F401  (assistant: messages table)
from sparx.tools import ToolError
from sparx.tools import productivity  # noqa: F401  (tasks, goals tables)

from conftest import run_tool

tools.load_all()   # before any stand-in: a later first load would replace it with the real (networked) tool

NINE = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)


def iso(d):
    return d.isoformat(timespec="seconds")


def notes(kind=None):
    return [n for n in reversed(jobs.unread()) if kind in (None, n["kind"])]


def add_task(title, due):
    return db.x("INSERT INTO tasks (title, due, created, updated) VALUES (?,?,?,?)", (title, iso(due), db.now(), db.now()))


def say(text, when):
    db.x("INSERT INTO messages (conversation, role, content, ts) VALUES ('c', 'user', ?, ?)", (text, iso(when)))


def used(session, when, *names, ok=1):
    for name in names:
        db.x("INSERT INTO audit (ts, session, agent, tool, risk, args, ok, summary, ms) VALUES (?,?,?,?,?,?,?,?,?)",
             (iso(when), session, "sparx", name, "read", "{}", ok, "", 5))


@pytest.fixture
def fake_tool(monkeypatch):
    """Put a stand-in for another module's tool in the registry for one test. Returns the list of calls it got."""
    def make(name, result, risk="read"):
        calls = []

        def fn(ctx, **args):
            calls.append(args)
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setitem(tools.REGISTRY, name, tools.Tool(name, "stand-in", {}, [], "testing", risk, True, 60, fn))
        return calls
    return make


async def test_briefing_fires_once_per_day_at_its_time():
    add_task("File the tax return", NINE.replace(hour=17))
    assert await proactive.daily_briefing(now=NINE.replace(hour=7, minute=59)) is None and notes() == []
    body = await proactive.daily_briefing(now=NINE)
    (n,) = notes()
    assert "File the tax return" in body and n["kind"] == "briefing" and "08:00" in n["why"]
    assert await proactive.daily_briefing(now=NINE.replace(minute=30)) is None and len(notes()) == 1
    assert await proactive.daily_briefing(now=NINE + timedelta(days=1)) and len(notes("briefing")) == 2


async def test_briefing_is_quiet_with_nothing_to_say_and_respects_its_flag(fake_tool):
    searched = fake_tool("web_search", {"results": [{"title": "Rust 2.0 released", "url": "https://example.org/r"}]})
    assert await proactive.daily_briefing(now=NINE) is None and notes() == [] and searched == []

    add_task("Something due", NINE.replace(hour=18))
    config.set_key("proactive.daily_briefing", False)
    assert await proactive.daily_briefing(now=NINE + timedelta(days=1)) is None and notes() == []

    config.set_key("proactive.daily_briefing", "10:30")
    config.set_key("proactive.news_topics", ["rust"])
    two = NINE + timedelta(days=2)
    assert await proactive.daily_briefing(now=two) is None                       # 09:00 is before 10:30
    body = await proactive.daily_briefing(now=two.replace(hour=10, minute=30))
    assert "Headlines on rust (from the web, not verified): Rust 2.0 released" in body
    assert searched == [{"query": "rust", "kind": "news", "max_results": 3}] and "10:30" in notes()[0]["why"]


async def test_dedupe_never_says_the_same_thing_twice():
    assert proactive.tell("k1", "Title", "Body", why="because") and not proactive.tell("k1", "Title", "Body", why="because")
    assert len(notes()) == 1 and notes()[0]["why"] == "because"

    add_task("Overdue report", NINE - timedelta(days=2))
    (r,) = await proactive.deadline_risk(now=NINE)
    assert r["risk"] == "overdue" and await proactive.deadline_risk(now=NINE + timedelta(hours=1)) == []
    risk = [n for n in notes() if "Overdue report" in n["title"]]
    assert len(risk) == 1 and risk[0]["why"] and "changed nothing" in risk[0]["body"]
    add_task("Second one", NINE - timedelta(days=1))
    assert [x["title"] for x in await proactive.deadline_risk(now=NINE)] == ["Second one"]   # only the new one


async def test_flags_switch_each_behaviour_off():
    add_task("Overdue report", NINE - timedelta(days=2))
    config.set_key("proactive.deadline_risk", False)
    assert await proactive.deadline_risk(now=NINE) is None and notes() == []
    config.set_key("proactive.deadline_risk", True)
    config.set_key("proactive.enabled", False)                                   # master switch
    assert await proactive.deadline_risk(now=NINE) is None and notes() == []
    assert db.q("SELECT * FROM proactive_runs") == []                            # it did not even count as a run


async def test_jobs_only_run_read_only_tools_by_themselves(fake_tool):
    config.set_key("permissions.send", "allow")                                  # even when the user allows sending
    calls = fake_tool("email_follow_ups", [{"id": 1, "subject": "Quote", "to": "a@b.c"}], risk="send")
    out = await proactive.follow_ups(now=NINE)
    assert out["mail"] == [] and calls == [] and notes() == []
    ok, why = await proactive._ask("email_follow_ups", {}, tools.Context(approve=None))
    assert not ok and "not read-only" in why
    assert all(not hasattr(proactive, n) for n in ("automation", "undo"))        # nothing here can start a recipe


async def test_mail_follow_ups_notify_once(fake_tool):
    fake_tool("email_follow_ups", {"follow_ups": [{"id": 7, "subject": "Invoice 12", "to": "sam@example.org"}]})
    assert len((await proactive.follow_ups(now=NINE))["mail"]) == 1
    assert (await proactive.follow_ups(now=NINE + timedelta(hours=1)))["mail"] == []
    (n,) = notes()
    assert "Invoice 12 (sam@example.org)" in n["body"] and "not replied" in n["body"] and n["why"]


async def test_promises_from_chat_are_checked_once_a_day_and_must_quote_the_user(fake_llm):
    assert (await proactive.follow_ups(now=NINE - timedelta(days=1)))["promises"] == [] and fake_llm.prompts == []
    say("I'll send Priya the budget deck by Friday", NINE - timedelta(hours=3))
    say("what's the weather", NINE - timedelta(hours=2))
    fake_llm.script = [{"promises": [
        {"quote": "I'll send Priya the budget deck by Friday", "what": "send Priya the budget deck", "who": "Priya", "when": "Friday"},
        {"quote": "I will call the bank", "what": "call the bank"}]}]             # not something the user wrote
    told = (await proactive.follow_ups(now=NINE))["promises"]
    (n,) = notes()
    assert len(told) == 1 and "send Priya the budget deck" in n["title"] and "who: Priya." in n["body"] and n["why"]
    assert (await proactive.follow_ups(now=NINE.replace(hour=15)))["promises"] == [] and len(fake_llm.prompts) == 1
    assert (await proactive.follow_ups(now=NINE + timedelta(days=1)))["promises"] == []
    assert len(fake_llm.prompts) == 1                                            # no new messages: no model call


async def test_routine_detection_on_seeded_audit_rows():
    for i, day in enumerate((2, 5, 9)):
        when = NINE - timedelta(days=day)
        used(f"s{i}", when, "task_list", "web_search", "web_search", "note_add")
        used(f"s{i}", when, "calc" if i else "weather")                          # noise that does not repeat
    used("s9", NINE - timedelta(days=1), "task_list", "web_search")              # the pair alone, once more
    used("old", NINE - timedelta(days=60), "task_list", "web_search", "note_add")
    monday = NINE - timedelta(days=NINE.weekday() + 7)
    for d, minute in ((0, 0), (1, 10), (2, 5)):
        say("Summarise my unread email, please!", monday + timedelta(days=d, minutes=minute))
    say("thanks", monday)

    found = {r["key"]: r for r in proactive.find_routines(now=NINE)}
    seq = found["seq:task_list>web_search>note_add"]
    assert seq["occasions"] == 3 and [r for r in found.values() if r["kind"] == "tool_sequence"] == [seq]
    (req,) = [r for r in found.values() if r["kind"] == "request"]
    assert req["days"] == 3 and req["schedule"] == "weekdays 09:05" and req["text"].startswith("Summarise my unread")

    assert len(await proactive.routine_detection(now=NINE)) == 2
    assert all("automate" in n["body"] and "nothing is set up until you do" in n["body"] and n["why"] for n in notes())
    assert await proactive.routine_detection(now=NINE + timedelta(days=1)) == [] and len(notes()) == 2
    assert db.q("SELECT * FROM automations") == []                               # it suggests; it creates nothing


async def test_goal_intervention_once_a_week():
    old = iso(NINE - timedelta(days=30))
    db.x("INSERT INTO goals (title, created, updated) VALUES ('Learn Kannada', ?, ?)", (old, old))
    assert len(await proactive.goal_intervention(now=NINE)) == 1
    assert await proactive.goal_intervention(now=NINE + timedelta(hours=6)) == []
    assert len(await proactive.goal_intervention(now=NINE + timedelta(days=7))) == 1
    assert len(notes()) == 2 and "no progress for" in notes()[0]["why"]


async def test_anomaly_digest_is_daily_and_quiet_when_all_is_well():
    used("s", NINE - timedelta(hours=2), "web_fetch")
    assert await proactive.anomaly_digest(now=NINE) == [] and notes() == []
    used("s", NINE - timedelta(hours=1), "web_fetch", "web_fetch", "web_fetch", ok=0)
    assert await proactive.anomaly_digest(now=NINE) == ["web_fetch failed 3 of 4 times"]
    assert await proactive.anomaly_digest(now=NINE.replace(hour=12)) == []
    (n,) = notes()
    assert n["kind"] == "digest" and "web_fetch failed" in n["body"] and n["why"]


async def test_opportunities_only_for_listed_interests(fake_tool):
    calls = fake_tool("web_search", {"results": [{"title": "New telescope", "url": "https://example.org/t"}]})
    assert await proactive.opportunities(now=NINE) == [] and calls == []         # no interests configured
    config.set_key("proactive.interests", ["astronomy"])
    assert len(await proactive.opportunities(now=NINE)) == 1
    assert await proactive.opportunities(now=NINE + timedelta(days=1)) == []     # same link: nothing new
    (n,) = notes()
    assert n["title"] == "New on astronomy" and "not verified" in n["body"] and "astronomy" in n["why"]


async def test_analytics_report_and_weekly_summary(ctx):
    monday = (NINE - timedelta(days=NINE.weekday())).replace(hour=10)
    assert await proactive.weekly_analytics(now=monday) is None and notes() == []    # an empty week says nothing
    db.x("INSERT INTO tasks (title, status, completed, created, updated) VALUES ('Ship v1', 'done', ?, ?, ?)",
         (iso(monday + timedelta(days=5)),) * 3)
    used("s", monday + timedelta(days=5), "web_search", "web_search", "task_list")
    db.x("INSERT INTO time_entries (activity, starts, minutes) VALUES ('writing', ?, 90)", (iso(monday + timedelta(days=5)),))
    say("plan my week please", monday + timedelta(days=5, hours=4))

    a = proactive.analytics(7, monday + timedelta(days=7))
    assert a["tasks"]["completed"] == 1 and a["tool_usage"]["calls"] == 3 and a["tool_usage"]["top_tools"]["web_search"] == 2
    assert a["tool_usage"]["by_group"]["web"] == 2 and a["time"]["tracked_minutes"] == {"writing": 90}
    assert a["conversations"] == {"requests": 1, "by_scope": {"personal": 1}, "busiest_hour": 14}

    nxt = monday + timedelta(days=7)
    assert await proactive.weekly_analytics(now=nxt.replace(hour=8)) is None     # Monday before 09:00
    assert (await proactive.weekly_analytics(now=nxt))["tasks"]["completed"] == 1
    assert await proactive.weekly_analytics(now=nxt + timedelta(days=2)) is None and len(notes()) == 1
    assert "1 completed" in notes()[0]["body"] and "writing 90 min" in notes()[0]["body"]
    assert '"summary": "Tasks:' in await run_tool("analytics_report", ctx, days=30)


async def test_background_research_queue(ctx, fake_tool):
    calls = fake_tool("web_research", {"path": "C:/sparx/research/heat-pumps.md", "sources": [1, 2, 3]})
    out = json.loads(await run_tool("research_queue_add", ctx, topic="  heat pumps  in cold climates "))
    assert out["status"] == "queued" and out["topic"] == "heat pumps in cold climates" and "daemon" in out["note"]
    assert '"already queued"' in await run_tool("research_queue_add", ctx, topic="Heat pumps in cold climates")
    await run_tool("research_queue_add", ctx, topic="second topic", depth=1)

    config.set_key("proactive.research", False)
    assert await proactive.background_research(now=NINE) is None and calls == []
    config.set_key("proactive.research", True)
    assert (await proactive.background_research(now=NINE))["status"] == "done"   # one topic per pass, oldest first
    assert calls == [{"question": "heat pumps in cold climates", "depth": 2}]
    (n,) = notes()
    assert n["title"] == "Research ready: heat pumps in cold climates" and "3 source(s)" in n["body"] and "queued" in n["why"]

    fake_tool("web_research", ToolError("none of the pages was relevant"))
    assert (await proactive.background_research(now=NINE))["status"] == "failed"
    assert await proactive.background_research(now=NINE) is None                 # queue is empty now
    rows = json.loads(await run_tool("research_queue_list", ctx))
    assert [(r["topic"], r["status"]) for r in rows] == [("second topic", "failed"), ("heat pumps in cold climates", "done")]
    assert "none of the pages was relevant" in rows[0]["error"] and len(notes()) == 2


async def test_interrupted_research_goes_back_to_the_queue(ctx, fake_tool, monkeypatch):
    await run_tool("research_queue_add", ctx, topic="tides")

    async def stopped(name, args, c):
        raise security.Stopped("Emergency stop is set.")
    fake_tool("web_research", {})
    monkeypatch.setattr(tools, "call", stopped)
    with pytest.raises(security.Stopped):
        await proactive.background_research(now=NINE)
    assert db.one("SELECT status, attempts FROM research_queue") == {"status": "queued", "attempts": 1}
    with pytest.raises(security.Stopped):
        await proactive.background_research(now=NINE)
    db.x("UPDATE research_queue SET started = ?", (iso(NINE - timedelta(hours=2)),))   # left 'running' by a dead process
    assert await proactive.background_research(now=NINE) is None
    assert db.one("SELECT status, error FROM research_queue") == {"status": "failed", "error": "interrupted twice"}


async def test_status_and_toggle(ctx):
    s = json.loads(await run_tool("proactive_status", ctx))
    by = {b["name"]: b for b in s["behaviours"]}
    assert set(by) == set(proactive.BEHAVIOURS) and all(b["on"] and b["next_run"] for b in by.values())
    assert s["enabled"] and not s["daemon_running"] and "sparx daemon" in s["note"]
    due = by["daily_briefing"]["next_run"][11:16]          # 08:00 today, or right now once 08:00 has passed
    assert due == "08:00" or "08:00" < due <= datetime.now().strftime("%H:%M")

    assert '"value": false' in await run_tool("proactive_toggle", ctx, name="Anomaly Digest", on=False)
    assert config.settings()["proactive"]["anomalies"] is False
    await run_tool("proactive_toggle", ctx, name="daily_briefing", on=False)
    assert config.settings()["proactive"]["daily_briefing"] is False
    assert '"value": "08:00"' in await run_tool("proactive_toggle", ctx, name="daily_briefing", on=True)
    await proactive.deadline_risk(now=NINE)
    daemon.heartbeat()
    s = json.loads(await run_tool("proactive_status", ctx))
    by = {b["name"]: b for b in s["behaviours"]}
    assert s["daemon_running"] and s["note"] is None and not by["anomaly_digest"]["on"] and by["anomaly_digest"]["next_run"] is None
    assert by["deadline_risk"]["last_run"] == iso(NINE) and by["daily_briefing"]["on"]

    assert "No proactive behaviour 'toasts'" in await run_tool("proactive_toggle", ctx, name="toasts", on=False)
    await run_tool("proactive_toggle", ctx, name="all", on=False)
    s = json.loads(await run_tool("proactive_status", ctx))
    assert not s["enabled"] and not any(b["on"] for b in s["behaviours"])


async def test_suggest_now_keeps_only_reasoned_suggestions(ctx, fake_llm):
    add_task("Overdue report", NINE - timedelta(days=2))
    fake_llm.script = [{"suggestions": [
        {"action": "Finish the overdue report first", "reason": "'Overdue report' is 2 days overdue", "tool": "productivity"},
        {"action": "Buy a boat", "reason": ""}, {"action": "Draft the email", "reason": "you are writing to Sam", "tool": "nope"}]}]
    out = json.loads(await run_tool("suggest_now", ctx, context="writing an email to Sam"))
    assert [s["action"] for s in out["suggestions"]] == ["Finish the overdue report first", "Draft the email"]
    assert out["suggestions"][0]["tool_group"] == "productivity" and "tool_group" not in out["suggestions"][1]
    assert out["based_on"]["tasks_overdue"][0]["title"] == "Overdue report"
    assert "writing an email to Sam" in fake_llm.prompts[0][1] and "Overdue report" in fake_llm.prompts[0][1]
    assert "Nothing clearly useful" in await run_tool("suggest_now", ctx, context="idle")


async def test_daemon_tick_runs_the_proactive_jobs(monkeypatch):
    add_task("Overdue report", NINE - timedelta(days=2))
    mine = {name: jobs.Job(name, j.seconds, j.fn, j.flag) for name, j in jobs.JOBS.items() if name in proactive.BEHAVIOURS}
    assert set(mine) == set(proactive.BEHAVIOURS) and all(j.flag == proactive.BEHAVIOURS[n]["flag"] for n, j in mine.items())
    monkeypatch.setattr(jobs, "JOBS", {k: mine[k] for k in ("deadline_risk", "goal_intervention", "background_research")})
    config.set_key("proactive.goal_intervention", False)
    out = await daemon.tick(now=1000)
    assert out == {"ran": ["deadline_risk", "background_research"], "errors": {}, "stopped": False}
    assert [n["title"] for n in notes()] == ["Deadline at risk: Overdue report"]
    assert (await daemon.tick(now=5000))["ran"] == ["deadline_risk", "background_research"] and len(notes()) == 1


# ---- audit: done-for-today only after the work succeeded; failures are told; model replies are checked

async def test_a_failed_briefing_is_tried_again_the_same_day(monkeypatch):
    add_task("File the tax return", NINE.replace(hour=17))
    real = productivity.briefing_data

    def broken(date=None):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(productivity, "briefing_data", broken)
    with pytest.raises(RuntimeError):
        await proactive.daily_briefing(now=NINE)
    assert notes() == []
    monkeypatch.setattr(productivity, "briefing_data", real)
    assert "File the tax return" in await proactive.daily_briefing(now=NINE.replace(minute=1))
    assert await proactive.daily_briefing(now=NINE.replace(minute=2)) is None and len(notes()) == 1


async def test_a_failed_weekly_summary_is_tried_again(monkeypatch):
    used("s", NINE - timedelta(days=1), "task_list")
    real = proactive.analytics

    def broken(days=7, now=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(proactive, "analytics", broken)
    with pytest.raises(RuntimeError):
        await proactive.weekly_analytics(now=NINE)
    monkeypatch.setattr(proactive, "analytics", real)
    assert await proactive.weekly_analytics(now=NINE.replace(hour=10)) and len(notes("digest")) == 1
    assert await proactive.weekly_analytics(now=NINE.replace(hour=11)) is None


async def test_promises_survive_a_model_failure_and_a_malformed_reply(fake_llm, monkeypatch):
    say("I'll send Priya the budget deck by Friday", NINE - timedelta(hours=3))
    real = proactive.llm.complete_json

    async def down(*a, **k):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(proactive.llm, "complete_json", down)
    with pytest.raises(RuntimeError):
        await proactive._promises(NINE)
    monkeypatch.setattr(proactive.llm, "complete_json", real)
    fake_llm.script = [{"promises": [{"quote": "I'll send Priya the budget deck by Friday", "what": "send the deck"}]}]
    assert len(await proactive._promises(NINE.replace(hour=10))) == 1 and len(notes()) == 1   # same day, next pass

    say("I will call the bank tomorrow", NINE + timedelta(hours=20))
    fake_llm.script = [{"promises": 5}]
    assert await proactive._promises(NINE + timedelta(days=1)) == [] and len(notes()) == 1


async def test_suggest_now_survives_a_malformed_reply(ctx, fake_llm):
    fake_llm.script = [{"suggestions": [{"action": "Plan the day", "reason": "nothing is planned", "tool": ["productivity"]}]}]
    out = json.loads(await run_tool("suggest_now", ctx, context="starting work"))
    assert out["suggestions"] == [{"action": "Plan the day", "reason": "nothing is planned"}]
    fake_llm.script = [{"suggestions": "none"}]
    assert "Nothing clearly useful" in await run_tool("suggest_now", ctx, context="idle")


async def test_research_abandoned_after_two_interruptions_is_reported():
    db.x("INSERT INTO research_queue (topic, status, attempts, added, started) VALUES ('tides', 'running', 2, ?, ?)",
         (iso(NINE - timedelta(days=1)), iso(NINE - timedelta(hours=2))))
    assert await proactive.background_research(now=NINE) is None
    assert db.one("SELECT status FROM research_queue")["status"] == "failed"
    (n,) = notes()
    assert n["title"] == "Research failed: tides" and "interrupted twice" in n["body"] and n["why"]
    assert await proactive.background_research(now=NINE) is None and len(notes()) == 1
