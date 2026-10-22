"""Tests for the Flask web routes, using Flask's built-in test client."""


def add(client, host, label="", **kwargs):
    return client.post("/api/targets", json={"host": host, "label": label}, **kwargs)


def target_by_host(client, host):
    data = client.post("/api/checks").get_json()
    return next(t for t in data["targets"] if t["host"] == host)


def test_dashboard_page_loads(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Network Monitor" in response.data


def test_check_now_runs_checks_and_returns_summary(client, fake_ping):
    fake_ping.results["8.8.8.8"] = ("no_reply", None, "No ping reply")
    fake_ping.results["1.1.1.1"] = ("up", 20.0, "Ping reply received")

    data = client.post("/api/checks").get_json()

    assert data["summary"] == {"total": 3, "up": 2, "down": 1, "average_latency_ms": 15.0}
    assert len(data["history"]) == 3
    by_host = {t["host"]: t for t in data["targets"]}
    assert by_host["8.8.8.8"]["status"] == "no_reply"
    assert by_host["1.1.1.1"]["latency_ms"] == 20.0


def test_add_target(client, fake_ping):
    response = add(client, "Router.Local", "Home router")
    assert response.status_code == 201
    assert response.get_json()["host"] == "router.local"
    assert target_by_host(client, "router.local")["label"] == "Home router"


def test_add_target_label_defaults_to_host(client, fake_ping):
    add(client, "example.com")
    assert target_by_host(client, "example.com")["label"] == "example.com"


def test_add_duplicate_target_is_rejected_even_with_different_case(client):
    assert add(client, "example.com").status_code == 201
    response = add(client, "EXAMPLE.com")
    assert response.status_code == 409
    assert "already" in response.get_json()["error"]


def test_add_rejects_option_injection(client, fake_ping):
    response = add(client, "-c 99999")
    assert response.status_code == 400
    client.post("/api/checks")
    assert all(not host.startswith("-") for host in fake_ping.calls)


def test_add_rejects_bad_input(client):
    assert add(client, "").status_code == 400
    assert add(client, "ok.example", "x" * 81).status_code == 400
    assert client.post("/api/targets", json=["not", "an", "object"]).status_code == 400
    assert client.post("/api/targets", data="host=x").status_code == 400


def test_toggle_pauses_and_resumes(client, fake_ping):
    target_id = target_by_host(client, "8.8.8.8")["id"]

    response = client.post(f"/api/targets/{target_id}/toggle")
    assert response.get_json()["enabled"] is False
    fake_ping.calls.clear()
    client.post("/api/checks")
    assert "8.8.8.8" not in fake_ping.calls

    response = client.post(f"/api/targets/{target_id}/toggle")
    assert response.get_json()["enabled"] is True


def test_toggle_missing_target_returns_404(client):
    response = client.post("/api/targets/9999/toggle")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Target not found."


def test_delete_removes_target_and_its_history(client, fake_ping):
    target_id = target_by_host(client, "8.8.8.8")["id"]  # also records a check

    assert client.delete(f"/api/targets/{target_id}").status_code == 200

    data = client.post("/api/checks").get_json()
    assert all(t["id"] != target_id for t in data["targets"])
    assert all(h["label"] != "Google DNS" for h in data["history"])
    assert client.delete(f"/api/targets/{target_id}").status_code == 404


def test_unknown_api_route_returns_json_error(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert "error" in response.get_json()


def test_cross_site_requests_are_blocked(client):
    response = client.post("/api/targets/1/toggle", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_same_origin_requests_are_allowed(client):
    response = client.post("/api/targets/1/toggle", headers={"Origin": "http://localhost"})
    assert response.status_code == 200


def test_dns_rebinding_host_header_is_blocked(client):
    response = client.get("/api/status", headers={"Host": "attacker.example:5000"})
    assert response.status_code == 403


def test_127_0_0_1_host_header_is_allowed(client, fake_ping):
    response = client.get("/api/status", headers={"Host": "127.0.0.1:5000"})
    assert response.status_code == 200
