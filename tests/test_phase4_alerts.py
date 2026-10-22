"""Phase 4 tests: alerts when a host stops replying, without repeats."""
import sqlite3
import time
from datetime import timedelta

import pytest

import app as monitor

UP = ("up", 5.0, "Ping reply received")
NO_REPLY = ("no_reply", None, "No ping reply (host may be offline or blocking ICMP)")
DNS_FAIL = ("dns_error", None, "DNS lookup failed: the hostname could not be resolved")


@pytest.fixture
def one_target(db_path):
    """Keep only the Google DNS target (id 3) so results are easy to read."""
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM targets WHERE host != '8.8.8.8'")
    return 3


def run_sequence(fake_ping, *results):
    """Run one check cycle per result, with 8.8.8.8 returning that result."""
    for result in results:
        fake_ping.results["8.8.8.8"] = result
        monitor.run_checks()


def alerts(db_path):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM alerts ORDER BY id")]


# --- When alerts are created ----------------------------------------------

def test_alert_when_host_stops_replying(one_target, db_path, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY)
    [alert] = alerts(db_path)
    assert alert["kind"] == "down"
    assert alert["status"] == "no_reply"
    assert (alert["host"], alert["label"]) == ("8.8.8.8", "Google DNS")
    assert alert["created_at"].endswith("+00:00")
    assert alert["last_reply_at"] is not None and alert["last_reply_at"] <= alert["created_at"]
    assert alert["resolved_at"] is None and alert["acknowledged_at"] is None


def test_repeated_failures_do_not_repeat_the_alert(one_target, db_path, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY, NO_REPLY, NO_REPLY, NO_REPLY)
    assert [a["kind"] for a in alerts(db_path)] == ["down"]


def test_changing_failure_type_is_not_a_new_alert(one_target, db_path, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY, DNS_FAIL, NO_REPLY)
    assert len(alerts(db_path)) == 1


def test_host_that_never_replied_does_not_alert(one_target, db_path, fake_ping):
    """Not a change from responding to not responding."""
    run_sequence(fake_ping, NO_REPLY, NO_REPLY, DNS_FAIL)
    assert alerts(db_path) == []


def test_healthy_host_has_no_alerts(one_target, db_path, fake_ping):
    run_sequence(fake_ping, UP, UP, UP)
    assert alerts(db_path) == []


def test_dns_failure_after_replies_also_alerts(one_target, db_path, fake_ping):
    run_sequence(fake_ping, UP, DNS_FAIL)
    assert alerts(db_path)[0]["status"] == "dns_error"


def test_recovery_is_recorded_and_resolves_the_alert(one_target, db_path, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY, NO_REPLY, UP, UP)
    down, recovered = alerts(db_path)
    assert recovered["kind"] == "recovered"
    assert down["resolved_at"] == recovered["created_at"]
    assert len(alerts(db_path)) == 2  # the second UP adds nothing


def test_each_new_outage_creates_a_new_alert(one_target, db_path, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY, UP, NO_REPLY)
    assert [a["kind"] for a in alerts(db_path)] == ["down", "recovered", "down"]


def test_alerts_require_several_failures_when_configured(one_target, db_path, fake_ping, monkeypatch):
    monkeypatch.setattr(monitor, "ALERT_AFTER_FAILURES", 2)
    run_sequence(fake_ping, UP, NO_REPLY, UP)       # one lost reply: ignored
    assert alerts(db_path) == []
    run_sequence(fake_ping, NO_REPLY, NO_REPLY)     # two in a row: alert
    assert [a["kind"] for a in alerts(db_path)] == ["down"]
    run_sequence(fake_ping, NO_REPLY, NO_REPLY)     # still no repeats
    assert len(alerts(db_path)) == 1


def test_paused_target_creates_no_alerts(one_target, client, db_path, fake_ping):
    run_sequence(fake_ping, UP)
    client.post(f"/api/targets/{one_target}/toggle")  # pause
    run_sequence(fake_ping, NO_REPLY, NO_REPLY)
    assert alerts(db_path) == []


def test_run_checks_reports_the_alert(one_target, db_path, fake_ping):
    run_sequence(fake_ping, UP)
    fake_ping.results["8.8.8.8"] = NO_REPLY
    [result] = monitor.run_checks()
    assert result["alert"] == "down"


def test_alerts_are_independent_per_host(db_path, fake_ping):
    run_sequence(fake_ping, UP)  # all three hosts reply
    fake_ping.results["1.1.1.1"] = NO_REPLY
    monitor.run_checks()
    assert [a["host"] for a in alerts(db_path)] == ["1.1.1.1"]


def test_background_worker_raises_the_alert_once(one_target, db_path, fake_ping, fresh_worker, monkeypatch):
    sequence = iter([UP] + [NO_REPLY] * 50)
    monkeypatch.setattr(monitor, "ping_host",
                        lambda host: monitor.PingResult(*next(sequence, NO_REPLY)))
    monkeypatch.setattr(monitor, "get_interval_seconds", lambda: 0.02)
    fresh_worker.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sqlite3.connect(db_path) as db:
            if db.execute("SELECT COUNT(*) FROM checks").fetchone()[0] >= 10:
                break
        time.sleep(0.02)
    fresh_worker.stop()
    assert [a["kind"] for a in alerts(db_path)] == ["down"]


# --- API ------------------------------------------------------------------

def test_status_lists_active_alerts(one_target, client, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY)
    active = client.get("/api/status").get_json()["active_alerts"]
    assert len(active) == 1 and active[0]["host"] == "8.8.8.8"


def test_recovered_alert_stays_active_until_dismissed(one_target, client, fake_ping):
    """So you still see an outage that happened while you were away."""
    run_sequence(fake_ping, UP, NO_REPLY, UP)
    [active] = client.get("/api/status").get_json()["active_alerts"]
    assert active["resolved_at"] is not None


def test_dismissing_an_alert(one_target, client, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY)
    alert_id = client.get("/api/alerts").get_json()["active"][0]["id"]

    response = client.post(f"/api/alerts/{alert_id}/acknowledge")
    assert response.status_code == 200
    assert client.get("/api/status").get_json()["active_alerts"] == []

    # Still in the log, with the time it was dismissed.
    [event] = client.get("/api/alerts").get_json()["events"]
    assert event["acknowledged_at"] is not None
    # Dismissing again is harmless.
    assert client.post(f"/api/alerts/{alert_id}/acknowledge").status_code == 200


def test_dismissing_does_not_cause_a_new_alert(one_target, client, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY)
    client.post("/api/alerts/acknowledge-all")
    run_sequence(fake_ping, NO_REPLY, NO_REPLY)
    assert client.get("/api/status").get_json()["active_alerts"] == []


def test_dismiss_all(client, fake_ping):
    run_sequence(fake_ping, UP)
    for host in ("localhost", "1.1.1.1", "8.8.8.8"):
        fake_ping.results[host] = NO_REPLY
    monitor.run_checks()
    response = client.post("/api/alerts/acknowledge-all").get_json()
    assert response["dismissed"] == 3
    assert client.get("/api/alerts").get_json()["active"] == []


def test_dismiss_unknown_alert_returns_404(client):
    assert client.post("/api/alerts/999/acknowledge").status_code == 404


def test_dismiss_is_blocked_cross_site(one_target, client, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY)
    response = client.post("/api/alerts/acknowledge-all", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert len(client.get("/api/alerts").get_json()["active"]) == 1


def test_alert_log_is_newest_first_and_limited(one_target, client, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY, UP, NO_REPLY, UP)
    data = client.get("/api/alerts?limit=3").get_json()
    assert [e["kind"] for e in data["events"]] == ["recovered", "down", "recovered"]
    assert client.get("/api/alerts?limit=0").status_code == 400
    assert client.get("/api/alerts?limit=abc").status_code == 400


def test_removing_a_target_removes_its_alerts(one_target, client, db_path, fake_ping):
    run_sequence(fake_ping, UP, NO_REPLY)
    client.delete(f"/api/targets/{one_target}")
    assert alerts(db_path) == []


# --- Storage --------------------------------------------------------------

def test_upgrading_an_existing_database_adds_alerts_and_keeps_data(db_path, fake_ping):
    monitor.run_checks()
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE alerts")  # simulate a database from Phase 3
        checks_before = db.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
    monitor.init_db()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == checks_before
        assert db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_old_finished_alerts_are_pruned_but_undismissed_ones_are_kept(db_path):
    old = monitor.to_iso(monitor.utc_now() - timedelta(days=monitor.RETENTION_DAYS + 1))
    with sqlite3.connect(db_path) as db:
        insert = ("INSERT INTO alerts (target_id, host, label, kind, status, created_at, acknowledged_at) "
                  "VALUES (3, '8.8.8.8', 'Google DNS', ?, 'no_reply', ?, ?)")
        db.execute(insert, ("down", old, old))        # old and dismissed: removed
        db.execute(insert, ("recovered", old, None))  # old recovery event: removed
        db.execute(insert, ("down", old, None))       # old but never dismissed: kept
    monitor.prune_old_records()
    remaining = alerts(db_path)
    assert len(remaining) == 1 and remaining[0]["acknowledged_at"] is None
