"""Tests for host validation, the ping wrapper, and the database logic."""
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

import app as monitor

MAC_REPLY = """PING 127.0.0.1 (127.0.0.1): 56 data bytes
64 bytes from 127.0.0.1: icmp_seq=0 ttl=64 time=0.101 ms

--- 127.0.0.1 ping statistics ---
1 packets transmitted, 1 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 0.101/0.101/0.101/0.000 ms"""

MAC_NO_REPLY = """PING 192.0.2.1 (192.0.2.1): 56 data bytes

--- 192.0.2.1 ping statistics ---
1 packets transmitted, 0 packets received, 100.0% packet loss"""


# --- Host validation ------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("localhost", "localhost"),
    ("8.8.8.8", "8.8.8.8"),
    ("  example.com  ", "example.com"),
    ("Router.Home.ARPA", "router.home.arpa"),   # DNS names are case-insensitive
    ("example.com.", "example.com"),            # trailing dot removed
    ("my-server-01", "my-server-01"),
])
def test_validate_host_accepts_hostnames_and_ipv4(raw, expected):
    assert monitor.validate_host(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    None,
    "-c 99999",          # would be treated as a ping option
    "-fexample.com",
    "a b",
    "http://example.com",
    "example.com/path",
    "host;rm -rf /",
    "$(whoami)",
    "-bad.example.com",
    "bad-.example.com",
    "1.1.1",             # ping would silently read this as 1.1.0.1
    "999.1.1.1",
    "a" * 254,
])
def test_validate_host_rejects_unsafe_or_invalid_values(raw):
    with pytest.raises(ValueError):
        monitor.validate_host(raw)


def test_validate_host_explains_ipv6_is_not_supported():
    with pytest.raises(ValueError, match="IPv6"):
        monitor.validate_host("::1")


# --- Ping command ---------------------------------------------------------

def test_mac_ping_command_uses_milliseconds_and_puts_host_last():
    command = monitor.build_ping_command("example.com", system="Darwin")
    assert command == ["ping", "-c", "1", "-W", "1500", "-t", "3", "example.com"]


def test_linux_and_windows_ping_commands():
    assert monitor.build_ping_command("h", system="Linux") == ["ping", "-c", "1", "-W", "2", "h"]
    assert monitor.build_ping_command("h", system="Windows") == ["ping", "-n", "1", "-w", "1500", "h"]


def test_parse_latency_from_mac_and_windows_output():
    assert monitor.parse_latency(MAC_REPLY) == 0.1
    assert monitor.parse_latency("Reply from 1.1.1.1: bytes=32 time<1ms TTL=57") == 1.0
    assert monitor.parse_latency("no timing here") is None


def fake_run(returncode, stdout="", stderr=""):
    def _run(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return _run


def test_ping_reply(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run(0, MAC_REPLY))
    assert monitor.ping_host("127.0.0.1") == ("up", 0.1, "Ping reply received")


def test_ping_reply_without_time_does_not_invent_latency(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run(0, "1 packets received"))
    status, latency, _ = monitor.ping_host("127.0.0.1")
    assert status == "up" and latency is None


def test_ping_no_reply_is_not_called_offline(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run(2, MAC_NO_REPLY))
    status, latency, detail = monitor.ping_host("192.0.2.1")
    assert (status, latency) == ("no_reply", None)
    assert "No ping reply" in detail and "blocking ICMP" in detail


def test_ping_dns_failure_is_reported_separately(monkeypatch):
    # macOS exits with code 68 and prints this when DNS lookup fails.
    monkeypatch.setattr(subprocess, "run", fake_run(
        68, stderr="ping: cannot resolve no-such-host.invalid: Unknown host"))
    status, _, detail = monitor.ping_host("no-such-host.invalid")
    assert status == "dns_error" and detail.startswith("DNS lookup failed")


def test_ping_timeout_and_missing_command(monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ping", timeout=5)

    def raise_missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    assert monitor.ping_host("x")[:2] == ("no_reply", None)
    assert "timed out" in monitor.ping_host("x")[2]

    monkeypatch.setattr(subprocess, "run", raise_missing)
    assert monitor.ping_host("x").status == "error"
    assert "not found" in monitor.ping_host("x").detail


@pytest.mark.skipif(shutil.which("ping") is None, reason="ping is not installed")
def test_real_ping_to_loopback():
    """Sends one real ICMP echo to this computer (no internet needed)."""
    status, latency, detail = monitor.ping_host("127.0.0.1")
    assert status == "up", detail
    assert latency is not None and latency >= 0


# --- Database -------------------------------------------------------------

def count(db_path, sql):
    with sqlite3.connect(db_path) as db:
        return db.execute(sql).fetchone()[0]


def test_init_db_seeds_default_targets_once(db_path):
    assert count(db_path, "SELECT COUNT(*) FROM targets") == len(monitor.DEFAULT_TARGETS)
    monitor.init_db()
    assert count(db_path, "SELECT COUNT(*) FROM targets") == len(monitor.DEFAULT_TARGETS)


def test_restarting_preserves_targets_and_history(db_path, fake_ping):
    monitor.run_checks()
    monitor.run_checks()
    checks_before = count(db_path, "SELECT COUNT(*) FROM checks")
    assert checks_before == 2 * len(monitor.DEFAULT_TARGETS)

    monitor.init_db()  # simulates restarting the app
    assert count(db_path, "SELECT COUNT(*) FROM checks") == checks_before


def test_run_checks_saves_results_and_skips_paused_targets(db_path, fake_ping):
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE targets SET enabled = 0 WHERE host = '8.8.8.8'")
    fake_ping.results["1.1.1.1"] = ("no_reply", None, "No ping reply")

    results = monitor.run_checks()

    assert "8.8.8.8" not in fake_ping.calls
    assert {r["host"]: r["status"] for r in results} == {"localhost": "up", "1.1.1.1": "no_reply"}
    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT is_up, status, latency_ms, checked_at FROM checks ORDER BY id").fetchall()
    assert [r[:3] for r in rows] == [(1, "up", 10.0), (0, "no_reply", None)]
    assert all(r[3].endswith("+00:00") for r in rows)  # timestamps are UTC


def test_run_checks_does_not_overlap(db_path, fake_ping):
    monitor.check_lock.acquire()
    try:
        assert monitor.run_checks() is None
        assert fake_ping.calls == []
    finally:
        monitor.check_lock.release()


def test_server_binds_to_loopback_only():
    assert monitor.HOST == "127.0.0.1"
