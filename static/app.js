const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;   // how often the page re-reads saved results (no pinging)
let loading = false;
let checking = false;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[char]));
}
// Fetch a URL and return its JSON body. Throws a readable error if the
// request fails or the server sends back something that is not JSON.
async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error("Cannot reach the monitoring server. Is app.py still running?");
  }
  let data = {};
  try { data = await response.json(); } catch { /* non-JSON body */ }
  if (!response.ok) throw new Error(data.error || `Request failed (HTTP ${response.status}).`);
  return data;
}
function showError(message) {
  $("form-message").textContent = message;
  $("form-message").className = "form-message error";
}
function formatTime(value) {
  if (!value) return "Not checked yet";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"});
}
function formatDateTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
}
const STATUS_LABELS = {
  up: ['up', 'Online'],
  no_reply: ['down', 'No reply'],
  dns_error: ['down', 'DNS failed'],
  error: ['down', 'Check error'],
};
function statusBadge(status) {
  const [css, text] = STATUS_LABELS[status] || ['wait', 'Unknown'];
  return `<span class="status ${css}">${text}</span>`;
}
function availabilityCell(a) {
  if (!a || a.percent == null) return '—<div class="rate-detail">No checks yet</div>';
  return `${a.percent}%<div class="rate-detail">${a.replies}/${a.checks} checks since ${escapeHtml(formatDateTime(a.first_check_at))}</div>`;
}
function renderMonitor(monitor) {
  let text;
  if (!monitor.running) {
    text = "⚠ Automatic monitoring is not running. Restart app.py.";
  } else if (monitor.checking_now) {
    text = `Checking now… (automatic checks every ${monitor.interval_seconds}s)`;
  } else {
    const next = monitor.next_check_at ? ` · next ≈ ${formatTime(monitor.next_check_at)}` : "";
    const last = monitor.last_cycle_finished_at ? ` · last run ${formatTime(monitor.last_cycle_finished_at)}` : "";
    text = `Automatic checks every ${monitor.interval_seconds}s${last}${next}`;
  }
  if (monitor.last_error) text += ` · last cycle error: ${monitor.last_error}`;
  $("monitor-status").textContent = text;
  // Don't overwrite the box while the user is typing in it.
  if (document.activeElement !== $("interval")) $("interval").value = monitor.interval_seconds;
}
function render(data) {
  $("total").textContent = data.summary.total;
  $("up").textContent = data.summary.up;
  $("down").textContent = data.summary.down;
  $("latency").textContent = data.summary.average_latency_ms == null ? "—" : `${data.summary.average_latency_ms} ms`;
  renderMonitor(data.monitor);

  if (!data.targets.length) {
    $("targets").innerHTML = '<tr><td colspan="7" class="empty">No targets yet. Add a host below.</td></tr>';
  } else {
    $("targets").innerHTML = data.targets.map(t => {
      const status = !t.enabled ? '<span class="status wait">Paused</span>' :
        !t.status ? '<span class="status wait">Not checked</span>' : statusBadge(t.status);
      const latency = t.latency_ms == null ? "—" : `${t.latency_ms} ms`;
      const detail = t.detail ? `<div class="host-detail">${escapeHtml(t.detail)}</div>` : "";
      return `<tr>
        <td><div class="host-name">${escapeHtml(t.label)}</div>${detail}</td>
        <td>${escapeHtml(t.host)}</td><td>${status}</td><td>${latency}</td>
        <td>${formatTime(t.checked_at)}</td>
        <td>${availabilityCell(t.availability)}</td>
        <td><button class="small-btn" onclick="toggleTarget(${t.id})">${t.enabled ? "Pause" : "Resume"}</button>
        <button class="small-btn remove-btn" onclick="removeTarget(${t.id})">Remove</button></td>
      </tr>`;
    }).join("");
  }

  const history = data.history.slice().reverse();
  $("history").innerHTML = history.length ? history.map(item => {
    const latency = item.latency_ms == null ? "—" : `${item.latency_ms} ms`;
    return `<div class="history-row"><span class="history-name">${escapeHtml(item.label)}</span>${statusBadge(item.status)}<span class="history-latency">${latency}<br><span class="history-time">${formatTime(item.checked_at)}</span></span></div>`;
  }).join("") : '<p class="empty">History will appear after the first check.</p>';
}
// Re-read saved results. This never pings; the background worker does that.
async function refreshData() {
  if (loading) return;
  loading = true;
  try {
    render(await requestJson("/api/status"));
  } catch (error) {
    showError(error.message);
  } finally {
    loading = false;
  }
}
// "Check now": ask the server to run a check cycle immediately.
async function checkNow() {
  if (checking) return;
  checking = true;
  $("refresh").disabled = true;
  $("refresh").textContent = "Checking…";
  try {
    render(await requestJson("/api/checks", {method: "POST"}));
  } catch (error) {
    showError(error.message);
  } finally {
    checking = false;
    $("refresh").disabled = false;
    $("refresh").textContent = "Check now ↻";
  }
}
$("refresh").addEventListener("click", checkNow);
$("interval-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("form-message");
  try {
    const data = await requestJson("/api/settings", {
      method: "PUT",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({interval_seconds: Number($("interval").value)})
    });
    message.textContent = data.message;
    message.className = "form-message";
    $("interval").blur();
    await refreshData();
  } catch (error) {
    showError(error.message);
  }
});
$("add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("form-message");
  message.textContent = "";
  message.className = "form-message";
  const host = $("host").value.trim();
  const label = $("label").value.trim();
  try {
    await requestJson("/api/targets", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({host, label})
    });
    $("host").value = "";
    $("label").value = "";
    message.textContent = "Target added. It will be checked in the next cycle, or press Check now.";
    await refreshData();
  } catch (error) {
    message.textContent = error.message;
    message.className = "form-message error";
  }
});
async function removeTarget(id) {
  if (!confirm("Remove this target and its saved check history?")) return;
  try {
    await requestJson(`/api/targets/${id}`, {method:"DELETE"});
  } catch (error) {
    showError(error.message);
  }
  await refreshData();
}
async function toggleTarget(id) {
  try {
    await requestJson(`/api/targets/${id}/toggle`, {method:"POST"});
  } catch (error) {
    showError(error.message);
  }
  await refreshData();
}
refreshData();
setInterval(refreshData, POLL_MS);
