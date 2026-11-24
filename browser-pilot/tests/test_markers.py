"""The slow real-browser tests carry a marker, so `-m "not browser"` gives a quick run of everything else."""
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def test_every_test_file_that_launches_a_browser_is_marked():
    unmarked = [f.name for f in sorted(TESTS.glob("test_*.py"))
                if f.name != "test_markers.py" and "sync_playwright" in f.read_text(encoding="utf-8")
                and "pytest.mark.browser" not in f.read_text(encoding="utf-8")]
    assert not unmarked, f"these files launch a browser but lack `pytestmark = pytest.mark.browser`: {unmarked}"


def test_the_browser_marker_is_registered():
    config = (TESTS.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "markers" in config and '"browser:' in config, "pyproject.toml does not register the `browser` marker"
