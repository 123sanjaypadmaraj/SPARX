"""Shared fixtures. Every test gets an empty Sparx home in a temp folder and never touches the network."""
import json

import pytest

from browserpilot.llm import Call, Reply
from sparx import config, db, llm, tools


def pytest_configure(config):
    """Per-test watchdog (tests/watchdog.py, loaded by path: a pip package named `watchdog` would shadow it)."""
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("sparx_test_watchdog", pathlib.Path(__file__).with_name("watchdog.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    config.pluginmanager.register(mod, "sparx_test_watchdog")


def _guarded(real):
    """socket.connect / connect_ex that only reaches this machine: AF_UNIX, loopback addresses and `localhost`."""
    import ipaddress
    import socket

    def check(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
            host = str(address[0])
            try:
                local = host == "localhost" or host == "" or ipaddress.ip_address(host.split("%")[0]).is_loopback
            except ValueError:
                local = False
            if not local:
                raise OSError(f"tests must not use the network: tried {host}:{address[1]}")
        return address

    def connect(self, address):
        check(self, address)
        return real(self, address)
    return connect


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Refuse non-loopback connections, so an unmocked call fails at once with a clear message instead of waiting for
    a timeout or reaching a real service. Tests marked `network` are exempt."""
    import socket
    if request.node.get_closest_marker("network"):
        return
    monkeypatch.setattr(socket.socket, "connect", _guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded(socket.socket.connect_ex))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "sparx-home")
    db.reset()
    yield config.HOME
    db.reset()


@pytest.fixture
def ctx():
    """A Context whose approver says yes to everything. ctx.asked lists what was asked."""
    asked = []

    async def approve(tool, args):
        asked.append(tool.name)
        return True
    c = tools.Context(session="test", approve=approve)
    c.asked = asked
    return c


class FakeLLM:
    """Scripted model. Each item is a string (final text), or (tool_name, args) / a list of those (tool calls)."""
    spec = "fake/model"

    def __init__(self, script=()):
        self.script = list(script)
        self.seen = []          # every messages list passed to chat()
        self.prompts = []       # every (system, user) passed to text()

    async def chat(self, messages, tool_schemas):
        self.seen.append(list(messages))
        item = self.script.pop(0) if self.script else "done"
        if isinstance(item, str):
            return Reply(item, [], {"role": "assistant", "content": item})
        calls = [item] if isinstance(item, tuple) else item
        calls = [Call(f"c{len(self.seen)}_{i}", name, json.dumps(args)) for i, (name, args) in enumerate(calls)]
        return Reply("", calls, {"role": "assistant", "content": ""})

    def result_message(self, call, text):
        return {"role": "tool", "tool_call_id": call.id, "content": text}

    async def text(self, system, user):
        self.prompts.append((system, user))
        item = self.script.pop(0) if self.script else "{}"
        return item if isinstance(item, str) else json.dumps(item)


@pytest.fixture
def fake_llm(monkeypatch):
    """Replaces every model role with one FakeLLM. Set fake_llm.script = [...] in the test."""
    fake = FakeLLM()
    monkeypatch.setattr(llm, "get", lambda role="default": fake)
    return fake


async def run_tool(name, ctx, /, **args):
    """Call a registered tool through the full registry path (permissions, audit, wrapping)."""
    tools.load_all()
    return await tools.call(name, args, ctx)


def pytest_collection_finish(session):
    """Test modules register throwaway tools in groups of their own at import time. Give those groups a short
    description, so the tool-group catalogue test sees the same 'every group is described' rule as in production."""
    from sparx import tools
    tools.load_all()
    for group in tools.by_category():
        tools.CATEGORIES.setdefault(group, "test")
