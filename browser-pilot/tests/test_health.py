import json
from datetime import datetime
from pathlib import Path

import pytest

from sparx import db, jobs, tools, undo
from sparx.tools import health

from conftest import run_tool

NOW = datetime(2026, 6, 10, 8, 5)       # a Wednesday


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(health, "_now", lambda: clock["now"])
    return clock


async def js(name, ctx, /, **args):
    return json.loads(await run_tool(name, ctx, **args))


def test_every_description_says_local():
    mine = [t for t in tools.REGISTRY.values() if t.category == "health" and t.name != "break_suggest"]
    assert len(mine) > 20 and all("Stored locally" in t.description for t in mine)
    assert "OAuth" in tools.REGISTRY["activity_import"].description
    assert tools.REGISTRY["health_delete_all"].risk == "delete"


async def test_mood_tools_gated_until_enabled(ctx):
    out = await run_tool("mood_log", ctx, mood=4, note="good day")
    assert "health_enable" in out and "off" in out
    assert "health_enable" in await run_tool("mood_trends", ctx)
    assert db.q("SELECT 1 FROM hl_mood") == []
    await run_tool("health_enable", ctx, feature="mood")
    assert (await js("mood_log", ctx, mood=4, note="good day", tags="sleep"))["saved"]
    await run_tool("mood_log", ctx, mood=2, tags="work")
    trends = await js("mood_trends", ctx)
    assert trends["entries"] == 2 and trends["average"] == 3 and trends["by_tag"]["work"]["average"] == 2
    assert "must be 1" in await run_tool("mood_log", ctx, mood=9)
    await run_tool("health_enable", ctx, feature="mood", on=False)
    assert "health_enable" in await run_tool("mood_log", ctx, mood=3)


async def test_red_flags_bypass_the_llm(ctx, fake_llm):
    out = await js("doctor_questions", ctx, concern="I've had chest pain since morning and I can't breathe properly")
    assert fake_llm.prompts == []
    assert out["red_flags"] == ["chest pain or pressure", "difficulty breathing"]
    assert "seek urgent medical care now" in out["urgent"] and "not medical advice" in out["not_medical_advice"]
    out = await js("doctor_questions", ctx, concern="lately I feel like I want to die")
    assert "14416" in out["urgent"] and fake_llm.prompts == []
    assert health.red_flags("my father's face is drooping and his speech is slurred") == ["possible stroke signs"]
    assert health.red_flags("my knee hurts when I climb stairs") == []


async def test_doctor_questions_normal_path_uses_llm_and_disclaims(ctx, fake_llm):
    fake_llm.script = [{"timeline": ["Knee pain started 2 weeks ago"], "questions": ["Do I need an X-ray?"], "bring": []}]
    out = await js("doctor_questions", ctx, concern="my knee hurts when I climb stairs", since="2 weeks")
    assert len(fake_llm.prompts) == 1 and "do not diagnose" in fake_llm.prompts[0][0]
    assert out["questions"] == ["Do I need an X-ray?"] and "not medical advice" in out["not_medical_advice"]
    assert "chest pain" in out["urgent_care"] and out["bring"]


async def test_medication_job_reminds_once_and_skips_taken(ctx, fixed_now):
    await run_tool("medication_add", ctx, name="Metformin", dose="500 mg", times=["08:00", "21:00"])
    await run_tool("medication_add", ctx, name="Vitamin D", times=["8:00"])
    assert "taken" in await run_tool("medication_taken", ctx, name="vitamin d")
    job = jobs.JOBS["medication_reminders"]
    assert job.flag == "reminders"
    job.fn(ctx)
    job.fn(ctx)
    notes = jobs.unread()
    assert len(notes) == 1 and notes[0]["title"] == "Medication: Metformin" and notes[0]["kind"] == "reminder"
    assert "500 mg due at 08:00" in notes[0]["body"]
    listed = await js("medication_list", ctx)
    assert listed[0]["today"] == {"08:00": "pending", "21:00": "pending"} and listed[1]["today"] == {"08:00": "taken"}
    fixed_now["now"] = NOW.replace(hour=21, minute=1)
    job.fn(ctx)
    assert len(jobs.unread()) == 2
    assert "not a time" in await run_tool("medication_add", ctx, name="X", times=["25:00"])
    await run_tool("medication_remove", ctx, name="Metformin")
    assert len(await js("medication_list", ctx)) == 1 and undo.run().startswith("Undone")
    assert len(await js("medication_list", ctx)) == 2


async def test_appointment_reminder(ctx, fixed_now):
    await run_tool("appointment_add", ctx, title="Dentist", when="2026-06-10T11:00", location="Indiranagar", remind_minutes=120)
    job = jobs.JOBS["medication_reminders"]
    job.fn(ctx)
    assert jobs.unread() == []                      # 08:05, nearly three hours ahead
    fixed_now["now"] = NOW.replace(hour=9, minute=30)
    job.fn(ctx)
    job.fn(ctx)
    notes = jobs.unread()
    assert len(notes) == 1 and notes[0]["title"] == "Appointment: Dentist" and "Indiranagar" in notes[0]["body"]
    assert (await js("appointment_list", ctx))[0]["title"] == "Dentist"


async def test_hydration_and_reminders_only_when_turned_on(ctx, fixed_now):
    job = jobs.JOBS["health_routines"]
    job.fn(ctx)
    assert jobs.unread() == []                      # off by default
    await run_tool("health_reminders_set", ctx, hydration=True, target_ml=2000, interval_minutes=60)
    out = await js("hydration_log", ctx, ml=500)
    assert out == {"date": "2026-06-10", "drunk_ml": 500, "target_ml": 2000, "remaining_ml": 1500, "target_met": False}
    job.fn(ctx)
    job.fn(ctx)                                     # inside the interval: no second reminder
    assert len(jobs.unread()) == 1 and "500 of 2000 ml" in jobs.unread()[0]["body"]
    fixed_now["now"] = NOW.replace(hour=23)
    job.fn(ctx)
    assert len(jobs.unread()) == 1                  # outside the active hours
    fixed_now["now"] = NOW.replace(hour=12)
    await run_tool("hydration_log", ctx, ml=1500)
    job.fn(ctx)
    assert len(jobs.unread()) == 1                  # target met
    assert (await js("hydration_today", ctx))["target_met"]


async def test_workout_log_and_progress(ctx):
    for day, kg in (("2026-05-25", 40), ("2026-05-27", 40), ("2026-06-01", 45), ("2026-06-03", 45), ("2026-06-05", 50)):
        await run_tool("workout_log", ctx, exercise="Squat", sets=3, reps=10, weight_kg=kg, date=day)
    await run_tool("workout_log", ctx, exercise="run", minutes=30, distance_km=5, date="2026-06-05")
    out = await js("workout_progress", ctx, weeks=4)
    assert [w["week_of"] for w in out["weeks"]] == ["2026-05-25", "2026-06-01"]
    assert out["weeks"][0] == {"week_of": "2026-05-25", "sessions": 2, "volume_kg": 2400.0, "minutes": 0.0, "distance_km": 0.0}
    assert out["weeks"][1]["sessions"] == 3 and out["weeks"][1]["volume_kg"] == 4200.0 and out["weeks"][1]["distance_km"] == 5
    assert out["volume_trend"] == "up" and out["exercises"]["squat"] == {"first_kg": 40, "latest_kg": 50, "best_kg": 50, "entries": 5}


async def test_workout_plan_saved(ctx, fake_llm):
    fake_llm.script = [{"days": [{"day": "Monday", "focus": "legs", "exercises": [{"name": "squat", "sets": 3, "reps": "10"}]}]}]
    out = await js("workout_plan", ctx, goal="build strength", equipment="dumbbells", days_per_week=1)
    assert out["plan_id"] == 1 and out["days"][0]["focus"] == "legs" and "dumbbells" in fake_llm.prompts[0][1]
    assert "did not return a usable plan" in await run_tool("workout_plan", ctx, goal="x")      # the fake answers {}


async def test_activity_import_wide_csv_and_summaries(ctx, tmp_path):
    f = tmp_path / "fit.csv"
    f.write_text("Date,Step count,Average heart rate (bpm),Minutes Asleep,Max heart rate (bpm)\n"
                 "2026-06-08,8000,70,420,150\n2026-06-09,12000,74,360,160\nnot a date,1,1,1,1\n", encoding="utf-8")
    out = await js("activity_import", ctx, path=str(f))
    assert out["days_imported"] == {"steps": 2, "heart_rate": 2, "sleep_minutes": 2} and out["rows_skipped"] == 1
    assert (await js("activity_import", ctx, path=str(f)))["from"] == "2026-06-08"          # re-import replaces
    assert db.one("SELECT COUNT(*) AS n FROM hl_activity")["n"] == 6
    act = await js("activity_summary", ctx, days=7)
    assert act["steps"]["average"] == 10000 and act["steps"]["best_day"] == "2026-06-09" and act["heart_rate"]["average"] == 72
    sleep = await js("sleep_summary", ctx, days=7)
    assert sleep["average_hours"] == 6.5 and sleep["nights_under_7h"] == 1 and sleep["by_night"]["2026-06-08"] == 7


async def test_activity_import_long_formats(ctx, tmp_path):
    apple = tmp_path / "apple.csv"
    apple.write_text(
        "type,startDate,endDate,value\n"
        "HKQuantityTypeIdentifierStepCount,2026-06-09 09:00:00 +0530,2026-06-09 09:10:00 +0530,500\n"
        "HKQuantityTypeIdentifierStepCount,2026-06-09 18:00:00 +0530,2026-06-09 18:10:00 +0530,700\n"
        "HKQuantityTypeIdentifierHeartRate,2026-06-09 09:00:00 +0530,2026-06-09 09:00:00 +0530,60\n"
        "HKQuantityTypeIdentifierHeartRate,2026-06-09 10:00:00 +0530,2026-06-09 10:00:00 +0530,80\n"
        "HKCategoryTypeIdentifierSleepAnalysis,2026-06-08 23:00:00 +0530,2026-06-09 06:30:00 +0530,HKCategoryValueSleepAnalysisAsleepCore\n"
        "HKCategoryTypeIdentifierSleepAnalysis,2026-06-08 22:30:00 +0530,2026-06-09 06:45:00 +0530,HKCategoryValueSleepAnalysisInBed\n",
        encoding="utf-8")
    await run_tool("activity_import", ctx, path=str(apple))
    got = {r["metric"]: r["value"] for r in db.q("SELECT metric, value FROM hl_activity WHERE date = '2026-06-09'")}
    assert got == {"steps": 1200, "heart_rate": 70, "sleep_minutes": 450}
    fitbit = tmp_path / "steps-2026-06-01.json"
    fitbit.write_text(json.dumps([{"dateTime": "06/01/26 00:00:00", "value": "300"}, {"dateTime": "06/01/26 00:01:00", "value": "200"}]))
    await run_tool("activity_import", ctx, path=str(fitbit))
    assert db.one("SELECT value FROM hl_activity WHERE date = '2026-06-01' AND metric = 'steps'")["value"] == 500
    junk = tmp_path / "junk.csv"
    junk.write_text("a,b\n1,2\n")
    assert "nothing recognisable" in await run_tool("activity_import", ctx, path=str(junk))


async def test_meal_plan_respects_preferences_and_grocery_list(ctx, fake_llm):
    fake_llm.script = [{"days": [
        {"day": 1, "meals": [{"meal": "breakfast", "dish": "oats", "ingredients": [{"item": "Oats", "qty": "60 g"}, {"item": "milk", "qty": "200 ml"}]}]},
        {"day": 2, "meals": [{"meal": "breakfast", "dish": "oats", "ingredients": [{"item": "oats", "qty": "60 g"}, {"item": "salt", "qty": "a pinch"}]}]}]}]
    plan = await js("meal_plan", ctx, days=2, diet="vegetarian", allergies="peanuts", calories=1800)
    system, user = fake_llm.prompts[0]
    assert "hard constraints" in system and "vegetarian" in user and "peanuts" in user and "1800" in user
    assert plan["constraints"]["never_include"] == "peanuts"
    groceries = await js("grocery_list_from_plan", ctx)
    assert groceries["items"] == [{"item": "milk", "qty": "200 ml"}, {"item": "oats", "qty": "120 g"}, {"item": "salt", "qty": "a pinch"}]
    assert (await js("meal_log", ctx, description="oats", meal="breakfast", calories=350, protein_g=12))["calories_logged"] == 350


async def test_break_suggest(ctx):
    assert (await js("break_suggest", ctx, minutes_worked=10))["break_now"] is False
    assert (await js("break_suggest", ctx, minutes_worked=60))["break_minutes"] == 10
    assert (await js("break_suggest", ctx, minutes_worked=200))["break_minutes"] == 30


async def test_export_and_delete_all(ctx, home):
    await run_tool("workout_log", ctx, exercise="run", minutes=20)
    await run_tool("hydration_log", ctx)
    await run_tool("health_enable", ctx, feature="mood")
    await run_tool("mood_log", ctx, mood=3, note="private")
    exp = await js("health_export", ctx)
    data = json.loads(Path(exp["file"]).read_text(encoding="utf-8"))
    assert exp["file"].startswith(str(home / "exports")) and data["hl_mood"][0]["note"] == "private"
    out = await js("health_delete_all", ctx)
    assert "health_delete_all" in ctx.asked and out["erased_rows"]["hl_mood"] == 1
    assert all(db.q(f"SELECT 1 FROM {t}") == [] for t in health.TABLES)
    assert "health_enable" in await run_tool("mood_log", ctx, mood=3)       # the opt-in is erased too


# ---- audit fixes

async def test_missed_dose_while_off_is_reported_once(ctx, fixed_now):
    fixed_now["now"] = NOW.replace(hour=7, minute=0)
    await run_tool("medication_add", ctx, name="Metformin", dose="500 mg", times=["08:00", "21:00"])
    job = jobs.JOBS["medication_reminders"]
    job.fn(ctx)                                     # the daemon runs, then is off until the evening
    fixed_now["now"] = NOW.replace(hour=15, minute=0)
    job.fn(ctx)
    job.fn(ctx)
    notes = jobs.unread()
    assert len(notes) == 1 and notes[0]["title"] == "Missed reminder: Metformin" and "08:00" in notes[0]["body"]
    assert "not running" in notes[0]["body"] and "advise" in notes[0]["body"]


async def test_taken_dose_is_not_reported_missed(ctx, fixed_now):
    await run_tool("medication_add", ctx, name="Metformin", times=["08:00"])
    job = jobs.JOBS["medication_reminders"]
    fixed_now["now"] = NOW.replace(hour=7, minute=0)
    job.fn(ctx)
    await run_tool("medication_taken", ctx, name="metformin", time="08:00")
    fixed_now["now"] = NOW.replace(hour=15, minute=0)
    job.fn(ctx)
    assert jobs.unread() == []


async def test_course_end_date_stops_reminders(ctx, fixed_now):
    out = await js("medication_add", ctx, name="Antibiotic", times=["08:00"], until="2026-06-09")
    assert out["until"] == "2026-06-09"
    jobs.JOBS["medication_reminders"].fn(ctx)      # 08:05 on the 10th, the day after the course ended
    assert jobs.unread() == []
    assert (await js("medication_list", ctx))[0]["course_ended"] is True
    await run_tool("medication_add", ctx, name="Vit C", times=["08:00"], until="2026-06-10")
    jobs.JOBS["medication_reminders"].fn(ctx)
    assert [n["title"] for n in jobs.unread()] == ["Medication: Vit C"]


async def test_missed_appointment_while_off_is_reported(ctx, fixed_now):
    await run_tool("appointment_add", ctx, title="Dentist", when="2026-06-10T11:00", remind_minutes=30)
    job = jobs.JOBS["medication_reminders"]
    fixed_now["now"] = NOW.replace(hour=9, minute=0)
    job.fn(ctx)
    fixed_now["now"] = NOW.replace(hour=12, minute=0)
    job.fn(ctx)
    job.fn(ctx)
    notes = jobs.unread()
    assert len(notes) == 1 and "Missed reminder: Dentist" in notes[0]["title"]


async def test_appointment_offset_is_converted_to_local(ctx):
    from datetime import timezone
    out = await js("appointment_add", ctx, title="Lab", when="2026-06-10T10:00:00+00:00")
    want = datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert out["at"] == want.isoformat(timespec="minutes")


async def test_workout_progress_uses_whole_calendar_weeks(ctx):
    for day in ("2026-05-21", "2026-05-26", "2026-05-28", "2026-06-02", "2026-06-09"):      # 21st is a Thursday
        await run_tool("workout_log", ctx, exercise="squat", sets=3, reps=10, weight_kg=40, date=day)
    out = await js("workout_progress", ctx, weeks=3)
    assert [w["week_of"] for w in out["weeks"]] == ["2026-05-25", "2026-06-01", "2026-06-08"]
    assert out["weeks"][0]["sessions"] == 2 and out["weeks"][2]["in_progress"] is True
    assert "in_progress" not in out["weeks"][0]


async def test_wearable_units(ctx, tmp_path):
    f = tmp_path / "w.csv"
    f.write_text("Date,Hours Asleep,Calories burned (kJ)\n2026-06-09,7.5,4184\n", encoding="utf-8")
    await run_tool("activity_import", ctx, path=str(f))
    got = {r["metric"]: r["value"] for r in db.q("SELECT metric, value FROM hl_activity")}
    assert got["sleep_minutes"] == 450 and got["calories"] == pytest.approx(1000)
    g = tmp_path / "apple.csv"
    g.write_text("type,startDate,value,unit\nHKQuantityTypeIdentifierActiveEnergyBurned,2026-06-08 10:00:00 +0530,2092,kJ\n"
                 "HKCategoryTypeIdentifierSleepAnalysis,2026-06-08 07:00:00 +0530,8,hr\n", encoding="utf-8")
    await run_tool("activity_import", ctx, path=str(g))
    got = {r["metric"]: r["value"] for r in db.q("SELECT metric, value FROM hl_activity WHERE date = '2026-06-08'")}
    assert got["calories"] == pytest.approx(500) and got["sleep_minutes"] == 480


# ---- second audit pass

def _taken():
    return db.q("SELECT date, slot FROM hl_med_log ORDER BY id")


async def test_dose_just_after_midnight_belongs_to_the_previous_evening_slot(ctx, fixed_now):
    await run_tool("medication_add", ctx, name="Metformin", times=["06:00", "23:30"])
    fixed_now["now"] = datetime(2026, 6, 11, 0, 10)
    assert "23:30 dose" in await run_tool("medication_taken", ctx, name="metformin")
    assert _taken() == [{"date": "2026-06-10", "slot": "23:30"}]
    assert "already recorded" in await run_tool("medication_taken", ctx, name="metformin")


async def test_dose_just_before_midnight_can_be_the_next_days_early_slot(ctx, fixed_now):
    await run_tool("medication_add", ctx, name="Night", times=["00:10", "12:00"])
    fixed_now["now"] = datetime(2026, 6, 10, 23, 50)
    await run_tool("medication_taken", ctx, name="night")
    assert _taken() == [{"date": "2026-06-11", "slot": "00:10"}]


async def test_explicit_slot_time_picks_the_right_day(ctx, fixed_now):
    await run_tool("medication_add", ctx, name="Metformin", times=["08:00", "23:30"])
    fixed_now["now"] = datetime(2026, 6, 11, 0, 10)
    await run_tool("medication_taken", ctx, name="metformin", time="23:30")
    assert _taken() == [{"date": "2026-06-10", "slot": "23:30"}]
    fixed_now["now"] = datetime(2026, 6, 10, 23, 0)      # a forgotten morning dose logged late is today's, not tomorrow's
    await run_tool("medication_taken", ctx, name="metformin", time="08:00")
    assert _taken()[-1] == {"date": "2026-06-10", "slot": "08:00"}


async def test_utc_wearable_timestamps_are_dated_in_local_time(ctx, tmp_path):
    from datetime import timezone
    f = tmp_path / "steps.csv"
    f.write_text("dateTime,value\n2026-06-09T20:30:00Z,100\n2026-06-10T03:00:00Z,50\n2026-06-09T09:00:00Z,7\n", encoding="utf-8")
    await run_tool("activity_import", ctx, path=str(f))
    want = {}
    for stamp, v in (((2026, 6, 9, 20, 30), 100), ((2026, 6, 10, 3, 0), 50), ((2026, 6, 9, 9, 0), 7)):
        d = datetime(*stamp, tzinfo=timezone.utc).astimezone().date().isoformat()
        want[d] = want.get(d, 0) + v
    assert {r["date"]: r["value"] for r in db.q("SELECT date, value FROM hl_activity WHERE metric = 'steps'")} == want
    g = tmp_path / "sleep.csv"
    g.write_text("type,startDate,endDate,value\nHKCategoryTypeIdentifierSleepAnalysis,2026-06-09T20:00:00Z,2026-06-10T02:00:00Z,"
                 "HKCategoryValueSleepAnalysisAsleepCore\n", encoding="utf-8")
    await run_tool("activity_import", ctx, path=str(g))
    day = datetime(2026, 6, 9, 20, 0, tzinfo=timezone.utc).astimezone().date().isoformat()
    assert db.one("SELECT value FROM hl_activity WHERE metric = 'sleep_minutes' AND date = ?", (day,))["value"] == 360


def test_health_module_has_no_like_queries():
    import inspect
    assert " LIKE " not in inspect.getsource(health)       # no user text reaches a LIKE here, so there is nothing to escape


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
async def test_numeric_tools_refuse_nan_infinity_and_negative(ctx, bad):
    calls = [("workout_log", {"exercise": "squat", "weight_kg": bad}), ("workout_log", {"exercise": "run", "minutes": bad}),
             ("workout_log", {"exercise": "run", "distance_km": bad}), ("workout_log", {"exercise": "squat", "sets": bad}),
             ("workout_log", {"exercise": "squat", "reps": bad}),
             ("meal_log", {"description": "rice", "calories": bad}), ("meal_log", {"description": "dal", "protein_g": bad}),
             ("hydration_log", {"ml": bad}), ("break_suggest", {"minutes_worked": bad}),
             ("health_reminders_set", {"interval_minutes": bad}), ("health_reminders_set", {"target_ml": bad}),
             ("appointment_add", {"title": "x", "when": "2026-06-20T10:00", "remind_minutes": bad})]
    for name, args in calls:
        out = await run_tool(name, ctx, **args)
        assert "must be" in out, (name, args, out)
    assert not db.q("SELECT 1 FROM hl_workouts") and not db.q("SELECT 1 FROM hl_meals") and not db.q("SELECT 1 FROM hl_hydration")
    assert not db.q("SELECT 1 FROM hl_appointments") and not db.q("SELECT 1 FROM hl_settings WHERE key = 'reminders'")


async def test_break_suggest_zero_is_fine(ctx):
    assert (await js("break_suggest", ctx, minutes_worked=0))["break_now"] is False


async def test_missed_dose_catch_up_is_capped_and_summarised(ctx, fixed_now):
    fixed_now["now"] = datetime(2026, 6, 8, 6, 0)
    for n in range(4):
        await run_tool("medication_add", ctx, name=f"Med{n}", times=["08:00", "12:00", "16:00", "20:00"])
    job = jobs.JOBS["medication_reminders"]
    job.fn(ctx)
    fixed_now["now"] = datetime(2026, 6, 10, 23, 30)       # off for two and a half days: 48 doses went by
    job.fn(ctx)
    notes = jobs.unread()
    assert len(notes) == 4, [n["title"] for n in notes]
    summary = [n for n in notes if "more" in n["title"] + n["body"]]
    assert len(summary) == 1 and "45 more" in summary[0]["title"] + summary[0]["body"]
    job.fn(ctx)
    assert len(jobs.unread()) == 4                         # nothing is sent a second time


async def test_no_missed_dose_notices_for_slots_before_the_medication_was_added(ctx, fixed_now):
    job = jobs.JOBS["medication_reminders"]
    fixed_now["now"] = datetime(2026, 6, 9, 20, 0)
    job.fn(ctx)                                            # the daemon ran yesterday evening, then was off
    fixed_now["now"] = datetime(2026, 6, 10, 14, 0)
    await run_tool("medication_add", ctx, name="Newmed", times=["08:00", "13:00", "14:30", "21:00"])
    fixed_now["now"] = datetime(2026, 6, 10, 16, 0)
    job.fn(ctx)
    notes = jobs.unread()
    assert len(notes) == 1 and "14:30" in notes[0]["body"] and "2026-06-10" in notes[0]["body"], [n["body"] for n in notes]


async def test_removed_and_ended_medications_get_no_missed_dose_notices(ctx, fixed_now):
    fixed_now["now"] = datetime(2026, 6, 8, 6, 0)
    await run_tool("medication_add", ctx, name="Gone", times=["08:00"])
    await run_tool("medication_add", ctx, name="Course", times=["08:00"], until="2026-06-08")
    job = jobs.JOBS["medication_reminders"]
    job.fn(ctx)
    await run_tool("medication_remove", ctx, name="Gone")
    fixed_now["now"] = datetime(2026, 6, 10, 15, 0)
    job.fn(ctx)
    notes = jobs.unread()
    assert [n["title"] for n in notes] == ["Missed reminder: Course"] and "2026-06-08" in notes[0]["body"]
