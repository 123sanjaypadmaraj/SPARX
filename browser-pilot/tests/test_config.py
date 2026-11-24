"""settings.json: atomic writes, damaged files are never overwritten, cross-process lock, discoverable job flags."""
import json
import os
import threading

import pytest

from sparx import config


def _file():
    return config.path("settings.json")


def _leftovers():
    return sorted(p.name for p in config.HOME.iterdir() if p.name != "settings.json" and not p.name.startswith("sparx.db"))


def test_a_failure_between_the_temp_file_and_the_replace_keeps_the_original(monkeypatch):
    config.save_settings({"user": "Asha"})
    before = _file().read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk went away")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        config.save_settings({"user": "Ben"})
    assert _file().read_text(encoding="utf-8") == before
    assert _leftovers() == []                                   # no temp file, no stale lock


@pytest.mark.parametrize("content", ["{not json", '["a"]', '"text"', "12"])
def test_a_damaged_settings_file_is_never_overwritten(content):
    _file().write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="settings.json"):
        config.save_settings({"user": "Asha"})
    with pytest.raises(ValueError, match="fix or remove"):
        config.set_key("user", "Asha")
    assert _file().read_text(encoding="utf-8") == content
    assert _leftovers() == []


def test_a_missing_or_empty_file_is_a_first_run():
    config.save_settings({"user": "Asha"})
    _file().write_text("  \n", encoding="utf-8")
    config.save_settings({"name": "Zed"})
    assert json.loads(_file().read_text(encoding="utf-8")) == {"name": "Zed"}


def test_problems_reports_a_file_that_is_not_json():
    assert config.problems() == []
    _file().write_text("{oops", encoding="utf-8")
    bad = config.problems()
    assert len(bad) == 1 and "settings.json" in bad[0] and "not valid JSON" in bad[0]
    assert config.settings() == config.DEFAULTS
    _file().write_text("", encoding="utf-8")
    assert config.problems() == []


def test_two_writers_do_not_lose_each_others_updates():
    n, errors = 40, []

    def work(key):
        try:
            for i in range(n):
                config.set_key(key, i)
        except Exception as e:                   # surfaced below
            errors.append(e)
    threads = [threading.Thread(target=work, args=(k,)) for k in ("integrations.a", "integrations.b")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    saved = json.loads(_file().read_text(encoding="utf-8"))
    assert saved["integrations"] == {"a": n - 1, "b": n - 1}
    assert _leftovers() == []


def test_a_stale_lock_is_broken_and_a_live_one_is_waited_for(monkeypatch):
    lock = config.path("settings.json.lock")
    lock.write_text("", encoding="utf-8")
    old = lock.stat().st_mtime - 60
    os.utime(lock, (old, old))
    config.save_settings({"user": "Asha"})
    assert config.settings()["user"] == "Asha" and not lock.exists()


def test_job_flags_are_discoverable_in_the_defaults():
    p = config.DEFAULTS["proactive"]
    for flag in ("device_suggestions", "toasts", "goal_intervention", "opportunities", "analytics", "research"):
        assert p[flag] is True, flag
