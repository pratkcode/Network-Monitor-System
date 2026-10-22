// Network Monitor dashboard.
// Everything shown here comes from the Flask API; nothing is hardcoded.

const POLL_MS = 5000;   // how often to re-read saved results (this never pings)
const HISTORY_LIMIT = 100;

const $ = (id) => document.getElementById(id);

const state = {
  status: null,              // latest /api/status response
  lastRenderedTargets: "",   // used to skip re-rendering when nothing changed
  lastRenderedHistory: "",
  lastSuccessAt: null,       // when the server last answered
  connected: true,
  polling: false,
  pollAgain: false,          // something changed while a refresh was running
  pending: new Set(),        // target ids with an action in progress
  knownAlertIds: null,       // active alert ids seen on the previous refresh
  lastRenderedAlerts: "",
  lastRenderedAlertLog: "",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[char]));
}

// Fetch a URL and return its JSON body. Throws an Error with a readable
// message (and the HTTP status) if anything goes wrong.
async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    const error = new Error("Cannot reach the monitoring server. Is app.py still running?");
    error.offline = true;
    throw error;
  }
  let data = {};
  try { data = await response.json(); } catch { /* body was not JSON */ }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (HTTP ${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function sendJson(url, method, body) {
  return requestJson(url, {
    method,
    headers: {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function formatClock(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—"
    : date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

function formatDateTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—"
    : date.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
}

// Like formatDateTime, but with seconds (useful for short outages).
function formatDateTimeSeconds(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—"
    : date.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

// "just now", "42s ago", "5m ago", "3h ago", or a date for older times.
function timeAgo(iso) {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (Number.isNaN(seconds)) return "—";
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return formatDateTime(iso);
}

function timeUntil(iso) {
  const seconds = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (Number.isNaN(seconds)) return "";
  return seconds <= 0 ? "any moment" : `in ${seconds}s`;
}

// "45s", "12m", "3h 5m" between two timestamps.
function formatDuration(fromIso, toIso) {
  const seconds = Math.max(0, Math.round((new Date(toIso) - new Date(fromIso)) / 1000));
  if (Number.isNaN(seconds)) return "—";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

// A <time> element whose "x ago" text is refreshed every second by tick().
function relativeTime(iso) {
  if (!iso) return '<span class="muted">Not checked yet</span>';
  return `<time data-ago datetime="${escapeHtml(iso)}" title="${escapeHtml(formatDateTime(iso))}">${timeAgo(iso)}</time>`;
}

function formatLatency(ms) {
  if (ms == null) return "—";
  return `${ms < 10 ? ms.toFixed(2) : ms.toFixed(1)} ms`;
}

const STATUS_BADGES = {
  up:        ["status-up", "Online"],
  no_reply:  ["status-warn", "No reply"],
  dns_error: ["status-bad", "DNS failed"],
  error:     ["status-bad", "Check error"],
};

function statusBadge(status) {
  const [css, text] = STATUS_BADGES[status] || ["status-idle", "Unknown"];
  return `<span class="status ${css}">${text}</span>`;
}

// ---------------------------------------------------------------------------
// Messages: error banner and toast
// ---------------------------------------------------------------------------

function showBanner(message) {
  $("banner-text").textContent = message;
  $("banner").hidden = false;
}

function hideBanner() {
  $("banner").hidden = true;
}

let toastTimer;
function showToast(message, kind = "info") {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast toast-${kind}`;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4000);
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderCards(summary) {
  for (const id of ["card-total", "card-up", "card-down", "card-latency"]) {
    $(id).classList.remove("loading");
  }
  $("card-total").textContent = summary.total;
  $("card-up").textContent = summary.up;
  $("card-down").textContent = summary.down;
  $("card-latency").textContent = formatLatency(summary.average_latency_ms);

  const totalNotes = [];
  if (summary.paused) totalNotes.push(`${summary.paused} paused`);
  if (summary.not_checked) totalNotes.push(`${summary.not_checked} not checked yet`);
  $("card-total-note").textContent = totalNotes.length ? totalNotes.join(" · ") : "Enabled targets";

  const downNotes = [];
  if (summary.no_reply) downNotes.push(`${summary.no_reply} no reply`);
  if (summary.dns_error) downNotes.push(`${summary.dns_error} DNS failed`);
  if (summary.error) downNotes.push(`${summary.error} check error`);
  $("card-down-note").textContent = downNotes.length ? downNotes.join(" · ") : "Offline, or blocking ICMP";
}

function renderMonitorPill() {
  const pill = $("monitor-pill");
  const text = $("monitor-pill-text");
  const monitor = state.status?.monitor;
  if (!state.connected) {
    pill.className = "pill pill-bad"; text.textContent = "Server unreachable";
  } else if (!monitor) {
    pill.className = "pill pill-wait"; text.textContent = "Connecting…";
  } else if (!monitor.running) {
    pill.className = "pill pill-bad"; text.textContent = "Monitor stopped";
  } else if (monitor.checking_now) {
    pill.className = "pill pill-busy"; text.textContent = "Checking now…";
  } else {
    pill.className = "pill pill-up"; text.textContent = `Monitoring every ${monitor.interval_seconds}s`;
  }
}

function renderMonitorLine() {
  const monitor = state.status?.monitor;
  if (!monitor) return;
  let text;
  if (!monitor.running) {
    text = "Automatic monitoring is not running. Restart app.py to resume checks.";
  } else if (monitor.checking_now) {
    text = "A check cycle is running now…";
  } else {
    const parts = [];
    parts.push(monitor.last_cycle_finished_at
      ? `Last cycle ${timeAgo(monitor.last_cycle_finished_at)}` : "Waiting for first cycle");
    if (monitor.next_check_at) parts.push(`next ${timeUntil(monitor.next_check_at)}`);
    text = parts.join(" · ");
  }
  if (monitor.last_error) text += ` · Last cycle failed: ${monitor.last_error}`;
  $("monitor-status").textContent = text;
}

function rateCell(availability) {
  if (!availability || availability.percent == null) {
    return '<div class="rate-content"><span class="muted">—</span><div class="sub">No checks in last 24h</div></div>';
  }
  const pct = availability.percent;
  const tone = pct >= 99 ? "" : pct >= 90 ? "mid" : "low";
  return `<div class="rate-content">
    <span class="num">${pct}%</span>
    <div class="sub">${availability.replies}/${availability.checks} checks since ${escapeHtml(formatDateTime(availability.first_check_at))}</div>
    <div class="rate-bar" aria-hidden="true"><span class="${tone}" style="width:${Math.max(0, Math.min(100, pct))}%"></span></div>
  </div>`;
}

function targetRow(t) {
  const busy = state.pending.has(t.id);
  const status = !t.enabled ? '<span class="status status-idle">Paused</span>'
    : !t.status ? '<span class="status status-idle">Not checked yet</span>'
    : statusBadge(t.status);
  const showName = t.label !== t.host;
  return `<tr class="${t.enabled ? "" : "row-paused"}">
    <td class="cell-host">
      <div class="host-name">${escapeHtml(showName ? t.label : t.host)}</div>
      ${showName ? `<div class="host-address">${escapeHtml(t.host)}</div>` : ""}
      ${t.detail && t.enabled ? `<div class="sub">${escapeHtml(t.detail)}</div>` : ""}
    </td>
    <td data-label="Status">${status}</td>
    <td data-label="Latency" class="num">${formatLatency(t.latency_ms)}</td>
    <td data-label="Last checked">${relativeTime(t.checked_at)}</td>
    <td data-label="Reply rate (24h)" class="rate">${rateCell(t.availability)}</td>
    <td class="actions">
      <button type="button" class="btn btn-secondary btn-small" data-action="toggle" data-id="${t.id}" ${busy ? "disabled" : ""}
        aria-label="${t.enabled ? "Pause" : "Resume"} ${escapeHtml(t.label)}">${t.enabled ? "Pause" : "Resume"}</button>
      <button type="button" class="btn btn-danger btn-small" data-action="remove" data-id="${t.id}" ${busy ? "disabled" : ""}
        aria-label="Remove ${escapeHtml(t.label)}">Remove</button>
    </td>
  </tr>`;
}

function renderTargets(targets) {
  // Only rebuild the table when something changed, so buttons are not
  // replaced under the mouse while someone is clicking them.
  const key = JSON.stringify([targets, [...state.pending]]);
  if (key === state.lastRenderedTargets) return;
  state.lastRenderedTargets = key;

  $("targets").innerHTML = targets.length ? targets.map(targetRow).join("")
    : `<tr><td colspan="6" class="empty"><strong>No hosts yet</strong>Add a hostname or IP address below to start monitoring.</td></tr>`;
}

function renderHistoryFilter(targets) {
  const select = $("history-filter");
  const current = select.value;
  const options = ['<option value="">All hosts</option>'].concat(
    targets.map(t => `<option value="${t.id}">${escapeHtml(t.label)}</option>`));
  const html = options.join("");
  if (select.dataset.rendered !== html) {
    select.innerHTML = html;
    select.dataset.rendered = html;
    // Keep the selection, unless that host was removed.
    select.value = targets.some(t => String(t.id) === current) ? current : "";
  }
}

function renderHistory(data) {
  const key = JSON.stringify(data.checks);
  if (key === state.lastRenderedHistory) return;
  state.lastRenderedHistory = key;

  $("history-caption").textContent =
    `Newest first · up to ${data.limit} most recent · results are kept for ${data.retention_days} days`;
  if (!data.checks.length) {
    const filtered = $("history-filter").value !== "";
    $("history").innerHTML = `<tr><td colspan="4" class="empty"><strong>No checks yet</strong>${
      filtered ? "This host has not been checked yet." : "Results appear here after the first check cycle."}</td></tr>`;
    return;
  }
  $("history").innerHTML = data.checks.map(c => `<tr>
      <td class="num" title="${escapeHtml(formatDateTime(c.checked_at))}">${formatClock(c.checked_at)}</td>
      <td><div>${escapeHtml(c.label)}</div>${c.label !== c.host ? `<div class="host-address">${escapeHtml(c.host)}</div>` : ""}</td>
      <td title="${escapeHtml(c.detail)}">${statusBadge(c.status)}</td>
      <td class="num">${formatLatency(c.latency_ms)}</td>
    </tr>`).join("");
}

function hostName(item) {
  return item.label !== item.host ? `${item.label} (${item.host})` : item.host;
}

function alertItem(alert) {
  const failure = (STATUS_BADGES[alert.status] || [null, "No reply"])[1];
  const lastReply = alert.last_reply_at ? ` · last reply ${escapeHtml(formatDateTimeSeconds(alert.last_reply_at))}` : "";
  const stateLine = alert.resolved_at
    ? `✓ Replying again since ${escapeHtml(formatClock(alert.resolved_at))} (no reply for about ${formatDuration(alert.created_at, alert.resolved_at)})`
    : `Still not replying · started ${relativeTime(alert.created_at)}`;
  return `<li class="alert-item ${alert.resolved_at ? "resolved" : ""}">
    <div>
      <div class="alert-title">${escapeHtml(hostName(alert))} stopped replying to ping</div>
      <div class="alert-meta">${failure} at ${escapeHtml(formatDateTimeSeconds(alert.created_at))}${lastReply} · ${escapeHtml(alert.detail)}</div>
      <div class="alert-state">${stateLine}</div>
    </div>
    <button type="button" class="btn btn-secondary btn-small" data-alert-id="${alert.id}"
      aria-label="Dismiss alert for ${escapeHtml(alert.label)}">Dismiss</button>
  </li>`;
}

function renderAlerts(active) {
  // Pop up a toast for alerts that appeared since the last refresh.
  // (Not on first page load, so old alerts don't all pop up at once.)
  if (state.knownAlertIds) {
    const fresh = active.filter(a => !state.knownAlertIds.has(a.id));
    if (fresh.length) {
      const more = fresh.length > 1 ? ` (+${fresh.length - 1} more)` : "";
      showToast(`⚠ ${fresh[0].label} stopped replying to ping.${more}`, "warn");
    }
  }
  state.knownAlertIds = new Set(active.map(a => a.id));
  document.title = active.length ? `(${active.length}) Network Monitor` : "Network Monitor";

  const key = JSON.stringify(active);
  if (key === state.lastRenderedAlerts) return;
  state.lastRenderedAlerts = key;
  $("alerts").hidden = active.length === 0;
  $("alerts-dismiss-all").hidden = active.length < 2;
  $("alerts-title").textContent = active.length === 1 ? "Active alert" : `Active alerts (${active.length})`;
  $("alerts-list").innerHTML = active.map(alertItem).join("");
}

function renderAlertLog(data) {
  const key = JSON.stringify(data.events);
  if (key === state.lastRenderedAlertLog) return;
  state.lastRenderedAlertLog = key;
  if (!data.events.length) {
    $("alert-log").innerHTML = `<tr><td colspan="4" class="empty"><strong>No alerts yet</strong>An alert is recorded when a host that was replying stops replying.</td></tr>`;
    return;
  }
  $("alert-log").innerHTML = data.events.map(e => {
    const event = e.kind === "down"
      ? '<span class="event-down">Stopped replying</span>'
      : '<span class="event-recovered">Replying again</span>';
    const dismissed = e.acknowledged_at ? ` · dismissed ${escapeHtml(formatClock(e.acknowledged_at))}` : "";
    return `<tr>
      <td class="num">${escapeHtml(formatDateTimeSeconds(e.created_at))}</td>
      <td><div>${escapeHtml(e.label)}</div>${e.label !== e.host ? `<div class="host-address">${escapeHtml(e.host)}</div>` : ""}</td>
      <td>${event}</td>
      <td class="sub">${escapeHtml(e.detail)}${dismissed}</td>
    </tr>`;
  }).join("");
}

function renderInterval(monitor) {
  // Don't overwrite the box while the user is typing in it.
  if (document.activeElement !== $("interval")) $("interval").value = monitor.interval_seconds;
}

function renderStatus(data) {
  state.status = data;
  renderCards(data.summary);
  renderAlerts(data.active_alerts);
  renderTargets(data.targets);
  renderHistoryFilter(data.targets);
  renderInterval(data.monitor);
  renderMonitorPill();
  renderMonitorLine();
}

// Runs every second: refresh "x ago" labels without rebuilding the tables.
function tick() {
  for (const el of document.querySelectorAll("time[data-ago]")) {
    el.textContent = timeAgo(el.getAttribute("datetime"));
  }
  renderMonitorLine();
}

// ---------------------------------------------------------------------------
// Loading data
// ---------------------------------------------------------------------------

function historyUrl() {
  const target = $("history-filter").value;
  return `/api/history?limit=${HISTORY_LIMIT}${target ? `&target_id=${encodeURIComponent(target)}` : ""}`;
}

async function loadAlertLog() {
  renderAlertLog(await requestJson("/api/alerts?limit=50"));
}

async function loadHistory() {
  try {
    renderHistory(await requestJson(historyUrl()));
  } catch (error) {
    if (error.status === 404) {  // the selected host was just removed
      $("history-filter").value = "";
      renderHistory(await requestJson(historyUrl()));
    } else {
      throw error;
    }
  }
}

// Re-read saved results from the server. This never sends pings.
async function poll() {
  if (state.polling) {
    state.pollAgain = true;  // run once more when the current refresh ends
    return;
  }
  state.polling = true;
  try {
    const [status] = await Promise.all([requestJson("/api/status"), loadHistory(), loadAlertLog()]);
    renderStatus(status);
    if (!state.connected) showToast("Reconnected to the monitoring server.");
    state.connected = true;
    state.lastSuccessAt = new Date().toISOString();
    if ($("banner").dataset.reason === "offline") hideBanner();
  } catch (error) {
    state.connected = !error.offline;
    const since = state.lastSuccessAt ? ` Showing data from ${formatClock(state.lastSuccessAt)}.` : "";
    showBanner(`${error.message}${since} Retrying every ${POLL_MS / 1000} seconds.`);
    $("banner").dataset.reason = "offline";
    if (!state.status) showLoadError(error.message);
    renderMonitorPill();
  } finally {
    state.polling = false;
    if (state.pollAgain) {
      state.pollAgain = false;
      poll();
    }
  }
}

function showLoadError(message) {
  const html = (cols) => `<tr><td colspan="${cols}" class="empty"><strong>Could not load data</strong>${escapeHtml(message)}</td></tr>`;
  $("targets").innerHTML = html(6);
  $("history").innerHTML = html(4);
  $("alert-log").innerHTML = html(4);
  state.lastRenderedTargets = state.lastRenderedHistory = state.lastRenderedAlertLog = "";
}

// ---------------------------------------------------------------------------
// User actions
// ---------------------------------------------------------------------------

function reportActionError(error) {
  showBanner(error.message);
  $("banner").dataset.reason = "action";
}

async function checkNow({quiet = false} = {}) {
  const button = $("check-now");
  button.disabled = true;
  button.innerHTML = '<span class="spinner" aria-hidden="true"></span>Checking…';
  try {
    renderStatus(await sendJson("/api/checks", "POST"));
    await Promise.all([loadHistory(), loadAlertLog()]);
    if (!quiet) showToast("Check complete.");
  } catch (error) {
    if (error.status === 409) {
      if (!quiet) showToast("A check is already running. Results will appear shortly.");
    } else {
      reportActionError(error);
    }
  } finally {
    button.disabled = false;
    button.textContent = "Check now";
  }
}

async function saveInterval(event) {
  event.preventDefault();
  const input = $("interval");
  const seconds = Number(input.value);
  if (!Number.isInteger(seconds) || seconds < 15 || seconds > 300) {
    input.setAttribute("aria-invalid", "true");
    showToast("Choose a whole number of seconds from 15 to 300.", "error");
    return;
  }
  input.removeAttribute("aria-invalid");
  $("interval-save").disabled = true;
  try {
    const data = await sendJson("/api/settings", "PUT", {interval_seconds: seconds});
    input.blur();
    showToast(data.message);
    await poll();
  } catch (error) {
    reportActionError(error);
  } finally {
    $("interval-save").disabled = false;
  }
}

function setHostError(message) {
  const error = $("host-error");
  error.textContent = message || "";
  error.hidden = !message;
  if (message) $("host").setAttribute("aria-invalid", "true");
  else $("host").removeAttribute("aria-invalid");
}

async function addHost(event) {
  event.preventDefault();
  const host = $("host").value.trim();
  const label = $("label").value.trim();
  if (!host) {
    setHostError("Enter a hostname or IP address.");
    $("host").focus();
    return;
  }
  setHostError("");
  const button = $("add-submit");
  button.disabled = true;
  button.innerHTML = '<span class="spinner" aria-hidden="true"></span>Adding…';
  try {
    const data = await sendJson("/api/targets", "POST", {host, label});
    $("host").value = "";
    $("label").value = "";
    showToast(`Added ${data.host}. Checking it now…`);
    await poll();
    await checkNow({quiet: true});  // give the new host a result right away
  } catch (error) {
    if (error.status === 400 || error.status === 409) {
      setHostError(error.message);
      $("host").focus();
    } else {
      reportActionError(error);
    }
  } finally {
    button.disabled = false;
    button.textContent = "Add host";
  }
}

async function handleTargetAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = Number(button.dataset.id);
  const target = state.status?.targets.find(t => t.id === id);
  if (!target || state.pending.has(id)) return;

  if (button.dataset.action === "remove" &&
      !confirm(`Remove "${target.label}" and all of its saved check history and alerts?`)) {
    return;
  }

  state.pending.add(id);
  renderTargets(state.status.targets);  // show the buttons as disabled
  try {
    if (button.dataset.action === "toggle") {
      const data = await sendJson(`/api/targets/${id}/toggle`, "POST");
      showToast(`${target.label} ${data.enabled ? "resumed" : "paused"}.`);
    } else {
      await sendJson(`/api/targets/${id}`, "DELETE");
      showToast(`Removed ${target.label}.`);
    }
  } catch (error) {
    reportActionError(error);
  } finally {
    state.pending.delete(id);
    await poll();
  }
}

// ---------------------------------------------------------------------------
// Start up
// ---------------------------------------------------------------------------

$("check-now").addEventListener("click", () => checkNow());
$("interval-form").addEventListener("submit", saveInterval);
$("add-form").addEventListener("submit", addHost);
$("host").addEventListener("input", () => setHostError(""));
$("targets").addEventListener("click", handleTargetAction);
$("history-filter").addEventListener("change", () => {
  state.lastRenderedHistory = "";
  loadHistory().catch(reportActionError);
});
$("banner-close").addEventListener("click", hideBanner);
$("alerts-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-alert-id]");
  if (!button) return;
  button.disabled = true;
  try {
    await sendJson(`/api/alerts/${button.dataset.alertId}/acknowledge`, "POST");
  } catch (error) {
    reportActionError(error);
  }
  await poll();
});
$("alerts-dismiss-all").addEventListener("click", async () => {
  $("alerts-dismiss-all").disabled = true;
  try {
    const data = await sendJson("/api/alerts/acknowledge-all", "POST");
    showToast(data.message);
  } catch (error) {
    reportActionError(error);
  } finally {
    $("alerts-dismiss-all").disabled = false;
  }
  await poll();
});

poll();
setInterval(poll, POLL_MS);
setInterval(tick, 1000);
