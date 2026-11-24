"""Tools for managing plugins (MCP servers) and the encrypted secret store."""
import ipaddress
import re
from urllib.parse import urlsplit

from .. import crypto, plugins, security, undo
from . import A, S, ToolError, category, tool

category("plugins", "Extra tools from MCP plugin servers (list, connect, add, remove) and the secret store")

_CREDENTIAL_VAR = re.compile(r"_(KEY|TOKEN|PASSWORD)$", re.I)


def _left_out(server) -> dict:
    return {k: v for k, v in (("renamed", dict(server.renamed)), ("skipped", list(server.skipped))) if v}


def _loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


@tool("plugin_list", "List the configured plugins (MCP servers), whether each is connected, and the tools each one provides. "
      "Use it to see what extra capabilities exist before connecting one.", category="plugins", untrusted=True)
def plugin_list(ctx):
    plugins.sync()
    return security.redact({"servers": [s.status() for s in plugins.SERVERS.values()],
                            "python_plugins": sorted(plugins.PYTHON),
                            "note": "Tools of a server appear after plugin_connect, in group 'mcp:<name>'."})


@tool("plugin_connect", "Start a configured plugin (this runs its program or contacts its URL) and make its tools available "
      "in this conversation as mcp_<plugin>_<tool>.", {"name": S}, ["name"], category="plugins", risk="exec", timeout=90)
async def plugin_connect(ctx, name: str):
    server = plugins.get(name)
    names = await server.connect()
    ctx.active.add(server.category)
    return {"connected": name, "group": server.category, "tools": names, **_left_out(server)}


@tool("plugin_disconnect", "Stop a running plugin and remove its tools until it is connected again.",
      {"name": S}, ["name"], category="plugins", risk="write")
async def plugin_disconnect(ctx, name: str):
    server = plugins.get(name)
    await server.disconnect()
    ctx.active.discard(server.category)
    return f"Plugin '{name}' stopped."


@tool("plugin_add", "Add an MCP server as a plugin and start it. Give either a command (with args) to run on this computer, "
      "or a url for a remote server. Credentials are never passed inline: store them with secret_store and reference them in env.",
      {"name": {**S, "description": "short lowercase name, e.g. 'files'"},
       "command": {**S, "description": "program to run, e.g. 'npx'"},
       "args": {**A, "description": "arguments for the command"},
       "url": {**S, "description": "streamable-HTTP MCP endpoint, instead of command"},
       "env": {**A, "description": "variables the plugin may see, each 'NAME=secret:<stored secret>' or 'NAME=env:<VARIABLE>'"}},
      ["name"], category="plugins", risk="exec", timeout=90)
async def plugin_add(ctx, name: str, command: str = "", args: list | None = None, url: str = "", env: list | None = None):
    if bool(command) == bool(url):
        raise ToolError("give exactly one of command or url")
    if url and not url.lower().startswith(("http://", "https://")):
        raise ToolError("url must start with http:// or https://")
    if name in plugins.configured():        # adding must not quietly swap the program or address behind a known name
        raise ToolError(f"a plugin named '{name}' already exists; remove it with plugin_remove first or pick another name")
    cfg: dict = {"url": url} if url else {"command": command, "args": [str(a) for a in args or []]}
    pairs = {}
    for item in env or []:
        key, _, ref = str(item).partition("=")
        if not key or not ref.startswith(("secret:", "env:")):
            raise ToolError("env entries must look like NAME=secret:<name> or NAME=env:<VARIABLE>; "
                            "plain values are refused so credentials do not end up in settings or logs")
        var = ref[4:] if ref.startswith("env:") else ""
        if var and (security._SECRET_KEY.search(var) or _CREDENTIAL_VAR.search(var)):
            raise ToolError(f"'{var}' looks like one of Sparx's own credentials, and env: would hand it to this plugin. "
                            "Not added. If the plugin really needs a credential, the user stores it with secret_store "
                            f"and it is referenced as {key}=secret:<name>.")
        if url and ref.startswith("secret:") and not url.lower().startswith("https://") and not _loopback(url):
            raise ToolError("a stored secret is not sent over plain http:// to another machine: anyone on the way could "
                            "read it. Not added. Use an https:// url.")
        pairs[key] = ref
    if pairs:
        cfg["headers" if url else "env"] = pairs
    plugins.add(name, cfg)
    server = plugins.get(name)
    try:
        names = await server.connect()
    except ToolError as e:
        return {"added": name, "connected": False, "error": str(e), "note": "Saved; fix the problem and use plugin_connect."}
    ctx.active.add(server.category)
    return {"added": name, "connected": True, "group": server.category, "tools": names, **_left_out(server)}


@tool("plugin_remove", "Stop a plugin and remove it from the configuration. Can be undone.",
      {"name": S}, ["name"], category="plugins", risk="delete")
async def plugin_remove(ctx, name: str):
    server = plugins.get(name)
    await server.disconnect()
    ctx.active.discard(server.category)
    cfg = plugins.remove(name)
    undo.record(ctx, "plugin_remove", f"Removed plugin '{name}'", {"name": name, "cfg": cfg})
    return f"Plugin '{name}' removed."


@undo.handler("plugin_remove")
def _undo_remove(data):
    plugins.add(data["name"], data["cfg"])


@tool("secret_store", "Save a credential (API key, password, token) encrypted on this computer under a name, so plugins can "
      "use it as 'secret:<name>'. The value is never shown again, logged or returned. It refuses to overwrite an existing "
      "name unless replace=true.",
      {"name": {**S, "description": "label, e.g. 'github'"}, "secret_value": {**S, "description": "the credential itself"},
       "replace": {"type": "boolean", "description": "true to throw away the stored value of an existing name; only with the user present"}},
      ["name", "secret_value"], category="plugins", risk="write")
def secret_store(ctx, name: str, secret_value: str, replace: bool = False):
    if name.lower() in (n.lower() for n in crypto.secret_names()):
        if replace is not True:
            raise ToolError(f"A secret named '{name}' already exists. Replacing it throws the old value away for good, "
                            "so ask the user first, then call again with replace=true.")
        if ctx.approve is None or ctx.data.get("automated"):
            raise ToolError("A stored credential can only be replaced while the user is present, not in a background job.")
    try:
        crypto.secret_set(name, secret_value)
    except crypto.CryptoError as e:
        raise ToolError(str(e))
    return f"Stored secret '{name}' (encrypted for this Windows user)."


@tool("secret_names", "Names of the stored secrets. Values are never returned.", category="plugins")
def secret_names(ctx):
    return crypto.secret_names() or "No secrets stored."
