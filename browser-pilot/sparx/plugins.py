"""Plugins: tools from MCP servers, and opt-in Python plugin files.

settings.json:
    "integrations": {
      "mcp_servers": {
        "files":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/work"]},
        "github": {"command": "github-mcp", "env": {"GITHUB_TOKEN": "secret:github"}},
        "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "secret:remote_auth"}}
      },
      "python_plugins": ["my_tools"]          # runs HOME/plugins/my_tools.py at start-up: explicit opt-in
    }

Nothing is started by load(). A server is launched when it is connected (plugin_connect); its tools then appear
as mcp_<server>_<tool> in group "mcp:<server>", risk "control" (asks first) unless the server sets "risk" or the
user sets tool_permissions for that tool. Their output is untrusted.

Credential isolation: a plugin process does not inherit Sparx's environment. It gets a few non-secret system
variables (PATH, SYSTEMROOT, TEMP, ...) plus exactly what its "env" lists. Values may be "secret:<name>"
(DPAPI store, see sparx.crypto) or "env:<NAME>" (copied from Sparx's environment); "env" may also be a list of
variable names to copy.
"""
import asyncio
import atexit
import concurrent.futures
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading

import httpx

from . import __version__, config, security, tools
from .tools import ToolError

PROTOCOL = "2025-06-18"
TIMEOUT = 60.0            # seconds for one tools/call unless the server config sets "timeout"
CONNECT_TIMEOUT = 20.0    # seconds for initialize and tools/list
_BASE_ENV = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "TMPDIR",
             "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
             "PROGRAMFILES", "PROGRAMFILES(X86)", "LANG"}
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,30}")
_PY_NAME = re.compile(r"[A-Za-z_]\w{0,40}")
_PROP = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_WITHHELD = "(withheld: it contained instruction-like text)"
MAX_TOOLS = 100           # tools taken from one server; the rest are left out and reported


class McpError(ToolError):
    pass


def _resolve(value) -> str:
    value = str(value)
    if value.startswith("secret:"):
        from . import crypto
        try:
            found = crypto.secret_get(value[7:])
        except crypto.CryptoError as e:
            raise McpError(f"cannot read secret '{value[7:]}': {e}")
        if found is None:
            raise McpError(f"secret '{value[7:]}' is not stored yet; save it with secret_store first")
        return found
    return os.environ.get(value[4:], "") if value.startswith("env:") else value


def child_env(spec=None) -> dict:
    """Environment for a plugin process: system basics plus only what its config lists."""
    env = {k: v for k, v in os.environ.items() if k.upper() in _BASE_ENV}
    if isinstance(spec, list):
        spec = {k: f"env:{k}" for k in spec}
    env.update({str(k): _resolve(v) for k, v in (spec or {}).items()})
    return env


class Stdio:
    """Newline-delimited JSON-RPC 2.0 over a child process's stdin/stdout. A thread reads; any event loop may ask."""

    def __init__(self, name: str, command: str, args: list, env: dict):
        self.name, self.command, self.args, self.env = name, command, [str(a) for a in args], env
        self.proc: subprocess.Popen | None = None
        self.pending: dict[int, concurrent.futures.Future] = {}
        self.n = 0
        self.wlock = threading.Lock()

    def start(self):
        exe = shutil.which(self.command, path=self.env.get("PATH") or self.env.get("Path")) or self.command
        with config.path("logs", f"mcp-{self.name}.log").open("ab") as log:
            try:
                self.proc = subprocess.Popen(
                    [exe, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=self.env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                raise McpError(f"cannot start '{self.command}': {e}")
        threading.Thread(target=self._read, name=f"mcp-{self.name}", daemon=True).start()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _read(self):
        proc = self.proc
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:      # the server asking us something: we only answer ping
                    reply = {"result": {}} if msg["method"] == "ping" else \
                        {"error": {"code": -32601, "message": "not supported by this client"}}
                    try:
                        self._write({"jsonrpc": "2.0", "id": msg["id"], **reply})
                    except (OSError, ValueError):
                        pass
                elif "id" in msg:
                    self._settle(self.pending.pop(msg["id"], None), msg)
        except (OSError, ValueError):
            pass
        for fut in [self.pending.pop(k) for k in list(self.pending)]:
            self._settle(fut, None)

    @staticmethod
    def _settle(fut, msg):
        if fut is not None and not fut.done():
            try:
                fut.set_result(msg)
            except concurrent.futures.InvalidStateError:
                pass

    def _write(self, msg: dict):
        with self.wlock:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()

    async def notify(self, method: str, params: dict | None = None):
        try:
            await asyncio.to_thread(self._write, {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})
        except (OSError, ValueError):
            pass

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        if not self.alive():
            raise McpError("the plugin process is not running")
        self.n += 1
        rid, fut = self.n, concurrent.futures.Future()
        self.pending[rid] = fut
        try:
            await asyncio.to_thread(self._write, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            msg = await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except asyncio.TimeoutError:
            await self.notify("notifications/cancelled", {"requestId": rid, "reason": "timeout"})
            raise McpError(f"{method} timed out after {timeout:g}s")
        except (OSError, ValueError) as e:
            raise McpError(f"the plugin process went away: {e}")
        finally:
            self.pending.pop(rid, None)
        return _result(msg, method)

    def close(self):
        """Ask the process to exit by closing its stdin; kill it (and its children) if it does not."""
        p, self.proc = self.proc, None
        if p is None:
            return
        for step in (lambda: p.stdin.close(), lambda: p.wait(1.5)):
            try:
                step()
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if p.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
        try:
            p.stdout.close()
        except OSError:
            pass


class Http:
    """Streamable-HTTP transport: one POST per request; the reply is JSON or a short SSE stream."""

    def __init__(self, url: str, headers: dict | None = None, transport=None):
        self.url, self.headers, self.transport = url, headers or {}, transport
        self.session = ""
        self.n = 0
        self.open = False

    def start(self):
        self.open = True

    def alive(self) -> bool:
        return self.open

    async def _post(self, msg: dict, timeout: float) -> httpx.Response:
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL,
                   **{k: _resolve(v) for k, v in self.headers.items()}}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as c:
                r = await c.post(self.url, json=msg, headers=headers)
        except httpx.TimeoutException:
            raise McpError(f"{msg['method']} timed out after {timeout:g}s")
        except httpx.HTTPError as e:
            raise McpError(f"cannot reach {self.url}: {type(e).__name__}")
        if r.status_code >= 400:
            raise McpError(f"{self.url} answered HTTP {r.status_code}")
        self.session = r.headers.get("mcp-session-id") or self.session
        return r

    async def notify(self, method: str, params: dict | None = None):
        try:
            await self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, CONNECT_TIMEOUT)
        except McpError:
            pass

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        self.n += 1
        r = await self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}, timeout)
        if "text/event-stream" in r.headers.get("content-type", ""):
            msgs = []
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        msgs.append(json.loads(line[5:]))
                    except ValueError:
                        pass
            msg = next((m for m in msgs if isinstance(m, dict) and m.get("id") == self.n), None)
        else:
            try:
                msg = r.json()
            except ValueError:
                msg = None
        return _result(msg, method)

    def close(self):
        self.open = False


def _result(msg, method: str) -> dict:
    if not isinstance(msg, dict):
        raise McpError(f"no valid reply to {method} (the plugin exited or answered with something else)")
    if msg.get("error"):
        e = msg["error"]
        said = str(e.get("message", e) if isinstance(e, dict) else e)[:500]      # the plugin's words, not Sparx's
        raise McpError(f"{method} failed: {security.wrap_untrusted(said, 'plugin error')}")
    return msg.get("result") if isinstance(msg.get("result"), dict) else {}


def _content_text(result: dict) -> str:
    out = []
    for c in result.get("content") or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            out.append(str(c.get("text", "")))
        elif c.get("type") == "resource" and isinstance(c.get("resource"), dict) and "text" in c["resource"]:
            out.append(str(c["resource"]["text"]))
        else:
            out.append(f"[{c.get('type', 'item')} {c.get('mimeType', '')} not shown]")
    if not out and result.get("structuredContent") is not None:
        out.append(json.dumps(result["structuredContent"], ensure_ascii=False, default=str))
    return "\n".join(out)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "x"


def _clean(node, depth: int = 0):
    """Copy of a plugin's parameter schema with every text in it capped and screened: the model reads all of it."""
    if isinstance(node, str):
        return _WITHHELD if security.scan_injection(node) else node[:300]
    if depth > 6:
        return None
    if isinstance(node, dict):
        return {k: _clean(v, depth + 1) for k, v in node.items()
                if isinstance(k, str) and len(k) <= 64 and not security.scan_injection(k)}
    if isinstance(node, list):
        return [_clean(v, depth + 1) for v in node]
    return node


def _plain(s) -> str:
    """A plugin-supplied name reduced to harmless characters, for reports and labels."""
    return re.sub(r"[^A-Za-z0-9_.\-/]", "?", str(s))[:64]


def _target(cfg: dict) -> str:
    """The command line or URL for display, with what looks like an inline credential masked."""
    if cfg.get("url"):
        return re.sub(r"\?.*", "?***", re.sub(r"//[^/@]*@", "//***@", str(cfg["url"])))
    out, hide = [str(cfg.get("command", ""))], False
    for a in map(str, cfg.get("args") or []):
        key, eq, _ = a.partition("=")
        out.append("***" if hide and not a.startswith("-") else
                   f"{key}=***" if eq and security._SECRET_KEY.search(key) else a)
        hide = not eq and a.startswith("-") and bool(security._SECRET_KEY.search(a))
    return " ".join(out)


class Server:
    def __init__(self, name: str, cfg: dict):
        self.name, self.cfg = name, cfg
        self.transport = None
        self.tools: list[str] = []          # registry names of this server's tools
        self.info: dict = {}
        self.error = ""
        self.renamed: dict[str, str] = {}   # registry name -> the plugin's own name, where the plain name was taken
        self.skipped: list[str] = []        # what was left out, in words

    @property
    def connected(self) -> bool:
        return self.transport is not None and self.transport.alive()

    @property
    def category(self) -> str:
        return f"mcp:{self.name}"

    def _make(self):
        if self.cfg.get("url"):
            return Http(str(self.cfg["url"]), self.cfg.get("headers"))
        if not self.cfg.get("command"):
            raise McpError(f"plugin '{self.name}' needs a \"command\" or a \"url\" in settings")
        return Stdio(self.name, str(self.cfg["command"]), self.cfg.get("args") or [], child_env(self.cfg.get("env")))

    async def connect(self) -> list[str]:
        if self.connected:
            return self.tools
        await self.disconnect()
        self.error = ""
        t = None
        try:
            t = self._make()
            await asyncio.to_thread(t.start)
            init = await t.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                  "clientInfo": {"name": "sparx", "version": __version__}}, CONNECT_TIMEOUT)
            await t.notify("notifications/initialized")
            found, cursor = [], None
            for _ in range(20):
                page = await t.request("tools/list", {"cursor": cursor} if cursor else {}, CONNECT_TIMEOUT)
                found += [x for x in page.get("tools") or [] if isinstance(x, dict) and x.get("name")]
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        except Exception as e:
            if t is not None:
                await asyncio.to_thread(t.close)
            self.error = str(e)
            raise e if isinstance(e, McpError) else McpError(f"{type(e).__name__}: {e}")
        self.transport, self.info = t, init.get("serverInfo") if isinstance(init.get("serverInfo"), dict) else {}
        self._register(found)
        if cursor:
            self.skipped.append("the server kept paging its tool list; stopped after 20 pages")
        return self.tools

    def _register(self, found: list[dict]):
        risk = self.cfg.get("risk") if self.cfg.get("risk") in security.RISKS else "control"
        timeout = float(self.cfg.get("timeout") or TIMEOUT)
        label = str(self.info.get("name") or "")     # the group description is model-facing too
        label = "" if security.scan_injection(label) else re.sub(r"[^A-Za-z0-9 ._/@-]", "", label)[:40].strip()
        tools.category(self.category, f"Plugin '{self.name}' (MCP server{': ' + label if label else ''})")
        seen, unique = set(), []
        for spec in found:                           # the same tool listed twice (or on every page) is one tool
            if str(spec["name"]) not in seen:
                seen.add(str(spec["name"]))
                unique.append(spec)
        if len(unique) > MAX_TOOLS:
            self.skipped.append(f"{len(unique) - MAX_TOOLS} tools beyond the first {MAX_TOOLS} were left out")
        for spec in unique[:MAX_TOOLS]:
            base = reg = f"mcp_{_slug(self.name)}_{_slug(spec['name'])}"[:64]
            n = 1
            while reg in tools.REGISTRY and n < 99:  # never replace a tool that is already there, whoever owns it
                n += 1
                reg = f"{base[:63 - len(str(n))]}_{n}"
            if reg in tools.REGISTRY:
                self.skipped.append(f"{_plain(spec['name'])}: no free tool name")
                continue
            if reg != base:
                self.renamed[reg] = _plain(spec["name"])
            about = " ".join(str(spec.get("description") or spec["name"]).split())[:600]
            if security.scan_injection(about):       # a tool description is model-facing text written by a third party
                about = "(description withheld: it contained instruction-like text)"
            schema = spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {}
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            props = {k: _clean(v) if isinstance(v, dict) else {} for k, v in props.items()
                     if isinstance(k, str) and _PROP.fullmatch(k)}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            required = [r for r in required if isinstance(r, str) and r in props]
            tools.tool(reg, f"[{self.name} plugin] {about}", props, required, category=self.category, risk=risk,
                       untrusted=True, timeout=timeout + 5)(self._caller(spec["name"], timeout))
            self.tools.append(reg)

    def _caller(self, remote: str, timeout: float):
        async def run(ctx, **args):
            return await self.call(remote, args, timeout)
        return run

    async def call(self, remote: str, args: dict, timeout: float = TIMEOUT) -> str:
        if not self.connected:
            raise McpError(f"plugin '{self.name}' is not connected (its process ended). Use plugin_connect to start it again.")
        result = await self.transport.request("tools/call", {"name": remote, "arguments": args}, timeout)
        text = _content_text(result)
        if result.get("isError"):
            raise ToolError(security.wrap_untrusted(text[:2000], f"plugin:{self.name}") if text else
                            "the plugin reported an error without a message")
        return text or "(the plugin returned nothing)"

    def _unregister(self):
        for reg in self.tools:
            tools.REGISTRY.pop(reg, None)
        tools.CATEGORIES.pop(self.category, None)
        self.tools, self.renamed, self.skipped = [], {}, []

    def close(self):
        t, self.transport = self.transport, None
        self._unregister()
        if t is not None:
            t.close()

    async def disconnect(self):
        await asyncio.to_thread(self.close)

    def status(self) -> dict:
        c = self.cfg
        env = c.get("env") or {}
        return {"name": self.name, "transport": "http" if c.get("url") else "stdio",
                "target": _target(c),
                "env": sorted(env) if isinstance(env, (dict, list)) else [],
                "status": "connected" if self.connected else "error" if self.error else "not connected",
                **({"error": self.error} if self.error and not self.connected else {}),
                "group": self.category, "tools": list(self.tools),
                **({"renamed": dict(self.renamed)} if self.renamed else {}),
                **({"skipped": list(self.skipped)} if self.skipped else {})}


SERVERS: dict[str, Server] = {}
PYTHON: dict[str, str] = {}      # loaded Python plugins: name -> file


def configured() -> dict:
    found = config.settings()["integrations"].get("mcp_servers") or {}
    return {k: v for k, v in found.items() if isinstance(v, dict) and _NAME.fullmatch(str(k))} if isinstance(found, dict) else {}


def load():
    """Read the plugin configuration. Starts no process; Python plugins named in settings are imported."""
    sync()
    _load_python()


def sync():
    cfg = configured()
    for name in list(SERVERS):
        if cfg.get(name) != SERVERS[name].cfg:
            SERVERS.pop(name).close()
    for name, c in cfg.items():
        SERVERS.setdefault(name, Server(name, c))


_PY_OWNED: set[str] = set()         # tools that opted-in Python plugins registered themselves (a reload may replace those)


def _guard_builtins(key: str, before: dict):
    """After a Python plugin ran: put back any tool it replaced that was not its own, and drop `mcp_` names, so a plugin
    cannot turn a built-in (or an MCP plugin's tool) into one that never asks. A plugin is arbitrary code, so this
    guards against accidents and copied snippets; it is not a sandbox."""
    refused = []
    for n, t in list(tools.REGISTRY.items()):
        if n in before and before[n] is not t and n not in _PY_OWNED:
            tools.REGISTRY[n] = before[n]
            refused.append(n)
        elif n not in before and n.startswith("mcp_"):
            del tools.REGISTRY[n]
            refused.append(n)
        elif n not in before or before[n] is not t:
            _PY_OWNED.add(n)
    if refused:
        tools.LOAD_ERRORS[key] = f"tried to replace built-in tool(s) {', '.join(sorted(refused))}; kept the built-in"


def _load_python():
    for name in config.settings()["integrations"].get("python_plugins") or []:
        key, name = f"plugin:{name}", str(name)
        f = config.path("plugins", f"{name}.py")
        if not _PY_NAME.fullmatch(name) or not f.is_file():
            tools.LOAD_ERRORS[key] = f"no such plugin file: {f}"
            continue
        before = dict(tools.REGISTRY)
        try:
            spec = importlib.util.spec_from_file_location(f"sparx_plugin_{name}", f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            PYTHON[name] = str(f)
            tools.LOAD_ERRORS.pop(key, None)
        except Exception as e:
            tools.LOAD_ERRORS[key] = f"{type(e).__name__}: {e}"
        _guard_builtins(key, before)


def get(name: str) -> Server:
    sync()
    if name not in SERVERS:
        raise McpError(f"no plugin named '{name}'. Configured: {', '.join(sorted(SERVERS)) or 'none'}.")
    return SERVERS[name]


def _edit(change):
    """Rewrite integrations.mcp_servers in settings.json (save_settings can only merge, not remove)."""
    f = config.path("settings.json")
    try:
        text = f.read_text(encoding="utf-8")
        cur = json.loads(text) if text.strip() else {}
        servers = cur.setdefault("integrations", {}).setdefault("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcp_servers is not a group")
    except FileNotFoundError:
        servers = (cur := {}).setdefault("integrations", {}).setdefault("mcp_servers", {})
    except (OSError, ValueError, AttributeError) as e:      # damaged: rewriting it would throw the other settings away
        raise McpError(f"settings.json cannot be read as settings ({type(e).__name__}); it was left untouched. Repair it first.")
    change(servers)
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")        # same folder, then swapped in: never a half-written file
    try:
        tmp.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    sync()


def add(name: str, cfg: dict):
    if not _NAME.fullmatch(name or ""):
        raise McpError("plugin names are lowercase letters, digits, '_' or '-' (max 31 characters)")
    _edit(lambda servers: servers.__setitem__(name, cfg))


def remove(name: str) -> dict:
    cfg = get(name).cfg
    _edit(lambda servers: servers.pop(name, None))
    return cfg


def close_all():
    for s in list(SERVERS.values()):
        s.close()


atexit.register(close_all)
