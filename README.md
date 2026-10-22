# Network Monitoring and Alerting System

A local network operations dashboard that checks whether hosts reply to ping, measures round-trip latency, keeps a timestamped history in SQLite, and raises an in-dashboard alert when a host that was replying stops replying.

Built with **Python, Flask, SQLite, HTML, CSS, and JavaScript**, using the operating system's `ping` command. It runs entirely on your own computer: no cloud hosting, paid services, API keys, or accounts.

> Only monitor systems you own or are authorized to test. This tool sends one ordinary ICMP echo request per host per check. It does not scan networks.

---

## Contents

- [Features](#features)
- [Setup on macOS](#setup-on-macos)
- [Using the dashboard](#using-the-dashboard)
- [How it works (architecture)](#how-it-works-architecture)
- [Networking concepts demonstrated](#networking-concepts-demonstrated)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Demonstrating the project in an interview](#demonstrating-the-project-in-an-interview)
- [Limitations](#limitations)
- [Future improvements](#future-improvements)
- [Resume bullet points](#resume-bullet-points)

---

## Features

**Monitoring**
- Background worker pings every enabled host automatically while the app runs.
- Check interval adjustable from **15 to 300 seconds** (default 60), saved between restarts.
- Hosts are pinged in parallel (up to 8 at once), so one unreachable host does not delay the rest.
- Check cycles never overlap, even if you press **Check now** during an automatic cycle.
- Each result is classified as **Online**, **No reply**, **DNS failed**, or **Check error**, and saved with a UTC timestamp.
- **24-hour reply rate** per host, calculated only from checks that were actually recorded.

**Dashboard**
- Dark, responsive layout for desktop and mobile. On phones, the hosts table becomes stacked cards.
- Summary cards: monitored hosts, online, no ping reply (with a breakdown), and average latency.
- Add, pause, resume, and remove hosts.
- Recent-check history with a per-host filter.
- Loading, empty, and error states, including a banner that stays at the top of the screen if the server stops responding.

**Alerts**
- An alert appears when a host that was replying stops replying. It is recorded **once**, not on every failed check.
- A "replying again" event is recorded when the host recovers.
- Alerts stay on screen until dismissed, so you still see outages that happened while you were away.
- The alert count appears in the browser tab title, e.g. `(1) Network Monitor`.
- An alert log keeps the history with timestamps.

**Safety**
- The server listens only on `127.0.0.1`, so other devices on your network cannot reach it.
- Host input is strictly validated, so values like `-c 99999` cannot be passed to `ping` as command options.
- Requests from other websites (cross-site request forgery) and DNS-rebinding tricks are rejected.

---

## Setup on macOS

### Requirements

| Requirement | Notes |
|---|---|
| macOS | Developed and tested on macOS 26 (Apple Silicon). |
| Python 3.9 or newer | Tested with Python **3.9.6** (the version built into macOS at `/usr/bin/python3`) and **3.13.9** (from python.org). |
| `ping` | Built into macOS at `/sbin/ping`. No admin rights (`sudo`) needed. |
| A web browser | Tested in Google Chrome. |

Python packages (installed in step 2):

| File | Packages | Purpose |
|---|---|---|
| `requirements.txt` | Flask 3.x | Web server and API |
| `requirements-dev.txt` | the above, plus pytest | Automated tests |

SQLite is built into Python, so there is no database to install.

### 1. Get the code

```bash
git clone https://github.com/pratkcode/Network-Monitor-System.git
cd Network-Monitor-System
```

(If you already have the folder, just `cd` into it.)

### 2. Create a virtual environment and install packages

A virtual environment (`.venv`) keeps this project's packages separate from the rest of your Mac.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

You only need to do this once. After that, run `source .venv/bin/activate` each time you open a new Terminal window.

### 3. Start the app

```bash
python app.py
```

You should see:

```
Network Monitor running at http://127.0.0.1:5050
Checking enabled targets every 60 seconds. Press Ctrl+C to stop.
```

Open **http://127.0.0.1:5050** in your browser. Leave the Terminal window open while you use the dashboard. Press **Ctrl+C** in Terminal to stop the app.

On first start, the app creates `monitor.db` in the project folder with three example hosts: `localhost` (this Mac), `1.1.1.1` (Cloudflare DNS), and `8.8.8.8` (Google DNS). `monitor.db` is listed in `.gitignore`, so your monitoring data is never committed.

**Why port 5050?** On recent macOS versions, the AirPlay Receiver already listens on port 5000. To use a different port: `MONITOR_PORT=5051 python app.py`.

---

## Using the dashboard

| Task | How |
|---|---|
| Add a host | Enter a hostname (`example.com`) or IPv4 address (`192.168.1.1`) and an optional display name, then **Add host**. It is checked immediately. |
| Pause / resume | **Pause** stops checks for that host (no history or alerts while paused). **Resume** starts again. |
| Remove | **Remove** deletes the host, its check history, and its alerts, after a confirmation. |
| Change interval | Enter 15–300 in **Check every … sec** and press **Save**. It takes effect immediately. |
| Check right away | **Check now** runs an extra check cycle in addition to the automatic ones. |
| Filter history | Pick a host in the **Recent checks** drop-down. |
| Dismiss alerts | **Dismiss** on an alert, or **Dismiss all** when there are several. They stay in the alert log. |

**What the statuses mean**

| Status | Meaning |
|---|---|
| 🟢 Online | An ICMP echo reply came back. |
| 🟠 No reply | No reply arrived in time. The host may be offline, **or** a firewall may be blocking ping while the host works normally. Amber, not red, for that reason. |
| 🔴 DNS failed | The hostname could not be turned into an IP address, so no ping was sent. |
| 🔴 Check error | The `ping` command itself could not run. |

---

## How it works (architecture)

```
┌──────────────────────────┐        HTTP (JSON)        ┌──────────────────────────────────────┐
│ Browser                  │  ───────────────────────► │ Flask app  (app.py, 127.0.0.1:5050)  │
│ templates/index.html     │  every 5 s: read status,  │                                      │
│ static/app.js, style.css │  history, alerts          │  API routes ──read/write──┐          │
│                          │  buttons: add, pause,     │                           ▼          │
│                          │  remove, interval, check  │                  ┌─────────────────┐ │
└──────────────────────────┘                           │                  │ SQLite          │ │
                                                       │                  │ monitor.db      │ │
                                                       │                  └─────────────────┘ │
                                                       │                           ▲ save     │
                                                       │  MonitorWorker thread ────┘ results  │
                                                       │  every N seconds:                    │
                                                       │   run_checks() → ping in parallel    │
                                                       │   detect_alert() → record changes    │
                                                       └───────────────┬──────────────────────┘
                                                                       │ subprocess
                                                                       ▼
                                                          /sbin/ping -c 1 -W 1500 -t 3 <host>
```

### One check cycle

1. `MonitorWorker` wakes up. One worker thread per app; a guard makes a second start impossible.
2. `run_checks()` takes a lock, so two cycles can never overlap.
3. Each enabled host is pinged once, up to 8 at a time, using the system command `ping -c 1 -W 1500 -t 3 <host>`:
   - `-c 1`: send one packet
   - `-W 1500`: wait up to 1500 ms for the reply (milliseconds on macOS)
   - `-t 3`: give up after 3 seconds in total
4. The output and exit code are classified:
   - exit code `0` → **Online**, with latency read from `time=… ms`
   - "cannot resolve" in the output (macOS exit code 68) → **DNS failed**
   - anything else → **No reply**
5. Results are saved to SQLite. For each host, `detect_alert()` compares the new result with the previous one and records an alert only if the state changed.
6. Checks and dismissed alerts older than 7 days are deleted.
7. The worker waits until the next interval. Changing the interval wakes it up so the new value applies immediately.

The browser never pings anything. It re-reads saved results every 5 seconds and only redraws parts of the page that changed, so buttons are not replaced while you are clicking them.

### Files

```
app.py                 Flask app: validation, ping, worker, alerts, database, API
templates/index.html   Page layout
static/app.js          Fetches data from the API and renders it (no hardcoded data)
static/style.css       Dark theme; colors defined once as CSS variables
tests/                 pytest tests (temporary databases, fake ping)
requirements.txt       Flask
requirements-dev.txt   Flask + pytest
monitor.db             Created on first run; not committed to git
```

### Database tables

| Table | What it stores |
|---|---|
| `targets` | Host, display name, enabled/paused, created time |
| `checks` | One row per ping: target, UTC time, status, latency (ms), detail text |
| `alerts` | `down` and `recovered` events: host/name snapshot, time, last reply time, resolved time, dismissed time |
| `settings` | The check interval |

Starting the app upgrades databases created by older versions of this project in place, keeping existing targets and history.

### API

All responses are JSON.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Hosts with latest result and 24 h reply rate, summary, active alerts, worker state |
| GET | `/api/history?target_id=&limit=` | Recent checks, newest first (limit 1–500, default 100) |
| POST | `/api/checks` | Run a check cycle now (409 if one is already running) |
| GET / PUT | `/api/settings` | Read or change `interval_seconds` (15–300) |
| POST | `/api/targets` | Add a host: `{"host": "...", "label": "..."}` |
| POST | `/api/targets/<id>/toggle` | Pause or resume |
| DELETE | `/api/targets/<id>` | Remove a host with its history and alerts |
| GET | `/api/alerts?limit=` | Active alerts and the alert log |
| POST | `/api/alerts/<id>/acknowledge` | Dismiss one alert |
| POST | `/api/alerts/acknowledge-all` | Dismiss all alerts |

### Settings in `app.py`

| Constant | Default | Meaning |
|---|---|---|
| `DEFAULT_INTERVAL_SECONDS` | 60 | Interval for a new database |
| `MAX_PARALLEL_PINGS` | 8 | Hosts pinged at the same time |
| `RETENTION_DAYS` | 7 | How long checks and dismissed alerts are kept |
| `AVAILABILITY_WINDOW_HOURS` | 24 | Period used for the reply rate |
| `ALERT_AFTER_FAILURES` | 1 | Missed replies in a row before an alert. Set to 2 to ignore a single lost packet, at the cost of alerting one interval later. |

### What the worker does on macOS

- It runs only while `app.py` is running. Closing the browser tab does not stop it; **Ctrl+C** does.
- While the Mac is asleep, no checks run. That time appears as a gap in the history, not as downtime. After waking, the next check should come within one interval (this hasn't been tested with a real sleep/wake cycle).
- Starting a second copy on the same port exits with a "Port 5050 is already in use" message, so you cannot accidentally run two workers.

---

## Networking concepts demonstrated

### ICMP and ping

`ping` sends an **ICMP Echo Request** (type 8) and waits for an **Echo Reply** (type 0). ICMP (Internet Control Message Protocol) works at the network layer alongside IP. It has no ports, unlike TCP and UDP. A reply proves the host's IP stack is reachable from your Mac along that network path. It does **not** prove that a website, database, or other service on that host is working.

### Why "no reply" is not proof a host is offline

Many firewalls, cloud providers, and operating systems drop ICMP on purpose. macOS's own firewall has a "stealth mode" that does this. A host can serve web pages normally while ignoring every ping. That is why this project says **No reply** (shown in amber) instead of "Down", and why the dashboard explains the difference.

### Latency (round-trip time)

The latency shown is the **round-trip time (RTT)** reported by `ping`: how long a packet took to reach the host and for the reply to come back, in milliseconds. Typical values seen while building this:

- `localhost`: under 0.2 ms (the packet never leaves the computer)
- public DNS servers: single-digit milliseconds over home Wi-Fi

RTT depends on distance, the number of router hops, Wi-Fi quality, and congestion, so it varies from check to check. The **average latency** card averages each online host's most recent RTT.

### Packet loss, and what this tool can and cannot measure

Each check sends **one** packet. So a single check cannot tell "the host is down" apart from "one packet was lost". The **reply rate** (replies ÷ checks recorded in the last 24 hours) is an estimate of reachability over many samples. It is not a true packet-loss measurement. Limitations:

- **Gaps are not counted.** Time when the app was closed, the Mac was asleep, or the host was paused is not counted at all.
- **Small samples are noisy.** 1 failure in 4 checks shows 75%, so the dashboard shows the check count next to every percentage.
- **ICMP isn't treated like normal traffic.** Routers may rate-limit or de-prioritize ICMP, so ping results can be worse than real application traffic.
- **Short outages can be missed.** An outage shorter than the interval can fall between two checks.

### DNS names vs IP addresses

When you add a hostname such as `example.com`, `ping` asks the system's DNS resolver to turn it into an IP address **on every check**. If that lookup fails, nothing is pinged, and the result is **DNS failed**, a different problem from "no reply". Monitoring an IP address directly skips DNS. That is more stable, but it won't follow the service if its IP address changes. Monitoring a hostname follows IP changes, but now depends on DNS working too.

Two names in the demo are reserved by internet standards for exactly this kind of testing:

- `192.0.2.1` is in **TEST-NET-1** (RFC 5737), a range reserved for documentation that is never routed on the internet, so it never replies.
- Names ending in `.invalid` (RFC 6761) are guaranteed never to resolve.

### Monitoring intervals

A shorter interval detects problems faster but sends more traffic and stores more rows. A longer interval is lighter but slower to notice problems and more likely to miss short outages. At 60 seconds, an outage is noticed within about a minute, and outages shorter than a minute may never be seen. The allowed range of 15–300 seconds keeps checks reasonably frequent without flooding a host with pings.

### Loopback and binding to 127.0.0.1

`127.0.0.1` is the **loopback** address: traffic to it never leaves the computer. Binding the web server to it means only programs on this Mac can open the dashboard. Note that `localhost` resolves to the IPv6 loopback `::1` first on macOS, which is why `localhost:5000` reached AirPlay instead of this app. That is one reason the app uses port 5050.

---

## Testing

### Automated tests

```bash
source .venv/bin/activate
python -m pytest -v
```

Expected result: `122 passed`. The tests:

- **Don't touch your data.** Each test uses a fresh temporary database, never your `monitor.db`.
- **Run offline.** They mostly replace `ping` with a fake, so they are fast and don't need the internet. One test sends a real ping to `127.0.0.1`.
- **Cover:**
  - host validation, including command-option injection like `-c 99999`
  - reading macOS ping output and exit codes
  - every API route
  - cross-site and DNS-rebinding protection
  - the worker: a single instance, the interval, waking on change, surviving errors
  - non-overlapping checks and parallel pings
  - reply-rate math
  - database upgrades and retention
  - alert rules: one alert per outage, no alert for hosts that never replied, recovery, dismissing

### Manual checks

1. Start the app and confirm the three example hosts show **Online** with latencies.
2. In Terminal, run `ping -c 1 1.1.1.1` and compare the `time=` value with the dashboard.
3. Add `192.0.2.1` and confirm **No reply**. Add `no-such-host.invalid` and confirm **DNS failed**.
4. Try adding `-c 5` and confirm it is rejected with a message under the field.
5. Set the interval to 15 and watch **Last checked** update without pressing anything.
6. Pause and resume a host, then remove one.
7. Stop the app (Ctrl+C) and confirm the dashboard shows the "Cannot reach the monitoring server" banner. Start it again and confirm the banner clears, then check that your hosts and history are still there.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Port 5050 is already in use` | Another copy is probably already running in another Terminal window. Stop it with Ctrl+C, or use `MONITOR_PORT=5051 python app.py`. Use `lsof -nP -iTCP:5050 -sTCP:LISTEN` to see what's using the port. |
| Browser shows **403 Forbidden** at `localhost:5000` | That's macOS AirPlay Receiver, not this app. Use http://127.0.0.1:5050. |
| `ModuleNotFoundError: No module named 'flask'` | The virtual environment isn't active. Run `source .venv/bin/activate`, then try again. |
| `python3: command not found` | Install Python from python.org, or run `xcode-select --install`. |
| Red banner: "Cannot reach the monitoring server" | `app.py` stopped or crashed. Look at its Terminal window, then start it again. The page reconnects by itself. |
| Banner: "The requested URL was not found on the server" right after updating the code | The running `app.py` is older than the page files. Restart it (Ctrl+C, then `python app.py`). |
| Every host except `localhost` shows **No reply** | Your Mac has no working network path: check Wi-Fi, VPN, or captive portal (hotel/campus login page). `localhost` still replies because loopback doesn't use the network. |
| A host shows **No reply** but its website works | It probably blocks ICMP. That's expected; see [Why "no reply" is not proof](#why-no-reply-is-not-proof-a-host-is-offline). |
| **DNS failed** | Check the spelling. Run `ping <name>` in Terminal; `cannot resolve` confirms a DNS problem. |
| "Automatic monitoring is not running" | Restart `app.py`. Any error is printed in its Terminal window. |
| Start over with an empty database | Stop the app, then delete `monitor.db`. **This permanently deletes all hosts, history, and alerts.** It is recreated with the example hosts on next start. |

---

## Demonstrating the project in an interview

About 5–7 minutes. Rehearse once beforehand, especially step 5.

**Before the interview:** start the app, set the interval to **15 seconds**, and let it run for a few minutes so there is history and a reply rate.

1. **The goal (30 s).** "A local monitoring tool that shows which hosts reply to ping, how fast, how reliably over 24 hours, and alerts me when something that was working stops, without paid services."
2. **Dashboard tour (1 min).** Summary cards, hosts table, reply rate with check counts, history filter, and the worker status ("Last cycle 5s ago · next in 10s").
3. **Prove it's real (1 min).** In Terminal, run `ping -c 1 1.1.1.1` and point out that the `time=` value is close to what the dashboard shows.
4. **Two kinds of failure (1–2 min).**
   - Add `192.0.2.1`: **No reply**. Explain TEST-NET, and why it's amber rather than red: a firewall dropping ICMP looks identical.
   - Add `no-such-host.invalid`: **DNS failed**. Explain that the name never became an IP, so no packet was sent.
5. **A real alert (2 min).**
   - Turn off Wi-Fi. Within one interval, `1.1.1.1` and `8.8.8.8` stop replying and alerts appear (tab title becomes "(2) Network Monitor"), while `localhost` stays Online because loopback doesn't use the network.
   - Wait one more interval to show the alert isn't repeated.
   - Turn Wi-Fi back on and show "Replying again" and the alert log.
   - Note: the alert logic is covered by automated tests, but try this Wi-Fi demo yourself before the interview to see exactly how your network behaves.
6. **Show the code (1–2 min).**
   - `validate_host()`: why `-c 99999` must never reach `ping`.
   - `detect_alert()`: alert on change, not on every failure.
   - `MonitorWorker`: one thread, a lock to prevent overlapping cycles.
   - Run `python -m pytest -q`.

**Questions you may be asked**

- *Why not use a TCP check instead?* ICMP shows the host is reachable at the network layer. A TCP connect to a port (e.g. 443) shows a specific service is accepting connections. They answer different questions; TCP checks are listed as a future improvement.
- *Why one packet per check?* It keeps checks quick and light. The trade-off is that one check can't measure packet loss; the 24 h reply rate gives a rough picture over many checks instead.
- *What happens if the Mac sleeps?* No checks run. The gap is not counted as downtime, because nothing was measured.
- *Why SQLite?* It needs no server, is built into Python, and handles this amount of data easily. A multi-user deployment would need a database server.
- *How do you avoid alert spam?* Alerts fire only on a change from replying to not replying, and `ALERT_AFTER_FAILURES` can require several failures in a row.

---

## Limitations

- **ICMP only.** It shows network reachability, not whether a website or other service works. Hosts that block ping always show "No reply".
- **One packet per check.** It can't distinguish a lost packet from an outage, or measure true packet loss or jitter.
- **Reply rate counts only recorded checks.** Time when the app was closed, the Mac was asleep, or a host was paused is excluded. Outages shorter than the interval can be missed.
- **Runs only while `app.py` is open.** There is no system service.
- **Dashboard-only alerts.** No email, SMS, or desktop notifications. You see alerts when you open the dashboard.
- **IPv4 only.** IPv6 addresses are rejected with a message.
- **Single user, local only.** There is no login, and the server is reachable only from this Mac. It uses Flask's built-in development server, which is fine for local use but not for production.
- **Limited history.** Results are kept for 7 days.
- **Tested environment.** Tested on macOS with Chrome. The `ping` options for Linux and Windows are in the code but have not been tested.

## Future improvements

- TCP port checks (e.g. "is port 443 accepting connections?") for authorized services
- Several packets per check to measure packet loss and jitter
- IPv6 support
- Latency charts over time
- Choose the reply-rate window (1 h / 24 h / 7 d) and the alert threshold in the dashboard
- Optional browser desktop notifications (free, built into browsers)
- CSV export of history and alerts
- Run as a background service with macOS `launchd`, so monitoring continues without an open Terminal
- Optional email alerts through a mail server you control

---

## Resume bullet points

These describe only features that are implemented and covered by tests:

**Network Monitoring and Alerting System** | Python, Flask, SQLite, JavaScript

- Built a local network monitoring dashboard in Python/Flask and SQLite that pings hosts on a user-configurable 15–300 second interval from a single background worker, stores timestamped ICMP results and latency, and reports 24-hour reply rates calculated from recorded checks.
- Implemented state-change alerting that records an event only when a host stops replying and when it recovers, and wrote 120+ pytest tests covering alert de-duplication, input validation that blocks command-option injection, and database upgrades.

Be ready to explain every word of these in an interview.
