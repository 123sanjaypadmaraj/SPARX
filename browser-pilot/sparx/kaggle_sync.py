"""Keep Sparx pointed at a live Kaggle model server, and start one when there is none.

The notebook (docs/kaggle_sparx_server.ipynb) writes its tunnel URL and token to a secret
GitHub gist. `ensure` reads the gist, checks the server answers, and otherwise pushes and runs
the notebook with the `kaggle` CLI and waits for a fresh URL. `apply` makes the running process
use it at once (no restart) and mirrors it into ~/.sparx/.env. `watch` repeats this forever;
`start_background` does it on a thread so chat, serve, voice, telegram and daemon load the model
by themselves.

Needs: SPARX_KAGGLE_GIST (gist id) and, to launch sessions, SPARX_KAGGLE_KERNEL (user/slug) plus
`pip install kaggle` and ~/.kaggle/kaggle.json.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

GIST_FILE = "sparx_server.json"
KEYS = ("BROWSERPILOT_BASE_URL", "BROWSERPILOT_API_KEY")
NOTEBOOK = Path(__file__).resolve().parent.parent / "docs" / "kaggle_sparx_server.ipynb"
RELAUNCH_AFTER = 20 * 60        # seconds before a second push is allowed; a session takes minutes to boot
BOOT_WAIT = 15 * 60             # how long ensure() waits for a launched session to publish a URL
POLL = 60                       # seconds between gist reads while waiting for a boot (GitHub allows 60 per hour)
RATE_WAIT = 15 * 60             # seconds to stay away from GitHub after it says "rate limit"
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{8,256}")
_LOCAL = ("localhost", "127.0.0.1", "::1")


class RateLimited(RuntimeError):
    pass


def _check(data, gist_id: str = "?") -> dict:
    """Refuse a gist whose URL or token could leak the token or write extra lines to .env."""
    url = data.get("base_url") if isinstance(data, dict) else None
    ok = isinstance(url, str) and len(url) <= 300 and not re.search(r"[\s\x00-\x1f\x7f]", url)
    if ok:
        u = urllib.parse.urlsplit(url)
        ok = bool(u.hostname) and (u.scheme == "https" or (u.scheme == "http" and u.hostname in _LOCAL))
    if not ok:
        raise RuntimeError(f"gist {gist_id} has an invalid base_url")
    token = data.get("token")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise RuntimeError(f"gist {gist_id} has an invalid token")
    return {"base_url": url, "token": token}


def fetch(gist_id: str) -> dict:
    """The gist's {base_url, token}. A secret gist is readable by id, so no login or `gh` is needed."""
    req = urllib.request.Request(f"https://api.github.com/gists/{gist_id}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "sparx"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            files = json.load(r).get("files", {})
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RateLimited(f"could not read gist {gist_id}: HTTP {e.code}") from e
        raise RuntimeError(f"could not read gist {gist_id}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"could not read gist {gist_id}: {e}") from e
    if GIST_FILE not in files:
        raise RuntimeError(f"gist {gist_id} has no {GIST_FILE} yet; run the Kaggle notebook first")
    try:
        data = json.loads(files[GIST_FILE]["content"])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"gist {gist_id} has an unreadable {GIST_FILE}") from e
    return _check(data, gist_id)


def healthy(data: dict, timeout: float = 8) -> bool:
    """True if the server behind `data` answers with its token (the tunnel dies with the Kaggle session)."""
    req = urllib.request.Request(data["base_url"].rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {data['token']}", "User-Agent": "sparx"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def update_env(values: dict) -> bool:
    """Set KEY=value lines in ~/.sparx/.env, keeping everything else. True if the file changed."""
    if any(re.search(r"[\r\n]", str(v)) for v in values.values()):
        raise RuntimeError("refusing to write a value with a line break to .env")
    f = config.path(".env")
    with config._locked(f):
        lines = f.read_text(encoding="utf-8").splitlines() if f.is_file() else []
        left = dict(values)
        out = []
        for line in lines:
            key = line.split("=", 1)[0].strip()
            out.append(f"{key}={left.pop(key)}" if key in left and not line.lstrip().startswith("#") else line)
        out += [f"{k}={v}" for k, v in left.items()]
        if out == lines:
            return False
        config._write_atomic(f, "\n".join(out) + "\n")
    return True


def apply(data: dict) -> bool:
    """Use this server in the running process and save it to .env. True if the URL or token changed."""
    values = {KEYS[0]: data["base_url"], KEYS[1]: data["token"]}
    changed = any(os.environ.get(k) != v for k, v in values.items())
    os.environ.update(values)
    config.bp.PROVIDERS["custom"] = (values[KEYS[0]], *config.bp.PROVIDERS["custom"][1:])
    if changed:
        from . import llm
        for spec in [s for s in llm._cache if s.startswith("custom/")]:   # their clients hold the old URL
            del llm._cache[spec]
    return update_env(values) or changed


def _kaggle(*args, timeout=180) -> subprocess.CompletedProcess:
    exe = shutil.which("kaggle")
    if not exe:
        raise RuntimeError("the kaggle CLI is not installed (pip install kaggle, then put kaggle.json in ~/.kaggle)")
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)


def kernel_status(kernel: str) -> str:
    """Lower-case Kaggle run state: queued, running, complete, error, cancelacknowledged... '' if unknown."""
    r = _kaggle("kernels", "status", kernel)
    text = (r.stdout + r.stderr).lower()
    for word in ("running", "queued", "complete", "error", "cancel"):
        if word in text:
            return word
    return ""


def launch(kernel: str) -> str:
    """Push the notebook as `kernel` (user/slug) on a GPU with internet, which also starts a run."""
    if not NOTEBOOK.is_file():
        raise RuntimeError(f"notebook not found at {NOTEBOOK}")
    work = config.path("kaggle_kernel", "x").parent
    shutil.copyfile(NOTEBOOK, work / NOTEBOOK.name)
    (work / "kernel-metadata.json").write_text(json.dumps({
        "id": kernel, "title": kernel.split("/")[-1].replace("-", " "), "code_file": NOTEBOOK.name,
        "language": "python", "kernel_type": "notebook", "is_private": "true",
        "enable_gpu": "true", "enable_internet": "true"}, indent=1), encoding="utf-8")
    r = _kaggle("kernels", "push", "-p", str(work))
    if r.returncode:
        raise RuntimeError(f"kaggle push failed: {(r.stderr or r.stdout).strip()[:200]}")
    config.path("kaggle_kernel", "last_push").write_text(str(time.time()))
    return (r.stdout or "pushed").strip()


def _recently_pushed() -> bool:
    try:
        return time.time() - float(config.path("kaggle_kernel", "last_push").read_text()) < RELAUNCH_AFTER
    except (OSError, ValueError):
        return False


def sync(gist_id: str) -> str:
    """Apply whatever the gist says, without checking it or starting anything."""
    data = fetch(gist_id)
    changed = apply(data)
    return ("updated" if changed else "already current") + f": {data['base_url']}"


def _start_once(kernel: str, say) -> bool:
    """Start the notebook unless a push is recent or a session is already up. The lock stops two Sparx processes both
    pushing. False if another process pushed a moment ago."""
    with config._locked(config.path("kaggle_kernel", "last_push"), wait=240, stale=600):
        if _recently_pushed():
            return False
        if kernel_status(kernel) in ("running", "queued"):
            say("kaggle: session is starting, waiting for its URL")
        else:
            say("kaggle: no model server is up; starting the notebook")
            say("kaggle: " + launch(kernel))
    return True


def ensure(gist_id: str, kernel: str = "", wait: float = 0, say=print) -> bool:
    """Make a live server the active one. If none answers and `kernel` is set, start a Kaggle session and wait up
    to `wait` seconds for it. True once a healthy server is in use."""
    current = {"base_url": os.environ.get(KEYS[0], ""), "token": os.environ.get(KEYS[1], "")}
    if all(current.values()) and healthy(current):      # already pointed at a live server: no GitHub request needed
        return True
    deadline = time.time() + wait
    launched = False
    while True:
        try:
            data = fetch(gist_id)
            if healthy(data):
                if apply(data):
                    say(f"kaggle: model server ready at {data['base_url']}")
                return True
        except RateLimited:
            say("kaggle: GitHub rate limit reached, trying again in 15 minutes")
            raise
        except RuntimeError as e:
            say(f"kaggle: {e}")
        if kernel and not launched:
            try:
                _start_once(kernel, say)
                launched = True
            except Exception as e:
                say(f"kaggle: cannot start it ({e})")
                return False
        if time.time() >= deadline:
            return False
        time.sleep(POLL)


def watch(gist_id: str, kernel: str = "", every: int = 60, say=print):
    """Forever: keep a healthy server in use, starting a Kaggle session whenever the last one has gone."""
    while True:
        pause = every
        try:
            ensure(gist_id, kernel, wait=BOOT_WAIT if kernel else 0, say=say)
        except RateLimited:
            pause = RATE_WAIT
        except Exception as e:
            say(f"kaggle: {e}")
        time.sleep(pause)


def start_background(say=lambda m: print(m, flush=True)) -> threading.Thread | None:
    """Called when Sparx starts: if SPARX_KAGGLE_GIST is configured, keep the model server up on a daemon thread."""
    gist = config.secret("SPARX_KAGGLE_GIST")
    if not gist:
        return None
    t = threading.Thread(target=watch, args=(gist, config.secret("SPARX_KAGGLE_KERNEL")), kwargs={"say": say},
                         daemon=True, name="kaggle-sync")
    t.start()
    return t


# ---------------------------------------------------------------- start at sign-in (Windows)
def _startup_file() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "sparx-kaggle.vbs"


def autostart(on: bool) -> str:
    """Install or remove a hidden Startup-folder script that runs `sparx kaggle-sync --watch` at every sign-in."""
    f = _startup_file()
    if not on:
        f.unlink(missing_ok=True)
        return f"removed {f}"
    if os.name != "nt":
        raise RuntimeError("autostart is for Windows; on Linux/macOS run `sparx kaggle-sync --watch` from systemd or launchd")
    py = Path(sys.executable)
    py = py.with_name("pythonw.exe") if py.with_name("pythonw.exe").is_file() else py
    repo = Path(__file__).resolve().parent.parent
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('CreateObject("Wscript.Shell").Run "cmd /c cd /d ""%s"" && ""%s"" -m sparx kaggle-sync --watch", 0, False\n'
                 % (repo, py), encoding="ascii", errors="replace")
    return f"will start at sign-in: {f}"
