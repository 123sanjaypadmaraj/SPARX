"""kaggle-sync with GitHub, the model server and the kaggle CLI faked: no network, no kaggle program."""
import io
import json
import os
import subprocess
import sys
import urllib.error

import pytest

from sparx import config, kaggle_sync as ks, llm

GOOD = {"base_url": "https://abc.trycloudflare.com/v1", "token": "tok-12345678"}


class Stop(Exception):
    pass


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Net:
    """Fake urlopen: serves the gist JSON and a /models answer, and records every URL asked for."""

    def __init__(self, gist=None, models_ok=True, gist_error=None):
        self.gist, self.models_ok, self.gist_error, self.urls = gist, models_ok, gist_error, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if "api.github.com" in url:
            if self.gist_error:
                raise urllib.error.HTTPError(url, self.gist_error, "x", {}, None)
            if self.gist is None:
                raise urllib.error.URLError("gone")
            return Resp(json.dumps({"files": {ks.GIST_FILE: {"content": json.dumps(self.gist)}}}).encode())
        if not self.models_ok:
            raise urllib.error.URLError("tunnel down")
        return Resp(b"{}")

    def github(self):
        return [u for u in self.urls if "api.github.com" in u]


@pytest.fixture
def net(monkeypatch):
    for k in ks.KEYS:
        monkeypatch.setenv(k, "")        # so monkeypatch restores "unset" after apply() writes os.environ
        monkeypatch.delenv(k)
    monkeypatch.setattr(config.bp, "PROVIDERS", {"custom": ("http://old", "X")})
    n = Net(dict(GOOD))
    monkeypatch.setattr(ks.urllib.request, "urlopen", n)
    return n


def env_text():
    f = config.path(".env")
    return f.read_text(encoding="utf-8") if f.is_file() else ""


def test_valid_gist_is_applied_and_env_lines_are_kept(net):
    config.path(".env").write_text("# my keys\n\nGROQ_API_KEY=x\n", encoding="utf-8")
    assert ks.sync("g1").startswith("updated")
    assert os.environ[ks.KEYS[0]] == GOOD["base_url"] and os.environ[ks.KEYS[1]] == GOOD["token"]
    assert env_text() == (f"# my keys\n\nGROQ_API_KEY=x\n{ks.KEYS[0]}={GOOD['base_url']}\n{ks.KEYS[1]}={GOOD['token']}\n")
    assert ks.sync("g1").startswith("already current")


def test_hostile_gists_are_refused_and_change_nothing(net, monkeypatch):
    config.path(".env").write_text("GROQ_API_KEY=x\n", encoding="utf-8")
    before = env_text()
    for bad in ({"base_url": GOOD["base_url"], "token": "abcdefgh\nOPENAI_API_KEY=evil"},
                {"base_url": "http://example.com/v1", "token": GOOD["token"]},
                {"base_url": "https://a.com/v1\nX=1", "token": GOOD["token"]},
                {"base_url": "ftp://a.com", "token": GOOD["token"]},
                {"base_url": GOOD["base_url"], "token": "short"},
                {"base_url": 5, "token": GOOD["token"]}):
        net.gist = bad
        with pytest.raises(RuntimeError, match="invalid"):
            ks.sync("g1")
    assert env_text() == before and ks.KEYS[0] not in os.environ
    with pytest.raises(RuntimeError, match="line break"):
        ks.update_env({"A": "x\nB=1"})
    assert env_text() == before


def test_local_http_is_accepted(net):
    net.gist = {"base_url": "http://127.0.0.1:8000/v1", "token": GOOD["token"]}
    assert ks.sync("g1").startswith("updated")


def test_healthy_current_server_needs_no_github(net, monkeypatch):
    monkeypatch.setenv(ks.KEYS[0], GOOD["base_url"])
    monkeypatch.setenv(ks.KEYS[1], GOOD["token"])
    assert ks.ensure("g1", "u/k") is True
    assert net.github() == []


def test_launches_once_within_relaunch_window(net, monkeypatch):
    net.gist = None
    calls = []

    def fake_kaggle(*args, timeout=180):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "pushed" if args[1] == "push" else "not found", "")
    monkeypatch.setattr(ks, "_kaggle", fake_kaggle)
    assert ks.ensure("g1", "u/k", say=lambda m: None) is False
    assert [c[1] for c in calls] == ["status", "push"]
    assert ks.ensure("g1", "u/k", say=lambda m: None) is False
    assert [c[1] for c in calls] == ["status", "push"]


def test_rate_limit_makes_watch_wait_fifteen_minutes(net, monkeypatch):
    net.gist_error = 403
    slept, said = [], []

    def sleep(n):
        slept.append(n)
        raise Stop
    monkeypatch.setattr(ks.time, "sleep", sleep)
    with pytest.raises(Stop):
        ks.watch("g1", say=said.append)
    assert slept == [900] and any("rate limit" in m for m in said)


def test_boot_wait_polls_slowly(net, monkeypatch):
    net.gist = None
    slept = []

    def sleep(n):
        slept.append(n)
        raise Stop
    monkeypatch.setattr(ks.time, "sleep", sleep)
    with pytest.raises(Stop):
        ks.ensure("g1", wait=100, say=lambda m: None)
    assert slept == [60]


def test_apply_drops_cached_custom_clients(net):
    llm._cache["custom/m"] = object()
    llm._cache["groq/m"] = object()
    assert ks.apply(dict(GOOD))
    assert "custom/m" not in llm._cache and "groq/m" in llm._cache
    llm._cache.pop("groq/m")


@pytest.mark.skipif(sys.platform != "win32", reason="Startup folder is a Windows thing")
def test_autostart_writes_and_removes_the_script(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    ks.autostart(True)
    f = ks._startup_file()
    assert f.is_file() and "kaggle-sync --watch" in f.read_text()
    ks.autostart(False)
    assert not f.exists()
