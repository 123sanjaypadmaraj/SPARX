"""This PC's settings: volume, brightness, Wi-Fi and Bluetooth, theme, wallpaper, power, media keys, Settings pages."""
import ctypes
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .. import db, jobs, undo
from . import B, I, S, ToolError, category, tool
from .files import safe_path
from .system import WINDOWS, _act, _log, _run, _windows_only, clean_env

category("pc", "This PC's settings: volume, brightness, Wi-Fi, Bluetooth, dark mode, wallpaper, power plan, sleep/shutdown")

db.schema("""
CREATE TABLE IF NOT EXISTS device_hints (key TEXT PRIMARY KEY, ts REAL NOT NULL);
""")

# Core Audio through a small C# shim: exact levels without pycaw. flow 0 = speakers, 1 = microphone.
_AUDIO_CS = """
using System; using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int f(); int g(); int h(); int i();
  int SetMasterVolumeLevelScalar(float level, Guid ctx);
  int j();
  int GetMasterVolumeLevelScalar(out float level);
  int k(); int l(); int m(); int n();
  int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, Guid ctx);
  int GetMute(out bool mute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
  int Activate(ref Guid id, int clsCtx, IntPtr p, out IAudioEndpointVolume aev);
  int OpenPropertyStore(); int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
}
[Guid("F8679F50-850A-41CF-9C72-430F290290C8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPolicyConfig {
  int a(); int b(); int c(); int d(); int e(); int f(); int g(); int h(); int i(); int j();
  int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string id, int role);
}
[ComImport, Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")] class PolicyConfigClient { }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int f(); int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice dev); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public class SparxAudio {
  static IAudioEndpointVolume Vol(int flow) {
    var en = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
    IMMDevice dev; Marshal.ThrowExceptionForHR(en.GetDefaultAudioEndpoint(flow, 1, out dev));
    IAudioEndpointVolume v; var id = typeof(IAudioEndpointVolume).GUID;
    Marshal.ThrowExceptionForHR(dev.Activate(ref id, 23, IntPtr.Zero, out v));
    return v;
  }
  public static string DefaultId(int flow) {
    var en = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
    IMMDevice dev; Marshal.ThrowExceptionForHR(en.GetDefaultAudioEndpoint(flow, 1, out dev));
    string id; Marshal.ThrowExceptionForHR(dev.GetId(out id)); return id;
  }
  public static void SetDefault(string id) {
    var pc = new PolicyConfigClient() as IPolicyConfig;
    for (int role = 0; role < 3; role++) Marshal.ThrowExceptionForHR(pc.SetDefaultEndpoint(id, role));
  }
  public static string Get(int flow) {
    var v = Vol(flow); float l; bool m;
    Marshal.ThrowExceptionForHR(v.GetMasterVolumeLevelScalar(out l)); Marshal.ThrowExceptionForHR(v.GetMute(out m));
    return Math.Round(l * 100) + "|" + (m ? 1 : 0);
  }
  public static void Set(int flow, int level, int mute) {
    var v = Vol(flow);
    if (level >= 0) Marshal.ThrowExceptionForHR(v.SetMasterVolumeLevelScalar(level / 100f, Guid.Empty));
    if (mute >= 0) Marshal.ThrowExceptionForHR(v.SetMute(mute == 1, Guid.Empty));
  }
}
"""

# WinRT radios (Wi-Fi, Bluetooth) need Windows PowerShell 5.1 and no admin rights.
_RADIOS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioState,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
"""

_STATE = r"""
$o = @{}
try { Add-Type -TypeDefinition $env:SPARX_CS; $o.speaker = [SparxAudio]::Get(0) } catch { $o.speaker = $null }
try { $o.mic = [SparxAudio]::Get(1) } catch { $o.mic = $null }
try { $o.brightness = [int](Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness)[0].CurrentBrightness } catch { $o.brightness = $null }
try { """ + _RADIOS + r"""; $o.radios = @{}; foreach ($r in $radios) { $o.radios["$($r.Kind)"] = "$($r.State)" } } catch { $o.radios = $null }
$o | ConvertTo-Json -Compress
"""

_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_PAGES = {
    "home": "", "display": "display", "night light": "nightlight", "sound": "sound", "notifications": "notifications",
    "focus": "quiethours", "do not disturb": "quiethours", "power": "powersleep", "battery": "batterysaver",
    "storage": "storagesense", "bluetooth": "bluetooth", "wifi": "network-wifi", "network": "network-status",
    "vpn": "network-vpn", "hotspot": "network-mobilehotspot", "airplane mode": "network-airplanemode",
    "personalization": "personalization", "background": "personalization-background", "colors": "personalization-colors",
    "lock screen": "lockscreen", "taskbar": "taskbar", "apps": "appsfeatures", "default apps": "defaultapps",
    "startup apps": "startupapps", "accounts": "yourinfo", "sign-in": "signinoptions", "date and time": "dateandtime",
    "language": "regionlanguage", "keyboard": "keyboard", "mouse": "mousetouchpad", "touchpad": "devices-touchpad",
    "printers": "printers", "privacy": "privacy", "microphone privacy": "privacy-microphone", "camera privacy": "privacy-webcam",
    "location": "privacy-location", "windows update": "windowsupdate", "about": "about", "accessibility": "easeofaccess",
}


def _proc(cmd: list, timeout: float, **env) -> tuple[int, str, str]:
    """Run a program and return (exit code, stdout, stderr) whole. system._run shortens long output, which breaks JSON."""
    _windows_only()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, cwd=str(Path.home()), env=clean_env(env),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ToolError(f"{Path(cmd[0]).stem} did not finish within {timeout:.0f} seconds.")
    except OSError as e:
        raise ToolError(f"Could not start {cmd[0]}: {e}")
    return r.returncode, r.stdout or "", r.stderr or ""


def _ps(script: str, timeout: float = 40, **env) -> str:
    """Run a Windows PowerShell script and return what it prints. Values travel in SPARX_* variables, never in the text."""
    code, out, err = _proc([shutil.which("powershell") or "powershell.exe", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-Command",
                            "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8\n" + script],
                           timeout, **{f"SPARX_{k.upper()}": str(v) for k, v in env.items()})
    if code:
        raise ToolError(f"Windows refused: {(err or out).strip()[:300] or 'unknown error'}")
    return out.strip()


def _cmd(*args, timeout: float = 20) -> str:
    """Run a Windows command-line tool (netsh, powercfg, shutdown) from an argument list."""
    _windows_only()
    r = _run(list(args), Path.home(), timeout)
    if r["timed_out"] or r["exit_code"]:
        raise ToolError(f"{args[0]} failed: {(r['stderr'] or r['stdout']).strip()[:300] or 'no output'}")
    return r["stdout"]


def _level(value, what: str) -> int:
    if not 0 <= int(value) <= 100:
        raise ToolError(f"{what} is a percentage from 0 to 100.")
    return int(value)


def _pair(text) -> dict | None:
    """'37|0' from the audio shim -> {'level': 37, 'muted': False}."""
    m = re.fullmatch(r"(\d+)\|([01])", str(text or ""))
    return {"level": int(m[1]), "muted": m[2] == "1"} if m else None


def _theme_get() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE) as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return None


def _theme_set(mode: str):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE) as k:
        for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(mode == "light"))
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "ImmersiveColorSet", 2, 2000, None)   # apply live


def _wallpaper_get() -> str:
    buf = ctypes.create_unicode_buffer(520)
    ctypes.windll.user32.SystemParametersInfoW(0x0073, len(buf), buf, 0)
    return buf.value


def _wallpaper_set(path: str):
    if not ctypes.windll.user32.SystemParametersInfoW(0x0014, 0, path, 3):
        raise ToolError("Windows did not accept that image as the wallpaper.")


def _wifi() -> dict | None:
    try:
        text = _cmd("netsh", "wlan", "show", "interfaces")
    except ToolError:
        return None
    got = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in text.splitlines()) if v.strip()}
    return {"connected": got.get("state") == "connected", "network": got.get("ssid", ""), "signal": got.get("signal", "")}


def _plans() -> list[dict]:
    return [{"guid": m[1], "name": m[2], "active": bool(m[3])}
            for m in re.finditer(r"([0-9a-f-]{36})\s+\((.+?)\)\s*(\*)?", _cmd("powercfg", "/list"), re.I)]


def state() -> dict:
    """Current settings of this PC. A value that could not be read is None."""
    try:
        raw = json.loads(_ps(_STATE, cs=_AUDIO_CS) or "{}")
    except (ToolError, ValueError):
        raw = {}
    radios = {k.lower(): v.lower() == "on" for k, v in (raw.get("radios") or {}).items()}
    out = {"volume": _pair(raw.get("speaker")), "microphone": _pair(raw.get("mic")), "brightness": raw.get("brightness"),
           "wifi_radio": radios.get("wifi"), "bluetooth": radios.get("bluetooth"), "wifi": _wifi(), "theme": None,
           "power_plan": None, "battery": None}
    if WINDOWS:
        out["theme"] = _theme_get()
        try:
            out["power_plan"] = next((p["name"] for p in _plans() if p["active"]), None)
        except ToolError:
            pass
    try:
        import psutil
        b = psutil.sensors_battery()
        out["battery"] = {"percent": round(b.percent), "plugged": bool(b.power_plugged)} if b else None
    except Exception:
        pass
    return out


@tool("pc_status", "Current settings of this PC: speaker and microphone volume, screen brightness, Wi-Fi and Bluetooth, "
      "dark or light theme, power plan and battery. Read it before changing a setting relative to its current value.",
      category="pc", untrusted=True)
def pc_status(ctx):
    return state()


@tool("pc_volume", "Set this PC's speaker or microphone volume to an exact percentage, and/or mute or unmute it.",
      {"level": {**I, "description": "0-100; leave out to keep the level"},
       "mute": {"type": "string", "enum": ["on", "off"], "description": "leave out to keep it"},
       "device": {"type": "string", "enum": ["speaker", "microphone"]}}, category="pc", risk="control")
def pc_volume(ctx, level: int = -1, mute: str = "", device: str = "speaker"):
    if device not in ("speaker", "microphone") or mute not in ("", "on", "off"):
        raise ToolError("device is speaker or microphone; mute is on or off.")
    if level < 0 and not mute:
        raise ToolError("Give a level, mute, or both.")
    flow = int(device == "microphone")
    script = "Add-Type -TypeDefinition $env:SPARX_CS; $f = [int]$env:SPARX_FLOW; "
    before = _pair(_ps(script + "[SparxAudio]::Get($f)", cs=_AUDIO_CS, flow=flow))
    now = _pair(_ps(script + "[SparxAudio]::Set($f, [int]$env:SPARX_LEVEL, [int]$env:SPARX_MUTE); [SparxAudio]::Get($f)",
                    cs=_AUDIO_CS, flow=flow, level=_level(level, "level") if level >= 0 else -1,
                    mute={"on": 1, "off": 0}.get(mute, -1)))
    if not now:
        raise ToolError(f"The {device} level could not be read back.")
    if before and before != now:
        undo.record(ctx, "pc_volume", f"{device} volume was {before['level']}%", {"flow": flow, **before})
    _log(ctx, "pc_volume", f"{device} {now['level']}%{' muted' if now['muted'] else ''}")
    return {"device": device, **now, "was": before}


@undo.handler("pc_volume")
def _undo_volume(d):
    _ps("Add-Type -TypeDefinition $env:SPARX_CS; [SparxAudio]::Set([int]$env:SPARX_FLOW, [int]$env:SPARX_LEVEL, [int]$env:SPARX_MUTE)",
        cs=_AUDIO_CS, flow=d["flow"], level=d["level"], mute=int(d["muted"]))


_ENDPOINTS = r"""
Add-Type -TypeDefinition $env:SPARX_CS
$d = @(Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue | ForEach-Object {
  @{ name = $_.FriendlyName; id = ($_.InstanceId -replace '^SWD\\MMDEVAPI\\', '') } })
@{ devices = $d; speaker = [SparxAudio]::DefaultId(0); microphone = [SparxAudio]::DefaultId(1) } | ConvertTo-Json -Compress -Depth 4
"""


def _endpoints() -> list[dict]:
    """Audio devices that are plugged in: name, kind (speaker | microphone), id, and whether each is the default."""
    raw = json.loads(_ps(_ENDPOINTS, cs=_AUDIO_CS))
    default = {str(raw.get(k, "")).lower() for k in ("speaker", "microphone")}
    return [{"name": d["name"], "kind": "microphone" if d["id"].lower().startswith("{0.0.1.") else "speaker",
             "id": d["id"], "default": d["id"].lower() in default} for d in raw.get("devices") or [] if d.get("name")]


@tool("pc_audio_devices", "The speakers, headphones and microphones this PC can use right now, and which are the defaults.",
      category="pc", untrusted=True)
def pc_audio_devices(ctx):
    return [{k: d[k] for k in ("name", "kind", "default")} for d in _endpoints()] or "Windows reports no audio devices."


@tool("pc_audio_switch", "Make a speaker, headphone or microphone the default audio device of this PC, by part of its "
      "name from pc_audio_devices (e.g. 'headphones', 'realme Buds').", {"device": S}, ["device"],
      category="pc", risk="control")
def pc_audio_switch(ctx, device: str):
    want, all_ = device.strip().lower(), _endpoints()
    hits = [d for d in all_ if want and want in d["name"].lower()]
    if len(hits) != 1:
        raise ToolError(f"'{device}' matches {len(hits)} audio devices. Available: {', '.join(d['name'] for d in all_) or 'none'}."
                        + (" Ask the user which one." if len(hits) > 1 else ""))
    before = next((d for d in all_ if d["default"] and d["kind"] == hits[0]["kind"]), None)
    _ps("Add-Type -TypeDefinition $env:SPARX_CS; [SparxAudio]::SetDefault($env:SPARX_ID)", cs=_AUDIO_CS, id=hits[0]["id"])
    now = next((d for d in _endpoints() if d["default"] and d["kind"] == hits[0]["kind"]), None)
    if not now or now["id"] != hits[0]["id"]:
        raise ToolError(f"Windows kept {now['name'] if now else 'its previous device'} as the default {hits[0]['kind']}.")
    if before and before["id"] != now["id"]:
        undo.record(ctx, "pc_audio", f"Default {now['kind']} was {before['name']}", {"id": before["id"]})
    _log(ctx, "pc_audio_switch", now["name"])
    return {"default": now["name"], "kind": now["kind"], "was": before["name"] if before else None}


@undo.handler("pc_audio")
def _undo_audio(d):
    _ps("Add-Type -TypeDefinition $env:SPARX_CS; [SparxAudio]::SetDefault($env:SPARX_ID)", cs=_AUDIO_CS, id=d["id"])


_BRIGHT_GET = "[int](Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness)[0].CurrentBrightness"
_BRIGHT_SET = ("Invoke-CimMethod -InputObject (Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)[0] "
               "-MethodName WmiSetBrightness -Arguments @{Timeout=[uint32]1; Brightness=[byte]$env:SPARX_LEVEL} | Out-Null; ")


def _brightness(level: int | None = None) -> int:
    try:
        return int(_ps((_BRIGHT_SET if level is not None else "") + _BRIGHT_GET, level=level if level is not None else ""))
    except (ToolError, ValueError) as e:
        raise ToolError("Brightness can only be set on a built-in laptop screen; this display does not expose it to "
                        f"Windows ({str(e)[:120]}). Use the monitor's own buttons.")


@tool("pc_brightness", "Set this PC's screen brightness to a percentage (built-in laptop screens only).",
      {"level": {**I, "description": "0-100"}}, ["level"], category="pc", risk="control")
def pc_brightness(ctx, level: int):
    level = _level(level, "level")
    before = _brightness()
    now = _brightness(level)
    if before != now:
        undo.record(ctx, "pc_brightness", f"Brightness was {before}%", {"level": before})
    _log(ctx, "pc_brightness", f"{now}%")
    return {"brightness": now, "was": before}


@undo.handler("pc_brightness")
def _undo_brightness(d):
    _brightness(int(d["level"]))


def _radio_set(kind: str, on: bool) -> str:
    return _ps(_RADIOS + r"""
$r = $radios | Where-Object { "$($_.Kind)" -eq $env:SPARX_KIND } | Select-Object -First 1
if (-not $r) { 'missing' } else { Await ($r.SetStateAsync($env:SPARX_STATE)) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null; "$($r.State)" }
""", kind=kind, state="On" if on else "Off")


@tool("pc_radio", "Turn this PC's Wi-Fi or Bluetooth radio on or off. Turning Wi-Fi off takes the PC offline, and "
      "hosted models and web tools stop working until it is back on.",
      {"radio": {"type": "string", "enum": ["wifi", "bluetooth"]}, "on": B}, ["radio", "on"], category="pc", risk="control")
def pc_radio(ctx, radio: str, on: bool):
    kind = {"wifi": "WiFi", "bluetooth": "Bluetooth"}.get(radio)
    if not kind:
        raise ToolError("radio is wifi or bluetooth.")
    if radio == "wifi" and not on and ctx.data.get("automated"):
        raise ToolError("Refused: a background job may not take the PC offline; nothing could turn Wi-Fi back on.")
    out = _radio_set(kind, on)
    if out == "missing":
        raise ToolError(f"This PC has no {radio} radio that Windows can switch.")
    if (out.lower() == "on") != bool(on):
        raise ToolError(f"Windows left {radio} {out.lower()}. A hardware switch or a policy may be holding it.")
    if not on:
        undo.record(ctx, "pc_radio", f"{radio} was on", {"kind": kind})
    _log(ctx, "pc_radio", f"{radio} {out.lower()}")
    return f"{radio} is now {out.lower()}."


@undo.handler("pc_radio")
def _undo_radio(d):
    if _radio_set(d["kind"], True).lower() != "on":
        raise ToolError("Windows left the radio off; a hardware switch or a policy may be holding it.")


@tool("pc_wifi_list", "Wi-Fi networks in range of this PC, and the one it is connected to.", category="pc", untrusted=True)
def pc_wifi_list(ctx):
    seen = re.findall(r"^SSID \d+\s*:\s*(.+?)\s*$", _cmd("netsh", "wlan", "show", "networks"), re.M)
    return {"connected": _wifi(), "in_range": sorted(set(seen))}


@tool("pc_wifi_connect", "Connect this PC to a Wi-Fi network it already has saved, or disconnect from Wi-Fi (empty "
      "network). Disconnecting will take the PC offline, and hosted models and web tools stop working until it is joined again. "
      "A network that was never joined needs its password typed once in Windows.",
      {"network": {**S, "description": "network name; empty disconnects"}}, category="pc", risk="control")
def pc_wifi_connect(ctx, network: str = ""):
    if not network.strip():
        if ctx.data.get("automated"):
            raise ToolError("Refused: a background job may not take the PC offline; nothing could reconnect it.")
        was = _wifi()
        _cmd("netsh", "wlan", "disconnect")
        if was and was["connected"] and was["network"]:
            undo.record(ctx, "pc_wifi", f"Wi-Fi was connected to {was['network']}", {"network": was["network"]})
        _log(ctx, "pc_wifi_connect", "disconnected")
        return "Disconnected from Wi-Fi."
    if not re.fullmatch(r"[^\"\r\n]{1,64}", network):
        raise ToolError("That is not a valid network name.")
    try:
        _cmd("netsh", "wlan", "connect", f"name={network}")
    except ToolError as e:
        raise ToolError(f"{e} Windows only connects from here to networks it has saved; join '{network}' once by hand.")
    _log(ctx, "pc_wifi_connect", network)
    return f"Asked Windows to join '{network}'. Check pc_status in a few seconds to confirm."


@undo.handler("pc_wifi")
def _undo_wifi(d):
    _cmd("netsh", "wlan", "connect", f"name={d['network']}")


@tool("pc_theme", "Switch Windows and its apps between dark and light mode.",
      {"mode": {"type": "string", "enum": ["dark", "light"]}}, ["mode"], category="pc", risk="control")
def pc_theme(ctx, mode: str):
    _windows_only()
    if mode not in ("dark", "light"):
        raise ToolError("mode is dark or light.")
    before = _theme_get()
    try:
        _theme_set(mode)
    except OSError as e:
        raise ToolError(f"Windows could not write the theme setting: {e}")
    after = _theme_get()
    if after and after != mode:
        raise ToolError(f"Windows is still {after}; the {mode} theme was not applied.")
    if before and before != mode:
        undo.record(ctx, "pc_theme", f"Theme was {before}", {"mode": before})
    _log(ctx, "pc_theme", mode)
    return {"theme": after, "was": before}


@undo.handler("pc_theme")
def _undo_theme(d):
    _theme_set(d["mode"])


@tool("pc_wallpaper", "Set the desktop wallpaper of this PC to an image file.",
      {"path": {**S, "description": "path to a jpg, png or bmp"}}, ["path"], category="pc", risk="control")
def pc_wallpaper(ctx, path: str):
    _windows_only()
    p = safe_path(path, exists=True)
    if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"):
        raise ToolError("The wallpaper must be a jpg, png or bmp image.")
    before = _wallpaper_get()
    _wallpaper_set(str(p))
    if before:
        undo.record(ctx, "pc_wallpaper", f"Wallpaper was {before}", {"path": before})
    _log(ctx, "pc_wallpaper", str(p))
    return {"wallpaper": str(p), "was": before}


@undo.handler("pc_wallpaper")
def _undo_wallpaper(d):
    if not os.path.isfile(d["path"]):
        raise ToolError(f"The previous wallpaper {d['path']} no longer exists, so it cannot be restored.")
    _wallpaper_set(d["path"])


@tool("pc_power_plan", "Show this PC's power plans, switch plan, or set after how many minutes the screen turns off "
      "and the PC sleeps (0 = never). Call it with no arguments to list the plans.",
      {"plan": {**S, "description": "plan name or part of it, e.g. 'balanced'"},
       "screen_off_minutes": I, "sleep_minutes": I}, category="pc", risk="control")
def pc_power_plan(ctx, plan: str = "", screen_off_minutes: int = -1, sleep_minutes: int = -1):
    done = []
    if plan.strip():
        known = _plans()
        hits = [p for p in known if plan.strip().lower() in p["name"].lower()]
        if len(hits) != 1:
            raise ToolError(f"'{plan}' matches {len(hits)} plans. This PC has: {', '.join(p['name'] for p in known)}.")
        was = next((p for p in known if p["active"]), None)
        _cmd("powercfg", "/setactive", hits[0]["guid"])
        if was and was["guid"] != hits[0]["guid"]:
            undo.record(ctx, "pc_power_plan", f"Power plan was {was['name']}", {"guid": was["guid"]})
        done.append(f"plan {hits[0]['name']}")
    for minutes, key, label in ((screen_off_minutes, "monitor-timeout", "screen off"), (sleep_minutes, "standby-timeout", "sleep")):
        if minutes >= 0:
            set_ = min(int(minutes), 600)
            for src in ("ac", "dc"):
                _cmd("powercfg", "/change", f"{key}-{src}", str(set_))
            done.append((f"{label} after {set_} min" + (f" ({minutes} was too long)" if set_ != minutes else "")) if set_
                        else f"{label} never")
    if done:
        _log(ctx, "pc_power_plan", ", ".join(done))
    return {"changed": done, "plans": [{"name": p["name"], "active": p["active"]} for p in _plans()]}


_POWER = {
    "lock": ["rundll32.exe", "user32.dll,LockWorkStation"], "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
    "hibernate": ["shutdown", "/h"], "sign_out": ["shutdown", "/l"], "cancel": ["shutdown", "/a"],
    "restart": ["shutdown", "/r", "/t"], "shutdown": ["shutdown", "/s", "/t"],
}


@undo.handler("pc_power_plan")
def _undo_power_plan(d):
    _cmd("powercfg", "/setactive", d["guid"])


def _spawn(*args):
    """Start a program and do not wait: sleep freezes this process along with the PC, so its exit code never arrives."""
    try:
        subprocess.Popen(list(args), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        raise ToolError(f"Could not start {args[0]}: {e}")


@tool("pc_power", "Lock, sleep, hibernate, sign out, restart or shut down this PC, or cancel a restart/shutdown that is "
      "counting down. Sleep hibernates instead when hibernation is enabled on this PC. Restart and shutdown wait at least 30 seconds so they can be cancelled. Unsaved work is lost: "
      "only use it when the user asked for it in this conversation.",
      {"action": {"type": "string", "enum": list(_POWER)},
       "delay_seconds": {**I, "description": "for restart and shutdown, default 30"}}, ["action"],
      category="pc", risk="delete")
def pc_power(ctx, action: str, delay_seconds: int = 30):
    if action not in _POWER:
        raise ToolError(f"action is one of: {', '.join(_POWER)}.")
    cmd = list(_POWER[action])
    timed = action in ("restart", "shutdown")
    if timed:
        cmd.append(str(max(30, min(int(delay_seconds), 3600))))
    if action in ("sleep", "hibernate"):
        _spawn(*cmd)
        _log(ctx, "pc_power", action)
        return f"Asked Windows to {action}. If the PC is awake again, it did."
    _cmd(*cmd)
    _log(ctx, "pc_power", action)
    return (f"{action.capitalize()} in {cmd[-1]} seconds. Say 'cancel the {action}' to stop it." if timed
            else "Pending restart or shutdown cancelled." if action == "cancel" else f"Done: {action}.")


_MEDIA = {"play_pause": "playpause", "next": "nexttrack", "previous": "prevtrack", "stop": "stop"}


@tool("pc_media", "Press a media key on this PC: play/pause, next track, previous track or stop. It acts on whichever "
      "app is playing.", {"action": {"type": "string", "enum": list(_MEDIA)}}, ["action"], category="pc", risk="control")
def pc_media(ctx, action: str):
    if action not in _MEDIA:
        raise ToolError(f"action is one of: {', '.join(_MEDIA)}.")
    return _act(ctx, "pc_media", action, lambda g: g.press(_MEDIA[action]))


@tool("pc_settings_open", "Open a page of the Windows Settings app for the user, for settings Sparx cannot change "
      "directly (night light, do not disturb, default apps, privacy, updates...). Call it with no page to list them.",
      {"page": {**S, "description": "e.g. 'night light', 'bluetooth', 'windows update'"}}, category="pc", risk="control")
def pc_settings_open(ctx, page: str = ""):
    _windows_only()
    key = page.strip().lower().replace("ms-settings:", "")
    if not key:
        return {"pages": sorted(_PAGES)}
    uri = _PAGES.get(key) if key in _PAGES else key if key in _PAGES.values() else None
    if uri is None:
        near = [p for p in _PAGES if key in p or p in key]
        raise ToolError(f"No Settings page called '{page}'. " + (f"Closest: {', '.join(near)}." if near else
                        f"Known: {', '.join(sorted(_PAGES))}."))
    os.startfile(f"ms-settings:{uri}")
    _log(ctx, "pc_settings_open", key)
    return f"Opened Settings > {key}."


# ---------------------------------------------------------------- suggestions

def pc_hints(s: dict, now: datetime) -> list[dict]:
    """Settings worth offering to change on this PC, given its state. Pure, so it can be tested."""
    out, late = [], now.hour >= 21 or now.hour < 6
    b, vol = s.get("battery"), s.get("volume")
    low = bool(b and not b["plugged"] and b["percent"] <= 30)
    if low and (s.get("brightness") or 0) >= 60:
        out.append({"key": "pc:brightness", "suggestion": "Lower the screen brightness to 40%",
                    "reason": f"The laptop is on battery at {b['percent']}% and the screen is at {s['brightness']}%.",
                    "tool": "pc_brightness", "args": {"level": 40}})
    if low and s.get("bluetooth"):
        out.append({"key": "pc:bluetooth", "suggestion": "Turn Bluetooth off if nothing is paired right now",
                    "reason": f"Battery is at {b['percent']}% and Bluetooth is on.",
                    "tool": "pc_radio", "args": {"radio": "bluetooth", "on": False}})
    if late and s.get("theme") == "light":
        out.append({"key": "pc:theme", "suggestion": "Switch Windows to dark mode", "reason": "It is late and the theme is light.",
                    "tool": "pc_theme", "args": {"mode": "dark"}})
    if (now.hour >= 23 or now.hour < 6) and vol and not vol["muted"] and vol["level"] >= 70:
        out.append({"key": "pc:volume", "suggestion": "Turn the volume down to 30%",
                    "reason": f"It is night and the speakers are at {vol['level']}%.", "tool": "pc_volume", "args": {"level": 30}})
    return out


def hints(now: datetime | None = None) -> list[dict]:
    """Suggestions for this PC and, when one is connected, the phone."""
    now = now or datetime.now()
    out = []
    try:
        out += [{"device": "pc", **h} for h in pc_hints(state(), now)]
    except Exception:
        pass
    try:
        from . import phone
        if phone.known():
            out += [{"device": "phone", **h} for h in phone.phone_hints(phone.state(), now)]
    except Exception:      # the phone is out of reach: nothing to suggest for it
        pass
    return out


@tool("settings_suggest", "Look at the current state of this PC and the connected phone and propose settings changes "
      "(brightness, dark mode, battery saver, do not disturb, volume), each with its reason and the tool call that would "
      "do it. It changes nothing: put the suggestions to the user as a question and act only on the ones they accept.",
      category="pc")
def settings_suggest(ctx):
    found = [{k: v for k, v in h.items() if k != "key"} for h in hints()]
    return {"suggestions": found} if found else "Nothing worth changing right now."


@jobs.every(900, "device_watch", flag="device_suggestions")
def device_watch(ctx=None, now: datetime | None = None) -> int:
    """Turn a new hint into a notification, at most once per hint every six hours."""
    sent = 0
    for h in hints(now):
        last = db.one("SELECT ts FROM device_hints WHERE key = ?", (h["key"],))
        if last and time.time() - last["ts"] < 6 * 3600:
            continue
        db.x("INSERT INTO device_hints (key, ts) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET ts = excluded.ts",
             (h["key"], time.time()))
        jobs.notify(f"Suggestion for your {h['device']}", f"{h['suggestion']}? {h['reason']}", kind="suggestion",
                    why=f"A device setting looked worth changing. If the user says yes, call {h['tool']} with {json.dumps(h['args'])}.")
        sent += 1
    return sent

