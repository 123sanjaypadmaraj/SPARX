"""pc tools. PowerShell, netsh, powercfg, the registry and the wallpaper call are all faked: nothing touches this PC."""
import json
from datetime import datetime

import pytest

from sparx import db, jobs, tools, undo
from sparx.tools import files, pc, system

from conftest import run_tool


class FakePC:
    """Stands in for Windows: remembers volume, brightness, radios, theme and wallpaper, and records commands."""

    def __init__(self):
        self.audio = {0: [40, 0], 1: [90, 0]}
        self.brightness, self.radios = 80, {"WiFi": "On", "Bluetooth": "On"}
        self.theme, self.wallpaper, self.cmds = "light", "C:\\old.jpg", []
        self.devices = [{"name": "Speakers (Realtek)", "id": "{0.0.0.00000000}.{A}"}, {"name": "Headphones (Buds)", "id": "{0.0.0.00000000}.{B}"},
                        {"name": "Headset (Buds)", "id": "{0.0.0.00000000}.{C}"}, {"name": "Microphone Array", "id": "{0.0.1.00000000}.{M}"}]
        self.default = {"speaker": "{0.0.0.00000000}.{A}", "microphone": "{0.0.1.00000000}.{M}"}

    def ps(self, script, timeout=40, **env):
        if "DefaultId" in script:
            return json.dumps({"devices": self.devices, **self.default})
        if "SetDefault" in script:
            if "{C}" not in env["id"]:                      # the fake refuses the headset, like a busy device would
                self.default["microphone" if ".1." in env["id"] else "speaker"] = env["id"]
            return ""
        if "ConvertTo-Json" in script:
            return json.dumps({"speaker": "{}|{}".format(*self.audio[0]), "mic": "{}|{}".format(*self.audio[1]),
                               "brightness": self.brightness, "radios": self.radios})
        if "SparxAudio" in script:
            a = self.audio[int(env["flow"])]
            if "::Set" in script:
                a[0] = int(env["level"]) if int(env["level"]) >= 0 else a[0]
                a[1] = int(env["mute"]) if int(env["mute"]) >= 0 else a[1]
            return "{}|{}".format(*a)
        if "WmiMonitorBrightness" in script:
            if self.brightness is None:
                raise tools.ToolError("Windows refused: no instances")
            if "WmiSetBrightness" in script:
                self.brightness = int(env["level"])
            return str(self.brightness)
        if "SetStateAsync" in script:
            if env["kind"] not in self.radios:
                return "missing"
            self.radios[env["kind"]] = env["state"]
            return env["state"]
        raise AssertionError(script)

    def cmd(self, *args, timeout=20):
        self.cmds.append(list(args))
        if args[:2] == ("powercfg", "/list"):
            return ("Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced) *\n"
                    "Power Scheme GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c  (High performance)\n")
        if args[:3] == ("netsh", "wlan", "show"):
            return ("    State : connected\n    SSID : Home\n    Signal : 80%\n" if args[3] == "interfaces"
                    else "SSID 1 : Home\nSSID 2 : Cafe\n")
        if args[:3] == ("netsh", "wlan", "connect") and "Cafe" in args[3]:
            raise tools.ToolError("netsh failed: There is no profile")
        return ""


@pytest.fixture(autouse=True)
def win(monkeypatch, tmp_path):
    w = FakePC()
    monkeypatch.setattr(pc, "_ps", w.ps)
    monkeypatch.setattr(pc, "_cmd", w.cmd)
    monkeypatch.setattr(pc, "_theme_get", lambda: w.theme)
    monkeypatch.setattr(pc, "_theme_set", lambda m: setattr(w, "theme", m))
    monkeypatch.setattr(pc, "_wallpaper_get", lambda: w.wallpaper)
    monkeypatch.setattr(pc, "_wallpaper_set", lambda p: setattr(w, "wallpaper", p))
    monkeypatch.setattr(pc, "_windows_only", lambda: None)
    monkeypatch.setattr(pc, "WINDOWS", True)
    monkeypatch.setattr(files, "_home", lambda: tmp_path)
    return w


def body(text):
    return json.loads(text.split("\n", 1)[1].rsplit("\n</untrusted>", 1)[0])


async def test_status_reads_everything(ctx, win):
    s = body(await run_tool("pc_status", ctx))
    assert s["volume"] == {"level": 40, "muted": False} and s["microphone"]["level"] == 90 and s["brightness"] == 80
    assert s["wifi_radio"] is True and s["bluetooth"] is True and s["wifi"]["network"] == "Home"
    assert s["theme"] == "light" and s["power_plan"] == "Balanced" and ctx.asked == []


async def test_volume_sets_asks_and_undoes(ctx, win):
    r = json.loads(await run_tool("pc_volume", ctx, level=15))
    assert r["level"] == 15 and r["was"]["level"] == 40 and win.audio[0] == [15, 0] and ctx.asked == ["pc_volume"]
    assert "Undone" in undo.run() and win.audio[0] == [40, 0]


async def test_volume_mute_microphone_and_validation(ctx, win):
    r = json.loads(await run_tool("pc_volume", ctx, mute="on", device="microphone"))
    assert r["muted"] is True and r["level"] == 90 and win.audio[0] == [40, 0]
    assert "0 to 100" in await run_tool("pc_volume", ctx, level=140)
    assert "Give a level" in await run_tool("pc_volume", ctx)


async def test_volume_not_changed_without_approval(win):
    out = await run_tool("pc_volume", tools.Context(session="t"), level=5)
    assert out.startswith("Not done") and win.audio[0] == [40, 0]


async def test_audio_devices_switch_and_undo(ctx, win):
    listed = body(await run_tool("pc_audio_devices", ctx))
    assert {"name": "Speakers (Realtek)", "kind": "speaker", "default": True} in listed and len(listed) == 4
    r = json.loads(await run_tool("pc_audio_switch", ctx, device="headphones"))
    assert r == {"default": "Headphones (Buds)", "kind": "speaker", "was": "Speakers (Realtek)"}
    assert "matches 2 audio devices" in await run_tool("pc_audio_switch", ctx, device="buds")
    assert "matches 0 audio devices" in await run_tool("pc_audio_switch", ctx, device="soundbar")
    assert "Windows kept Headphones (Buds)" in await run_tool("pc_audio_switch", ctx, device="headset")
    undo.run()
    assert win.default["speaker"].endswith("{A}")


async def test_brightness_sets_and_undoes(ctx, win):
    assert json.loads(await run_tool("pc_brightness", ctx, level=35)) == {"brightness": 35, "was": 80}
    undo.run()
    assert win.brightness == 80


async def test_brightness_on_external_monitor_says_why(ctx, win):
    win.brightness = None
    assert "built-in laptop screen" in await run_tool("pc_brightness", ctx, level=35)


async def test_radio_toggles_and_reports_missing(ctx, win):
    assert "bluetooth is now off" in await run_tool("pc_radio", ctx, radio="bluetooth", on=False)
    assert win.radios["Bluetooth"] == "Off"
    del win.radios["WiFi"]
    assert "no wifi radio" in await run_tool("pc_radio", ctx, radio="wifi", on=True)


async def test_wifi_list_and_connect(ctx, win):
    assert body(await run_tool("pc_wifi_list", ctx))["in_range"] == ["Cafe", "Home"]
    assert "Asked Windows to join 'Home'" in await run_tool("pc_wifi_connect", ctx, network="Home")
    assert ["netsh", "wlan", "connect", "name=Home"] in win.cmds
    assert "join 'Cafe' once by hand" in await run_tool("pc_wifi_connect", ctx, network="Cafe")
    assert "Disconnected" in await run_tool("pc_wifi_connect", ctx)
    assert "not a valid network name" in await run_tool("pc_wifi_connect", ctx, network='a" & calc')


async def test_theme_and_undo(ctx, win):
    assert json.loads(await run_tool("pc_theme", ctx, mode="dark")) == {"theme": "dark", "was": "light"}
    undo.run()
    assert win.theme == "light"


async def test_wallpaper_needs_an_image_inside_home(ctx, win, tmp_path):
    img, old = tmp_path / "w.png", tmp_path / "old.jpg"
    img.write_bytes(b"x")
    old.write_bytes(b"x")                       # the undo now refuses a previous wallpaper that is gone
    win.wallpaper = str(old)
    (tmp_path / "n.txt").write_text("x")
    assert json.loads(await run_tool("pc_wallpaper", ctx, path=str(img)))["was"] == str(old)
    assert win.wallpaper == str(img)
    assert "must be a jpg" in await run_tool("pc_wallpaper", ctx, path=str(tmp_path / "n.txt"))
    undo.run()
    assert win.wallpaper == str(old)


async def test_power_plan_switch_and_timeouts(ctx, win):
    r = json.loads(await run_tool("pc_power_plan", ctx, plan="high", screen_off_minutes=5, sleep_minutes=0))
    assert r["changed"] == ["plan High performance", "screen off after 5 min", "sleep never"]
    assert ["powercfg", "/setactive", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"] in win.cmds
    assert ["powercfg", "/change", "monitor-timeout-dc", "5"] in win.cmds
    assert "matches 0 plans" in await run_tool("pc_power_plan", ctx, plan="turbo")


async def test_power_shutdown_waits_and_can_be_cancelled(ctx, win):
    out = await run_tool("pc_power", ctx, action="shutdown", delay_seconds=1)
    assert "Shutdown in 30 seconds" in out and win.cmds[-1] == ["shutdown", "/s", "/t", "30"]
    assert "cancelled" in await run_tool("pc_power", ctx, action="cancel") and win.cmds[-1] == ["shutdown", "/a"]
    assert "one of" in await run_tool("pc_power", ctx, action="explode")
    assert tools.REGISTRY["pc_power"].risk == "delete"


async def test_media_key(ctx, monkeypatch):
    pressed = []

    class G:
        class FailSafeException(Exception):
            pass
        press = staticmethod(pressed.append)
    monkeypatch.setattr(system, "_gui", lambda: G)
    assert "pc_media" in await run_tool("pc_media", ctx, action="next") and pressed == ["nexttrack"]


async def test_settings_open(ctx, monkeypatch):
    opened = []
    monkeypatch.setattr(pc.os, "startfile", opened.append, raising=False)
    assert "night light" in await run_tool("pc_settings_open", ctx, page="Night Light") and opened == ["ms-settings:nightlight"]
    assert "Closest: default apps" in await run_tool("pc_settings_open", ctx, page="default")
    assert "night light" in json.loads(await run_tool("pc_settings_open", ctx))["pages"]


def test_hints_include_the_phone_only_once_one_is_known(win, monkeypatch):
    from sparx import config
    from sparx.tools import phone
    low = {"battery": {"percent": 10, "plugged": False}, "settings": {"battery_saver": False}}
    monkeypatch.setattr(phone, "state", lambda: low)
    assert [h for h in pc.hints(datetime(2026, 1, 1, 11)) if h["device"] == "phone"] == []
    config.set_key("integrations.phone.serial", "R58M")
    assert [h["key"] for h in pc.hints(datetime(2026, 1, 1, 11)) if h["device"] == "phone"] == ["phone:saver"]


def test_pc_hints():
    night, day = datetime(2026, 1, 1, 23, 30), datetime(2026, 1, 1, 11, 0)
    s = {"battery": {"percent": 20, "plugged": False}, "brightness": 90, "bluetooth": True, "theme": "light",
         "volume": {"level": 85, "muted": False}}
    assert [h["tool"] for h in pc.pc_hints(s, night)] == ["pc_brightness", "pc_radio", "pc_theme", "pc_volume"]
    assert [h["tool"] for h in pc.pc_hints(s, day)] == ["pc_brightness", "pc_radio"]
    assert pc.pc_hints({**s, "battery": {"percent": 20, "plugged": True}, "theme": "dark"}, day) == []
    assert pc.pc_hints({}, night) == []


async def test_suggest_changes_nothing_and_watch_notifies_once(ctx, win, monkeypatch):
    monkeypatch.setattr(pc, "hints", lambda now=None: [{"device": "pc", "key": "pc:theme", "suggestion": "Switch Windows to dark mode",
                                                        "reason": "late", "tool": "pc_theme", "args": {"mode": "dark"}}])
    r = json.loads(await run_tool("settings_suggest", ctx))
    assert r["suggestions"][0]["tool"] == "pc_theme" and "key" not in r["suggestions"][0]
    assert win.theme == "light" and ctx.asked == []
    assert pc.device_watch() == 1 and pc.device_watch() == 0
    assert [n["kind"] for n in jobs.unread()] == ["suggestion"] and "device_watch" in jobs.JOBS
    assert db.one("SELECT count(*) c FROM device_hints")["c"] == 1


# ---------------------------------------------------------------- audit

_REAL_CMD = pc._cmd          # captured at import, before the autouse fixture replaces it


async def test_power_plan_reports_the_value_it_set_and_undoes_a_plan_switch(ctx, win):
    r = json.loads(await run_tool("pc_power_plan", ctx, plan="high", sleep_minutes=5000))
    assert "sleep after 600 min (5000 was too long)" in r["changed"][1]
    assert ["powercfg", "/change", "standby-timeout-ac", "600"] in win.cmds
    win.cmds.clear()
    assert "Undone" in undo.run()
    assert ["powercfg", "/setactive", "381b4222-f694-41f0-9685-ff5bb260df2e"] in win.cmds


async def test_sleep_and_hibernate_are_started_without_waiting(ctx, win, monkeypatch):
    started = []
    monkeypatch.setattr(pc, "_spawn", lambda *a: started.append(list(a)))
    assert "Asked Windows to sleep" in await run_tool("pc_power", ctx, action="sleep")
    assert "Asked Windows to hibernate" in await run_tool("pc_power", ctx, action="hibernate")
    assert started == [["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], ["shutdown", "/h"]]
    assert win.cmds == [] and "hibernates instead" in tools.REGISTRY["pc_power"].description


def test_shutdown_is_not_refused_by_the_shell_denylist(monkeypatch):
    ran = []
    monkeypatch.setattr(pc, "_windows_only", lambda: None)
    monkeypatch.setattr(pc, "_run", lambda cmd, cwd, timeout: ran.append(cmd) or
                        {"timed_out": False, "exit_code": 0, "stdout": "", "stderr": ""})
    _REAL_CMD("shutdown", "/s", "/t", "30")
    assert ran == [["shutdown", "/s", "/t", "30"]]


async def test_wifi_off_and_disconnect_can_be_undone(ctx, win):
    assert "wifi is now off" in await run_tool("pc_radio", ctx, radio="wifi", on=False)
    assert win.radios["WiFi"] == "Off"
    undo.run()
    assert win.radios["WiFi"] == "On"
    assert "Disconnected" in await run_tool("pc_wifi_connect", ctx)
    win.cmds.clear()
    undo.run()
    assert ["netsh", "wlan", "connect", "name=Home"] in win.cmds
    assert "take the PC offline" in tools.REGISTRY["pc_wifi_connect"].description


async def test_going_offline_is_refused_for_a_background_job(ctx, win):
    ctx.data["automated"] = True
    assert "may not take the PC offline" in await run_tool("pc_radio", ctx, radio="wifi", on=False)
    assert "may not take the PC offline" in await run_tool("pc_wifi_connect", ctx)
    assert win.radios["WiFi"] == "On" and ["netsh", "wlan", "disconnect"] not in win.cmds
    assert "bluetooth is now off" in await run_tool("pc_radio", ctx, radio="bluetooth", on=False)


def test_device_watch_puts_the_exact_call_in_why(win, monkeypatch):
    monkeypatch.setattr(pc, "hints", lambda now=None: [{"device": "pc", "key": "pc:theme", "suggestion": "Switch Windows to dark mode",
                                                        "reason": "late", "tool": "pc_theme", "args": {"mode": "dark"}}])
    pc.device_watch()
    why = jobs.unread()[0]["why"]
    assert 'pc_theme' in why and '"mode": "dark"' in why and "Say yes and Sparx will do it" not in why


async def test_wallpaper_undo_says_when_the_old_image_is_gone(ctx, win, tmp_path):
    old = tmp_path / "old.png"
    old.write_bytes(b"x")
    new = tmp_path / "new.png"
    new.write_bytes(b"x")
    win.wallpaper = str(old)
    await run_tool("pc_wallpaper", ctx, path=str(new))
    old.unlink()
    with pytest.raises(tools.ToolError, match="no longer exists"):
        undo.run()
    assert win.wallpaper == str(new)


async def test_theme_reports_a_change_that_did_not_happen(ctx, win, monkeypatch):
    monkeypatch.setattr(pc, "_theme_set", lambda m: None)
    out = await run_tool("pc_theme", ctx, mode="dark")
    assert "still light" in out and win.theme == "light"
    def boom(m):
        raise PermissionError("denied")
    monkeypatch.setattr(pc, "_theme_set", boom)
    assert "could not write the theme" in await run_tool("pc_theme", ctx, mode="dark")
