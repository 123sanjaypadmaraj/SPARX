"""Per-test watchdog (pytest-timeout is not installed). Loaded by tests/conftest.py.

SPARX_TEST_TIMEOUT = seconds per test (default 120, 0 disables). On expiry every thread's traceback is written to
the real stderr and the process exits, so a hung test cannot stall a whole run. Not named for import: a pip package
called `watchdog` exists, so conftest loads this file by path.
"""
import faulthandler
import os
import sys

import pytest

DEFAULT_SECONDS = 120.0
_fd = None      # duplicate of the real stderr, taken before per-test capture redirects fd 2


def timeout_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("SPARX_TEST_TIMEOUT", DEFAULT_SECONDS)))
    except ValueError:
        return DEFAULT_SECONDS


def pytest_configure(config):
    global _fd
    if _fd is None:
        try:
            _fd = os.dup((sys.stderr or sys.__stderr__).fileno())
        except (AttributeError, ValueError, OSError):
            _fd = os.dup(sys.__stderr__.fileno())


def pytest_unconfigure(config):
    global _fd
    if _fd is not None:
        os.close(_fd)
        _fd = None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Arm around setup, call and teardown of one test."""
    seconds = timeout_seconds()
    if seconds <= 0 or _fd is None:
        return (yield)
    out = os.fdopen(os.dup(_fd), "w")       # faulthandler needs the file kept open while armed
    faulthandler.dump_traceback_later(seconds, exit=True, file=out)
    try:
        return (yield)
    finally:
        faulthandler.cancel_dump_traceback_later()
        out.close()
