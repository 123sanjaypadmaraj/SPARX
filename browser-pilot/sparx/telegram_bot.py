"""Remote control over Telegram: message your bot from anywhere and Sparx answers; risky actions ask you with Yes/No buttons.

Only the owner is obeyed: a private chat with the bot, whose chat_id (integrations.telegram.chat_id) is the owner's own
user id. Every other chat and sender is ignored. Run it with `sparx telegram`.
"""
import asyncio
import json
import secrets

import httpx

from . import config, security
from .assistant import Sparx

APPROVE_SECONDS = 120
SLEEP = asyncio.sleep


class Bot:
    def __init__(self, token: str, chat: str, client: httpx.AsyncClient | None = None):
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}/"
        self.chat = str(chat)
        self.http = client or httpx.AsyncClient(timeout=70)
        self.offset = 0
        self.waiting: dict[str, asyncio.Future] = {}
        self.busy = False
        self.status = 200

    async def call(self, method: str, **body) -> dict:
        r = await self.http.post(self.api + method, json=body)
        self.status = r.status_code
        return r.json() if r.status_code < 500 else {}

    async def say(self, text: str, **extra):
        if self.token:
            text = text.replace(self.token, "<token>")
        for i in range(0, max(len(text), 1), 4000):
            await self.call("sendMessage", chat_id=self.chat, text=text[i:i + 4000] or "(empty)", **extra)

    async def approve(self, tool, args) -> bool:
        key = secrets.token_hex(8)
        fut = asyncio.get_running_loop().create_future()
        self.waiting[key] = fut
        shown = json.dumps(args, default=str)[:600]
        await self.say(f"Allow {tool.name} ({tool.risk})?\n{shown}", reply_markup={"inline_keyboard": [[
            {"text": "Yes", "callback_data": f"y|{key}"}, {"text": "No", "callback_data": f"n|{key}"}]]})
        try:
            return await asyncio.wait_for(fut, APPROVE_SECONDS)
        except asyncio.TimeoutError:
            return False
        finally:
            self.waiting.pop(key, None)

    def _answer(self, data: str) -> bool:
        verdict, _, key = data.partition("|")
        fut = self.waiting.get(key)
        if verdict not in ("y", "n") or fut is None or fut.done():
            return False
        fut.set_result(verdict == "y")
        return True

    async def handle(self, text: str):
        text = text.strip()
        if text in ("/stop", "stop"):
            security.request_stop("telegram")
            return await self.say("Emergency stop set. Send /resume to clear it.")
        if text == "/resume":
            security.clear_stop()
            return await self.say("Resumed.")
        if self.busy:
            return await self.say("Still working on the last request. Send /stop to halt it.")
        self.busy = True
        try:
            sparx = Sparx(approve=self.approve, emit=lambda e: None)
            await self.say(await sparx.turn(text) or "Done.")
        except Exception as e:
            await self.say(f"Error: {type(e).__name__}: {str(e)[:300]}")
        finally:
            self.busy = False

    async def dispatch(self, update: dict):
        self.offset = update["update_id"] + 1
        cb = update.get("callback_query")
        if cb:
            ok = False
            if str((cb.get("from") or {}).get("id")) == self.chat:
                ok = self._answer(cb.get("data", ""))
            extra = {} if ok else {"text": "This request has expired."}
            await self.call("answerCallbackQuery", callback_query_id=cb["id"], **extra)
            return
        msg = update.get("message") or {}
        if (msg.get("chat", {}).get("type") != "private" or str((msg.get("from") or {}).get("id")) != self.chat
                or str(msg.get("chat", {}).get("id")) != self.chat or not msg.get("text")):
            return                                                        # strangers are ignored
        asyncio.create_task(self.handle(msg["text"]))

    async def run(self):
        await self.say("Sparx is online.")
        while True:
            try:
                data = await self.call("getUpdates", offset=self.offset, timeout=50)
                if not data.get("ok", True) or not data:
                    await self._backoff(data)
                    continue
                for u in data.get("result", []):
                    await self.dispatch(u)
            except (httpx.HTTPError, ValueError):
                await SLEEP(5)

    async def _backoff(self, data: dict):
        if self.status in (401, 404):
            raise SystemExit("Telegram rejected the bot token (SPARX_TELEGRAM_TOKEN). Create a new one with @BotFather.")
        if self.status == 409:
            print("Another process is polling this bot; waiting 30 seconds.")
            await SLEEP(30)
        elif self.status == 429:
            await SLEEP((data.get("parameters") or {}).get("retry_after", 5))
        else:
            await SLEEP(5)


async def main():
    token = config.secret("SPARX_TELEGRAM_TOKEN")
    tg = config.settings()["integrations"].get("telegram")
    chat = (tg if isinstance(tg, dict) else {}).get("chat_id")
    if not (token and chat):
        raise SystemExit("Set SPARX_TELEGRAM_TOKEN in ~/.sparx/.env and run `sparx config integrations.telegram.chat_id <id>` first.")
    if str(chat).startswith("-"):
        raise SystemExit("Remote control works only in a private chat with the bot: "
                         "set integrations.telegram.chat_id to your own user id.")
    await Bot(token, str(chat)).run()
