"""Phase 2 tests: background worker, interval setting, overlap protection,
availability percentages, database upgrades, and history cleanup."""
import sqlite3
import threading
import time
from datetime import timedelta

import pytest

import app as monitor


def wait_for(condition, timeout=5.0):
    """Poll until condition() is true, or fail after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    pytest.fail("Timed out waiting for condition")


def insert_check(db_path, target_id, status, age):
    """Add a check result that happened `age` (a timedelta) ago."""
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO checks (target_id, checked_at, is_up, latency_ms, detail, status) "
            "VALUES (?, ?, ?, NULL, 'test', ?)",
            (target_id, monitor.to_iso(monitor.utc_now() - age), int(status == "up"), status),
        )


def worker_threads():
    return [t for t in threading.enumerate() if t.name == "monitor-worker"]


# --- Interval setting -----------------------------------------------------

def test_interval_defaults_to_60_seconds(client):
    data = client.get("/api/settings").get_json()
    assert data == {"interval_seconds": 60, "min_interval_seconds": 15, "max_interval_seconds": 300}


@pytest.mark.parametrize("seconds", [15, 90, 300])
def test_interval_can_be_set_within_range(client, seconds):
    response = client.put("/api/settings", json={"interval_seconds": seconds})
    assert response.status_code == 200
    assert client.get("/api/settings").get_json()["interval_seconds"] == seconds


@pytest.mark.parametrize("bad", [14, 301, 0, -5, "60", 60.5, True, None, [60]])
def test_interval_outside_range_or_wrong_type_is_rejected(client, bad):
    response = client.put("/api/settings", json={"interval_seconds": bad})
    assert response.status_code == 400
    assert client.get("/api/settings").get_json()["interval_seconds"] == 60


def test_interval_survives_restart(client, db_path):
    client.put("/api/settings", json={"interval_seconds": 120})
    monitor.init_db()
    assert monitor.get_interval_seconds() == 120


def test_settings_update_is_blocked_cross_site(client):
    response = client.put("/api/settings", json={"interval_seconds": 15},
                          headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


# --- Status endpoint no longer pings ---------------------------------------

def test_status_endpoint_only_reads_saved_results(client, fake_ping):
    data = client.get("/api/status").get_json()
    assert fake_ping.calls == []
    assert data["monitor"]["interval_seconds"] == 60
    assert data["availability_window_hours"] == 24


def test_check_now_returns_409_while_a_check_is_running(client, fake_ping):
    monitor.check_lock.acquire()
    try:
        response = client.post("/api/checks")
    finally:
        monitor.check_lock.release()
    assert response.status_code == 409
    assert fake_ping.calls == []


# --- Background worker ----------------------------------------------------

def test_worker_checks_immediately_and_reports_its_state(db_path, fake_ping, fresh_worker):
    assert fresh_worker.start() is True
    wait_for(lambda: fresh_worker.last_cycle_finished_at is not None)

    assert sorted(fake_ping.calls) == ["1.1.1.1", "8.8.8.8", "localhost"]
    state = fresh_worker.snapshot()
    assert state["running"] is True
    assert state["next_check_at"] is not None
    fresh_worker.stop()
    assert fresh_worker.is_running() is False


def test_worker_is_never_started_twice(db_path, fake_ping, fresh_worker):
    # Ten threads all try to start the worker at the same moment.
    results = []
    starters = [threading.Thread(target=lambda: results.append(fresh_worker.start()))
                for _ in range(10)]
    for thread in starters:
        thread.start()
    for thread in starters:
        thread.join()

    assert results.count(True) == 1
    assert len(worker_threads()) == 1


def test_requests_start_the_worker_only_once(client, fake_ping, fresh_worker):
    monitor.app.config["MONITOR_AUTOSTART"] = True
    for _ in range(5):
        client.get("/api/status")
    assert fresh_worker.is_running()
    assert len(worker_threads()) == 1


def test_worker_repeats_on_the_interval(db_path, fake_ping, fresh_worker, monkeypatch):
    # Use a tiny interval so the test runs quickly.
    monkeypatch.setattr(monitor, "get_interval_seconds", lambda: 0.1)
    fresh_worker.start()
    wait_for(lambda: len(fake_ping.calls) >= 9)  # 3 cycles x 3 targets
    fresh_worker.stop()


def test_changing_interval_wakes_a_sleeping_worker(db_path, fake_ping, fresh_worker, monkeypatch):
    interval = {"value": 300}
    monkeypatch.setattr(monitor, "get_interval_seconds", lambda: interval["value"])
    fresh_worker.start()
    wait_for(lambda: len(fake_ping.calls) == 3)  # first cycle done, now waiting 300 s

    interval["value"] = 0.05
    fresh_worker.wake()
    wait_for(lambda: len(fake_ping.calls) >= 6, timeout=2)  # second cycle ran promptly


def test_worker_keeps_running_after_an_error(db_path, fresh_worker, monkeypatch):
    calls = {"count": 0}

    def flaky_run_checks():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("database is locked")
        return []

    monkeypatch.setattr(monitor, "run_checks", flaky_run_checks)
    monkeypatch.setattr(monitor, "get_interval_seconds", lambda: 0.05)
    fresh_worker.start()
    wait_for(lambda: calls["count"] >= 3)
    assert fresh_worker.is_running()
    assert fresh_worker.last_error is None  # cleared by the later successful cycles


def test_worker_records_error_message(db_path, fresh_worker, monkeypatch):
    def broken():
        raise RuntimeError("disk full")
    monkeypatch.setattr(monitor, "run_checks", broken)
    fresh_worker.run_cycle()
    assert fresh_worker.last_error == "disk full"


def test_stop_interrupts_a_long_wait(db_path, fake_ping, fresh_worker):
    fresh_worker.start()  # interval is 60 s
    wait_for(lambda: fresh_worker.last_cycle_finished_at is not None)
    started = time.monotonic()
    fresh_worker.stop()
    assert time.monotonic() - started < 2
    assert not fresh_worker.is_running()


# --- Overlap protection and parallel pings --------------------------------

def test_two_check_cycles_never_overlap(db_path, monkeypatch):
    release = threading.Event()

    def slow_ping(host):
        release.wait(5)
        return monitor.PingResult("up", 1.0, "ok")

    monkeypatch.setattr(monitor, "ping_host", slow_ping)
    first = []
    runner = threading.Thread(target=lambda: first.append(monitor.run_checks()))
    runner.start()
    wait_for(monitor.check_lock.locked)

    assert monitor.run_checks() is None  # second cycle refuses to start
    release.set()
    runner.join()
    assert len(first[0]) == 3


def test_hosts_are_pinged_in_parallel(db_path, monkeypatch):
    def slow_ping(host):
        time.sleep(0.5)
        return monitor.PingResult("no_reply", None, "No ping reply")

    monkeypatch.setattr(monitor, "ping_host", slow_ping)
    started = time.monotonic()
    results = monitor.run_checks()
    # One after another would take 1.5 s; in parallel it takes about 0.5 s.
    assert time.monotonic() - started < 1.2
    assert [r["status"] for r in results] == ["no_reply"] * 3


def test_unreachable_and_dns_failures_are_saved_gracefully(client, fake_ping):
    fake_ping.results["8.8.8.8"] = ("no_reply", None, "No ping reply")
    fake_ping.results["1.1.1.1"] = ("dns_error", None, "DNS lookup failed")
    data = client.post("/api/checks").get_json()
    statuses = {t["host"]: t["status"] for t in data["targets"]}
    assert statuses == {"localhost": "up", "8.8.8.8": "no_reply", "1.1.1.1": "dns_error"}
    assert data["summary"]["down"] == 2


def test_target_removed_during_check_is_not_recorded(db_path, monkeypatch):
    def ping_then_delete(host):
        if host == "8.8.8.8":
            with sqlite3.connect(db_path) as db:
                db.execute("DELETE FROM targets WHERE host = '8.8.8.8'")
        return monitor.PingResult("up", 1.0, "ok")

    monkeypatch.setattr(monitor, "ping_host", ping_then_delete)
    results = monitor.run_checks()
    assert "8.8.8.8" not in [r["host"] for r in results]


# --- Availability ---------------------------------------------------------

def test_availability_uses_only_recorded_checks_in_last_24_hours(client, db_path):
    insert_check(db_path, 1, "up", timedelta(hours=3))
    insert_check(db_path, 1, "up", timedelta(hours=2))
    insert_check(db_path, 1, "up", timedelta(hours=1))
    insert_check(db_path, 1, "no_reply", timedelta(minutes=5))
    insert_check(db_path, 1, "no_reply", timedelta(hours=30))  # outside window

    data = client.get("/api/status").get_json()
    availability = data["targets"][0]["availability"]
    assert availability["percent"] == 75.0
    assert (availability["checks"], availability["replies"]) == (4, 3)
    assert availability["first_check_at"] is not None


def test_dns_failures_count_as_no_reply_in_availability(client, db_path):
    insert_check(db_path, 1, "up", timedelta(minutes=2))
    insert_check(db_path, 1, "dns_error", timedelta(minutes=1))
    assert client.get("/api/status").get_json()["targets"][0]["availability"]["percent"] == 50.0


def test_availability_is_empty_without_checks(client):
    availability = client.get("/api/status").get_json()["targets"][0]["availability"]
    assert availability == {"percent": None, "checks": 0, "replies": 0, "first_check_at": None}


# --- Database upgrade and retention ---------------------------------------

def test_upgrade_from_phase1_database_keeps_history(tmp_path):
    """A monitor.db made by the earlier version (no status column, no
    settings table) must keep all its data after upgrading."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE targets (id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE checks (id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL, checked_at TEXT NOT NULL,
                is_up INTEGER NOT NULL, latency_ms REAL, detail TEXT);
            INSERT INTO targets VALUES (1, 'router.local', 'Router', 1, '2026-10-01T00:00:00+00:00');
            INSERT INTO checks VALUES (1, 1, '2026-10-22T10:00:00.123456+00:00', 1, 2.5, 'Ping reply received');
            INSERT INTO checks VALUES (2, 1, '2026-10-22T10:01:00.123456+00:00', 0, NULL, 'No ping reply');
            INSERT INTO checks VALUES (3, 1, '2026-10-22T10:02:00.123456+00:00', 0, NULL,
                'DNS lookup failed: the hostname could not be resolved');
        """)

    old = monitor.app.config["DATABASE"]
    monitor.app.config["DATABASE"] = str(path)
    try:
        monitor.init_db()
        monitor.init_db()  # running twice must be harmless
        with sqlite3.connect(path) as db:
            statuses = [r[0] for r in db.execute("SELECT status FROM checks ORDER BY id")]
            targets = db.execute("SELECT host FROM targets").fetchall()
        assert statuses == ["up", "no_reply", "dns_error"]
        assert targets == [("router.local",)]  # defaults not added on top
        assert monitor.get_interval_seconds() == 60
    finally:
        monitor.app.config["DATABASE"] = old


def test_checks_older_than_retention_are_deleted(db_path):
    insert_check(db_path, 1, "up", timedelta(days=monitor.RETENTION_DAYS, hours=1))
    insert_check(db_path, 1, "up", timedelta(days=monitor.RETENTION_DAYS - 1))
    monitor.prune_old_checks()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 1


def test_timestamps_have_consistent_format():
    stamp = monitor.to_iso(monitor.utc_now())
    assert len(stamp) == len("2026-10-22T17:00:00+00:00") and stamp.endswith("+00:00")


def test_default_port_avoids_macos_airplay_port():
    assert monitor.PORT != 5000
