"""Which models Sparx can use right now: what local Ollama has pulled, what each hosted provider with a key serves,
and what the Kaggle notebook server (the `custom` provider) is running. Specs come back ready to paste into
`models.default` and friends: ollama/gpt-oss:latest, groq/openai/gpt-oss-120b, custom/<served name>.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import config

TTL = 60                # seconds a listing is reused; the web UI asks every time the settings page opens
TIMEOUT = 4
_cache: tuple[float, dict] | None = None


def _ids(payload: dict) -> list[str]:
    """Model ids from an OpenAI-style /models answer ({data: [{id}]}) or Ollama's /api/tags ({models: [{name}]})."""
    rows = payload.get("data") or payload.get("models") or []
    ids = [r.get("id") or r.get("name") or "" for r in rows if isinstance(r, dict)]
    return sorted({i.removeprefix("models/") for i in ids if i})      # Gemini prefixes ids with "models/"


def _list(provider: str) -> tuple[list[str], str]:
    """(model ids, note). The note is '' when the listing worked, otherwise why it did not."""
    base, key_env, default = config.bp.PROVIDERS[provider]
    if provider == "custom":
        base = os.environ.get("BROWSERPILOT_BASE_URL", "")
        if not base:
            return [], "not running"
    key = os.environ.get(key_env, "") if key_env else ""
    if key_env and not key:
        return [], f"needs {key_env}"
    try:
        if provider == "ollama":
            r = httpx.get(base.removesuffix("/v1").rstrip("/") + "/api/tags", timeout=TIMEOUT)
        else:
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"} if provider == "anthropic" \
                else {"Authorization": f"Bearer {key}"} if key else {}
            r = httpx.get(base.rstrip("/") + "/models", headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return _ids(r.json()), ""
    except Exception as e:
        note = "Ollama is not running" if provider == "ollama" else \
            "not reachable (the Kaggle session may have ended)" if provider == "custom" else f"{type(e).__name__}"
        return ([default] if default and provider not in ("ollama", "custom") else []), note


def kaggle_status() -> dict:
    """Is the Kaggle notebook configured, and is its server answering? `custom` is that server."""
    gist = config.secret("SPARX_KAGGLE_GIST")
    base = os.environ.get("BROWSERPILOT_BASE_URL", "")
    return {"configured": bool(gist), "url": base,
            "hint": "" if base else ("run `sparx kaggle-sync --now` to start the notebook" if gist
                                     else "set SPARX_KAGGLE_GIST in ~/.sparx/.env to use the Kaggle notebook")}


def available(refresh: bool = False) -> dict:
    """{models: [{spec, provider, source}], providers: {name: note}, kaggle: {...}}. Providers are asked in parallel."""
    global _cache
    if _cache and not refresh and time.time() - _cache[0] < TTL:
        return _cache[1]
    names = [p for p, (_b, key_env, _d) in config.bp.PROVIDERS.items()
             if p in ("ollama", "custom", "lmstudio") or (key_env and os.environ.get(key_env))]
    with ThreadPoolExecutor(max_workers=len(names) or 1) as pool:
        listed = dict(zip(names, pool.map(_list, names)))
    kaggle = kaggle_status()
    models, providers = [], {}
    for name, (ids, note) in listed.items():
        if name == "lmstudio" and note:
            continue                                   # only mention LM Studio when it is actually running
        providers[name] = note or f"{len(ids)} models"
        source = "kaggle" if name == "custom" and kaggle["configured"] else name
        models += [{"spec": f"{name}/{i}", "provider": name, "source": source} for i in ids]
    if not kaggle["url"]:
        providers["custom"] = "Kaggle notebook: " + (kaggle["hint"] or "not running")
    elif providers.get("custom", "").endswith("models") and kaggle["configured"]:
        providers["custom"] = "Kaggle notebook: " + providers["custom"]
    out = {"models": models, "providers": providers, "kaggle": kaggle,
           "current": {r: config.settings()["models"].get(r, "") for r in config.DEFAULTS["models"]}}
    _cache = (time.time(), out)
    return out


NON_CHAT = ("whisper", "tts", "orpheus", "guard", "safeguard", "embed", "image", "audio", "aqa", "live", "imagen", "veo")


def text(info: dict, only: str = "", limit: int = 25) -> str:
    """The listing for a terminal: chat models only, `limit` per provider (`only` names one provider)."""
    lines = []
    for provider, note in info["providers"].items():
        if only and provider != only:
            continue
        ids = [s for s in chat_specs(info) if s.startswith(provider + "/")]
        lines.append(f"{provider}: {len(ids)} chat models" if ids else f"{provider}: {note}")
        lines += [f"  {s}" for s in ids[:limit]]
        if len(ids) > limit:
            lines.append(f"  ... {len(ids) - limit} more: /models {provider}")
    return "\n".join(lines)


def dead() -> set[str]:
    """Specs that failed a live test request (HOME/dead_models.json); they are left out of every listing."""
    try:
        return set(json.loads((config.HOME / "dead_models.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def chat_specs(info: dict) -> list[str]:
    """Every chat model spec that is not known to be broken, grouped by provider."""
    gone = dead()
    return [m["spec"] for m in info["models"] if m["spec"] not in gone and not any(w in m["spec"].lower() for w in NON_CHAT)]


def summary(info: dict) -> str:
    """One line per provider with how many chat models it serves."""
    specs = chat_specs(info)
    return "\n".join(f"{p:<11} {sum(s.startswith(p + '/') for s in specs):>4} models" if p in {m['provider'] for m in info['models']}
                     else f"{p:<11} {note}" for p, note in info["providers"].items())


async def pick(info: dict, current: str) -> str | None:
    """Arrow-key list of every chat model (up/down, Enter selects, Esc cancels). None when cancelled."""
    from prompt_toolkit.shortcuts import radiolist_dialog
    specs = chat_specs(info)
    return await radiolist_dialog(title="Choose a model", text="Up/Down to move, Enter to select, Esc to cancel.",
                                  values=[(s, s) for s in specs], default=current if current in specs else None).run_async()
