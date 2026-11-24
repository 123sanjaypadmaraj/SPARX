"""Managing this PC: window layout, installed apps and software (winget), startup apps, display, network, recycle bin."""
import ctypes
import json
import re
import socket
import subprocess
import time
from pathlib import Path

from .. import security, undo
from . import B, I, S, ToolError, category, tool
from .pc import _proc, _ps
from .system import _focus, _log, _processes, _visible_windows, _windows_only

category("desktop", "Manage this PC: windows, launch apps, install/remove software, startup apps, resolution, network, recycle bin")

# ---------------------------------------------------------------- windows

_LAUNCH_WAIT = 4.0
_SHOW = {"minimize": 6, "maximize": 3, "restore": 9}
_SNAP = {"left": (0, 0, .5, 1), "right": (.5, 0, .5, 1), "top": (0, 0, 1, .5), "bottom": (0, .5, 1, .5),
         "top_left": (0, 0, .5, .5), "top_right": (.5, 0, .5, .5), "bottom_left": (0, .5, .5, .5),
         "bottom_right": (.5, .5, .5, .5), "center": (.15, .1, .7, .8)}


def _work_area() -> tuple[int, int, int, int]:
    r = (ctypes.c_long * 4)()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, r, 0)
    return r[0], r[1], r[2] - r[0], r[3] - r[1]


def _win_do(hwnd: int, action: str) -> bool:
    """Apply one layout action to a window handle. False when Windows refused (an elevated window, a closed one)."""
    u, h = ctypes.WinDLL("user32", use_last_error=True), ctypes.c_void_p(hwnd)
    if action in _SHOW:
        u.ShowWindow(h, _SHOW[action])           # its return value is the old state, so look at the new one
        iconic, zoomed = bool(u.IsIconic(h)), bool(u.IsZoomed(h))
        return {"minimize": iconic, "maximize": zoomed, "restore": not iconic and not zoomed}[action]
    if action == "close":
        return bool(u.PostMessageW(h, 0x0010, 0, 0))          # WM_CLOSE: the app may still ask to save
    x, y, w, hgt = _work_area()
    fx, fy, fw, fh = _SNAP[action]
    u.ShowWindow(h, 9)
    moved = bool(u.SetWindowPos(h, None, int(x + w * fx), int(y + hgt * fy), int(w * fw), int(hgt * fh), 0x0004))
    _focus(hwnd)
    return moved


def _find_windows(name: str) -> list[dict]:
    want = name.strip().lower()
    wins = [w for w in _visible_windows() if w["title"] not in ("Program Manager", "Windows Input Experience")]
    hit = [w for w in wins if want in w["title"].lower()]
    if not hit:
        exe = want.removesuffix(".exe")
        pids = {p["pid"] for p in _processes() if p["name"].lower().removesuffix(".exe") == exe}
        hit = [w for w in wins if w["pid"] in pids]
    return hit


@tool("window_arrange", "Arrange an open window: minimize, maximize, restore, close (politely: the app may ask to save), "
      "or snap it to left, right, top, bottom, a corner or the center of the screen. Name the window by part of its "
      "title or its app ('chrome').",
      {"window": {**S, "description": "part of the title, or the app's process name"},
       "action": {"type": "string", "enum": [*_SHOW, "close", *_SNAP]},
       "all_matching": {**B, "description": "apply to every matching window, not only the first"}},
      ["window", "action"], category="desktop", risk="control")
def window_arrange(ctx, window: str, action: str, all_matching: bool = False):
    if action not in (*_SHOW, "close", *_SNAP):
        raise ToolError(f"action is one of: {', '.join([*_SHOW, 'close', *_SNAP])}.")
    if not window.strip():
        raise ToolError("Give the window's title or app name; a blank name would match every window.")
    hits = _find_windows(window)
    if not hits:
        raise ToolError(f"No open window matches '{window}'. app_list shows what is open.")
    targets = hits if all_matching else hits[:1]
    done = [w for w in targets if _win_do(w["hwnd"], action)]
    failed = [w for w in targets if w not in done]
    if not done:
        raise ToolError(f"Windows refused to {action} '{failed[0]['title'][:80]}'" + (f" and {len(failed) - 1} more" if len(failed) > 1 else "")
                        + ". If it runs as administrator, Sparx cannot control it.")
    _log(ctx, "window_arrange", f"{action}: {done[0]['title'][:80]}" + (f" (+{len(done) - 1})" if len(done) > 1 else ""))
    return {"action": action, "windows": [w["title"] for w in done],
            **({"failed": [w["title"] for w in failed]} if failed else {}),
            **({"other_matches": [w["title"] for w in hits[1:6]]} if len(hits) > 1 and not all_matching else {})}


@tool("windows_layout", "Lay out several windows at once: 'side_by_side' puts two named windows left and right, 'grid' "
      "tiles up to four, 'show_desktop' minimises everything, 'restore_all' brings minimised windows back.",
      {"layout": {"type": "string", "enum": ["side_by_side", "grid", "show_desktop", "restore_all"]},
       "windows": {"type": "array", "items": {"type": "string"}, "description": "window names, in order"}},
      ["layout"], category="desktop", risk="control")
def windows_layout(ctx, layout: str, windows: list | None = None):
    names = [str(n) for n in windows or []]
    if layout in ("show_desktop", "restore_all"):
        act = "minimize" if layout == "show_desktop" else "restore"
        targets = [w for w in _find_windows("")]
        ok = [_win_do(w["hwnd"], act) for w in targets]
        _log(ctx, "windows_layout", layout)
        return (f"{'Minimised' if act == 'minimize' else 'Restored'} {sum(ok)} windows."
                + (f" {len(ok) - sum(ok)} refused (they may run as administrator)." if not all(ok) else ""))
    slots = {"side_by_side": ["left", "right"], "grid": ["top_left", "top_right", "bottom_left", "bottom_right"]}.get(layout)
    if not slots:
        raise ToolError("layout is side_by_side, grid, show_desktop or restore_all.")
    if not 2 <= len(names) <= len(slots):
        raise ToolError(f"{layout} needs between 2 and {len(slots)} window names.")
    if not all(n.strip() for n in names):
        raise ToolError("Give the window's title or app name; a blank name would match every window.")
    placed = {}
    for name, slot in zip(names, slots):
        hits = _find_windows(name)
        if not hits:
            raise ToolError(f"No open window matches '{name}'. Nothing was moved yet." if not placed else
                            f"No open window matches '{name}'. Already placed: {placed}.")
        if not _win_do(hits[0]["hwnd"], slot):
            raise ToolError(f"Windows refused to move '{hits[0]['title'][:60]}'. If it runs as administrator, Sparx cannot control it."
                            + (f" Already placed: {placed}." if placed else " Nothing was moved yet."))
        placed[hits[0]["title"][:60]] = slot
    _log(ctx, "windows_layout", f"{layout}: {', '.join(names)}")
    return placed


# ---------------------------------------------------------------- installed apps

def _start_apps() -> list[dict]:
    """Everything in the Start menu, Store apps included: [{'name', 'id'}]."""
    raw = json.loads(_ps("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress") or "[]")
    return [{"name": a["Name"], "id": a["AppID"]} for a in (raw if isinstance(raw, list) else [raw]) if a.get("Name")]


def _safe_windows() -> list[dict]:
    try:
        return _visible_windows()
    except Exception:
        return []


def _launch(app_id: str):
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@tool("apps_installed", "Apps in this PC's Start menu, including Microsoft Store apps. Use it to find the exact name "
      "before app_launch.", {"filter": S}, category="desktop", untrusted=True)
def apps_installed(ctx, filter: str = ""):
    names = sorted({a["name"] for a in _start_apps() if filter.lower() in a["name"].lower()})
    return names[:200] or f"No installed app has '{filter}' in its name."


@tool("app_launch", "Start any installed app by its Start-menu name, including Store apps that app_open cannot find "
      "('WhatsApp', 'Spotify', 'Calculator', 'Settings').", {"name": S}, ["name"], category="desktop", risk="control")
def app_launch(ctx, name: str):
    want = name.strip().lower()
    if not want:
        raise ToolError("Give the app's name.")
    apps = _start_apps()
    hits = [a for a in apps if a["name"].lower() == want] or [a for a in apps if a["name"].lower().startswith(want)] \
        or [a for a in apps if want in a["name"].lower()]
    if not hits:
        raise ToolError(f"No installed app is called '{name}'. apps_installed lists them; software_install can add one.")
    if len({a["name"] for a in hits}) > 1 and hits[0]["name"].lower() != want:
        raise ToolError(f"'{name}' could be: {', '.join(sorted({a['name'] for a in hits})[:8])}. Ask the user which one.")
    before = {w["hwnd"] for w in _safe_windows()}
    _launch(hits[0]["id"])
    _log(ctx, "app_launch", hits[0]["name"])
    shown = hits[0]["name"].lower()
    end = time.monotonic() + _LAUNCH_WAIT
    while True:
        if any(w["hwnd"] not in before and shown in w["title"].lower() for w in _safe_windows()):
            return f"Started {hits[0]['name']}."
        if time.monotonic() >= end:
            break
        time.sleep(0.4)
    return (f"Asked Windows to start {hits[0]['name']}. No new window appeared within {_LAUNCH_WAIT:g} seconds, "
            "so it may still be starting or may have failed.")


# ---------------------------------------------------------------- software (winget)

_ID = re.compile(r"[A-Za-z0-9][\w.+\-]{1,99}")
# winget exit codes that mean "nothing to do", not "failed" (compared as unsigned; Windows may hand back the signed form)
_WINGET_NOTHING = {0x8A15002B, 0x8A15010E, 0x8A150014}


def _winget(*args, timeout: float = 120) -> str:
    """Run winget and return its table without the spinner and progress-bar lines."""
    code, out, err = _proc(["winget", *args, "--disable-interactivity"], timeout)
    rows = [re.sub(r" {3,}", "   ", l.rsplit("\r", 1)[-1].rstrip()) for l in out.splitlines()]
    text = "\n".join(l for l in rows if l.strip(" -\\|/") and "█" not in l and "▒" not in l)
    if code and (code & 0xFFFFFFFF) not in _WINGET_NOTHING:
        raise ToolError(f"winget failed: {(text or err.strip() or 'no output')[-600:]} (exit code {code & 0xFFFFFFFF:#x})")
    return text


def _id(package: str) -> str:
    if not _ID.fullmatch(package.strip()):
        raise ToolError("Give the package id from software_search, e.g. 'VideoLAN.VLC'.")
    return package.strip()


@tool("software_search", "Search the winget catalogue for a program to install. Returns names and package ids.",
      {"query": S}, ["query"], category="desktop", untrusted=True, timeout=90)
def software_search(ctx, query: str):
    if not re.fullmatch(r"[\w .+\-]{2,60}", query.strip()):
        raise ToolError("Search with a plain name, e.g. 'vlc'.")
    return _winget("search", query.strip(), "--accept-source-agreements", "--count", "15", timeout=80)


@tool("software_list", "Programs installed on this PC (winget's view), or only those with an update available.",
      {"filter": S, "updates_only": B}, category="desktop", untrusted=True, timeout=150)
def software_list(ctx, filter: str = "", updates_only: bool = False):
    text = _winget("upgrade" if updates_only else "list", "--accept-source-agreements", timeout=140)
    rows = [l for l in text.splitlines() if filter.lower() in l.lower()]
    more = f"\n…{len(rows) - 120} more; narrow it with filter." if len(rows) > 120 else ""
    return "\n".join(rows[:120]) + more if rows else "Nothing matches."


@tool("software_install", "Install a program on this PC with winget, by the package id from software_search. It "
      "downloads and runs the publisher's installer.", {"package": {**S, "description": "exact id, e.g. VideoLAN.VLC"}},
      ["package"], category="desktop", risk="exec", untrusted=True, timeout=900)
def software_install(ctx, package: str):
    out = _winget("install", "--id", _id(package), "-e", "--silent", "--accept-source-agreements",
                  "--accept-package-agreements", timeout=880)
    _log(ctx, "software_install", package)
    return out[-3000:]


@tool("software_update", "Update one installed program with winget (by package id), or all of them with package='all'. "
      "A long update keeps running in the background after Sparx stops waiting, so check software_list before retrying.",
      {"package": S}, ["package"], category="desktop", risk="exec", untrusted=True, timeout=1800)
def software_update(ctx, package: str):
    target = ["--all"] if package.strip().lower() == "all" else ["--id", _id(package), "-e"]
    out = _winget("upgrade", *target, "--silent", "--accept-source-agreements", "--accept-package-agreements", timeout=1780)
    _log(ctx, "software_update", package)
    return out[-5000:]


@tool("software_uninstall", "Remove a program from this PC with winget, by package id from software_list. This cannot "
      "be undone from Sparx.", {"package": S}, ["package"], category="desktop", risk="delete", untrusted=True, timeout=600)
def software_uninstall(ctx, package: str):
    out = _winget("uninstall", "--id", _id(package), "-e", "--silent", timeout=580)
    _log(ctx, "software_uninstall", package)
    return out[-3000:]


# ---------------------------------------------------------------- startup apps

_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def _startup_read() -> list[dict]:
    """Programs that start at sign-in, from the Run keys. `enabled` reflects Task Manager's switch."""
    import winreg
    out = []
    for scope, hive in (("user", winreg.HKEY_CURRENT_USER), ("all users", winreg.HKEY_LOCAL_MACHINE)):
        off = {}
        try:
            with winreg.OpenKey(hive, _APPROVED) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    n, v, _t = winreg.EnumValue(k, i)
                    off[n] = bool(v) and v[0] % 2 == 1          # 02/06 = enabled, 03/07 = disabled
        except OSError:
            pass
        try:
            with winreg.OpenKey(hive, _RUN) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    n, v, _t = winreg.EnumValue(k, i)
                    out.append({"name": n, "command": str(v)[:200], "scope": scope, "enabled": not off.get(n, False)})
        except OSError:
            pass
    return out


def _startup_write(name: str, enabled: bool):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _APPROVED) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, bytes([2 if enabled else 3]) + bytes(11))


@tool("startup_apps", "Programs that start automatically when the user signs in to this PC, and whether each is enabled.",
      category="desktop", untrusted=True)
def startup_apps(ctx):
    _windows_only()
    rows = [{**r, "command": security.redact(r["command"])} for r in _startup_read()]
    return rows or "Nothing is registered to start at sign-in."


@tool("startup_app_set", "Enable or disable a program's automatic start at sign-in (the same switch as Task Manager's "
      "Startup tab). The program itself is not removed.", {"name": {**S, "description": "name from startup_apps"}, "enabled": B},
      ["name", "enabled"], category="desktop", risk="control")
def startup_app_set(ctx, name: str, enabled: bool):
    _windows_only()
    rows = _startup_read()
    hit = [r for r in rows if r["name"].lower() == name.strip().lower()] or [r for r in rows if name.strip().lower() in r["name"].lower()]
    if len(hit) != 1:
        raise ToolError(f"'{name}' matches {len(hit)} startup entries. Known: {', '.join(r['name'] for r in rows) or 'none'}.")
    if hit[0]["scope"] != "user":
        raise ToolError(f"{hit[0]['name']} starts for all users, which needs administrator rights. Change it in Task Manager > Startup apps.")
    _startup_write(hit[0]["name"], bool(enabled))
    if hit[0]["enabled"] != bool(enabled):
        undo.record(ctx, "startup_app", f"{hit[0]['name']} startup was {'on' if hit[0]['enabled'] else 'off'}",
                    {"name": hit[0]["name"], "enabled": hit[0]["enabled"]})
    _log(ctx, "startup_app_set", f"{hit[0]['name']} {'on' if enabled else 'off'}")
    return f"{hit[0]['name']} will {'start' if enabled else 'no longer start'} at sign-in."


@undo.handler("startup_app")
def _undo_startup(d):
    _startup_write(d["name"], bool(d["enabled"]))


# ---------------------------------------------------------------- display

def _modes() -> list[tuple[int, int]]:
    import win32api
    seen, i = set(), 0
    while True:
        try:
            m = win32api.EnumDisplaySettings(None, i)
        except Exception:
            break
        seen.add((m.PelsWidth, m.PelsHeight))
        i += 1
    return sorted(seen, reverse=True)


def _resolution(new: tuple[int, int] | None = None) -> tuple[int, int]:
    """Current resolution of the main display; with `new`, change it (after Windows confirms the mode is valid)."""
    import win32api
    import win32con
    dm = win32api.EnumDisplaySettings(None, win32con.ENUM_CURRENT_SETTINGS)
    if new:
        dm.PelsWidth, dm.PelsHeight = new
        dm.Fields = win32con.DM_PELSWIDTH | win32con.DM_PELSHEIGHT
        if win32api.ChangeDisplaySettings(dm, win32con.CDS_TEST) != 0 or win32api.ChangeDisplaySettings(dm, win32con.CDS_UPDATEREGISTRY) != 0:
            raise ToolError(f"The display refused {new[0]}x{new[1]}.")
        dm = win32api.EnumDisplaySettings(None, win32con.ENUM_CURRENT_SETTINGS)
    return dm.PelsWidth, dm.PelsHeight


@tool("display_info", "The main display's current resolution and the resolutions it supports.", category="desktop")
def display_info(ctx):
    _windows_only()
    w, h = _resolution()
    return {"resolution": f"{w}x{h}", "supported": [f"{a}x{b}" for a, b in _modes() if a >= 800][:25]}


@tool("display_resolution", "Change the main display's resolution to one of the modes from display_info.",
      {"width": I, "height": I}, ["width", "height"], category="desktop", risk="control")
def display_resolution(ctx, width: int, height: int):
    _windows_only()
    if (int(width), int(height)) not in _modes():
        raise ToolError(f"{width}x{height} is not a mode this display supports. See display_info.")
    before = _resolution()
    now = _resolution((int(width), int(height)))
    if before != now:
        undo.record(ctx, "display_resolution", f"Resolution was {before[0]}x{before[1]}", {"w": before[0], "h": before[1]})
    _log(ctx, "display_resolution", f"{now[0]}x{now[1]}")
    return {"resolution": f"{now[0]}x{now[1]}", "was": f"{before[0]}x{before[1]}"}


@undo.handler("display_resolution")
def _undo_resolution(d):
    _resolution((int(d["w"]), int(d["h"])))


# ---------------------------------------------------------------- network, bluetooth, recycle bin

def _online() -> dict:
    out = {}
    for label, host in (("internet", "1.1.1.1"), ("dns", "www.google.com")):
        try:
            socket.create_connection((host, 443), 3).close()
            out[label] = True
        except OSError:
            out[label] = False
    return out


@tool("network_info", "This PC's network state: active adapters with their IP addresses and link speed, and whether "
      "the internet and DNS are reachable. Use it for 'am I online', 'what is my IP', 'why is the internet down'.",
      category="desktop", untrusted=True)
def network_info(ctx):
    import psutil
    stats, adapters = psutil.net_if_stats(), []
    for name, addrs in psutil.net_if_addrs().items():
        ip = [a.address for a in addrs if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254."))]
        if ip and stats.get(name) and stats[name].isup:
            adapters.append({"adapter": name, "ip": ip[0], "speed_mbps": stats[name].speed or None})
    return {"adapters": adapters, **_online()}


_BT_NOISE = re.compile(r"Avrcp Transport|Service$|Profile$|Enumerator|Protocol|^Bluetooth |Generic Att", re.I)


@tool("bluetooth_devices", "Bluetooth devices paired with this PC (headphones, phones, speakers).",
      category="desktop", untrusted=True)
def bluetooth_devices(ctx):
    raw = json.loads(_ps("@(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -and "
                         "$_.InstanceId -like 'BTH*' } | Select-Object FriendlyName) | ConvertTo-Json -Compress") or "[]")
    names = {d["FriendlyName"] for d in (raw if isinstance(raw, list) else [raw]) if d.get("FriendlyName")}
    return sorted(n for n in names if not _BT_NOISE.search(n)) or "No paired Bluetooth devices found."


class _RB(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def _bin(empty: bool = False) -> dict:
    sh = ctypes.windll.shell32
    info = _RB()
    info.cbSize = ctypes.sizeof(_RB)
    sh.SHQueryRecycleBinW(None, ctypes.byref(info))
    got = {"items": info.i64NumItems, "mb": round(info.i64Size / 2**20, 1)}
    if empty and got["items"]:
        hr = sh.SHEmptyRecycleBinW(None, None, 0x7)       # no confirmation, progress UI or sound: the tool already asked
        if hr:
            raise ToolError(f"Windows refused to empty the Recycle Bin (code {hr & 0xFFFFFFFF:#x}).")
    return got


@tool("recycle_bin", "How many items are in the Recycle Bin and how much space they take.", category="desktop")
def recycle_bin(ctx):
    _windows_only()
    return _bin()


@tool("recycle_bin_empty", "Permanently delete everything in the Recycle Bin. This cannot be undone.",
      category="desktop", risk="delete")
def recycle_bin_empty(ctx):
    _windows_only()
    got = _bin(empty=True)
    left = _bin() if got["items"] else {"items": 0, "mb": 0}
    _log(ctx, "recycle_bin_empty", f"{got['items'] - left['items']} of {got['items']} items")
    if not got["items"]:
        return "The Recycle Bin was already empty."
    if left["items"]:
        return (f"Emptied only part of the Recycle Bin: {got['items'] - left['items']} of {got['items']} items are gone and "
                f"{left['items']} items ({left['mb']} MB) remain.")
    return f"Emptied the Recycle Bin: {got['items']} items, {got['mb']} MB freed."
