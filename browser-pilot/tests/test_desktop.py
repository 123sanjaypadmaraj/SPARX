"""desktop tools. Window handles, the Start menu, winget, the registry, the display and the recycle bin are all faked."""
import json

import pytest

from sparx import tools, undo
from sparx.tools import desktop

from conftest import run_tool

WINS = [{"hwnd": 1, "title": "Inbox - Google Chrome", "pid": 10}, {"hwnd": 2, "title": "notes.txt - Notepad", "pid": 20},
        {"hwnd": 3, "title": "Docs - Google Chrome", "pid": 10}, {"hwnd": 4, "title": "Program Manager", "pid": 1}]


class FakeDesk:
    def __init__(self):
        self.done, self.launched, self.winget, self.res, self.items = [], [], [], (1920, 1080), 4
        self.windows, self.opens, self.refuse, self.keep, self.bin_error = list(WINS), {}, set(), 0, None
        self.startup = [{"name": "Spotify", "command": "spotify.exe", "scope": "user", "enabled": True},
                        {"name": "Updater", "command": "u.exe", "scope": "all users", "enabled": True}]

    def proc(self, cmd, timeout, **env):
        self.winget.append(cmd[1:])
        if cmd[1] == "list":
            return 0, "Name   Id   Version\r\n---\r\n \\ \r\nVLC media player      VideoLAN.VLC   3.0\r\nGit   Git.Git   2.4\r\n", ""
        if cmd[1] == "install" and "Nope.Nope" in cmd:
            return 1, "", "No package found matching input criteria."
        return 0, f"Successfully {cmd[1]}ed\r\n", ""

    def write(self, name, enabled):
        next(r for r in self.startup if r["name"] == name)["enabled"] = enabled

    def resolution(self, new=None):
        self.res = new or self.res
        return self.res

    def bin(self, empty=False):
        got = {"items": self.items, "mb": 12.5}
        if empty and self.bin_error:
            raise tools.ToolError(self.bin_error)
        self.items = self.keep if empty else self.items
        return got


@pytest.fixture(autouse=True)
def desk(monkeypatch):
    d = FakeDesk()
    monkeypatch.setattr(desktop, "_visible_windows", lambda: list(d.windows))
    monkeypatch.setattr(desktop, "_processes", lambda: [{"name": "chrome.exe", "pid": 10}, {"name": "notepad.exe", "pid": 20}])
    monkeypatch.setattr(desktop, "_win_do", lambda hwnd, action: (d.done.append((hwnd, action)), hwnd not in d.refuse)[1])
    monkeypatch.setattr(desktop, "_LAUNCH_WAIT", 0)
    monkeypatch.setattr(desktop.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("real network")))
    monkeypatch.setattr(desktop, "_start_apps", lambda: [{"name": "WhatsApp", "id": "wa!App"}, {"name": "Spotify", "id": "sp!App"},
                                                         {"name": "Visual Studio Code", "id": "code"},
                                                         {"name": "Visual Studio Code - Insiders", "id": "code-i"}])
    monkeypatch.setattr(desktop, "_launch", lambda app_id: (d.launched.append(app_id), d.windows.extend(d.opens.get(app_id, []))))
    monkeypatch.setattr(desktop, "_proc", d.proc)
    monkeypatch.setattr(desktop, "_startup_read", lambda: [dict(r) for r in d.startup])
    monkeypatch.setattr(desktop, "_startup_write", d.write)
    monkeypatch.setattr(desktop, "_modes", lambda: [(1920, 1080), (1280, 720)])
    monkeypatch.setattr(desktop, "_resolution", d.resolution)
    monkeypatch.setattr(desktop, "_bin", d.bin)
    monkeypatch.setattr(desktop, "_windows_only", lambda: None)
    return d


def body(text):
    return text.split("\n", 1)[1].rsplit("\n</untrusted>", 1)[0]


async def test_window_arrange_by_title_and_by_process(ctx, desk):
    r = json.loads(await run_tool("window_arrange", ctx, window="notepad", action="left"))
    assert r == {"action": "left", "windows": ["notes.txt - Notepad"]} and desk.done == [(2, "left")]
    r = json.loads(await run_tool("window_arrange", ctx, window="chrome.exe", action="minimize"))
    assert r["windows"] == ["Inbox - Google Chrome"] and r["other_matches"] == ["Docs - Google Chrome"]
    await run_tool("window_arrange", ctx, window="chrome", action="close", all_matching=True)
    assert desk.done[-2:] == [(1, "close"), (3, "close")]
    assert "No open window matches" in await run_tool("window_arrange", ctx, window="excel", action="left")
    assert "action is one of" in await run_tool("window_arrange", ctx, window="chrome", action="explode")
    assert ctx.asked.count("window_arrange") == 5


async def test_layouts(ctx, desk):
    r = json.loads(await run_tool("windows_layout", ctx, layout="side_by_side", windows=["inbox", "notepad"]))
    assert r == {"Inbox - Google Chrome": "left", "notes.txt - Notepad": "right"}
    assert "Minimised 3 windows" in await run_tool("windows_layout", ctx, layout="show_desktop")     # not Program Manager
    assert "needs between 2 and 2" in await run_tool("windows_layout", ctx, layout="side_by_side", windows=["inbox"])
    assert "Nothing was moved yet" in await run_tool("windows_layout", ctx, layout="grid", windows=["excel", "inbox"])


async def test_app_launch_matches_and_asks_when_ambiguous(ctx, desk):
    assert "Asked Windows to start WhatsApp" in await run_tool("app_launch", ctx, name="whatsapp") and desk.launched == ["wa!App"]
    assert "Asked Windows to start Visual Studio Code." in await run_tool("app_launch", ctx, name="Visual Studio Code")
    assert "Ask the user which one" in await run_tool("app_launch", ctx, name="visual")
    assert "No installed app" in await run_tool("app_launch", ctx, name="photoshop")
    assert json.loads(body(await run_tool("apps_installed", ctx, filter="spot"))) == ["Spotify"]


async def test_software_list_install_uninstall(ctx, desk):
    out = body(await run_tool("software_list", ctx, filter="vlc"))
    assert out == "VLC media player   VideoLAN.VLC   3.0"
    assert "Successfully" in await run_tool("software_install", ctx, package="VideoLAN.VLC")
    assert desk.winget[-1][:4] == ["install", "--id", "VideoLAN.VLC", "-e"] and "--silent" in desk.winget[-1]
    assert "winget failed: No package found" in await run_tool("software_install", ctx, package="Nope.Nope")
    assert "package id" in await run_tool("software_install", ctx, package="vlc & calc")
    await run_tool("software_update", ctx, package="all")
    assert desk.winget[-1][:2] == ["upgrade", "--all"]
    await run_tool("software_uninstall", ctx, package="Git.Git")
    assert tools.REGISTRY["software_uninstall"].risk == "delete" and tools.REGISTRY["software_install"].risk == "exec"


async def test_software_not_installed_without_approval(desk):
    out = await run_tool("software_install", tools.Context(session="t"), package="VideoLAN.VLC")
    assert out.startswith("Not done") and desk.winget == []


async def test_startup_toggle_undo_and_admin_entries(ctx, desk):
    assert "no longer start" in await run_tool("startup_app_set", ctx, name="spotify", enabled=False)
    assert desk.startup[0]["enabled"] is False
    undo.run()
    assert desk.startup[0]["enabled"] is True
    assert "administrator rights" in await run_tool("startup_app_set", ctx, name="Updater", enabled=False)
    assert "matches 0 startup entries" in await run_tool("startup_app_set", ctx, name="zoom", enabled=False)


async def test_display_resolution_validates_and_undoes(ctx, desk):
    assert json.loads(await run_tool("display_info", ctx))["resolution"] == "1920x1080"
    assert "not a mode" in await run_tool("display_resolution", ctx, width=1234, height=567)
    r = json.loads(await run_tool("display_resolution", ctx, width=1280, height=720))
    assert r == {"resolution": "1280x720", "was": "1920x1080"}
    undo.run()
    assert desk.res == (1920, 1080)


async def test_recycle_bin(ctx, desk):
    assert json.loads(await run_tool("recycle_bin", ctx)) == {"items": 4, "mb": 12.5} and ctx.asked == []
    assert "4 items, 12.5 MB freed" in await run_tool("recycle_bin_empty", ctx) and ctx.asked == ["recycle_bin_empty"]
    assert "already empty" in await run_tool("recycle_bin_empty", ctx)
    assert tools.REGISTRY["recycle_bin_empty"].risk == "delete"


async def test_network_info(ctx, monkeypatch):
    monkeypatch.setattr(desktop, "_online", lambda: {"internet": True, "dns": False})
    r = json.loads(body(await run_tool("network_info", ctx)))
    assert r["internet"] is True and r["dns"] is False and isinstance(r["adapters"], list)


# ---------------------------------------------------------------- audit fixes

def _proc_returning(desk, code, out, err=""):
    desk.proc = lambda cmd, timeout, **env: (desk.winget.append(cmd[1:]), (code, out, err))[1]
    return desk.proc


@pytest.mark.parametrize("tool_name", ["software_install", "software_update", "software_uninstall"])
async def test_winget_failure_with_output_is_an_error(ctx, desk, monkeypatch, tool_name):
    monkeypatch.setattr(desktop, "_proc", _proc_returning(desk, 1603, "x" * 900 + "Installer failed with exit code 1603"))
    r = await run_tool(tool_name, ctx, package="Some.Package")
    assert r.startswith("Error: winget failed") and "Installer failed with exit code 1603" in r and r.count("x") <= 600


async def test_winget_no_update_available_is_a_plain_result(ctx, desk, monkeypatch):
    monkeypatch.setattr(desktop, "_proc", _proc_returning(desk, 0x8A15002B, "No applicable update found."))
    r = await run_tool("software_update", ctx, package="Some.Package")
    assert not r.startswith("Error") and "No applicable update" in r
    monkeypatch.setattr(desktop, "_proc", _proc_returning(desk, 0x8A15002B - 2**32, "No applicable update found."))
    assert not (await run_tool("software_update", ctx, package="all")).startswith("Error")


@pytest.mark.parametrize("package", ["--all", "-h", "-e", "--source", "-"])
async def test_package_id_cannot_start_with_a_dash(ctx, desk, package):
    for name in ("software_install", "software_update", "software_uninstall"):
        if package == "--all" and name == "software_update":
            continue
        assert "package id" in await run_tool(name, ctx, package=package)
    assert desk.winget == []


@pytest.mark.parametrize("blank", [" ", "   ", "	"])
async def test_blank_window_name_matches_nothing(ctx, desk, blank):
    assert "Give the window" in await run_tool("window_arrange", ctx, window=blank, action="close", all_matching=True)
    assert "Give the window" in await run_tool("windows_layout", ctx, layout="side_by_side", windows=[blank, "notepad"])
    assert desk.done == []


async def test_refused_window_is_not_reported_as_done(ctx, desk):
    desk.refuse = {2}
    r = await run_tool("window_arrange", ctx, window="notepad", action="left")
    assert r.startswith("Error") and "refused" in r and "administrator" in r
    r = json.loads(await run_tool("window_arrange", ctx, window="chrome", action="minimize", all_matching=True))
    assert r["windows"] == ["Inbox - Google Chrome", "Docs - Google Chrome"] and "failed" not in r
    desk.refuse = {3}
    r = json.loads(await run_tool("window_arrange", ctx, window="chrome", action="minimize", all_matching=True))
    assert r["windows"] == ["Inbox - Google Chrome"] and r["failed"] == ["Docs - Google Chrome"]


async def test_layouts_report_refused_windows(ctx, desk):
    desk.refuse = {2}
    r = await run_tool("windows_layout", ctx, layout="side_by_side", windows=["inbox", "notepad"])
    assert r.startswith("Error") and "notes.txt - Notepad" in r and "Inbox - Google Chrome" in r
    r = await run_tool("windows_layout", ctx, layout="show_desktop")
    assert "Minimised 2 windows" in r and "1 refused" in r


async def test_recycle_bin_empty_reports_what_is_left(ctx, desk):
    desk.keep = 3
    r = await run_tool("recycle_bin_empty", ctx)
    assert "3 items" in r and "remain" in r and "Emptied the Recycle Bin" not in r
    desk.items, desk.keep, desk.bin_error = 4, 0, "Windows refused to empty the Recycle Bin (code 0x80004005)."
    r = await run_tool("recycle_bin_empty", ctx)
    assert r.startswith("Error") and "refused" in r


async def test_app_launch_says_started_only_when_a_window_appears(ctx, desk):
    desk.opens["wa!App"] = [{"hwnd": 9, "title": "WhatsApp", "pid": 99}]
    assert "Started WhatsApp" in await run_tool("app_launch", ctx, name="whatsapp")
    r = await run_tool("app_launch", ctx, name="spotify")
    assert "Asked Windows to start Spotify" in r and "No new window" in r


async def test_startup_apps_commands_are_redacted(ctx, desk):
    desk.startup[0]["command"] = "sync.exe --api_key=sk-abcdefghijklmnopqrstuvwxyz123456"
    out = body(await run_tool("startup_apps", ctx))
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out and "sync.exe" in out


async def test_software_update_timeout_text_and_description(ctx, desk, monkeypatch):
    import time
    assert "keeps running" in tools.REGISTRY["software_update"].description
    monkeypatch.setattr(tools.REGISTRY["software_update"], "timeout", 0.05)
    monkeypatch.setattr(desktop, "_proc", lambda cmd, timeout, **env: (time.sleep(0.4), (0, "done", ""))[1])
    r = await run_tool("software_update", ctx, package="all")
    assert "timed out" in r and "could not be stopped" in r


async def test_no_test_reaches_the_network(ctx, desk):
    with pytest.raises(AssertionError):
        desktop.socket.create_connection(("1.1.1.1", 443), 3)
