"""The watchdog kills a hung test and reports the stuck function on the real stderr."""
import os
import subprocess
import sys
import time
from pathlib import Path

WATCHDOG = Path(__file__).with_name("watchdog.py")

CONFTEST = f'''
import importlib.util
spec = importlib.util.spec_from_file_location("sparx_test_watchdog", {str(WATCHDOG)!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def pytest_configure(config):
    config.pluginmanager.register(mod, "sparx_test_watchdog")
'''

HANG = '''
import time

def test_sleeps_forever_marker():
    time.sleep(30)
'''


def test_hung_test_is_killed_with_traceback(tmp_path):
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_hang.py").write_text(HANG)
    env = {**os.environ, "SPARX_TEST_TIMEOUT": "1"}
    start = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_hang.py"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25)
    assert time.monotonic() - start < 15
    assert p.returncode != 0
    assert "test_sleeps_forever_marker" in p.stderr
