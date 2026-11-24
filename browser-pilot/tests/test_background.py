import asyncio
import socket
import subprocess
import sys
import time

import httpx
import pytest

from sparx import config, db, security
from sparx.tools import ToolError, background

PY = f'"{sys.executable}"'


async def run_tool(name, ctx, /, **args):
    """Calls the tool function directly (the registry would flatten results to text)."""
    return await asyncio.to_thread(getattr(background, name), ctx, **args)


@pytest.fixture
def homedir():
    import pathlib
    import shutil
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp(prefix="sparx-bg-test-", dir=pathlib.Path.home()))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    background.stop_all()
    background._LIVE.clear()


async def wait_for(ctx, bid, text, secs=15):
    end = time.time() + secs
    out = ""
    while time.time() < end:
        r = await run_tool("bg_read", ctx, id=bid, tail_lines=50)
        out = r["output"]
        if text in out:
            return r
        await asyncio.sleep(0.2)
    raise AssertionError(f"{text!r} not in output: {out!r}")


async def test_start_read_incremental_stop(ctx):
    cmd = f"{PY} -c \"import time\nfor i in range(1000):\n print('tick',i,flush=True); time.sleep(0.1)\""
    cmd = f"{PY} -c \"import time;[print('tick',i,flush=True) or time.sleep(0.1) for i in range(1000)]\""
    r = await run_tool("bg_start", ctx, command=cmd, name="ticker")
    bid = r["id"]
    assert r["status"] == "running"
    await wait_for(ctx, bid, "tick 1")
    first = await run_tool("bg_read", ctx, id=bid)
    assert "tick" in first["output"]
    await asyncio.sleep(0.5)
    second = await run_tool("bg_read", ctx, id=bid)
    assert second["output"] and not second["output"].startswith(first["output"][:20]) or second["output"] != first["output"]
    assert any(x["id"] == bid for x in await run_tool("bg_list", ctx))
    s = await run_tool("bg_stop", ctx, id=bid)
    assert s["status"] == "stopped"
    assert not background._alive(s["pid"])


async def test_exit_code_reported(ctx):
    r = await run_tool("bg_start", ctx, command=f"{PY} -c \"print('bye'); raise SystemExit(3)\"")
    end = time.time() + 15
    while time.time() < end:
        v = await run_tool("bg_read", ctx, id=r["id"])
        if v["status"] != "running":
            break
        await asyncio.sleep(0.2)
    assert v["status"] == "exited" and v["exit_code"] == 3
    assert "bye" in v["output"]


async def test_tree_kill(ctx, homedir):
    marker = homedir / "child.pid"
    child = ["import os,time,pathlib", f"pathlib.Path(r'{marker}').write_text(str(os.getpid()))", "time.sleep(300)"]
    parent = ["import subprocess,sys,time", "subprocess.Popen([sys.executable,'child.py'])", "print('parent up',flush=True)", "time.sleep(300)"]
    (homedir / "child.py").write_text(chr(10).join(child) + chr(10))
    (homedir / "parent.py").write_text(chr(10).join(parent) + chr(10))
    r = await run_tool("bg_start", ctx, command=f"{PY} parent.py", cwd=str(homedir))
    await wait_for(ctx, r["id"], "parent up")
    end = time.time() + 10
    while not marker.exists() and time.time() < end:
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)
    child_pid = int(marker.read_text())
    assert background._alive(child_pid)
    await run_tool("bg_stop", ctx, id=r["id"])
    await asyncio.sleep(0.5)
    assert not background._alive(child_pid)


async def test_process_cap(ctx, monkeypatch):
    s = config.settings()
    monkeypatch.setattr(config, "settings", lambda: {**s, "background": {"max_processes": 2}})
    loop = f"{PY} -c \"import time;time.sleep(300)\""
    await run_tool("bg_start", ctx, command=loop)
    await run_tool("bg_start", ctx, command=loop)
    with pytest.raises(ToolError, match="limit 2"):
        background.bg_start(ctx, loop)


async def test_output_cap_stops_process(ctx, monkeypatch, homedir):
    s = config.settings()
    monkeypatch.setattr(config, "settings", lambda: {**s, "background": {"max_output_kb": 20}})
    spam = ["import sys,time", "while 1:", "    sys.stdout.write('x' * 1000 + chr(10)); sys.stdout.flush(); time.sleep(0.005)"]
    (homedir / "spam.py").write_text(chr(10).join(spam) + chr(10))
    r = await run_tool("bg_start", ctx, command=f"{PY} spam.py", cwd=str(homedir))
    end = time.time() + 15
    while time.time() < end:
        v = await run_tool("bg_read", ctx, id=r["id"], tail_lines=1)
        if v["status"] != "running":
            break
        await asyncio.sleep(0.3)
    assert "output cap" in v["status"]


async def test_secrets_scrubbed(ctx, monkeypatch):
    monkeypatch.setenv("SPARX_TEST_API_KEY", "supersecretvalue123")
    monkeypatch.setenv("HARMLESS_VAR", "visible")
    code = "import os;print('K=',os.environ.get('SPARX_TEST_API_KEY'),'H=',os.environ.get('HARMLESS_VAR'),flush=True)"
    r = await run_tool("bg_start", ctx, command=f"{PY} -c \"{code}\"")
    v = await wait_for(ctx, r["id"], "H=")
    assert "K= None" in v["output"] and "H= visible" in v["output"]
    assert "supersecretvalue123" not in v["output"]


async def test_dangerous_command_refused(ctx):
    with pytest.raises(ToolError, match="Refused"):
        background.bg_start(ctx, "diskpart")
    assert db.q("SELECT * FROM bg_procs") == []


async def test_persistence_after_restart(ctx):
    r = await run_tool("bg_start", ctx, command=f"{PY} -c \"import time;time.sleep(300)\"")
    background._LIVE.clear()                          # simulate a restarted Sparx: no Popen handle left
    listed = await run_tool("bg_list", ctx)
    assert listed[0]["id"] == r["id"] and listed[0]["status"] == "running"
    s = await run_tool("bg_stop", ctx, id=r["id"])
    assert s["status"] == "stopped" and not background._alive(r["pid"])


async def test_emergency_stop_kills(ctx):
    r = await run_tool("bg_start", ctx, command=f"{PY} -c \"import time;time.sleep(300)\"")
    security.request_stop("test")
    try:
        await asyncio.sleep(0.5)
        assert not background._alive(r["pid"])
        assert (await run_tool("bg_list", ctx))[0]["status"] == "stopped"
    finally:
        security.clear_stop()


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


async def test_http_server_smoke(ctx, homedir):
    (homedir / "hello.txt").write_text("hello sparx", encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = await run_tool("bg_start", ctx, command=f"{PY} -m http.server {port} --bind 127.0.0.1", cwd=str(homedir), name="http")
    end = time.time() + 15
    while not _port_open(port) and time.time() < end:
        await asyncio.sleep(0.2)
    assert _port_open(port)
    async with httpx.AsyncClient() as c:
        resp = await c.get(f"http://127.0.0.1:{port}/hello.txt")
    assert resp.text == "hello sparx"
    v = await wait_for(ctx, r["id"], "GET /hello.txt")
    assert v["status"] == "running"
    await run_tool("bg_stop", ctx, id=r["id"])
    end = time.time() + 10
    while _port_open(port) and time.time() < end:
        await asyncio.sleep(0.2)
    assert not _port_open(port)
