import json
from datetime import date, timedelta

import httpx
import pytest

from sparx import db, jobs, undo
from sparx.tools import finance

from conftest import run_tool

TODAY = date(2026, 6, 10)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(finance, "_today", lambda: TODAY)


def add(day, amount, what, cat="food", merchant=""):
    return db.x("INSERT INTO fin_expenses (date, amount, description, merchant, category, created) VALUES (?,?,?,?,?,?)",
                (day, amount, what, merchant or finance._merchant(what), cat, db.now()))


def unwrap(text):
    """The body of an untrusted-wrapped tool result (the text itself when it is not wrapped)."""
    if "<untrusted" in text:
        text = text.split("<untrusted", 1)[1].split("\n", 1)[1].rsplit("\n</untrusted>", 1)[0]
    return text


async def js(name, ctx, /, **args):
    return json.loads(unwrap(await run_tool(name, ctx, **args)))


async def test_categorisation_rules_before_llm(ctx, fake_llm):
    assert (await js("expense_add", ctx, amount=250, what="Swiggy dinner"))["category"] == "food"
    assert (await js("expense_add", ctx, amount=90, what="Uber ride home"))["category"] == "transport"
    assert (await js("expense_add", ctx, amount=199, what="Netflix"))["category"] == "subscriptions"
    assert fake_llm.prompts == []                      # rules matched: the model was never asked
    fake_llm.script = [{"category": "entertainment"}]
    out = await js("expense_add", ctx, amount=500, what="Zorbing with friends")
    assert out["category"] == "entertainment" and out["categorised_by"] == "llm" and len(fake_llm.prompts) == 1
    fake_llm.script = [{"category": "nonsense"}]
    assert (await js("expense_add", ctx, amount=5, what="Qwerty zxcv"))["category"] == "other"


async def test_user_rule_wins_and_recategorises(ctx, fake_llm):
    first = await js("expense_add", ctx, amount=300, what="Swiggy for the office party")
    await run_tool("expense_rule_add", ctx, keyword="office party", category="work", apply_to_existing=True)
    assert db.one("SELECT category FROM fin_expenses WHERE id = ?", (first["id"],))["category"] == "work"
    assert (await js("expense_add", ctx, amount=80, what="Cake for office party"))["category"] == "work"
    assert fake_llm.prompts == []


async def test_delete_and_undo(ctx):
    e = await js("expense_add", ctx, amount=120, what="Coffee")
    assert "Deleted" in await run_tool("expense_delete", ctx, id=e["id"])
    assert ctx.asked == ["expense_delete"] and not db.q("SELECT 1 FROM fin_expenses")
    assert undo.run().startswith("Undone")
    assert db.one("SELECT amount FROM fin_expenses WHERE id = ?", (e["id"],))["amount"] == 120


async def test_import_debit_credit_csv_and_dedupe(ctx, fake_llm, tmp_path):
    f = tmp_path / "stmt.csv"
    f.write_text("Account statement for XXXX1234\n\n"
                 "Txn Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
                 "01/06/2026,UPI/DR/4412/SWIGGY/YESB/pay,\"1,250.50\",,10000\n"
                 "02/06/2026,SALARY ACME,,50000.00,60000\n"
                 "03/06/2026,POS UBER TRIP,320.00,,59680\n"
                 "03/06/2026,POS UBER TRIP,320.00,,59360\n", encoding="utf-8")
    out = await js("expense_import", ctx, path=str(f))
    assert out["imported"] == 3 and out["credits_skipped"] == 1 and out["already_imported"] == 0
    rows = db.q("SELECT date, amount, merchant, category FROM fin_expenses ORDER BY id")
    assert rows[0] == {"date": "2026-06-01", "amount": 1250.5, "merchant": "Swiggy", "category": "food"}
    assert rows[1]["category"] == "transport" and rows[1]["date"] == "2026-06-03"
    again = await js("expense_import", ctx, path=str(f))
    assert again["imported"] == 0 and again["already_imported"] == 3
    assert db.one("SELECT COUNT(*) AS n FROM fin_expenses")["n"] == 3


async def test_import_single_amount_column_with_type(ctx, fake_llm, tmp_path):
    f = tmp_path / "upi.csv"
    f.write_text("Date;Description;Amount (INR);Dr/Cr\n2026-06-05;Zomato order;450;DR\n2026-06-06;Refund;450;CR\n", encoding="utf-8")
    out = await js("expense_import", ctx, path=str(f))
    assert out["imported"] == 1 and out["credits_skipped"] == 1
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    assert "could not find the header row" in await run_tool("expense_import", ctx, path=str(bad))


async def test_budget_pace(ctx):
    await run_tool("budget_set", ctx, category="food", amount=3000)
    await run_tool("budget_set", ctx, category="transport", amount=3000)
    add("2026-06-03", 2000, "Swiggy")
    add("2026-06-04", 500, "Uber", "transport")
    add("2026-05-20", 9999, "Swiggy")                  # another month: ignored
    out = await js("budget_status", ctx)
    food, transport = out["budgets"]
    assert out["day"] == 10 and out["days_in_month"] == 30
    assert food["spent"] == 2000 and food["expected_by_today"] == 1000 and food["pace"] == "over pace"
    assert food["projected_month_end"] == 6000 and food["remaining"] == 1000
    assert transport["pace"] == "under pace"
    assert "not financial advice" in out["note"]


async def test_spending_summary_vs_previous_month(ctx):
    add("2026-06-02", 400, "Swiggy")
    add("2026-06-09", 600, "Uber", "transport")
    add("2026-05-15", 500, "Swiggy")
    out = await js("spending_summary", ctx, month="2026-06")
    assert out["total"] == 1000 and out["by_category"] == {"transport": 600, "food": 400}
    assert out["by_week"] == {"days 1-7": 400, "days 8-14": 600}
    assert out["top_merchants"][0] == {"merchant": "Uber", "spent": 600}
    assert out["previous_month"]["change"] == 500 and out["previous_month"]["change_pct"] == 100.0


async def test_recurring_charge_detection(ctx):
    for day, amt in (("2026-03-05", 649), ("2026-04-05", 649), ("2026-05-06", 660)):
        add(day, amt, "Netflix", "subscriptions")
    for day, amt in (("2026-03-01", 200), ("2026-03-09", 900), ("2026-05-28", 150)):     # irregular: not a subscription
        add(day, amt, "Swiggy")
    out = await js("subscription_detect", ctx)
    assert [r["merchant"] for r in out["recurring"]] == ["Netflix"]
    r = out["recurring"][0]
    assert r["cycle"] == "monthly" and r["next_expected"] == "2026-06-06" and not r["already_tracked"]
    await run_tool("subscription_add", ctx, name="Netflix", amount=649, next_renewal="2026-06-06")
    assert (await js("subscription_detect", ctx))["recurring"][0]["already_tracked"]


async def test_bill_reminder_fires_once(ctx):
    await run_tool("subscription_add", ctx, name="Spotify", amount=119, next_renewal="2026-06-12", remind_days=3)
    await run_tool("bill_add", ctx, name="Electricity", amount=1800, due="2026-06-25")
    job = jobs.JOBS["bill_reminders"]
    assert job.flag == "reminders"
    job.fn(ctx)
    job.fn(ctx)
    notes = jobs.unread()
    assert len(notes) == 1 and "Spotify renews in 2 days" in notes[0]["title"] and notes[0]["kind"] == "reminder"
    assert notes[0]["why"]


async def test_passed_renewal_rolls_forward_and_reminds_again(ctx, monkeypatch):
    await run_tool("subscription_add", ctx, name="Spotify", amount=119, next_renewal="2026-06-12")
    jobs.JOBS["bill_reminders"].fn(ctx)
    monkeypatch.setattr(finance, "_today", lambda: date(2026, 7, 11))
    jobs.JOBS["bill_reminders"].fn(ctx)
    assert db.one("SELECT next_date FROM fin_dues")["next_date"] == "2026-07-12"
    assert len(jobs.unread()) == 2


async def test_emi_and_sip_against_known_values(ctx):
    out = await js("finance_scenario", ctx, kind="emi", inputs={"principal": 1000000, "annual_rate": 10, "years": 10})
    assert out["emi"] == pytest.approx(13215.07, abs=0.01)
    assert out["total_interest"] == pytest.approx(585808.84, abs=1)
    assert out["yearly_schedule"][-1]["balance"] == 0 and len(out["yearly_schedule"]) == 10
    assert "formula" in out and out["inputs"]["months"] == 120 and "not financial advice" in out["note"]
    assert finance.emi(100000, 12, 12) == pytest.approx(8884.88, abs=0.01)
    assert finance.emi(12000, 0, 12) == 1000
    sip = await js("finance_scenario", ctx, kind="sip", inputs={"monthly": 5000, "annual_rate": 12, "years": 10})
    assert sip["future_value"] == pytest.approx(1161695.38, abs=0.01) and sip["invested"] == 600000
    comp = await js("finance_scenario", ctx, kind="compound", inputs={"principal": 100000, "annual_rate": 8, "years": 10})
    assert comp["future_value"] == pytest.approx(215892.50, abs=0.01)
    goal = await js("finance_scenario", ctx, kind="time_to_goal", inputs={"target": 120000, "monthly": 10000})
    assert goal["months"] == 12 and goal["reached_on"] == "2027-06-10"
    aff = await js("finance_scenario", ctx, kind="affordability",
                   inputs={"income": 80000, "expenses": 50000, "existing_emi": 10000, "new_monthly_cost": 25000})
    assert aff["before"]["surplus"] == 20000 and aff["after"]["surplus"] == -5000 and aff["after"]["deficit"]
    assert "missing input" in await run_tool("finance_scenario", ctx, kind="emi", inputs={"principal": 5})


async def test_anomaly_detection_and_job(ctx):
    for i, amt in enumerate((200, 220, 180, 210, 190, 230, 205, 195, 215, 185, 200)):
        add((TODAY - timedelta(days=40 + i)).isoformat(), amt, "Swiggy")
    add("2026-03-07", 999, "Airtel", "utilities")          # Airtel is a known merchant
    big = add("2026-06-08", 4000, "Swiggy")
    add("2026-06-09", 210, "Swiggy")                       # ordinary: not flagged
    new = add("2026-06-09", 9000, "Croma", "shopping")
    add("2026-06-07", 999, "Airtel", "utilities")
    dup = add("2026-06-07", 999, "Airtel", "utilities")
    found = {a["expense_id"]: " ".join(a["reasons"]) for a in (await js("spending_anomalies", ctx))["anomalies"]}
    assert set(found) == {big, new, dup}
    assert "far above your typical food" in found[big] and "first charge from Croma" in found[new]
    assert "possible duplicate" in found[dup]
    job = jobs.JOBS["spending_anomaly_alerts"]
    assert job.flag == "anomalies"
    job.fn(ctx)
    job.fn(ctx)
    notes = jobs.unread()
    assert len(notes) == 3 and all(n["kind"] == "alert" for n in notes)
    assert any("duplicate" in n["body"] for n in notes)


async def test_savings_goal_projection(ctx):
    g = await js("savings_goal_add", ctx, name="Laptop", target=60000, saved=10000, monthly=10000, deadline="2026-10-10")
    assert g["months_to_go"] == 5 and g["projected_completion"] == "2026-11-10" and g["on_track"] is False
    g = await js("savings_goal_update", ctx, id=g["id"], add=10000, monthly=15000)
    assert g["saved"] == 20000 and g["projected_completion"] == "2026-09-10" and g["on_track"] is True


async def test_receipt_copy_and_warranty_expiry(ctx, tmp_path, home):
    src = tmp_path / "bill.pdf"
    src.write_bytes(b"%PDF-fake")
    r = await js("receipt_add", ctx, item="Headphones", store="Croma", date="2025-07-01", amount=4999,
                 warranty_months=12, file=str(src))
    assert r["warranty_until"] == "2026-07-01" and r["warranty_days_left"] == 21
    assert r["file"].startswith(str(home / "receipts")) and open(r["file"], "rb").read() == b"%PDF-fake"
    await run_tool("receipt_add", ctx, item="Fridge", date="2025-01-01", warranty_months=60)
    await run_tool("receipt_add", ctx, item="Old kettle", date="2024-01-01", warranty_months=12)      # already over
    soon = await js("warranty_expiring", ctx, days=30)
    assert [s["item"] for s in soon] == ["Headphones"]
    assert (await js("receipt_find", ctx, query="croma"))[0]["item"] == "Headphones"


async def test_shopping_optimize(ctx):
    items = [{"name": "milk", "qty": 2, "prices": {"A": 50, "B": 46}},
             {"name": "rice", "prices": {"A": 300, "B": 340}},
             {"name": "soap", "prices": {"B": 40}}]
    out = await js("shopping_optimize", ctx, items=items)
    assert out["cheapest_split"]["total"] == 432 and out["cheapest_split"]["stores"] == 2
    assert out["single_store"][0] == {"store": "B", "total": 472}
    assert out["single_store"][1] == {"store": "A", "missing": ["soap"]} and out["saving_vs_best_single_store"] == 40
    fee = await js("shopping_optimize", ctx, items=items, per_store_cost=60)       # a second trip is not worth 40
    assert fee["cheapest_split"]["stores"] == 1 and fee["cheapest_split"]["total"] == 532


def mock_quotes(monkeypatch, handler):
    monkeypatch.setattr(finance, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_watchlist_quotes_with_source_and_timestamp(ctx, monkeypatch):
    await run_tool("watchlist_add", ctx, symbol="aapl")
    await run_tool("watchlist_add", ctx, symbol="Bitcoin", kind="crypto")

    def handler(req):
        if req.url.host == "query1.finance.yahoo.com":
            assert req.url.path == "/v8/finance/chart/AAPL"
            return httpx.Response(200, json={"chart": {"result": [{"meta": {
                "regularMarketPrice": 210.5, "currency": "USD", "regularMarketTime": 1780000000, "chartPreviousClose": 200.0}}]}})
        assert req.url.params["ids"] == "bitcoin" and req.url.params["vs_currencies"] == "inr"
        return httpx.Response(200, json={"bitcoin": {"inr": 5000000, "inr_24h_change": 1.234, "last_updated_at": 1780000000}})
    mock_quotes(monkeypatch, handler)
    text = await run_tool("watchlist_quotes", ctx)
    assert text.startswith('<untrusted source="tool:watchlist_quotes">')
    out = json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])
    stock, coin = out["quotes"]
    assert out["status"] == "ok" and stock["price"] == 210.5 and stock["change_pct"] == 5.25
    assert "Yahoo" in stock["source"] and stock["as_of"].startswith("2026-05-28")
    assert coin["price"] == 5000000 and coin["currency"] == "INR" and "CoinGecko" in coin["source"]


async def test_watchlist_quotes_unavailable_has_no_numbers(ctx, monkeypatch):
    await run_tool("watchlist_add", ctx, symbol="AAPL")
    await run_tool("watchlist_add", ctx, symbol="bitcoin", kind="crypto")

    def handler(req):
        if req.url.host == "api.coingecko.com":
            raise httpx.ConnectError("no network")
        return httpx.Response(429, text="Too Many Requests")
    mock_quotes(monkeypatch, handler)
    text = await run_tool("watchlist_quotes", ctx)
    out = json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert out["status"] == "quotes unavailable"
    assert all(q["status"] == "quotes unavailable" and "price" not in q for q in out["quotes"])


def test_no_money_moving_tools_and_advice_note():
    from sparx import tools
    mine = [t for t in tools.REGISTRY.values() if t.category == "finance"]
    assert mine and all(t.risk in ("read", "write", "delete") for t in mine)
    assert not any(w in t.name for t in mine for w in ("transfer", "purchase", "trade", "pay_"))
    for name in ("budget_status", "spending_summary", "subscription_detect", "savings_goal_list", "finance_scenario",
                 "watchlist_quotes", "spending_anomalies", "shopping_optimize"):
        assert "not financial advice" in tools.REGISTRY[name].description


# ---- the listing, correcting and removing tools

async def test_expense_list_filters_orders_and_clamps(ctx):
    assert unwrap(await run_tool("expense_list", ctx)) == "No expenses match."
    a = add("2026-05-20", 100, "Swiggy dinner")
    b = add("2026-06-01", 40, "Uber ride", "transport")
    c = add("2026-06-05", 250, "Team lunch", "food", merchant="Truffles")
    assert [e["id"] for e in await js("expense_list", ctx)] == [c, b, a]                    # newest first
    assert [e["id"] for e in await js("expense_list", ctx, month="2026-06")] == [c, b]
    assert [e["id"] for e in await js("expense_list", ctx, category="Food ")] == [c, a]
    assert [e["id"] for e in await js("expense_list", ctx, search="truffles")] == [c]      # merchant
    assert [e["id"] for e in await js("expense_list", ctx, search="ride")] == [b]          # description
    assert [e["id"] for e in await js("expense_list", ctx, limit=0)] == [c]
    assert unwrap(await run_tool("expense_list", ctx, month="2025-01")) == "No expenses match."


async def test_expense_update_corrects_one_expense(ctx):
    eid = add("2026-06-01", 40, "Uber ride", "transport")
    out = await js("expense_update", ctx, id=eid, amount=45.5, category=" Travel ")
    assert out["amount"] == 45.5 and out["category"] == "travel" and out["description"] == "Uber ride" and out["id"] == eid
    for args, why in (({"id": eid, "amount": 0}, "greater than zero"), ({"id": 999, "amount": 5}, "no expense with id 999"),
                      ({"id": eid}, "nothing to change")):
        out = await run_tool("expense_update", ctx, **args)
        assert out.startswith("Error:") and why in out
    assert db.one("SELECT amount FROM fin_expenses WHERE id = ?", (eid,))["amount"] == 45.5


async def test_subscription_list_totals_and_remove_with_undo(ctx):
    assert "No subscriptions tracked" in await run_tool("subscription_list", ctx)
    m = (await js("subscription_add", ctx, name="Music", amount=100, next_renewal="2026-06-20"))["id"]
    y = (await js("subscription_add", ctx, name="Cloud", amount=1200, next_renewal="2026-09-01", cycle="yearly"))["id"]
    out = await js("subscription_list", ctx)
    assert out["monthly_cost"] == 200 and out["yearly_cost"] == 2400
    assert [s["id"] for s in out["subscriptions"]] == [m, y] and out["subscriptions"][0]["days_left"] == 10

    assert "not cancelled with the provider" in await run_tool("subscription_remove", ctx, id=m)
    assert ctx.asked == ["subscription_remove"]
    assert [s["id"] for s in (await js("subscription_list", ctx))["subscriptions"]] == [y]
    assert undo.run().startswith("Undone")
    assert [s["id"] for s in (await js("subscription_list", ctx))["subscriptions"]] == [m, y]      # same id came back
    out = await run_tool("subscription_remove", ctx, id=999)
    assert out.startswith("Error:") and "no subscription or bill with id 999" in out


async def test_bill_list_and_bill_paid(ctx):
    assert await run_tool("bill_list", ctx) == "No unpaid bills tracked."
    rent = (await js("bill_add", ctx, name="Rent", amount=15000, due="2026-06-15", cycle="monthly"))["id"]
    power = (await js("bill_add", ctx, name="Electricity", amount=900, due="2026-06-05"))["id"]
    sub = (await js("subscription_add", ctx, name="Music", amount=100, next_renewal="2026-06-20"))["id"]
    out = await js("bill_list", ctx)
    assert [b["id"] for b in out["bills"]] == [power, rent] and out["total_due"] == 15900     # soonest first, no subscriptions
    assert out["bills"][0]["overdue"] is True and "overdue" not in out["bills"][1]

    assert await run_tool("bill_paid", ctx, id=power) == "Marked Electricity as paid."
    assert "Next due 2026-07-15" in await run_tool("bill_paid", ctx, id=rent)
    out = await js("bill_list", ctx)
    assert [(b["id"], b["next_date"]) for b in out["bills"]] == [(rent, "2026-07-15")] and out["total_due"] == 15000
    for bad in (999, sub):
        out = await run_tool("bill_paid", ctx, id=bad)
        assert out.startswith("Error:") and f"no bill with id {bad}" in out


async def test_watchlist_add_list_remove(ctx):
    assert await run_tool("watchlist_list", ctx) == "The watchlist is empty."
    await run_tool("watchlist_add", ctx, symbol=" reliance.ns ")
    out = await js("watchlist_add", ctx, symbol="Bitcoin", kind="crypto")
    assert out == [{"symbol": "bitcoin", "kind": "crypto"}, {"symbol": "RELIANCE.NS", "kind": "stock"}]
    assert await js("watchlist_list", ctx) == out
    for args, why in (({"symbol": "TATA MOTORS"}, "does not look like a symbol"), ({"symbol": "AAPL", "kind": "bond"}, "stock or crypto")):
        res = await run_tool("watchlist_add", ctx, **args)
        assert res.startswith("Error:") and why in res
    assert await js("watchlist_remove", ctx, symbol="RELIANCE.ns") == [{"symbol": "bitcoin", "kind": "crypto"}]
    res = await run_tool("watchlist_remove", ctx, symbol="AAPL")
    assert res.startswith("Error:") and "AAPL is not on the watchlist" in res


# ---- audit fixes

async def test_import_cr_suffix_without_any_dr_and_indian_format(ctx, fake_llm, tmp_path):
    f = tmp_path / "cr.csv"
    f.write_text('Date,Narration,Amount\n05/06/2026,Swiggy,"₹1,23,456.78"\n06/06/2026,Refund Swiggy,"2,000.00Cr"\n'
                 '07/06/2026,Chai tea,Rs. .50\n', encoding="utf-8")
    out = await js("expense_import", ctx, path=str(f))
    assert out["imported"] == 2 and out["credits_skipped"] == 1
    assert [r["amount"] for r in db.q("SELECT amount FROM fin_expenses ORDER BY id")] == [123456.78, 0.5]
    assert finance._num("Rs.50") == 50 and finance._num("1,200.00Dr") == -1200


async def test_monthly_bill_on_31st_keeps_its_day(ctx, monkeypatch):
    b = (await js("bill_add", ctx, name="Rent", amount=100, due="2026-01-31", cycle="monthly"))["id"]
    got = []
    for _ in range(4):
        await run_tool("bill_paid", ctx, id=b)
        got.append(db.one("SELECT next_date FROM fin_dues")["next_date"])
    assert got == ["2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"]
    await run_tool("subscription_add", ctx, name="Cloud", amount=5, next_renewal="2026-01-30")
    monkeypatch.setattr(finance, "_today", lambda: date(2026, 4, 1))
    jobs.JOBS["bill_reminders"].fn(ctx)
    assert db.one("SELECT next_date FROM fin_dues WHERE name = 'Cloud'")["next_date"] == "2026-04-30"


async def test_budget_not_over_by_float_dust(ctx):
    await run_tool("budget_set", ctx, category="food", amount=0.3)
    add("2026-06-02", 0.1, "Swiggy")
    add("2026-06-03", 0.2, "Swiggy")
    assert (await js("budget_status", ctx))["budgets"][0]["pace"] != "over budget"


async def test_goal_months_not_rounded_up_by_float_dust(ctx):
    g = await js("savings_goal_add", ctx, name="X", target=0.9, monthly=0.3)
    assert g["months_to_go"] == 3


async def test_calculators_refuse_fraction_rates_and_bad_inputs(ctx):
    for kind, inp in (("emi", {"principal": 100000, "annual_rate": 0.08, "years": 5}),
                      ("sip", {"monthly": 1000, "annual_rate": 0.12, "years": 5}),
                      ("emi", {"principal": -5, "annual_rate": 8, "years": 5}),
                      ("compound", {"principal": 1000, "annual_rate": 8, "years": 5, "compounds_per_year": -1}),
                      ("emi", {"principal": 1000, "annual_rate": 1e6, "years": 5})):
        out = await run_tool("finance_scenario", ctx, kind=kind, inputs=inp)
        assert out.startswith("Error:"), out
    assert "fraction" in await run_tool("finance_scenario", ctx, kind="emi", inputs={"principal": 1000, "annual_rate": 0.08, "years": 1})
    ok = await js("finance_scenario", ctx, kind="emi", inputs={"principal": 1200, "annual_rate": 0, "months": 1})
    assert ok["emi"] == 1200
    ok = await js("finance_scenario", ctx, kind="emi", inputs={"principal": 1200, "annual_rate": 0.5, "rate_unit": "percent", "months": 1})
    assert ok["emi"] == pytest.approx(1200.5, abs=0.01)


async def test_stale_quotes_are_marked(ctx, monkeypatch):
    await run_tool("watchlist_add", ctx, symbol="AAPL")
    await run_tool("watchlist_add", ctx, symbol="bitcoin", kind="crypto")
    t = 1780000000

    def handler(req):
        if req.url.host == "query1.finance.yahoo.com":
            return httpx.Response(200, json={"chart": {"result": [{"meta": {
                "regularMarketPrice": 210.5, "currency": "USD", "regularMarketTime": t}}]}})
        return httpx.Response(200, json={"bitcoin": {"inr": 5000000, "last_updated_at": t}})
    mock_quotes(monkeypatch, handler)
    from datetime import datetime, timezone
    monkeypatch.setattr(finance, "_utcnow", lambda: datetime.fromtimestamp(t + 3 * 3600, timezone.utc))
    out = json.loads(unwrap(await run_tool("watchlist_quotes", ctx)))
    stock, coin = out["quotes"]
    assert "stale" not in stock and stock["age_hours"] == 3 and coin["stale"] and "not a current one" in coin["warning"]
    assert "bitcoin" in out["warning"]
    monkeypatch.setattr(finance, "_utcnow", lambda: datetime.fromtimestamp(t + 10 * 86400, timezone.utc))
    out = json.loads(unwrap(await run_tool("watchlist_quotes", ctx)))
    assert all(q["stale"] for q in out["quotes"])


def test_statement_and_receipt_text_tools_are_untrusted():
    from sparx import tools
    for n in ("expense_import", "expense_list", "expense_update", "spending_summary", "subscription_detect",
              "spending_anomalies", "receipt_add", "receipt_find", "watchlist_quotes"):
        assert tools.REGISTRY[n].untrusted, n


# ---- second audit pass

async def test_import_over_the_row_limit_says_so(ctx, fake_llm, tmp_path):
    f = tmp_path / "big.csv"
    junk = "01/06/2026,,5\n" * 20000                     # unreadable rows (no description) fill the limit cheaply
    f.write_text("Date,Narration,Withdrawal Amt.\n" + junk + "02/06/2026,Swiggy a,10\n02/06/2026,Swiggy b,20\n02/06/2026,Swiggy c,30\n",
                 encoding="utf-8")
    out = await js("expense_import", ctx, path=str(f))
    assert out["truncated"] is True and out["rows_read"] == 20000 and out["rows_not_read"] == 4
    assert "not imported" in out["warning"] and out["imported"] == 0


async def test_import_lists_probable_duplicates_of_manual_entries(ctx, fake_llm, tmp_path):
    mine = add("2026-06-01", 1250.5, "Swiggy dinner", merchant="Swiggy")
    add("2026-06-03", 320, "Uber ride", merchant="Uber")
    f = tmp_path / "stmt.csv"
    f.write_text("Txn Date,Narration,Withdrawal Amt.,Deposit Amt.\n"
                 "01/06/2026,UPI/DR/4412/SWIGGY/YESB/pay,\"1,250.50\",\n"
                 "03/06/2026,POS UBER TRIP,321.00,\n"             # amount differs: not a duplicate
                 "04/06/2026,POS UBER TRIP,320.00,\n"             # date differs
                 "03/06/2026,POS ZOMATO,320.00,\n", encoding="utf-8")       # merchant differs
    out = await js("expense_import", ctx, path=str(f))
    assert out["imported"] == 3 and len(out["probable_duplicates"]) == 1
    dup = out["probable_duplicates"][0]
    assert dup["statement_description"] == "UPI/DR/4412/SWIGGY/YESB/pay" and dup["existing_description"] == "Swiggy dinner"
    assert dup["existing_id"] == mine and dup["date"] == "2026-06-01" and dup["amount"] == 1250.5
    assert db.one("SELECT COUNT(*) AS n FROM fin_expenses WHERE date = '2026-06-01'")["n"] == 1
    again = await js("expense_import", ctx, path=str(f))               # nothing is lost silently on a second run either
    assert again["imported"] == 0 and len(again["probable_duplicates"]) == 1
    forced = await js("expense_import", ctx, path=str(f), import_duplicates=True)
    assert forced["imported"] == 1 and forced["probable_duplicates"] == []
    assert db.one("SELECT COUNT(*) AS n FROM fin_expenses WHERE date = '2026-06-01'")["n"] == 2


async def test_like_searches_escape_wildcards(ctx):
    add("2026-06-02", 100, "Sale 50% off shirt", "shopping")
    add("2026-06-02", 40, "Coffee", "food")
    add("2026-06-02", 60, "snake_case cafe", "food")
    assert [r["description"] for r in await js("expense_list", ctx, search="50%")] == ["Sale 50% off shirt"]
    assert [r["description"] for r in await js("expense_list", ctx, search="_")] == ["snake_case cafe"]
    assert "No expenses match" in unwrap(await run_tool("expense_list", ctx, search="%%"))
    await run_tool("expense_rule_add", ctx, keyword="e_c", category="work", apply_to_existing=True)
    assert db.q("SELECT description FROM fin_expenses WHERE category = 'work'") == [{"description": "snake_case cafe"}]
    await run_tool("receipt_add", ctx, item="Kettle", store="Home Store")
    assert "No receipts match" in unwrap(await run_tool("receipt_find", ctx, query="%x"))
    assert "No receipts match" in unwrap(await run_tool("receipt_find", ctx, query="K_ttle_"))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5])
async def test_amount_tools_refuse_nan_infinity_and_negative(ctx, bad):
    for name, args in [("budget_set", {"category": "food", "amount": bad}),
                       ("savings_goal_add", {"name": "Trip", "target": 1000, "saved": bad}),
                       ("savings_goal_add", {"name": "Trip", "target": 1000, "monthly": bad}),
                       ("receipt_add", {"item": "Kettle", "amount": bad}),
                       ("shopping_optimize", {"items": [{"name": "milk", "qty": bad, "prices": {"A": 5}}]}),
                       ("shopping_optimize", {"items": [{"name": "milk", "prices": {"A": bad}}]}),
                       ("shopping_optimize", {"items": [{"name": "milk", "prices": {"A": 5}}], "per_store_cost": bad})]:
        out = await run_tool(name, ctx, **args)
        assert "must be" in out, (name, args, out)
    assert db.q("SELECT 1 FROM fin_budgets") == [] and db.q("SELECT 1 FROM fin_goals") == [] and db.q("SELECT 1 FROM fin_receipts") == []


async def test_budget_set_zero_still_removes_and_goal_update_rejects_nan(ctx):
    await run_tool("budget_set", ctx, category="food", amount=500)
    assert "Removed" in await run_tool("budget_set", ctx, category="food", amount=0)
    g = await js("savings_goal_add", ctx, name="Trip", target=1000)
    for field in ("add", "saved", "target", "monthly"):
        assert "must be" in await run_tool("savings_goal_update", ctx, id=g["id"], **{field: float("nan")}), field
    assert db.one("SELECT saved FROM fin_goals")["saved"] == 0


async def test_calculators_refuse_nan_and_infinity(ctx):
    nan, inf = float("nan"), float("inf")
    for kind, inputs in [("emi", {"principal": 1000, "annual_rate": 8, "months": nan}),
                         ("emi", {"principal": 1000, "annual_rate": 8, "months": inf}),
                         ("emi", {"principal": nan, "annual_rate": 8, "months": 12}),
                         ("sip", {"monthly": inf, "annual_rate": 8, "years": 1}),
                         ("compound", {"principal": 100, "annual_rate": 8, "years": nan}),
                         ("compound", {"principal": 100, "annual_rate": 8, "years": 1, "compounds_per_year": nan}),
                         ("time_to_goal", {"target": nan, "monthly": 100}),
                         ("time_to_goal", {"target": 1000, "monthly": inf}),
                         ("time_to_goal", {"target": 1000, "monthly": -100}),
                         ("affordability", {"income": nan, "expenses": 10}),
                         ("affordability", {"income": 100, "expenses": 10, "new_monthly_cost": inf})]:
        out = await run_tool("finance_scenario", ctx, kind=kind, inputs=inputs)
        assert "must be" in out or "missing input" in out or "number" in out, (kind, inputs, out)
        assert out.startswith("Error") and "ValueError" not in out and "OverflowError" not in out, (kind, inputs, out)
