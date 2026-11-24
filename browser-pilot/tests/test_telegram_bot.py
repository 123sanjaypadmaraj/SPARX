"""Telegram remote control, with the Telegram API and the assistant faked."""
import asyncio

import pytest

from sparx import config, telegram_bot
from sparx.telegram_bot import Bot


class FakeHttp:
    def __init__(self, queued=()):
        self.sent = []
        self.queued = list(queued)

    async def post(self, url, json):
        self.sent.append((url.rsplit("/", 1)[1], json))
        status, body = self.queued.pop(0) if self.queued else (200, {"ok": True, "result": []})

        class R:
            status_code = status

            def json(self):
                return body
        return R()


def bot(token="T", queued=()):
    return Bot(token, "42", FakeHttp(queued))


def cb(data, uid=42):
    return {"update_id": 5, "callback_query": {"id": "c", "data": data, "from": {"id": uid}, "message": {"chat": {"id": 42}}}}


def private(text, uid=42, chat_type="private", chat_id=42):
    return {"update_id": 1, "message": {"chat": {"id": chat_id, "type": chat_type}, "from": {"id": uid}, "text": text}}


def pressed(b):
    return [j["reply_markup"]["inline_keyboard"][0][0]["callback_data"] for m, j in b.http.sent if m == "sendMessage" and "reply_markup" in j]


def texts(b):
    return [j["text"] for m, j in b.http.sent if m == "sendMessage"]


async def test_strangers_are_ignored():
    b = bot()
    await b.dispatch(private("delete everything", uid=99, chat_id=99))
    assert b.http.sent == [] and b.offset == 2


async def test_owner_message_runs_a_turn(monkeypatch):
    class FakeSparx:
        def __init__(self, **kw):
            pass

        async def turn(self, text):
            return f"echo {text}"
    monkeypatch.setattr(telegram_bot, "Sparx", FakeSparx)
    b = bot()
    await b.handle("hi")
    assert texts(b) == ["echo hi"] and not b.busy


async def test_approval_button_resolves(monkeypatch):
    b = bot()

    class T:
        name, risk = "mouse_click", "control"
    task = asyncio.create_task(b.approve(T, {"x": 1}))
    await asyncio.sleep(0.05)
    data = b.http.sent[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await b.dispatch(cb(data))
    assert await task is True


class LongTool:
    name, risk = "mcp_" + "x" * 60, "control"


async def test_each_approval_has_its_own_key():
    b = bot()
    t1 = asyncio.create_task(b.approve(LongTool, {"a": 1}))
    t2 = asyncio.create_task(b.approve(LongTool, {"a": 2}))
    await asyncio.sleep(0.05)
    d1, d2 = pressed(b)
    assert d1 != d2 and len(d1) <= 64
    await b.dispatch(cb(d1))
    assert await t1 is True and not t2.done()
    await b.dispatch(cb(d2.replace("y|", "n|", 1)))
    assert await t2 is False


async def test_stale_button_approves_nothing():
    b = bot()
    await b.dispatch(cb("y|deadbeef"))
    assert b.http.sent[-1][0] == "answerCallbackQuery"
    assert b.http.sent[-1][1]["text"] == "This request has expired."


async def test_only_the_owner_sender_is_obeyed(monkeypatch):
    ran = []

    async def fake_handle(text):
        ran.append(text)
    b = bot()
    monkeypatch.setattr(b, "handle", fake_handle)
    await b.dispatch(private("hi", uid=7, chat_type="group"))
    await b.dispatch(private("hi", uid=7))
    await b.dispatch(private("hi", chat_type="group"))
    await asyncio.sleep(0.01)
    assert ran == []
    await b.dispatch(private("hi"))
    await asyncio.sleep(0.01)
    assert ran == ["hi"]


async def test_callback_from_another_user_approves_nothing():
    b = bot()
    task = asyncio.create_task(b.approve(LongTool, {}))
    await asyncio.sleep(0.05)
    await b.dispatch(cb(pressed(b)[0], uid=7))
    await asyncio.sleep(0.01)
    assert not task.done()
    task.cancel()


async def test_group_chat_id_is_refused(monkeypatch):
    monkeypatch.setattr(config, "secret", lambda k: "tok")
    monkeypatch.setattr(config, "settings", lambda: {"integrations": {"telegram": {"chat_id": "-100123"}}})
    with pytest.raises(SystemExit, match="private chat"):
        await telegram_bot.main()


async def test_token_never_reaches_the_chat(monkeypatch):
    token = "123456:ABCdef"

    class Boom:
        def __init__(self, **kw):
            pass

        async def turn(self, text):
            raise RuntimeError(f"failed https://api.telegram.org/bot{token}/getUpdates")
    monkeypatch.setattr(telegram_bot, "Sparx", Boom)
    b = bot(token)
    await b.handle("hi")
    assert texts(b) and all(token not in t for t in texts(b))


async def test_bad_token_stops_the_poll_loop():
    b = bot(queued=[(200, {"ok": True, "result": []}), (401, {"ok": False})])
    with pytest.raises(SystemExit, match="rejected the bot token"):
        await b.run()


async def test_conflict_waits_then_carries_on(monkeypatch):
    slept = []

    async def sleep(n):
        slept.append(n)
    monkeypatch.setattr(telegram_bot, "SLEEP", sleep)
    b = bot(queued=[(200, {"ok": True}), (409, {"ok": False}), (401, {"ok": False})])
    with pytest.raises(SystemExit):
        await b.run()
    assert slept == [30]


async def test_rate_limit_sleeps_retry_after(monkeypatch):
    slept = []

    async def sleep(n):
        slept.append(n)
    monkeypatch.setattr(telegram_bot, "SLEEP", sleep)
    b = bot(queued=[(200, {"ok": True}), (429, {"ok": False, "parameters": {"retry_after": 7}}), (401, {"ok": False})])
    with pytest.raises(SystemExit):
        await b.run()
    assert slept == [7]
