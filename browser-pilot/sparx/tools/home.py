"""Smart home, IoT and robotics: Home Assistant over its REST API, user-registered ESP32 / Raspberry Pi devices
over HTTP or MQTT, telemetry with threshold rules, energy, device health and guarded robot commands.

Nothing is simulated: without a configured hub or a registered device the tools say exactly what is missing.
Locks, alarms and garage doors go through home_security_set only, and that tool never runs unattended.
Everything a hub or device sends back is untrusted data.
"""
import asyncio
import json
import math
import re
import socket
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import unquote, urlsplit

import httpx

from .. import config, db, jobs, security, undo
from . import A, B, I, N, O, REGISTRY, S, ToolError, category, tool

category("home", "Smart home and IoT: Home Assistant lights, switches, climate, scenes, ESP32/Pi devices, sensor rules, routines")

SECURITY_DOMAINS = {"lock", "alarm_control_panel"}
SECURITY_KINDS = {"lock", "alarm", "security", "garage", "door", "gate"}
_ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_WORD = re.compile(r"^[a-z0-9_]+$")
_ENTRANCE = re.compile(r"garage|gate|door")
_SECRET_ATTR = re.compile(r"token|password|secret|api_?key", re.I)
_SECRET_QUERY = re.compile(r"((?:token|password|secret|api_?key)=)[^&\s\"']+", re.I)


def _client() -> httpx.AsyncClient:
    """Redirects are not followed, so a hub or device cannot bounce a token to another host."""
    return httpx.AsyncClient(timeout=10, follow_redirects=False)


def _obj(v) -> dict:
    """An object argument: a dict, or the JSON text of one."""
    if isinstance(v, str) and v.strip():
        try:
            v = json.loads(v)
        except ValueError:
            raise ToolError("expected a JSON object")
    if v in (None, ""):
        return {}
    if not isinstance(v, dict):
        raise ToolError("expected a JSON object")
    return v


# ---- Home Assistant

def _ha() -> tuple[str, str]:
    cfg = config.settings()["integrations"].get("home_assistant")
    url = str(cfg.get("url") or "").rstrip("/") if isinstance(cfg, dict) else ""
    token = config.secret("SPARX_HA_TOKEN")
    missing = []
    if not url:
        missing.append("the setting integrations.home_assistant.url "
                       "(run: sparx config integrations.home_assistant.url http://homeassistant.local:8123)")
    if not token:
        missing.append("the environment variable SPARX_HA_TOKEN (a Home Assistant long-lived access token, created "
                       f"under your HA profile > Security; put SPARX_HA_TOKEN=... in {config.path('.env', mkdir=False)})")
    if missing:
        raise ToolError("Home Assistant is not configured. Missing: " + " and ".join(missing) + ".")
    if not url.startswith(("http://", "https://")):
        raise ToolError(f"integrations.home_assistant.url must start with http:// or https:// (it is '{url}').")
    return url, token


async def _ha_request(method: str, path: str, body=None, params=None):
    url, token = _ha()
    try:
        async with _client() as c:
            r = await c.request(method, f"{url}/api/{path}", json=body, params=params,
                                headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise ToolError(f"Home Assistant at {url} did not answer ({type(e).__name__}: {e}). Check the url and that it is running.")
    if r.status_code == 401:
        raise ToolError("Home Assistant rejected the token (HTTP 401). Create a new long-lived access token and set SPARX_HA_TOKEN.")
    if r.status_code == 404:
        raise ToolError(f"Home Assistant has nothing at '{path}': unknown entity or service.")
    if not 200 <= r.status_code < 300:              # a redirect is not followed, so it is a failure too
        raise ToolError(f"Home Assistant returned HTTP {r.status_code} for {method} {path}"
                        + (", a redirect, which Sparx does not follow: nothing was done. Set integrations.home_assistant.url "
                           "to the address it redirects to." if r.status_code < 400 else "."))
    try:
        return r.json()
    except ValueError:
        return r.text[:2000]


def _entities(entity: str, domains: tuple = ()) -> list[str]:
    ids = [e.strip().lower() for e in str(entity).split(",") if e.strip()]
    for e in ids:
        if not _ENTITY.match(e):
            raise ToolError(f"'{e}' is not an entity id; they look like 'light.kitchen'. Find it with ha_states.")
        if domains and e.split(".")[0] not in domains:
            raise ToolError(f"'{e}' is not a {' / '.join(domains)} entity.")
    if not ids:
        raise ToolError("entity is required, e.g. 'light.kitchen'.")
    return ids


def _is_security(domain: str, ids: list[str]) -> bool:
    doms = {domain, *(e.split(".")[0] for e in ids)}
    return bool(doms & SECURITY_DOMAINS) or any(e.startswith("cover.") and _ENTRANCE.search(e) for e in ids)


async def _reaches_security(ids: list[str], depth: int = 0, seen: set | None = None) -> str:
    """The lock, alarm or entrance that acting on these entities would operate, found by asking Home Assistant what a
    scene or group contains and what kind of cover it is. '' when there is none."""
    seen = set() if seen is None else seen
    for e in ids:
        if e.split(".")[0] not in ("scene", "group", "cover") or e in seen or not _ENTITY.match(e):
            continue
        if depth > 3 or len(seen) >= 60:
            return e                                # nested too deep to check: treated as guarded
        seen.add(e)
        s = await _ha_request("GET", f"states/{e}")
        a = s.get("attributes") if isinstance(s, dict) else None
        a = a if isinstance(a, dict) else {}
        if e.startswith("cover.") and str(a.get("device_class") or "").strip().lower() in ("garage", "gate", "door"):
            return e
        members = a.get("entity_id") or []
        members = [str(m).strip().lower() for m in (members if isinstance(members, list) else [members])]
        if hit := next((m for m in members if _is_security("", [m])), "") or await _reaches_security(members, depth + 1, seen):
            return hit
    return ""


async def _ha_service(domain: str, service: str, entity: str, data: dict | None = None, domains: tuple = ()) -> dict:
    """Call a non-security service. Every Home Assistant control tool except home_security_set ends up here."""
    domain, service = domain.strip().lower(), service.strip().lower()
    if not _WORD.match(domain) or not _WORD.match(service):
        raise ToolError("domain and service are lowercase words, e.g. domain 'light', service 'turn_on'.")
    ids = _entities(entity, domains)
    data = dict(data or {})
    bad = [k for k in data if k in ("entity_id", "area_id", "device_id", "label_id", "floor_id")]
    if bad:
        raise ToolError(f"Pass the target in `entity`, not in data ({', '.join(bad)}): Sparx only acts on entities it can name.")
    if _is_security(domain, ids):
        raise ToolError("Locks, alarms and garage/door/gate covers are only controlled with home_security_set, "
                        "which needs the user's explicit confirmation.")
    if hit := await _reaches_security(ids):         # a scene or group holding a lock, a garage door with a plain name
        raise ToolError(f"Refused: Home Assistant reports that this would operate '{hit}', a lock, alarm or "
                        "garage/door/gate. Sparx does not run scenes or groups that contain one; operate it with "
                        "home_security_set, which needs the user's explicit confirmation.")
    changed = await _ha_request("POST", f"services/{domain}/{service}", {**data, "entity_id": ids})
    return {"called": f"{domain}.{service}", "entity": ids, "data": data,
            "changed": [s.get("entity_id") for s in changed if isinstance(s, dict)][:50] if isinstance(changed, list) else []}


def _brief(s: dict) -> dict:
    a = s.get("attributes") or {}
    return {"entity_id": s.get("entity_id"), "state": str(s.get("state"))[:120],
            "name": str(a.get("friendly_name", ""))[:80], "unit": str(a.get("unit_of_measurement", ""))[:20],
            "last_changed": str(s.get("last_changed", ""))[:32]}


@tool("ha_states", "List Home Assistant entities with their current state, optionally filtered by domain "
      "(light, switch, sensor, climate...) or by text in the id or name. Use it to find an entity id.",
      {"domain": S, "search": S, "limit": I}, category="home", untrusted=True)
async def ha_states(ctx, domain: str = "", search: str = "", limit: int = 60):
    states = await _ha_request("GET", "states")
    if not isinstance(states, list):
        raise ToolError("Home Assistant returned something that is not a list of states.")
    domain, search = domain.strip().lower(), search.strip().lower()
    rows = [_brief(s) for s in states if isinstance(s, dict)]
    rows = [r for r in rows if (not domain or str(r["entity_id"]).startswith(domain + "."))
            and (not search or search in f"{r['entity_id']} {r['name']}".lower())]
    return {"total": len(rows), "entities": sorted(rows, key=lambda r: str(r["entity_id"]))[:max(1, limit)]}


@tool("ha_get", "Current state and attributes of one Home Assistant entity, e.g. 'sensor.living_room_temperature'.",
      {"entity": S}, ["entity"], category="home", untrusted=True)
async def ha_get(ctx, entity: str):
    s = await _ha_request("GET", f"states/{_entities(entity)[0]}")
    if not isinstance(s, dict):
        raise ToolError("Home Assistant returned an unexpected answer.")
    attrs = {str(k)[:60]: (v if isinstance(v, (int, float, bool)) else str(v)[:200])
             for k, v in list((s.get("attributes") or {}).items())[:40]}
    attrs = {k: "***" if _SECRET_ATTR.search(k) else _SECRET_QUERY.sub(r"\1***", v) if isinstance(v, str) else v
             for k, v in attrs.items()}             # cameras and media players carry access tokens in their attributes
    return {**_brief(s), "attributes": attrs}


@tool("ha_call", "Call any Home Assistant service on an entity (domain 'media_player', service 'volume_set', ...). "
      "Prefer light_set / switch_set / fan_set / climate_set / scene_activate when they fit. "
      "Cannot operate locks, alarms or garage doors: those need home_security_set.",
      {"domain": S, "service": S, "entity": {**S, "description": "entity id, or several separated by commas"},
       "data": {**O, "description": "extra service data, e.g. {\"volume_level\": 0.4}"}},
      ["domain", "service", "entity"], category="home", risk="control", untrusted=True)
async def ha_call(ctx, domain: str, service: str, entity: str, data: dict | None = None):
    return await _ha_service(domain, service, entity, _obj(data))


def _colour(colour: str) -> dict:
    c = colour.strip().lower()
    if m := re.fullmatch(r"#?([0-9a-f]{6})", c):
        return {"rgb_color": [int(m[1][i:i + 2], 16) for i in (0, 2, 4)]}
    if m := re.fullmatch(r"(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", c):
        rgb = [int(g) for g in m.groups()]
        if max(rgb) > 255:
            raise ToolError("RGB values go from 0 to 255.")
        return {"rgb_color": rgb}
    if m := re.fullmatch(r"(\d{4,5})\s*k", c):
        return {"color_temp_kelvin": int(m[1])}
    if re.fullmatch(r"[a-z ]{3,30}", c):
        return {"color_name": c.replace(" ", "")}
    raise ToolError("colour is a name ('warm white', 'red'), '#rrggbb', 'r,g,b' or a temperature like '2700K'.")


@tool("light_set", "Turn a Home Assistant light on or off, optionally with brightness (percent) and colour.",
      {"entity": S, "on": B, "brightness": {**N, "description": "0-100 percent"},
       "colour": {**S, "description": "name, '#rrggbb', 'r,g,b' or '2700K'"}},
      ["entity"], category="home", risk="control", untrusted=True)
async def light_set(ctx, entity: str, on: bool = True, brightness: float = -1, colour: str = ""):
    if not on:
        return await _ha_service("light", "turn_off", entity, domains=("light",))
    data = _colour(colour) if colour else {}
    if brightness != -1:                            # -1 = not given
        if not 0 <= brightness <= 100:
            raise ToolError("brightness is a percentage from 0 to 100.")
        data["brightness_pct"] = round(brightness)
    return await _ha_service("light", "turn_on", entity, data, domains=("light",))


@tool("switch_set", "Turn a Home Assistant switch (or input_boolean) on or off.",
      {"entity": S, "on": B}, ["entity", "on"], category="home", risk="control", untrusted=True)
async def switch_set(ctx, entity: str, on: bool):
    domain = _entities(entity, ("switch", "input_boolean"))[0].split(".")[0]
    return await _ha_service(domain, "turn_on" if on else "turn_off", entity, domains=(domain,))


@tool("fan_set", "Turn a Home Assistant fan on or off, optionally at a speed percentage.",
      {"entity": S, "on": B, "percentage": {**N, "description": "speed 0-100"}},
      ["entity"], category="home", risk="control", untrusted=True)
async def fan_set(ctx, entity: str, on: bool = True, percentage: float = -1):
    if not on:
        return await _ha_service("fan", "turn_off", entity, domains=("fan",))
    if percentage != -1 and not 0 <= percentage <= 100:       # -1 = not given
        raise ToolError("percentage goes from 0 to 100.")
    return await _ha_service("fan", "turn_on", entity, {"percentage": round(percentage)} if percentage >= 0 else {},
                             domains=("fan",))


@tool("climate_set", "Set a Home Assistant thermostat / AC: target temperature and/or mode "
      "(off, heat, cool, heat_cool, auto, dry, fan_only).",
      {"entity": S, "temperature": N, "hvac_mode": S}, ["entity"], category="home", risk="control", untrusted=True)
async def climate_set(ctx, entity: str, temperature: float | None = None, hvac_mode: str = ""):
    mode = hvac_mode.strip().lower()
    if mode and mode not in ("off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"):
        raise ToolError("hvac_mode is one of off, heat, cool, heat_cool, auto, dry, fan_only.")
    if temperature is None and not mode:
        raise ToolError("Give a temperature, an hvac_mode, or both.")
    if temperature is not None and not 5 <= temperature <= 40:
        raise ToolError("temperature must be between 5 and 40 (the unit your Home Assistant uses).")
    done = []
    if mode:
        done.append(await _ha_service("climate", "set_hvac_mode", entity, {"hvac_mode": mode}, domains=("climate",)))
    if temperature is not None:
        try:
            done.append(await _ha_service("climate", "set_temperature", entity, {"temperature": temperature}, domains=("climate",)))
        except ToolError as e:
            if not done:
                raise
            raise ToolError(f"hvac_mode was set to '{mode}', but the temperature was NOT set: {e}")
    return done[0] if len(done) == 1 else done


@tool("scene_activate", "Activate a Home Assistant scene, e.g. 'scene.movie_night'.",
      {"entity": S}, ["entity"], category="home", risk="control", untrusted=True)
async def scene_activate(ctx, entity: str):
    return await _ha_service("scene", "turn_on", entity, domains=("scene",))


# ---- locks and security: one separate tool, never unattended

_SECURITY_SERVICES = {
    "lock": {"lock": "lock", "unlock": "unlock", "open": "open"},
    "alarm_control_panel": {"arm_home": "alarm_arm_home", "arm_away": "alarm_arm_away", "arm_night": "alarm_arm_night",
                            "disarm": "alarm_disarm"},
    "cover": {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"},
}


def _require_live_user(ctx, name: str):
    """Locks and security devices need a person confirming this very call."""
    if ctx.data.get("automated"):
        raise ToolError("Refused: locks and security devices are never operated from a routine, sensor rule or "
                        "background job. The user has to ask for it in a live conversation and confirm it.")
    if ctx.approve is None:
        raise ToolError("Refused: nobody is available to confirm, and locks and security devices always need the "
                        "user's explicit confirmation.")
    if security.permission("control", name) != "ask":
        raise ToolError(f"Refused: '{name}' would run without asking because of the permission settings, and locks and "
                        f"security devices always need explicit confirmation. Run: sparx config tool_permissions.{name} ask")


@tool("home_security_set", "Operate a lock, alarm panel or garage/door/gate: actions lock, unlock, open, close, stop, "
      "arm_home, arm_away, arm_night, disarm. Locks and security devices ALWAYS need the user's explicit confirmation "
      "for each call, so this is the only tool that can touch them and it refuses inside routines, sensor rules and "
      "any other automated context. Only use it when the user asked for exactly this action just now.",
      {"target": {**S, "description": "Home Assistant entity id (lock.front_door, alarm_control_panel.home, "
                                      "cover.garage_door) or the name of a registered security device"},
       "action": S, "pin": {**S, "description": "code, only if the lock or alarm requires one"}},
      ["target", "action"], category="home", risk="control", untrusted=True)
async def home_security_set(ctx, target: str, action: str, pin: str = ""):
    _require_live_user(ctx, "home_security_set")
    action = action.strip().lower()
    if "." not in target:
        dev = _device(target)
        if dev["kind"] not in SECURITY_KINDS:
            raise ToolError(f"'{dev['name']}' is registered as a {dev['kind']}, not a security device. Use device_command.")
        known = sorted({a for s in _SECURITY_SERVICES.values() for a in s})
        if action not in known:
            raise ToolError(f"action is one of: {', '.join(known)}.")
        out = await _send(dev, action, {"code": pin} if pin else None, "POST")
        if pin:                                     # the code is not returned: results also go to the audit log
            if "payload" in out:
                out["payload"] = {"code": "***"}
            if "response" in out:
                out["response"] = out["response"].replace(pin, "***")
        return out
    ids = _entities(target)
    if len(ids) != 1:
        raise ToolError("One lock or security device at a time.")
    domain = ids[0].split(".")[0]
    services = _SECURITY_SERVICES.get(domain)
    if not services or not (_is_security(domain, ids) or await _reaches_security(ids)):
        raise ToolError(f"'{ids[0]}' is not a lock, alarm panel or garage/door/gate cover. Use the ordinary home tools for it.")
    if action not in services:
        raise ToolError(f"A {domain} supports: {', '.join(services)}.")
    changed = await _ha_request("POST", f"services/{domain}/{services[action]}",
                                {"entity_id": ids[0], **({"code": pin} if pin else {})})
    return {"called": f"{domain}.{services[action]}", "entity": ids[0],
            "changed": [s.get("entity_id") for s in changed if isinstance(s, dict)][:10] if isinstance(changed, list) else []}


# ---- registered devices (ESP32 / Raspberry Pi) over HTTP or MQTT

db.schema("""
CREATE TABLE IF NOT EXISTS home_devices (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, transport TEXT NOT NULL,
    address TEXT NOT NULL, auth_env TEXT NOT NULL DEFAULT '', read_path TEXT NOT NULL DEFAULT '',
    poll INTEGER NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL,
    last_seen TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS home_contacts (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, device TEXT NOT NULL, ok INTEGER NOT NULL, ms INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS home_contacts_key ON home_contacts (device, ts);
CREATE TABLE IF NOT EXISTS home_telemetry (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, device TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS home_telemetry_key ON home_telemetry (device, metric, ts);
CREATE TABLE IF NOT EXISTS home_rules (
    id INTEGER PRIMARY KEY, device TEXT NOT NULL, metric TEXT NOT NULL, op TEXT NOT NULL, value REAL NOT NULL,
    action TEXT NOT NULL DEFAULT 'notify', target TEXT NOT NULL DEFAULT '', command TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}', breached INTEGER NOT NULL DEFAULT 0, last_fired TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL
);
""")

KINDS = ("sensor", "actuator", "robot", "camera", "other", *sorted(SECURITY_KINDS))
AUTH_PREFIX = "SPARX_DEVICE_"
_NAME = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,39}$")
_METRIC = re.compile(r"[^a-z0-9_.-]+")
_TOPIC = re.compile(r"^[A-Za-z0-9_\-./]+$")
_TABLES = ("home_devices", "home_rules", "home_routines")


def _now() -> datetime:
    return datetime.now()


def _name(name: str) -> str:
    n = str(name).strip().lower()
    if not _NAME.match(n):
        raise ToolError("A device name is 1-40 characters: letters, digits, spaces, '-' or '_' (e.g. 'greenhouse').")
    return n


def _device(name: str) -> dict:
    dev = db.one("SELECT * FROM home_devices WHERE name = ?", (str(name).strip().lower(),))
    if not dev:
        known = ", ".join(r["name"] for r in db.q("SELECT name FROM home_devices ORDER BY name")) or "none"
        raise ToolError(f"No registered device named '{name}'. Registered: {known}. Register one with device_add.")
    dev["meta"] = json.loads(dev["meta"] or "{}")
    return dev


def _remove(ctx, table: str, row: dict, label: str) -> str:
    db.x(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
    undo.record(ctx, "home_restore", f"Removed {label}", {"table": table, "row": row})
    return f"Removed {label}. It can be restored with undo."


@undo.handler("home_restore")
def _undo_remove(data):
    if data["table"] in _TABLES:
        row = data["row"]
        db.x(f"INSERT INTO {data['table']} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def _contact(name: str, ok: bool, ms: int, error: str = ""):
    """Health log: one row per attempt to reach a device, plus last-seen and the consecutive-failure count."""
    now = db.now()
    db.x("INSERT INTO home_contacts (ts, device, ok, ms, error) VALUES (?,?,?,?,?)", (now, name, int(ok), ms, error[:200]))
    if ok:
        db.x("UPDATE home_devices SET last_seen = ?, failures = 0, last_error = '' WHERE name = ?", (now, name))
    else:
        db.x("UPDATE home_devices SET failures = failures + 1, last_error = ? WHERE name = ?", (error[:200], name))


def _auth(dev: dict) -> dict:
    if not dev["auth_env"]:
        return {}
    token = config.secret(dev["auth_env"])
    if not token:
        raise ToolError(f"Device '{dev['name']}' needs the environment variable {dev['auth_env']} (its access token), "
                        f"which is not set. Add {dev['auth_env']}=... to {config.path('.env', mkdir=False)}.")
    return {"Authorization": f"Bearer {token}"}


def _url(dev: dict, path: str) -> str:
    path = str(path or "").strip()
    if "://" in path or path.startswith("//") or ".." in path or re.search(r"[\s@\\]", path):
        raise ToolError("A command is a path on the registered device (e.g. 'relay/on'), not a URL.")
    return dev["address"].rstrip("/") + ("/" + path.lstrip("/") if path else "")


async def _http(dev: dict, method: str, path: str, payload: dict | None = None) -> httpx.Response:
    url, headers, start = _url(dev, path), _auth(dev), time.monotonic()
    try:
        async with _client() as c:
            r = await c.request(method, url, json=payload or None, headers=headers)
    except httpx.HTTPError as e:
        _contact(dev["name"], False, int((time.monotonic() - start) * 1000), type(e).__name__)
        raise ToolError(f"Device '{dev['name']}' at {dev['address']} did not answer ({type(e).__name__}). "
                        "Run device_diagnose to find out why.")
    ms = int((time.monotonic() - start) * 1000)
    if not 200 <= r.status_code < 300:              # redirects are not followed, so 3xx means nothing was done
        _contact(dev["name"], False, ms, f"HTTP {r.status_code}")
        raise ToolError(f"Device '{dev['name']}' answered HTTP {r.status_code} for {method} {url}.")
    _contact(dev["name"], True, ms)
    return r


def _mqtt_cfg() -> tuple[str, int, str, str]:
    cfg = config.settings()["integrations"].get("mqtt")
    host = str(cfg.get("host") or "").strip() if isinstance(cfg, dict) else ""
    if not host:
        raise ToolError("MQTT is not configured. Missing: the setting integrations.mqtt.host (run: sparx config "
                        "integrations.mqtt.host 192.168.1.10). Optional: integrations.mqtt.port (default 1883) and "
                        "integrations.mqtt.username with its password in the environment variable SPARX_MQTT_PASSWORD.")
    try:
        port = int(cfg.get("port") or 1883)
    except (TypeError, ValueError):
        raise ToolError("integrations.mqtt.port must be a number.")
    return host, port, str(cfg.get("username") or ""), config.secret("SPARX_MQTT_PASSWORD")


def _paho():
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise ToolError("Install paho-mqtt to use MQTT devices: pip install paho-mqtt")
    return mqtt


def _mqtt_exchange(publish: tuple | None, listen: str, timeout: float) -> bytes | None:
    """Blocking: publish one message (topic, text), or wait for the next message on `listen`."""
    import queue
    import threading
    host, port, user, password = _mqtt_cfg()
    mqtt = _paho()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
    if user:
        c.username_pw_set(user, password or None)
    connected, got = threading.Event(), queue.Queue()

    def on_connect(client, *_):
        if listen:
            client.subscribe(listen)
        connected.set()
    c.on_connect = on_connect
    c.on_message = lambda client, userdata, m: got.put(bytes(m.payload))
    try:
        c.connect(host, port, 30)
        c.loop_start()
        try:
            if not connected.wait(timeout):
                raise OSError("the broker did not accept the connection in time (check username / SPARX_MQTT_PASSWORD)")
            if not publish:
                return got.get(timeout=timeout)
            info = c.publish(publish[0], publish[1], qos=1)
            info.wait_for_publish(timeout)
            if not info.is_published():
                raise OSError("the broker did not confirm the message")
        finally:
            c.loop_stop()
            c.disconnect()
    except queue.Empty:
        raise ToolError(f"No message arrived on MQTT topic '{listen}' within {timeout:.0f}s. The device may be off, "
                        "or it publishes on another topic.")
    except (OSError, RuntimeError, ValueError) as e:
        raise ToolError(f"MQTT broker {host}:{port} could not be used: {e}")


async def _mqtt(dev: dict, publish: tuple | None, listen: str = "", timeout: float = 10) -> bytes | None:
    start = time.monotonic()
    try:
        out = await asyncio.to_thread(_mqtt_exchange, publish, listen, timeout)
    except ToolError as e:
        _contact(dev["name"], False, int((time.monotonic() - start) * 1000), str(e))
        raise
    _contact(dev["name"], True, int((time.monotonic() - start) * 1000))
    return out


async def _send(dev: dict, command: str, payload: dict | None = None, method: str = "") -> dict:
    """Deliver one command to a registered device. Callers decide whether the device may be commanded."""
    command = str(command).strip()
    if not command:
        raise ToolError("command is required.")
    if dev["transport"] == "mqtt":
        if not _TOPIC.match(command):
            raise ToolError("An MQTT command is a sub-topic: letters, digits, '_', '-', '.', '/'.")
        topic = f"{dev['address'].rstrip('/')}/{command.strip('/')}"
        await _mqtt(dev, (topic, json.dumps(payload) if payload else ""))
        return {"device": dev["name"], "command": command, "published_to": topic, "payload": payload or {}}
    method = (method or ("POST" if payload else "GET")).upper()
    if method not in ("GET", "POST", "PUT"):
        raise ToolError("method is GET, POST or PUT.")
    r = await _http(dev, method, command, payload)
    return {"device": dev["name"], "command": command, "method": method, "status": r.status_code, "response": r.text[:2000]}


def _readings(data) -> dict[str, float]:
    """Numeric readings from a device payload (JSON text, a bare number, or a parsed object). Keys are reduced to
    safe metric names and everything that is not a finite number is dropped, so no device text is stored."""
    if isinstance(data, (str, bytes)):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        try:
            data = json.loads(text)
        except ValueError:
            data = text.strip()
    out: dict[str, float] = {}

    def put(key, v):
        if isinstance(v, str):
            v = {"on": 1, "off": 0, "true": 1, "false": 0}.get(v.strip().lower(), v)
            try:
                v = float(v)
            except ValueError:
                return
        key = _METRIC.sub("_", str(key).lower()).strip("_.")[:40]
        if key and isinstance(v, (int, float)) and math.isfinite(v) and len(out) < 40:
            out[key] = float(v)
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    put(f"{k}.{k2}", v2)
            else:
                put(k, v)
    else:
        put("value", data)
    return out


@tool("device_add", "Register an ESP32, Raspberry Pi or other device on the local network so Sparx can read and "
      "command it. transport 'http' needs its base URL (http://192.168.1.50); 'mqtt' needs its base topic. "
      "kind 'sensor' devices on http are polled every minute for telemetry. For kind 'robot' declare the allowed "
      "actions and numeric limits here; they can only be changed by registering the robot again.",
      {"name": S, "kind": {**S, "description": "sensor | actuator | robot | camera | lock | alarm | garage | door | gate | security | other"},
       "transport": {**S, "description": "http | mqtt"}, "address": {**S, "description": "base URL, or MQTT base topic"},
       "auth_env": {**S, "description": f"optional: name of the environment variable holding the device's bearer token; must start with {AUTH_PREFIX}"},
       "read_path": {**S, "description": "optional: path (http) or sub-topic (mqtt) that returns readings as JSON"},
       "actions": {**A, "description": "robots only: the allow-list of action names, e.g. ['forward', 'turn', 'stop']"},
       "limits": {**O, "description": "robots only: numeric limits per parameter, e.g. {\"speed\": 0.5} (max absolute value) or {\"angle\": [-90, 90]}"}},
      ["name", "kind", "transport", "address"], category="home", risk="control")
def device_add(ctx, name: str, kind: str, transport: str, address: str, auth_env: str = "", read_path: str = "",
               actions: list | None = None, limits: dict | None = None):
    name, kind, transport = _name(name), kind.strip().lower(), transport.strip().lower()
    address, auth_env, read_path = address.strip(), auth_env.strip(), read_path.strip()
    if kind not in KINDS:
        raise ToolError(f"kind is one of: {', '.join(KINDS)}.")
    if transport == "http":
        u = urlsplit(address)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ToolError("For http the address is the device's base URL without credentials or a query, "
                            "e.g. http://192.168.1.50 or http://esp32.local:8080.")
    elif transport == "mqtt":
        if not _TOPIC.match(address):
            raise ToolError("For mqtt the address is the device's base topic, e.g. home/greenhouse (no wildcards).")
    else:
        raise ToolError("transport is 'http' or 'mqtt'.")
    if auth_env and not re.fullmatch(AUTH_PREFIX + r"[A-Z0-9_]+", auth_env):
        raise ToolError(f"auth_env must be an environment variable name starting with {AUTH_PREFIX} "
                        f"(e.g. {AUTH_PREFIX}GREENHOUSE), so a device can never be handed one of Sparx's other credentials.")
    if read_path:
        _url({"address": address}, read_path)
    meta = {}
    if kind == "robot":
        acts = [str(a).strip().lower() for a in (actions or []) if _WORD.match(str(a).strip().lower())]
        if not acts or len(acts) != len(actions or []):
            raise ToolError("A robot needs an allow-list of actions (lowercase words), e.g. ['forward', 'turn', 'stop'].")
        lim = {}
        for k, v in _obj(limits).items():
            pair = [-abs(v), abs(v)] if isinstance(v, (int, float)) and not isinstance(v, bool) else v
            if not (isinstance(pair, list) and len(pair) == 2 and pair[0] <= pair[1]
                    and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in pair)):
                raise ToolError(f"limit '{k}' must be a maximum (0.5) or a [min, max] pair.")
            lim[str(k)] = pair
        meta = {"actions": acts, "limits": lim}
    if db.one("SELECT 1 FROM home_devices WHERE name = ?", (name,)):
        raise ToolError(f"A device named '{name}' already exists. Remove it with device_remove first.")
    db.x("INSERT INTO home_devices (name, kind, transport, address, auth_env, read_path, poll, meta, created) VALUES (?,?,?,?,?,?,?,?,?)",
         (name, kind, transport, address, auth_env, read_path, int(kind == "sensor" and transport == "http"), json.dumps(meta), db.now()))
    out = {"registered": name, "kind": kind, "transport": transport, "address": address, **meta}
    if auth_env and not config.secret(auth_env):
        out["missing"] = f"{auth_env} is not set yet; add it to {config.path('.env', mkdir=False)} before using the device."
    if kind in SECURITY_KINDS:
        out["note"] = "Security device: only home_security_set can command it, with the user's confirmation each time."
    return out


@tool("device_list", "Registered devices with kind, transport, address and when each was last reachable.", category="home")
def device_list(ctx):
    rows = db.q("SELECT name, kind, transport, address, auth_env, read_path, poll, meta, last_seen, failures FROM home_devices ORDER BY name")
    for r in rows:
        r.update(json.loads(r.pop("meta") or "{}"))
    return rows or "No devices registered. Register one with device_add."


@tool("device_remove", "Unregister a device. Its recorded telemetry is kept.", {"name": S}, ["name"],
      category="home", risk="delete")
def device_remove(ctx, name: str):
    row = db.one("SELECT * FROM home_devices WHERE name = ?", (_device(name)["name"],))
    return _remove(ctx, "home_devices", row, f"device '{row['name']}'")


def _landing(transport: str, address: str, command: str = "") -> str:
    """Where a command ends up, in a form two devices can be compared in: lowercase, the port explicit, escapes and
    '.' segments resolved, always ending in '/'. An address that cannot be read compares as itself."""
    full = f"{address.rstrip('/')}/{str(command).strip().lstrip('/')}"
    host = ""
    if transport != "mqtt":
        try:
            u = httpx.URL(full)
            host, full = f"{u.host}:{u.port or (443 if u.scheme == 'https' else 80)}/", unquote(u.path)
        except Exception:
            pass
    return host.lower() + "".join(p + "/" for p in full.lower().split("/") if p not in ("", "."))


@tool("device_command", "Send a command to a registered device: over HTTP the command is a path on the device "
      "(GET, or POST when there is a payload), over MQTT it is published to <topic>/<command>. "
      "Not for robots (robot_command) or locks and security devices (home_security_set).",
      {"name": S, "command": {**S, "description": "e.g. 'relay/on' or 'pump'"},
       "payload": {**O, "description": "optional JSON body"}, "method": {**S, "description": "http only: GET | POST | PUT"}},
      ["name", "command"], category="home", risk="control", untrusted=True)
async def device_command(ctx, name: str, command: str, payload: dict | None = None, method: str = ""):
    dev = _device(name)
    if dev["kind"] in SECURITY_KINDS:
        raise ToolError(f"'{dev['name']}' is a security device ({dev['kind']}). Only home_security_set can command it, "
                        "with the user's explicit confirmation.")
    if dev["kind"] == "robot":
        raise ToolError(f"'{dev['name']}' is a robot: use robot_command, which enforces its allow-list and limits.")
    if dev["transport"] == "http":
        _url(dev, command)                          # refuse a malformed command before comparing addresses
    dest = _landing(dev["transport"], dev["address"], command)
    for other in db.q("SELECT name, kind, address FROM home_devices WHERE transport = ? AND name != ?",
                      (dev["transport"], dev["name"])):
        if (other["kind"] in SECURITY_KINDS or other["kind"] == "robot") \
                and dest.startswith(_landing(dev["transport"], other["address"])):
            raise ToolError(f"Refused: this command would land on '{other['name']}' ({other['kind']}), which is registered "
                            f"at {other['address']}. Locks, security devices and robots are never commanded through "
                            "another device: use home_security_set or robot_command.")
    return await _send(dev, command, _obj(payload), method)


@tool("robot_command", "Send an action to a registered robot. Only actions on the allow-list declared when the robot "
      "was registered are sent, every numeric parameter must be inside its declared limit, and anything else is refused.",
      {"name": S, "action": S, "params": {**O, "description": "e.g. {\"speed\": 0.3, \"seconds\": 2}"}},
      ["name", "action"], category="home", risk="control", untrusted=True)
async def robot_command(ctx, name: str, action: str, params: dict | None = None):
    dev = _device(name)
    if dev["kind"] != "robot":
        raise ToolError(f"'{dev['name']}' is registered as a {dev['kind']}, not a robot. Use device_command.")
    allowed, limits = dev["meta"].get("actions", []), dev["meta"].get("limits", {})
    action, params = action.strip().lower(), _obj(params)
    if action not in allowed:
        raise ToolError(f"Refused: '{action}' is not on the allow-list for robot '{dev['name']}' (allowed: {', '.join(allowed)}).")
    for k, v in list(params.items()):
        if isinstance(v, (dict, list)):
            raise ToolError(f"Refused: parameter '{k}' must be a plain value so its limit can be checked.")
        if isinstance(v, str):
            try:
                v = params[k] = float(v)              # "0.9" is a number too: it must not slip past the limit
            except ValueError:
                pass
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if k not in limits:
                raise ToolError(f"Refused: no limit was declared for numeric parameter '{k}' of robot '{dev['name']}' "
                                f"(declared: {', '.join(limits) or 'none'}). Register the robot again with a limit for it.")
            lo, hi = limits[k]
            if not math.isfinite(v) or not lo <= v <= hi:
                raise ToolError(f"Refused: {k}={v} is outside the limit {lo:g}..{hi:g} declared for robot '{dev['name']}'.")
    return await _send(dev, action, params, "POST")


# ---- telemetry

_OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
_ALERT = re.compile(r"temp|motion|water|leak|flood|smoke|gas|co2|aqi|pm2|pm10|air")
_RANGES = {"temp": (-40, 85), "humid": (0, 100), "battery": (0, 100), "co2": (250, 10000), "pressure": (300, 1100)}
FRESH = timedelta(minutes=10)      # a rule only fires on a reading newer than this
REFIRE = timedelta(minutes=10)     # a rule that fired stays quiet this long, so a value hovering at the threshold cannot flood


def _record(device: str, readings: dict[str, float], ts: str = "") -> str:
    ts = ts or db.now()
    db.many("INSERT INTO home_telemetry (ts, device, metric, value) VALUES (?,?,?,?)",
            [(ts, device, m, v) for m, v in readings.items()])
    return ts


async def _read(dev: dict) -> dict[str, float]:
    """Ask a device for its readings and store them. Only numbers are kept."""
    if dev["transport"] == "mqtt":
        if not dev["read_path"]:
            raise ToolError(f"Device '{dev['name']}' has no read_path, so Sparx does not know which sub-topic carries its "
                            "readings. Register it again with read_path (e.g. 'state').")
        raw = await _mqtt(dev, None, listen=f"{dev['address'].rstrip('/')}/{dev['read_path'].strip('/')}")
    else:
        raw = (await _http(dev, "GET", dev["read_path"])).text
    readings = _readings(raw)
    if not readings:
        raise ToolError(f"Device '{dev['name']}' answered, but with no numeric readings. It should return JSON such as "
                        '{"temperature": 24.5, "humidity": 61}.')
    _record(dev["name"], readings)
    return readings


@tool("device_read", "Read the current sensor values from a registered device and record them as telemetry.",
      {"name": S}, ["name"], category="home", untrusted=True)
async def device_read(ctx, name: str):
    dev = _device(name)
    return {"device": dev["name"], "readings": await _read(dev), "recorded": db.now()}


@tool("telemetry_record", "Record one sensor reading by hand (for a device Sparx cannot poll).",
      {"device": S, "metric": {**S, "description": "e.g. temperature, humidity, power"}, "value": N},
      ["device", "metric", "value"], category="home", risk="write")
def telemetry_record(ctx, device: str, metric: str, value: float):
    readings = _readings({metric: value})
    if not readings:
        raise ToolError("metric must be a name and value a finite number.")
    return {"device": _name(device), "recorded": _record(_name(device), readings), **readings}


@tool("telemetry_query", "Recorded sensor telemetry for a device over the last hours: per metric the count, minimum, "
      "maximum, average and latest value; give a metric to also get its most recent points.",
      {"device": S, "metric": S, "hours": N}, ["device"], category="home")
def telemetry_query(ctx, device: str, metric: str = "", hours: float = 24):
    device, metric = _name(device), _METRIC.sub("_", metric.strip().lower())
    since = (_now() - timedelta(hours=max(hours, 0.1))).isoformat(timespec="seconds")
    rows = db.q("SELECT ts, metric, value FROM home_telemetry WHERE device = ? AND ts >= ?"
                + (" AND metric = ?" if metric else "") + " ORDER BY ts, id", (device, since, *([metric] if metric else [])))
    if not rows:
        raise ToolError(f"No telemetry for '{device}'" + (f" metric '{metric}'" if metric else "") + f" in the last {hours:g} hours. "
                        "Readings arrive from device_read, the sensor_rules job (http sensors, every minute while the "
                        "daemon runs) or telemetry_record.")
    by = defaultdict(list)
    for r in rows:
        by[r["metric"]].append(r)
    out = {"device": device, "since": since, "metrics": {
        m: {"count": len(v), "min": min(x["value"] for x in v), "max": max(x["value"] for x in v),
            "mean": round(statistics.fmean(x["value"] for x in v), 3), "latest": v[-1]["value"], "latest_at": v[-1]["ts"]}
        for m, v in by.items()}}
    if metric:
        out["points"] = [[r["ts"], r["value"]] for r in rows[-100:]]
    return out


# ---- threshold rules

def _commandable(dev: dict, who: str = "A sensor rule", hint: str = " Use action 'notify' instead."):
    if dev["kind"] in SECURITY_KINDS or dev["kind"] == "robot":
        raise ToolError(f"{who} cannot command '{dev['name']}' ({dev['kind']}): locks, security devices and "
                        f"robots are never operated unattended.{hint}")


@tool("sensor_rule_add", "Watch a sensor metric and react when it crosses a threshold, e.g. greenhouse temperature > 35. "
      "action 'notify' tells the user; action 'command' also sends `command` to a registered device (never a lock, "
      "security device or robot). A rule fires once when the threshold is crossed and again only after the value has "
      "gone back. Rules are checked every minute while the daemon runs.",
      {"device": S, "metric": S, "op": {**S, "enum": list(_OPS)}, "value": N,
       "action": {**S, "enum": ["notify", "command"]},
       "target": {**S, "description": "action 'command': the device to command (default: the same device)"},
       "command": {**S, "description": "action 'command': e.g. 'fan/on'"}, "payload": O},
      ["device", "metric", "op", "value"], category="home", risk="control")
def sensor_rule_add(ctx, device: str, metric: str, op: str, value: float, action: str = "notify", target: str = "",
                    command: str = "", payload: dict | None = None):
    dev, metric, action = _device(device), _METRIC.sub("_", metric.strip().lower()).strip("_."), action.strip().lower()
    if op not in _OPS:
        raise ToolError(f"op is one of: {' '.join(_OPS)}.")
    if not metric or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ToolError("Give a metric name and a numeric threshold value.")
    if action not in ("notify", "command"):
        raise ToolError("action is 'notify' or 'command'.")
    payload = _obj(payload)
    if action == "command":
        tgt = _device(target or dev["name"])
        _commandable(tgt)
        if not command.strip():
            raise ToolError("action 'command' needs the command to send, e.g. 'fan/on'.")
        target = tgt["name"]
    else:
        target, command, payload = "", "", {}
    rid = db.x("INSERT INTO home_rules (device, metric, op, value, action, target, command, payload, created) VALUES (?,?,?,?,?,?,?,?,?)",
               (dev["name"], metric, op, float(value), action, target, command.strip(), json.dumps(payload), db.now()))
    out = {"id": rid, "rule": f"{dev['name']} {metric} {op} {value:g}", "action": action}
    if action == "command":
        out["then"] = f"send '{command.strip()}' to {target}"
        if security.permission("control", "device_command") != "allow":
            out["note"] = ("When it fires, the command is not sent: device_command needs approval and nobody is asked "
                           "in the background. It is queued as a pending action for limits.pending_minutes "
                           "(pending_list, pending_approve). To let rules run it without asking: "
                           "sparx config tool_permissions.device_command allow")
    if not dev["poll"]:
        out["polling"] = (f"'{dev['name']}' is not polled automatically (only http devices of kind 'sensor' are); the "
                          "rule is checked against readings from device_read or telemetry_record.")
    return out


@tool("sensor_rule_list", "The sensor threshold rules, with whether each is currently breached and when it last fired.",
      category="home")
def sensor_rule_list(ctx):
    rows = db.q("SELECT id, device, metric, op, value, action, target, command, breached, last_fired FROM home_rules ORDER BY id")
    return [{k: v for k, v in r.items() if v not in ("", None)} for r in rows] or "No sensor rules. Add one with sensor_rule_add."


@tool("sensor_rule_remove", "Delete a sensor rule by id (from sensor_rule_list).", {"id": I}, ["id"],
      category="home", risk="delete")
def sensor_rule_remove(ctx, id: int):
    row = db.one("SELECT * FROM home_rules WHERE id = ?", (id,))
    if not row:
        raise ToolError(f"No sensor rule with id {id}.")
    return _remove(ctx, "home_rules", row, f"sensor rule {id} ({row['device']} {row['metric']} {row['op']} {row['value']:g})")


async def _fire(rule: dict, reading: dict, ctx) -> None:
    what = f"{rule['device']} {rule['metric']} is {reading['value']:g} ({rule['op']} {rule['value']:g})"
    why = f"Sensor rule {rule['id']}, which you added on {rule['created'][:10]}; reading taken at {reading['ts'][11:16]}."
    body = what + "."
    if rule["action"] == "command":
        from .. import tools                      # through the registry, so the user's permission settings decide
        try:
            result = await tools.call("device_command", {"name": rule["target"], "command": rule["command"],
                                                         "payload": json.loads(rule["payload"] or "{}")}, ctx)
        except security.Stopped as e:             # the command is not sent, but the user still hears about the reading
            result = f"Not done: {e}"
        if result.startswith(("Error:", "Not done:")):
            body += f" The command '{rule['command']}' to {rule['target']} was NOT sent: {result[:300]}"
        else:
            body += f" Sent '{rule['command']}' to {rule['target']}."
    jobs.notify(f"Sensor: {rule['device']} {rule['metric']}", body, kind="alert" if _ALERT.search(rule["metric"]) else "info", why=why)


async def evaluate_rules(ctx) -> list[int]:
    """Check every rule against the latest fresh reading of its metric. Returns the ids that fired."""
    fresh, fired = (_now() - FRESH).isoformat(timespec="seconds"), []
    for rule in db.q("SELECT * FROM home_rules ORDER BY id"):
        reading = db.one("SELECT ts, value FROM home_telemetry WHERE device = ? AND metric = ? AND ts >= ? ORDER BY ts DESC, id DESC LIMIT 1",
                         (rule["device"], rule["metric"], fresh))
        if not reading:
            continue
        breached = _OPS[rule["op"]](reading["value"], rule["value"])
        if breached == bool(rule["breached"]):
            continue
        if breached and rule["last_fired"] > (_now() - REFIRE).isoformat(timespec="seconds"):
            continue                # crossed again too soon: left un-fired, so it fires after the quiet period if still over
        db.x("UPDATE home_rules SET breached = ?, last_fired = CASE WHEN ? THEN ? ELSE last_fired END WHERE id = ?",
             (int(breached), int(breached), db.now(), rule["id"]))
        try:                # event:sensor.<device>.<metric> recipes; a failure here must not stop the rule itself
            from .. import automation
            automation.publish(f"{'sensor' if breached else 'sensor_clear'}.{rule['device'].replace(' ', '_')}.{rule['metric']}",
                               {"rule": rule["id"], "device": rule["device"], "metric": rule["metric"],
                                "value": reading["value"], "op": rule["op"], "threshold": rule["value"], "ts": reading["ts"]})
        except Exception:
            pass
        if breached:
            await _fire(rule, reading, ctx)
            fired.append(rule["id"])
    return fired


@jobs.every(60, "sensor_rules", flag="anomalies")
async def sensor_rules(ctx):
    """Poll the http sensors, record what they report, then evaluate the threshold rules."""
    ctx.data["automated"] = True
    for dev in db.q("SELECT * FROM home_devices WHERE poll = 1 ORDER BY name"):
        try:
            await _read({**dev, "meta": {}})
        except ToolError:
            pass                                   # already in the device's health log; device_health reports it
    return await evaluate_rules(ctx)


# ---- routines: a named, ordered list of ordinary home steps

db.schema("""
CREATE TABLE IF NOT EXISTS home_routines (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, steps TEXT NOT NULL, created TEXT NOT NULL,
    last_run TEXT NOT NULL DEFAULT ''
);
""")

ROUTINE_TOOLS = {"light_set": ("light",), "switch_set": ("switch", "input_boolean"), "fan_set": ("fan",),
                 "climate_set": ("climate",), "scene_activate": ("scene",), "device_command": ()}   # tool -> entity domains
MAX_STEPS = 20


def _steps(steps) -> list[dict]:
    """Validated routine steps. Everything a routine may ever do is decided here, when it is added."""
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except ValueError:
            steps = None
    if not isinstance(steps, list) or not steps:
        raise ToolError('steps is a non-empty list such as [{"tool": "light_set", "args": {"entity": "light.kitchen", "on": true}}].')
    if len(steps) > MAX_STEPS:
        raise ToolError(f"A routine has at most {MAX_STEPS} steps (this one has {len(steps)}).")
    out = []
    for i, step in enumerate(steps, 1):
        try:
            step = _obj(step)
            name, args = str(step.get("tool") or "").strip().lower(), _obj(step.get("args"))
            if name not in ROUTINE_TOOLS:
                raise ToolError(f"'{name}' cannot be used in a routine. Allowed: {', '.join(ROUTINE_TOOLS)}. Locks, "
                                "alarms, garage doors and robots are never part of a routine.")
            t = REGISTRY[name]
            if unknown := sorted(set(args) - set(t.params)):
                raise ToolError(f"{name} has no argument {', '.join(unknown)} (it takes: {', '.join(t.params)}).")
            if missing := [r for r in t.required if args.get(r) in (None, "")]:
                raise ToolError(f"{name} needs {', '.join(missing)}.")
            if name == "device_command":
                dev = _device(args["name"])
                _commandable(dev, "A routine", "")
                args["name"] = dev["name"]
            else:
                ids = _entities(args["entity"], ROUTINE_TOOLS[name])
                if _is_security("", ids):
                    raise ToolError("locks, alarms and garage/door/gate covers are never part of a routine.")
                args["entity"] = ",".join(ids)
        except ToolError as e:
            raise ToolError(f"Step {i}: {e}")
        out.append({"tool": name, "args": args})
    return out


def _routine(name: str) -> dict:
    row = db.one("SELECT * FROM home_routines WHERE name = ?", (str(name).strip().lower(),))
    if not row:
        known = ", ".join(r["name"] for r in db.q("SELECT name FROM home_routines ORDER BY name")) or "none"
        raise ToolError(f"No home routine named '{name}'. Routines: {known}. Add one with home_routine_add.")
    return row


@tool("home_routine_add", "Save a named home routine: an ordered list of steps to run together later with "
      "home_routine_run, e.g. 'movie night' = dim a light, switch a plug, start a fan. Each step is "
      "{\"tool\": ..., \"args\": {...}} and tool is one of light_set, switch_set, fan_set, climate_set, scene_activate, "
      "device_command. Locks, alarms, garage doors and robots cannot be put in a routine.",
      {"name": S, "steps": {**A, "description": "up to 20 steps, in order, e.g. [{\"tool\": \"light_set\", \"args\": "
                                                "{\"entity\": \"light.kitchen\", \"brightness\": 30}}]"}},
      ["name", "steps"], category="home", risk="write")
def home_routine_add(ctx, name: str, steps: list):
    n = str(name).strip().lower()
    if not _NAME.match(n):
        raise ToolError("A routine name is 1-40 characters: letters, digits, spaces, '-' or '_' (e.g. 'movie night').")
    steps = _steps(steps)
    if db.one("SELECT 1 FROM home_routines WHERE name = ?", (n,)):
        raise ToolError(f"A home routine named '{n}' already exists. Remove it with home_routine_remove first.")
    db.x("INSERT INTO home_routines (name, steps, created) VALUES (?,?,?)", (n, json.dumps(steps), db.now()))
    return {"saved": n, "steps": len(steps), "run_with": "home_routine_run"}


@tool("home_routine_list", "The saved home routines with their steps and when each was last run.", category="home")
def home_routine_list(ctx):
    rows = db.q("SELECT name, steps, created, last_run FROM home_routines ORDER BY name")
    return [{**r, "steps": json.loads(r["steps"]), "last_run": r["last_run"] or "never"} for r in rows] \
        or "No home routines. Add one with home_routine_add."


@tool("home_routine_remove", "Delete a saved home routine by name.", {"name": S}, ["name"], category="home", risk="delete")
def home_routine_remove(ctx, name: str):
    row = _routine(name)
    return _remove(ctx, "home_routines", row, f"home routine '{row['name']}'")


@tool("home_routine_run", "Run a saved home routine: its steps are carried out in order, each one subject to the "
      "user's permission settings, and a failed step does not stop the rest. Returns which steps were done and which "
      "were not, with the reason; report the ones that were not done to the user.",
      {"name": S}, ["name"], category="home", risk="control", timeout=300)
async def home_routine_run(ctx, name: str):
    from .. import tools                          # each step goes through the registry: permissions, audit, timeout
    row = _routine(name)
    was = ctx.data.get("automated")
    ctx.data["automated"] = True                  # so nothing a step reaches can be mistaken for a live request
    results = []
    try:
        for i, step in enumerate(json.loads(row["steps"]), 1):
            if step.get("tool") not in ROUTINE_TOOLS:      # the stored row was edited by hand
                text = f"Error: '{step.get('tool')}' cannot be used in a routine."
            else:
                text = await tools.call(step["tool"], dict(step.get("args") or {}), ctx)
            done = not text.startswith(("Error:", "Not done:"))
            results.append({"step": i, "tool": step.get("tool"), "args": step.get("args") or {}, "done": done,
                            **({} if done else {"result": text[:400]})})
    finally:
        if was is None:
            ctx.data.pop("automated", None)
        else:
            ctx.data["automated"] = was
    db.x("UPDATE home_routines SET last_run = ? WHERE id = ?", (db.now(), row["id"]))
    ok = sum(r["done"] for r in results)
    failed = [str(r["step"]) for r in results if not r["done"]]
    return {"routine": row["name"], "steps": results,
            "summary": f"{ok} of {len(results)} steps done" + (f"; NOT done: step {', '.join(failed)}" if failed else "")}


# ---- device health, diagnosis, energy

@tool("device_health", "Health of every registered device: last time it answered, consecutive failures, last error, "
      "and success rate and typical response time over the last 24 hours.", category="home")
def device_health(ctx):
    since = (_now() - timedelta(hours=24)).isoformat(timespec="seconds")
    out = []
    for d in db.q("SELECT name, kind, transport, last_seen, failures, last_error FROM home_devices ORDER BY name"):
        c = db.q("SELECT ok, ms FROM home_contacts WHERE device = ? AND ts >= ?", (d["name"], since))
        good = [r["ms"] for r in c if r["ok"]]
        out.append({**{k: v for k, v in d.items() if v not in ("", None)}, "last_seen": d["last_seen"] or "never",
                    "contacts_24h": len(c), "success_rate": round(len(good) / len(c), 2) if c else None,
                    "median_ms": int(statistics.median(good)) if good else None,
                    "status": "failing" if d["failures"] >= 3 else "unstable" if d["failures"] else "ok" if d["last_seen"] else "never contacted"})
    return out or "No devices registered. Register one with device_add."


def _tcp(host: str, port: int) -> tuple[str, str]:
    """('ok' | 'dns' | 'connect', detail). Blocking."""
    try:
        ip = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
    except OSError as e:
        return "dns", f"the name '{host}' does not resolve ({e})"
    try:
        socket.create_connection((ip, port), timeout=3).close()
    except OSError as e:
        return "connect", f"{host} ({ip}) port {port} does not accept connections ({type(e).__name__})"
    return "ok", f"{host} ({ip}) port {port} is reachable"


def _telemetry_checks(name: str) -> list[dict]:
    last = db.one("SELECT ts FROM home_telemetry WHERE device = ? ORDER BY ts DESC LIMIT 1", (name,))
    if not last:
        return [{"check": "telemetry", "ok": None, "detail": "no reading has ever been recorded"}]
    age = _now() - datetime.fromisoformat(last["ts"])
    checks = [{"check": "last reading", "ok": age <= timedelta(minutes=15),
               "detail": f"{int(age.total_seconds() // 60)} minute(s) ago ({last['ts']})"}]
    for m in [r["metric"] for r in db.q("SELECT DISTINCT metric FROM home_telemetry WHERE device = ? ORDER BY metric", (name,))]:
        pts = db.q("SELECT ts, value FROM home_telemetry WHERE device = ? AND metric = ? ORDER BY ts DESC, id DESC LIMIT 30", (name, m))
        span = datetime.fromisoformat(pts[0]["ts"]) - datetime.fromisoformat(pts[-1]["ts"])
        if len(pts) >= 10 and span >= timedelta(minutes=30) and len({p["value"] for p in pts}) == 1 \
                and not re.search(r"state|relay|switch|motion|door|on$", m):
            checks.append({"check": f"{m} stuck", "ok": False,
                           "detail": f"the last {len(pts)} readings over {int(span.total_seconds() // 60)} min are all {pts[0]['value']:g}"})
        rng = next((r for k, r in _RANGES.items() if k in m), None)
        if rng and not rng[0] <= pts[0]["value"] <= rng[1]:
            checks.append({"check": f"{m} range", "ok": False,
                           "detail": f"latest value {pts[0]['value']:g} is outside the plausible range {rng[0]}..{rng[1]}"})
    return checks


@tool("device_diagnose", "Find out why a registered device misbehaves, with fixed checks (no guessing): name resolution, "
      "network reachability, its HTTP answer or the MQTT broker, missing token, time since the last reading, stuck "
      "values and implausible values. Returns each check and a plain-language verdict.",
      {"name": S}, ["name"], category="home", untrusted=True)
async def device_diagnose(ctx, name: str):
    dev = _device(name)
    checks, verdict = [], ""

    def add(check, ok, detail):
        checks.append({"check": check, "ok": ok, "detail": detail})
        return ok
    if dev["auth_env"] and not config.secret(dev["auth_env"]):
        add("token", False, f"the environment variable {dev['auth_env']} is not set")
        verdict = f"Set {dev['auth_env']} in {config.path('.env', mkdir=False)}."
    if dev["transport"] == "http":
        u = urlsplit(dev["address"])
        state, detail = await asyncio.to_thread(_tcp, u.hostname, u.port or (443 if u.scheme == "https" else 80))
        add("network", state == "ok", detail)
        if state == "dns":
            verdict = verdict or ("The device's name cannot be found on the network: it is off, not on this Wi-Fi, or its "
                                  "address changed. Try its IP address.")
        elif state == "connect":
            verdict = verdict or ("The address exists but nothing answers on that port: the device is off, crashed, or "
                                  "its web server is not running.")
        elif not verdict:
            try:
                r = await _http(dev, "GET", dev["read_path"])
                add("http", True, f"HTTP {r.status_code}")
                found = bool(_readings(r.text))
                if not add("readings", found, "numeric readings found" if found else "the answer has no numeric readings"):
                    verdict = "The device answers, but not with JSON readings: check read_path and the firmware's output."
            except ToolError as e:
                add("http", False, str(e))
                verdict = ("The device is reachable but its web server refused or failed the request: check the path, "
                           "the token and the firmware.")
    else:
        try:
            host, port, _, _ = _mqtt_cfg()
            _paho()
            state, detail = await asyncio.to_thread(_tcp, host, port)
            if not add("mqtt broker", state == "ok", detail):
                verdict = verdict or "The MQTT broker cannot be reached, so no MQTT device can be either."
        except ToolError as e:
            add("mqtt setup", False, str(e))
            verdict = verdict or str(e)
    tele = _telemetry_checks(dev["name"])
    checks += tele
    bad = [c for c in tele if c["ok"] is False]
    if not verdict and bad:
        verdict = "The device is reachable, but its data looks wrong: " + "; ".join(f"{c['check']}: {c['detail']}" for c in bad) + "."
    return {"device": dev["name"], "failures_in_a_row": dev["failures"], "checks": checks,
            "verdict": verdict or "Every check passed: the device is reachable and its data looks normal."}


@tool("energy_usage", "Energy use per device and day from recorded telemetry: metric 'energy' (a kWh meter reading, "
      "usage is the rise over the day) or 'power' (watts, integrated over time). Only as complete as the readings.",
      {"days": I}, category="home")
def energy_usage(ctx, days: int = 7):
    since = (_now() - timedelta(days=max(1, days))).isoformat(timespec="seconds")
    rows = db.q("SELECT ts, device, metric, value FROM home_telemetry WHERE ts >= ? AND metric IN ('energy', 'energy_kwh', 'power', 'power_w') "
                "ORDER BY device, metric, ts, id", (since,))
    if not rows:
        raise ToolError("No energy telemetry recorded. A registered device has to report a metric named 'power' (watts) "
                        "or 'energy' (kWh meter); for Home Assistant energy sensors use ha_states with domain 'sensor'.")
    series = defaultdict(list)
    for r in rows:
        series[(r["device"], r["metric"].split("_")[0])].append(r)
    use = defaultdict(lambda: defaultdict(float))
    for (device, metric), pts in series.items():
        if metric == "power" and (device, "energy") in series:
            continue                                # the meter is the better source for the same device
        for a, b in zip(pts, pts[1:]):
            gap = (datetime.fromisoformat(b["ts"]) - datetime.fromisoformat(a["ts"])).total_seconds()
            if metric == "energy":
                kwh = max(b["value"] - a["value"], 0)           # a meter reset counts as zero, not negative
            else:
                kwh = (a["value"] + b["value"]) / 2 * gap / 3_600_000 if gap <= 900 else 0   # gaps over 15 min are not guessed
            use[device][b["ts"][:10]] += kwh
    per_device = {d: {"total_kwh": round(sum(v.values()), 3), "by_day": {k: round(x, 3) for k, x in sorted(v.items())}}
                  for d, v in use.items()}
    return {"since": since[:10], "total_kwh": round(sum(d["total_kwh"] for d in per_device.values()), 3), "devices": per_device,
            "note": "Computed only from recorded readings; periods without readings are not estimated."}
