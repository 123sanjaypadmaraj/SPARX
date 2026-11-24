"""One chat interface over any OpenAI-compatible endpoint (Groq, Gemini, Ollama, OpenAI, ...).

Uses native tool calling when the model supports it and falls back to a
JSON-in-text protocol when it doesn't (common with small Ollama models).
"""
import asyncio
import json
import os
import re
from dataclasses import dataclass

from openai import AsyncOpenAI, BadRequestError

from .config import resolve_model


@dataclass
class Call:
    id: str | None  # None in JSON-fallback mode
    name: str
    args: dict | str


@dataclass
class Reply:
    text: str
    calls: list
    message: dict  # assistant message to append to the history


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def extract_json(text: str):
    """First JSON object found in text, or None."""
    text = _strip_think(text)
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _call_in_text(text: str, tools) -> list:
    """A tool call written as JSON text: {"tool": ..., "args": ...} or {"name": ..., "parameters"|"arguments": ...}."""
    obj = extract_json(text) or {}
    name = obj.get("tool") or obj.get("name")
    if not isinstance(name, str) or name not in {t["function"]["name"] for t in tools}:
        return []
    args = obj.get("args") or obj.get("parameters") or obj.get("arguments") or {}
    return [Call(None, name, args)]


def _json_protocol(tools) -> str:
    lines = [
        "You cannot call functions natively. To act, reply with ONLY one JSON object and nothing else:",
        '{"tool": "<tool name>", "args": {<arguments>}}',
        "You will then receive the tool result. Available tools:",
    ]
    for t in tools:
        f = t["function"]
        props = f["parameters"].get("properties", {})
        req = set(f["parameters"].get("required", []))
        sig = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" for k, v in props.items())
        lines.append(f"- {f['name']}({sig}): {f['description']}")
    return "\n".join(lines)


class LLM:
    def __init__(self, spec: str, concurrency: int = 8):
        self.spec = spec
        self.provider, base_url, key, self.model = resolve_model(spec)
        self.client = AsyncOpenAI(base_url=base_url, api_key=key, max_retries=6, timeout=180)
        self.native = os.environ.get("BROWSERPILOT_JSON_TOOLS") != "1"
        self.sem = asyncio.Semaphore(concurrency)

    async def _create(self, **kw):
        async with self.sem:
            return await self.client.chat.completions.create(model=self.model, **kw)

    async def chat(self, messages, tools) -> Reply:
        if self.native:
            try:
                return await self._native(messages, tools)
            except BadRequestError as e:
                msg = str(e).lower()
                if "tool" not in msg or not ("support" in msg or "not available" in msg):
                    raise
                self.native = False  # model has no tool calling; use the JSON protocol from here on
        return await self._json(messages, tools)

    async def _native(self, messages, tools) -> Reply:
        for attempt in range(3):
            try:
                r = await self._create(messages=messages, tools=tools)
                break
            except BadRequestError as e:
                # Groq returns this when the model emits a malformed tool call; a retry usually fixes it
                if "tool_use_failed" in str(e) and attempt < 2:
                    continue
                raise
        m = r.choices[0].message
        text = _strip_think(m.content or "")
        message = {"role": "assistant", "content": m.content or ""}
        calls = []
        if m.tool_calls:
            # model_dump keeps provider extras (e.g. Gemini thought signatures) that must be echoed back
            message["tool_calls"] = [c.model_dump(exclude_none=True) for c in m.tool_calls]
            if not m.content:
                del message["content"]
            calls = [Call(c.id, c.function.name, c.function.arguments or "{}") for c in m.tool_calls]
        elif text:
            calls = _call_in_text(text, tools)  # small local models often write the call as text instead
        return Reply(text, calls, message)

    async def _json(self, messages, tools) -> Reply:
        msgs = list(messages)
        if msgs and msgs[0]["role"] == "system":
            msgs[0] = {"role": "system", "content": msgs[0]["content"] + "\n\n" + _json_protocol(tools)}
        r = await self._create(messages=msgs)
        raw = r.choices[0].message.content or ""
        return Reply(_strip_think(raw), _call_in_text(raw, tools), {"role": "assistant", "content": raw})

    def result_message(self, call: Call, text: str) -> dict:
        if call.id is None:
            return {"role": "user", "content": f"TOOL RESULT ({call.name}):\n{text}"}
        return {"role": "tool", "tool_call_id": call.id, "content": text}

    async def text(self, system: str, user: str) -> str:
        r = await self._create(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return _strip_think(r.choices[0].message.content or "")
