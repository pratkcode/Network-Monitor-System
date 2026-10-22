import ipaddress
import os
import platform
import re
import socket
import sqlite3
import subprocess
import sys
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

BASE_DIR = Path(__file__).resolve().parent

# The web server only listens on the loopback address, so other computers on
# your network cannot reach the dashboard.
HOST = "127.0.0.1"
# Port 5000 is used by the AirPlay Receiver on recent versions of macOS,
# so this app uses 5050 by default.
PORT = int(os.environ.get("MONITOR_PORT", "5050"))

# Browsers must use one of these names to reach the app. Rejecting other names
# blocks "DNS rebinding", where a malicious website points its own domain name
# at 127.0.0.1 to read data from apps running on your computer.
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost"}

# Monitoring settings
DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 15
MAX_INTERVAL_SECONDS = 300
MAX_PARALLEL_PINGS = 8          # how many hosts are pinged at the same time
RETENTION_DAYS = 7              # older check results are deleted
AVAILABILITY_WINDOW_HOURS = 24  # period used for the reply-rate percentage
MAX_HISTORY_ROWS = 500          # most rows the history view can request at once

# Possible results of one check
STATUS_UP = "up"                # an ICMP echo reply came back
STATUS_NO_REPLY = "no_reply"    # no reply: offline, OR ping is blocked
STATUS_DNS_ERROR = "dns_error"  # the hostname could not be turned into an IP
STATUS_ERROR = "error"          # ping itself could not run

PingResult = namedtuple("PingResult", ["status", "latency_ms", "detail"])

app = Flask(__name__)
app.config["DATABASE"] = os.environ.get("MONITOR_DB", str(BASE_DIR / "monitor.db"))
# Start the background monitor automatically. Tests turn this off.
app.config["MONITOR_AUTOSTART"] = True
check_lock = Lock()

# These are examples. You can add hosts you own or have permission to monitor.
DEFAULT_TARGETS = [
    ("localhost", "This Mac"),
    ("1.1.1.1", "Cloudflare DNS"),
    ("8.8.8.8", "Google DNS"),
]

# One DNS label: letters, digits, and hyphens, 1-63 characters,
# and it cannot start or end with a hyphen.
HOSTNAME_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def utc_now():
    return datetime.now(timezone.utc)


def to_iso(moment):
    # Always the same format (UTC, whole seconds) so timestamps stored as text
    # in SQLite sort and compare correctly.
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    """Open a database connection, commit on success, and always close it."""
    connection = sqlite3.connect(app.config["DATABASE"], timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    """Create or upgrade tables. Existing targets and history are kept."""
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                checked_at TEXT NOT NULL,
                is_up INTEGER NOT NULL,
                latency_ms REAL,
                detail TEXT,
                status TEXT,
                FOREIGN KEY(target_id) REFERENCES targets(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Upgrade databases created by older versions, which had no status
        # column. Fill it in from the old is_up / detail values.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(checks)")}
        if "status" not in columns:
            db.execute("ALTER TABLE checks ADD COLUMN status TEXT")
        db.execute("""
            UPDATE checks SET status = CASE
                WHEN is_up = 1 THEN 'up'
                WHEN detail LIKE 'DNS lookup failed%' THEN 'dns_error'
                ELSE 'no_reply'
            END
            WHERE status IS NULL
        """)

        db.execute("CREATE INDEX IF NOT EXISTS idx_checks_target ON checks(target_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_checks_time ON checks(checked_at)")
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('interval_seconds', ?)",
            (str(DEFAULT_INTERVAL_SECONDS),),
        )

        count = db.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
        if count == 0:
            now = to_iso(utc_now())
            db.executemany(
                "INSERT INTO targets (host, label, enabled, created_at) VALUES (?, ?, 1, ?)",
                [(host, label, now) for host, label in DEFAULT_TARGETS],
            )


def get_interval_seconds():
    """Read the monitoring interval, falling back to the default if needed."""
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key = 'interval_seconds'"
            ).fetchone()
        value = int(row["value"])
    except (sqlite3.Error, TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return min(MAX_INTERVAL_SECONDS, max(MIN_INTERVAL_SECONDS, value))


def set_interval_seconds(seconds):
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('interval_seconds', ?)",
            (str(seconds),),
        )


def prune_old_checks():
    """Delete check results older than RETENTION_DAYS."""
    cutoff = to_iso(utc_now() - timedelta(days=RETENTION_DAYS))
    with get_db() as db:
        db.execute("DELETE FROM checks WHERE checked_at < ?", (cutoff,))


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def validate_host(raw_host):
    """Return a cleaned hostname or IPv4 address, or raise ValueError.

    This matters for security: the value is passed to the ping command, so
    something like "-c 99999" must never be accepted as a "host".
    """
    host = str(raw_host or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("Enter a hostname or IP address.")
    if len(host) > 253:
        raise ValueError("Hostnames must be 253 characters or fewer.")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    if address is not None:
        if address.version == 6:
            raise ValueError("IPv6 addresses are not supported yet. Use an IPv4 address or hostname.")
        return str(address)

    labels = host.split(".")
    if not all(HOSTNAME_LABEL.match(label) for label in labels):
        raise ValueError(
            "Use a hostname (like example.com) or IPv4 address (like 192.168.1.1), "
            "without spaces, paths, or special characters."
        )
    # "1.1.1" looks like an IP but is not a valid one. ping would quietly
    # interpret it as 1.1.0.1, so reject it instead of guessing.
    if all(label.isdigit() for label in labels):
        raise ValueError("That is not a valid IPv4 address. Use four numbers from 0-255, like 192.168.1.1.")
    return host


def validate_interval(value):
    """Return the interval as an int, or raise ValueError."""
    # bool is a subclass of int in Python, so rule it out explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("The interval must be a whole number of seconds.")
    if not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS:
        raise ValueError(
            f"The interval must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS} seconds."
        )
    return value


# ---------------------------------------------------------------------------
# Pinging
# ---------------------------------------------------------------------------

def build_ping_command(host, system=None):
    """Build a one-packet ping command for the current operating system."""
    system = (system or platform.system()).lower()
    if system == "windows":
        # -n count, -w reply timeout in milliseconds
        return ["ping", "-n", "1", "-w", "1500", host]
    if system == "darwin":
        # macOS: -W reply wait in milliseconds, -t total time limit in seconds
        return ["ping", "-c", "1", "-W", "1500", "-t", "3", host]
    # Linux: -W reply wait in seconds
    return ["ping", "-c", "1", "-W", "2", host]


def parse_latency(output):
    """Find the round-trip time in ping output, e.g. 'time=12.3 ms'."""
    match = re.search(r"time[=<]\s*([\d.]+)\s*ms", output, re.IGNORECASE)
    return round(float(match.group(1)), 2) if match else None


DNS_FAILURE_PHRASES = ("cannot resolve", "unknown host", "could not find host",
                       "name or service not known")


def ping_host(host):
    """Send one ping and return a PingResult(status, latency_ms, detail).

    STATUS_NO_REPLY is NOT proof the host is offline: firewalls often block
    ping while the host keeps working normally.
    """
    command = build_ping_command(host)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return PingResult(STATUS_ERROR, None, "Ping command was not found on this system")
    except subprocess.TimeoutExpired:
        return PingResult(STATUS_NO_REPLY, None, "Ping timed out (no reply within 5 seconds)")
    except OSError as error:
        return PingResult(STATUS_ERROR, None, f"Could not run ping: {error}")

    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode == 0:
        latency = parse_latency(output)
        if latency is None:
            return PingResult(STATUS_UP, None, "Ping reply received (latency not reported)")
        return PingResult(STATUS_UP, latency, "Ping reply received")

    if any(phrase in output.lower() for phrase in DNS_FAILURE_PHRASES):
        return PingResult(STATUS_DNS_ERROR, None,
                          "DNS lookup failed: the hostname could not be resolved")
    return PingResult(STATUS_NO_REPLY, None,
                      "No ping reply (host may be offline or blocking ICMP)")


def check_target(target):
    """Ping one target and note the time the result came back."""
    result = ping_host(target["host"])
    return target, result, to_iso(utc_now())


def run_checks():
    """Ping every enabled target once and save the results.

    Returns the list of results, or None if another check was already running.
    Both the background worker and the "Check now" button use this function,
    and the lock guarantees that two check cycles never overlap.
    """
    if not check_lock.acquire(blocking=False):
        return None
    try:
        with get_db() as db:
            targets = [dict(row) for row in db.execute(
                "SELECT id, host, label FROM targets WHERE enabled = 1 ORDER BY id"
            )]
        if not targets:
            return []

        # Ping several hosts at the same time, so one unreachable host
        # (which takes ~3 seconds to give up) doesn't delay all the others.
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_PINGS, len(targets))) as pool:
            outcomes = list(pool.map(check_target, targets))

        results = []
        with get_db() as db:
            for target, result, checked_at in outcomes:
                # The target may have been removed while we were pinging it.
                if not db.execute("SELECT 1 FROM targets WHERE id = ?", (target["id"],)).fetchone():
                    continue
                db.execute(
                    """INSERT INTO checks
                       (target_id, checked_at, is_up, latency_ms, detail, status)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (target["id"], checked_at, int(result.status == STATUS_UP),
                     result.latency_ms, result.detail, result.status),
                )
                results.append({**target, **result._asdict(), "checked_at": checked_at})
        return results
    finally:
        check_lock.release()


# ---------------------------------------------------------------------------
# Background monitoring worker
# ---------------------------------------------------------------------------

class MonitorWorker:
    """Runs run_checks() every few seconds in one background thread.

    start() is safe to call many times: it only creates a thread if one is
    not already running, so there is never more than one worker per process.
    """

    def __init__(self):
        self._thread = None
        self._start_lock = Lock()
        self._stop_event = Event()
        self._wake_event = Event()
        self.last_cycle_started_at = None
        self.last_cycle_finished_at = None
        self.next_check_at = None
        self.last_error = None

    def start(self):
        with self._start_lock:
            if self.is_running():
                return False
            self._stop_event.clear()
            self._thread = Thread(target=self._run, name="monitor-worker", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=10):
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def wake(self):
        """Tell the worker the interval changed so it recalculates its wait."""
        self._wake_event.set()

    def run_cycle(self):
        self.last_cycle_started_at = to_iso(utc_now())
        try:
            run_checks()
            prune_old_checks()
            self.last_error = None
        except Exception as error:  # keep the worker alive after any error
            app.logger.exception("Monitoring cycle failed")
            self.last_error = str(error)
        self.last_cycle_finished_at = to_iso(utc_now())

    def _run(self):
        while not self._stop_event.is_set():
            cycle_started = time.monotonic()
            self.run_cycle()
            self._wait_until_next_cycle(cycle_started)

    def _wait_until_next_cycle(self, cycle_started):
        # Cycles start every <interval> seconds, counted from the START of the
        # previous cycle. If a cycle takes longer than the interval, the next
        # one starts right after it finishes (cycles never overlap).
        while not self._stop_event.is_set():
            remaining = cycle_started + get_interval_seconds() - time.monotonic()
            if remaining <= 0:
                return
            self.next_check_at = to_iso(utc_now() + timedelta(seconds=remaining))
            if not self._wake_event.wait(remaining):
                return  # waited the full interval
            self._wake_event.clear()  # interval changed (or stopping): recalculate

    def snapshot(self):
        return {
            "running": self.is_running(),
            "checking_now": check_lock.locked(),
            "interval_seconds": get_interval_seconds(),
            "last_cycle_started_at": self.last_cycle_started_at,
            "last_cycle_finished_at": self.last_cycle_finished_at,
            "next_check_at": self.next_check_at if self.is_running() else None,
            "last_error": self.last_error,
        }


monitor_worker = MonitorWorker()


# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------

def get_availability(db, window_hours=AVAILABILITY_WINDOW_HOURS):
    """Reply rate per target, from the checks actually recorded in the window.

    This only counts checks that really happened. Time when the app was not
    running, the Mac was asleep, or the target was paused is not included.
    """
    since = to_iso(utc_now() - timedelta(hours=window_hours))
    rows = db.execute("""
        SELECT target_id,
               COUNT(*) AS checks,
               SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) AS replies,
               MIN(checked_at) AS first_check_at
        FROM checks
        WHERE checked_at >= ?
        GROUP BY target_id
    """, (since,)).fetchall()
    return {
        row["target_id"]: {
            "percent": round(100 * row["replies"] / row["checks"], 1),
            "checks": row["checks"],
            "replies": row["replies"],
            "first_check_at": row["first_check_at"],
        }
        for row in rows
    }


def get_dashboard_data():
    with get_db() as db:
        targets = db.execute("""
            SELECT t.id, t.host, t.label, t.enabled,
                   c.checked_at, c.is_up, c.status, c.latency_ms, c.detail
            FROM targets t
            LEFT JOIN checks c ON c.id = (
                SELECT c2.id FROM checks c2
                WHERE c2.target_id = t.id
                ORDER BY c2.id DESC LIMIT 1
            )
            ORDER BY t.id
        """).fetchall()

        history = db.execute("""
            SELECT t.label, c.checked_at, c.is_up, c.status, c.latency_ms
            FROM checks c JOIN targets t ON t.id = c.target_id
            ORDER BY c.id DESC LIMIT 60
        """).fetchall()
        availability = get_availability(db)

    empty = {"percent": None, "checks": 0, "replies": 0, "first_check_at": None}
    rows = []
    for row in targets:
        item = dict(row)
        item["availability"] = availability.get(item["id"], empty)
        rows.append(item)

    enabled = [row for row in rows if row["enabled"]]
    checked = [row for row in enabled if row["status"] is not None]

    def count_status(status):
        return sum(1 for row in checked if row["status"] == status)

    up = count_status(STATUS_UP)
    # Average of each responding host's most recent latency.
    latencies = [row["latency_ms"] for row in checked
                 if row["status"] == STATUS_UP and row["latency_ms"] is not None]
    return {
        "targets": rows,
        "history": [dict(row) for row in reversed(history)],
        "summary": {
            "total": len(enabled),
            "up": up,
            "down": len(checked) - up,  # every checked host that did not reply
            "no_reply": count_status(STATUS_NO_REPLY),
            "dns_error": count_status(STATUS_DNS_ERROR),
            "error": count_status(STATUS_ERROR),
            "not_checked": len(enabled) - len(checked),
            "paused": len(rows) - len(enabled),
            "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        },
        "monitor": monitor_worker.snapshot(),
        "availability_window_hours": AVAILABILITY_WINDOW_HOURS,
        "retention_days": RETENTION_DAYS,
    }


# ---------------------------------------------------------------------------
# Request safety checks and error handling
# ---------------------------------------------------------------------------

@app.before_request
def block_untrusted_requests():
    # 1. Only answer requests addressed to this computer by a local name.
    if (request.host or "").rsplit(":", 1)[0].strip("[]") not in ALLOWED_HOSTNAMES:
        return jsonify({"error": "Requests must be sent to 127.0.0.1 or localhost."}), 403

    # 2. Block other websites from changing your targets (cross-site request
    #    forgery). Browsers attach an Origin header to these requests.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify({"error": "Cross-site requests are not allowed."}), 403
    return None


@app.before_request
def ensure_monitor_started():
    # Normally the worker is started in __main__ below. This also starts it
    # if the app was launched another way (for example "flask run"). In
    # Flask's debug reloader, only the child process that serves requests
    # gets here, so the reloader's watcher process never starts a worker.
    if app.config["MONITOR_AUTOSTART"]:
        monitor_worker.start()


@app.errorhandler(HTTPException)
def handle_http_error(error):
    # The dashboard expects JSON from /api routes, even for errors.
    if request.path.startswith("/api/"):
        return jsonify({"error": error.description}), error.code
    return error


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return handle_http_error(error)
    app.logger.exception("Unexpected error")
    return jsonify({"error": "Internal server error. Check the terminal for details."}), 500


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    # Read-only: returns the latest saved results. The background worker
    # does the pinging.
    return jsonify(get_dashboard_data())


@app.get("/api/history")
def api_history():
    """Recent check results, newest first. Optional: ?target_id=3&limit=100"""
    raw_limit = request.args.get("limit", "100")
    limit = int(raw_limit) if raw_limit.isdigit() else 0
    if not 1 <= limit <= MAX_HISTORY_ROWS:
        return jsonify({"error": f"limit must be a number from 1 to {MAX_HISTORY_ROWS}."}), 400

    target_id = request.args.get("target_id", default=None)
    sql = """
        SELECT c.id, c.target_id, t.label, t.host, c.checked_at,
               c.status, c.latency_ms, c.detail
        FROM checks c JOIN targets t ON t.id = c.target_id
    """
    params = []
    with get_db() as db:
        if target_id not in (None, ""):
            if not target_id.isdigit():
                return jsonify({"error": "target_id must be a number."}), 400
            if not db.execute("SELECT 1 FROM targets WHERE id = ?", (int(target_id),)).fetchone():
                return jsonify({"error": "Target not found."}), 404
            sql += " WHERE c.target_id = ?"
            params.append(int(target_id))
        # Sort by time. Pings run in parallel, so save order (id) is not
        # always the same as the order the results came back in.
        sql += " ORDER BY c.checked_at DESC, c.id DESC LIMIT ?"
        params.append(limit)
        rows = [dict(row) for row in db.execute(sql, params)]
    return jsonify({"checks": rows, "limit": limit, "retention_days": RETENTION_DAYS})


@app.post("/api/checks")
def api_check_now():
    """Run a check cycle immediately (the "Check now" button)."""
    if run_checks() is None:
        return jsonify({"error": "A check is already in progress. Results will appear shortly."}), 409
    return jsonify(get_dashboard_data())


@app.get("/api/settings")
def get_settings():
    return jsonify({
        "interval_seconds": get_interval_seconds(),
        "min_interval_seconds": MIN_INTERVAL_SECONDS,
        "max_interval_seconds": MAX_INTERVAL_SECONDS,
    })


@app.put("/api/settings")
def update_settings():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Send the settings as a JSON object."}), 400
    try:
        seconds = validate_interval(data.get("interval_seconds"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    set_interval_seconds(seconds)
    monitor_worker.wake()
    return jsonify({"message": f"Checking every {seconds} seconds.", "interval_seconds": seconds})


@app.post("/api/targets")
def add_target():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Send the target as a JSON object."}), 400

    try:
        host = validate_host(data.get("host"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    label = str(data.get("label") or "").strip() or host
    if len(label) > 80:
        return jsonify({"error": "Label must be 80 characters or fewer."}), 400

    try:
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO targets (host, label, enabled, created_at) VALUES (?, ?, 1, ?)",
                (host, label, to_iso(utc_now())),
            )
            target_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({"error": "That host is already on the list."}), 409
    return jsonify({"message": "Target added.", "id": target_id, "host": host}), 201


@app.delete("/api/targets/<int:target_id>")
def delete_target(target_id):
    with get_db() as db:
        db.execute("DELETE FROM checks WHERE target_id = ?", (target_id,))
        result = db.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        deleted = result.rowcount
    if deleted == 0:
        return jsonify({"error": "Target not found."}), 404
    return jsonify({"message": "Target removed."})


@app.post("/api/targets/<int:target_id>/toggle")
def toggle_target(target_id):
    with get_db() as db:
        row = db.execute("SELECT enabled FROM targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            return jsonify({"error": "Target not found."}), 404
        new_value = 0 if row["enabled"] else 1
        db.execute("UPDATE targets SET enabled = ? WHERE id = ?", (new_value, target_id))
    return jsonify({"message": "Target updated.", "enabled": bool(new_value)})


def port_is_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


if __name__ == "__main__":
    init_db()
    if not port_is_free(HOST, PORT):
        sys.exit(
            f"Port {PORT} is already in use. Stop the other program, or pick another port:\n"
            f"  MONITOR_PORT=5051 python app.py"
        )
    monitor_worker.start()
    print(f"Network Monitor running at http://{HOST}:{PORT}")
    print(f"Checking enabled targets every {get_interval_seconds()} seconds. Press Ctrl+C to stop.")
    # use_reloader=False: the reloader would run this file twice.
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
