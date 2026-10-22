import sys
from pathlib import Path

import pytest

# Make "import app" work when pytest is run from the project folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as monitor  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_worker(monkeypatch):
    """Give each test its own (stopped) worker and disable auto-start."""
    monkeypatch.setitem(monitor.app.config, "MONITOR_AUTOSTART", False)
    worker = monitor.MonitorWorker()
    monkeypatch.setattr(monitor, "monitor_worker", worker)
    yield worker
    worker.stop()


@pytest.fixture
def db_path(tmp_path):
    """Point the app at a fresh temporary database for each test."""
    path = tmp_path / "test_monitor.db"
    old_path = monitor.app.config["DATABASE"]
    monitor.app.config["DATABASE"] = str(path)
    monitor.init_db()
    yield path
    monitor.app.config["DATABASE"] = old_path


@pytest.fixture
def fake_ping(monkeypatch):
    """Replace the real ping with a fake one so tests are fast and offline.

    Set results[host] = PingResult(...) or ("no_reply", None, "...") to
    control the outcome. Hosts that are not listed reply with 10 ms latency.
    """
    results = {}
    calls = []

    def _fake(host):
        calls.append(host)
        return monitor.PingResult(*results.get(host, ("up", 10.0, "Ping reply received")))

    monkeypatch.setattr(monitor, "ping_host", _fake)
    _fake.results = results
    _fake.calls = calls
    return _fake


@pytest.fixture
def client(db_path):
    monitor.app.config["TESTING"] = True
    with monitor.app.test_client() as test_client:
        yield test_client
