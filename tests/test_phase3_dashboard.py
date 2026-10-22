"""Phase 3 tests: history endpoint, summary breakdown, and the dashboard page."""
import re
import sqlite3

import app as monitor


def test_history_is_newest_first_and_includes_details(client, fake_ping):
    fake_ping.results["8.8.8.8"] = ("no_reply", None, "No ping reply")
    client.post("/api/checks")
    client.post("/api/checks")

    data = client.get("/api/history").get_json()
    checks = data["checks"]
    assert len(checks) == 6
    times = [c["checked_at"] for c in checks]
    assert times == sorted(times, reverse=True)
    assert {"label", "host", "status", "latency_ms", "detail", "checked_at"} <= set(checks[0])
    assert data["retention_days"] == monitor.RETENTION_DAYS


def test_history_can_be_filtered_by_host(client, fake_ping):
    client.post("/api/checks")
    client.post("/api/checks")
    checks = client.get("/api/history?target_id=2").get_json()["checks"]
    assert len(checks) == 2
    assert {c["host"] for c in checks} == {"1.1.1.1"}


def test_history_limit(client, fake_ping):
    for _ in range(4):
        client.post("/api/checks")
    assert len(client.get("/api/history?limit=5").get_json()["checks"]) == 5


def test_history_rejects_bad_parameters(client):
    for query in ("limit=0", "limit=501", "limit=abc", "target_id=abc", "target_id=-1"):
        response = client.get(f"/api/history?{query}")
        assert response.status_code == 400, query
        assert "error" in response.get_json()
    assert client.get("/api/history?target_id=999").status_code == 404


def test_history_is_empty_before_first_check(client):
    assert client.get("/api/history").get_json()["checks"] == []


def test_summary_breaks_down_each_kind_of_result(client, fake_ping, db_path):
    client.post("/api/targets", json={"host": "no-such-host.invalid"})
    client.post("/api/targets", json={"host": "new.example"})
    fake_ping.results["8.8.8.8"] = ("no_reply", None, "No ping reply")
    fake_ping.results["no-such-host.invalid"] = ("dns_error", None, "DNS lookup failed")
    fake_ping.results["localhost"] = ("up", 0.5, "Ping reply received")
    fake_ping.results["1.1.1.1"] = ("up", 9.5, "Ping reply received")
    client.post("/api/checks")
    # Pause one host, and add one that has never been checked.
    client.post("/api/targets/5/toggle")
    client.post("/api/targets", json={"host": "later.example"})

    summary = client.get("/api/status").get_json()["summary"]
    assert summary == {
        "total": 5, "up": 2, "down": 2, "no_reply": 1, "dns_error": 1, "error": 0,
        "not_checked": 1, "paused": 1, "average_latency_ms": 5.0,
    }


def test_summary_with_no_targets(client, db_path):
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM targets")
    summary = client.get("/api/status").get_json()["summary"]
    assert summary["total"] == 0 and summary["average_latency_ms"] is None


def test_dashboard_page_has_all_sections(client):
    html = client.get("/").get_data(as_text=True)
    for element_id in ("card-total", "card-up", "card-down", "card-latency", "targets",
                       "history", "history-filter", "add-form", "interval-form",
                       "check-now", "banner", "toast"):
        assert f'id="{element_id}"' in html, element_id


def test_dashboard_page_contains_no_hardcoded_monitoring_data(client):
    """Hosts, results, and numbers must come from the API, not the HTML."""
    html = client.get("/").get_data(as_text=True)
    for host, _label in monitor.DEFAULT_TARGETS:
        assert host not in html.replace("localhost:", "")
    assert not re.search(r"\d+(\.\d+)?\s*ms\b", html)  # no latency values
    assert not re.search(r"\d+(\.\d+)?%", html)         # no percentages


def test_javascript_has_no_inline_fake_data(client):
    js = client.get("/static/app.js").get_data(as_text=True)
    for host, _label in monitor.DEFAULT_TARGETS:
        assert host not in js
    assert "Math.random" not in js


def test_history_sorts_by_time_not_save_order(client, db_path):
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO checks (target_id, checked_at, is_up, status) "
                   "VALUES (1, '2026-10-22T10:00:05+00:00', 1, 'up')")
        db.execute("INSERT INTO checks (target_id, checked_at, is_up, status) "
                   "VALUES (2, '2026-10-22T10:00:01+00:00', 1, 'up')")
    times = [c["checked_at"] for c in client.get("/api/history").get_json()["checks"]]
    assert times == ["2026-10-22T10:00:05+00:00", "2026-10-22T10:00:01+00:00"]
