# Network Monitoring and Alerting System

A free, local-first network monitoring dashboard built with Python, Flask, SQLite, HTML, CSS, and JavaScript.

## Features

- Checks whether configured hosts respond to ping.
- Shows current status and response latency.
- Saves check history in a local SQLite database.
- Adds, pauses, resumes, and removes monitoring targets.
- Displays recent checks and summary metrics.
- Works locally; no API keys, cloud hosting, or paid services required.

## Requirements

- Python 3.10 or newer
- `pip`
- A system with the `ping` command (included on macOS, Linux, and Windows)

## Run on macOS

Open Terminal and move into this project folder:

```bash
cd "Network Monitor System"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5050 in your browser. Stop the app with `Ctrl+C`.

The app uses port 5050 because macOS's AirPlay Receiver already uses port 5000. To use a different port: `MONITOR_PORT=5051 python app.py`.

## Run on Windows

```powershell
cd "Network Monitor System"
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5050.

## How it works

1. While `app.py` is running, one background worker thread pings every enabled target on a schedule (every 60 seconds by default; you can choose 15–300 seconds in the dashboard).
2. Each result (status, latency, UTC timestamp) is saved to `monitor.db` using SQLite. Results older than 7 days are deleted.
3. The dashboard reads the saved results from `/api/status` every 5 seconds. **Check now** runs an extra check cycle immediately.
4. The **Reply rate (24h)** column is the share of recorded checks in the last 24 hours that got a ping reply. Time when the app was closed, the Mac was asleep, or a target was paused is not counted.

Checks only run while `app.py` is running. Closing the browser tab does not stop them; stopping the app (`Ctrl+C`) does.

## Automated tests

```bash
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -v
```

The tests use a temporary database, so they never touch your real `monitor.db`. Most tests replace `ping` with a fake so they run offline; one test sends a real ping to `127.0.0.1`.

## Suggested manual tests

1. Check `localhost` and verify that it responds.
2. Add an authorized host or a known public DNS address.
3. Add an invalid or unreachable test address and observe the no-reply state.
4. Pause and resume a target.
5. Remove a target and verify that it disappears.
6. Restart the app and confirm that target configuration and history persist.
7. Compare ping output in Terminal with the dashboard's result.

A failed ping is not definitive proof that a host is offline; firewalls may block ICMP.

## Safety and scope

Only monitor systems you own or are authorized to test. This tool sends ordinary ICMP ping requests to the hosts you configure. It does not scan networks or attempt to bypass access controls.

The web server binds to `127.0.0.1`, so it is intended for use on your own computer.

## Future improvements

- Choose the availability time range (currently fixed at 24 hours).
- TCP port checks for authorized services.
- CSV export of monitoring history.
- Unit tests and a deployment guide.

## Resume wording

Use only after you have run and verified the project:

**Network Monitoring and Alerting System** | Python, Flask, SQLite, JavaScript
- Built a local network monitoring dashboard to track host reachability, response latency, and historical connectivity checks.
- Implemented a Flask API and SQLite database for managing monitoring targets and storing timestamped results.

Adjust the bullets to match the features you actually tested and understand.
