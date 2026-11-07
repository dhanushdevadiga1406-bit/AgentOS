const state = {
  conversationId: null,
  conversations: [],
  agents: [],
  sending: false,
  monitorSnapshot: null,
  websocketConnected: false,
  notificationsEnabled: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const messageList = $("#message-list");
const input = $("#chat-input");
let toastTimer;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status}).`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch { /* Keep the HTTP status as the visible error. */ }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), 3300);
}

function setView(name) {
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-item[data-view]").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  const titles = { chat: "Assistant", monitor: "System monitor", agents: "Agent network", tasks: "Active tasks", approvals: "Approval center", activity: "Activity history", settings: "Settings & privacy" };
  $("#page-title").textContent = titles[name] || "Assistant";
  $("#sidebar").classList.remove("open");
  if (name === "tasks") loadTasks();
  if (name === "approvals") loadApprovals();
  if (name === "activity") loadActivity();
  if (name === "monitor") loadMonitor();
}

function makeElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderMessage(role, content, isError = false) {
  const message = makeElement("article", `message ${role}${isError ? " message-error" : ""}`);
  const avatar = makeElement("div", "message-avatar", role === "user" ? "D" : "✳");
  avatar.setAttribute("aria-hidden", "true");
  const body = makeElement("div", "message-body");
  body.append(makeElement("div", "message-role", role === "user" ? "YOU" : "AGENTOS"));
  body.append(makeElement("div", "message-content", content));
  message.append(avatar, body);
  messageList.append(message);
  message.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function showTyping() {
  removeTyping();
  const message = makeElement("article", "message", undefined);
  message.id = "typing-indicator";
  const avatar = makeElement("div", "message-avatar", "✳");
  const body = makeElement("div", "message-body");
  body.append(makeElement("div", "message-role", "AGENTOS"));
  const dots = makeElement("span", "typing-dots");
  for (let i = 0; i < 3; i += 1) dots.append(makeElement("i"));
  body.append(dots);
  message.append(avatar, body);
  messageList.append(message);
}

function removeTyping() {
  $("#typing-indicator")?.remove();
}

async function loadConversations() {
  state.conversations = await api("/api/conversations");
  const list = $("#conversation-list");
  list.replaceChildren();
  if (!state.conversations.length) {
    list.append(makeElement("div", "history-empty", "Your recent chats will appear here."));
    return;
  }
  for (const conversation of state.conversations) {
    const row = makeElement("div", `conversation-item${conversation.id === state.conversationId ? " selected" : ""}`);
    const open = makeElement("button", "conversation-open", conversation.title);
    open.type = "button";
    open.title = conversation.title;
    open.addEventListener("click", () => selectConversation(conversation.id));
    const remove = makeElement("button", "icon-button conversation-delete", "×");
    remove.type = "button";
    remove.title = "Delete conversation";
    remove.setAttribute("aria-label", `Delete ${conversation.title}`);
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteConversation(conversation.id);
    });
    row.append(open, remove);
    list.append(row);
  }
}

async function selectConversation(id) {
  try {
    const messages = await api(`/api/conversations/${id}/messages`);
    state.conversationId = id;
    messageList.replaceChildren();
    for (const message of messages) renderMessage(message.role, message.content);
    $("#suggestions").hidden = messages.length > 0;
    $("#delete-conversation").hidden = false;
    await Promise.all([loadConversations(), loadTasks(id)]);
    setView("chat");
  } catch (error) {
    toast(error.message);
  }
}

async function newConversation() {
  state.conversationId = null;
  messageList.replaceChildren();
  $("#suggestions").hidden = false;
  $("#delete-conversation").hidden = true;
  renderPipeline([]);
  await loadConversations();
  setView("chat");
  input.focus();
}

async function deleteConversation(id = state.conversationId) {
  if (!id) return;
  try {
    await api(`/api/conversations/${id}`, { method: "DELETE" });
    if (state.conversationId === id) {
      state.conversationId = null;
      messageList.replaceChildren();
      $("#suggestions").hidden = false;
      $("#delete-conversation").hidden = true;
      renderPipeline([]);
    }
    await loadConversations();
    await loadTasks();
    toast("Conversation deleted.");
  } catch (error) {
    toast(error.message);
  }
}

async function sendMessage(text = input.value) {
  const message = text.trim();
  if (!message || state.sending) return;
  state.sending = true;
  $("#send-button").disabled = true;
  $("#suggestions").hidden = true;
  renderMessage("user", message);
  showTyping();
  input.value = "";
  updateCharacterCount();
  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, conversation_id: state.conversationId }),
    });
    state.conversationId = result.conversation_id;
    removeTyping();
    renderMessage("assistant", result.response);
    await Promise.all([loadConversations(), loadTasks(state.conversationId), loadApprovals()]);
    $("#delete-conversation").hidden = false;
  } catch (error) {
    removeTyping();
    renderMessage("assistant", error.message, true);
    toast(error.message);
  } finally {
    state.sending = false;
    $("#send-button").disabled = false;
    input.focus();
  }
}

function updateCharacterCount() {
  $("#char-count").textContent = `${input.value.length} / 6000`;
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 130)}px`;
}

const agentIcons = { planner: "⌘", researcher: "⌕", executor: "↗", verifier: "✓", communicator: "◈" };

function renderAgents(agents) {
  const mini = $("#agent-mini-list");
  const grid = $("#agent-grid");
  mini.replaceChildren();
  grid.replaceChildren();
  for (const agent of agents) {
    const row = makeElement("div", "agent-mini");
    const icon = makeElement("span", "agent-icon", agentIcons[agent.id] || "✳");
    const description = makeElement("span", "agent-mini-text");
    description.append(makeElement("strong", "", agent.name), makeElement("small", "", agent.role));
    const status = makeElement("i", "agent-state");
    status.dataset.agent = agent.id;
    row.append(icon, description, status);
    mini.append(row);

    const card = makeElement("article", "agent-card");
    card.append(makeElement("span", "agent-icon", agentIcons[agent.id] || "✳"));
    const details = makeElement("div");
    details.append(makeElement("h3", "", `${agent.name} Agent`), makeElement("p", "", agent.role));
    details.append(makeElement("small", "", "Sequential workflow · Groq powered"));
    card.append(details);
    grid.append(card);
  }
}

function renderPipeline(tasks) {
  const list = $("#pipeline-mini");
  list.replaceChildren();
  if (!tasks.length) {
    list.append(makeElement("div", "empty-pipeline", "Your next request will appear here."));
    return;
  }
  const latest = [...tasks].sort((a, b) => a.id - b.id).slice(-5);
  for (const task of latest) {
    const item = makeElement("div", `pipeline-step ${task.status}`);
    const label = task.status === "pending_approval" ? "!" : task.status === "completed" ? "✓" : task.status === "failed" ? "×" : "·";
    item.append(makeElement("span", "pipeline-node", label), makeElement("span", "", task.agent[0].toUpperCase() + task.agent.slice(1)));
    list.append(item);
  }
}

function renderTaskList(tasks) {
  const list = $("#task-list");
  list.replaceChildren();
  if (!tasks.length) {
    list.append(makeElement("div", "empty-state", "No tasks yet. Send a request to start a workflow."));
    return;
  }
  for (const task of tasks) {
    const card = makeElement("article", "task-card");
    const info = makeElement("div");
    info.append(makeElement("h3", "", task.title), makeElement("p", "", task.detail || `Assigned to ${task.agent}.`));
    const status = makeElement("span", `task-status ${task.status}`, task.status.replaceAll("_", " "));
    const meta = makeElement("div", "task-meta", `${task.agent} · ${formatDate(task.created_at)}`);
    card.append(info, status, meta);
    list.append(card);
  }
}

async function loadTasks(conversationId = null) {
  try {
    const suffix = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : "";
    const tasks = await api(`/api/tasks${suffix}`);
    renderPipeline(tasks);
    if ($("#view-tasks").classList.contains("active")) renderTaskList(tasks);
  } catch (error) {
    toast(error.message);
  }
}

async function loadApprovals() {
  try {
    const tasks = await api("/api/approvals");
    const pending = tasks.filter((task) => task.status === "pending_approval");
    $("#approval-count").textContent = String(pending.length);
    const list = $("#approval-list");
    list.replaceChildren();
    if (!tasks.length) {
      list.append(makeElement("div", "empty-state", "Nothing needs your approval right now."));
      return;
    }
    for (const task of tasks) {
      const card = makeElement("article", "approval-card");
      const info = makeElement("div");
      info.append(makeElement("h3", "", `Sensitive request · ${task.agent}`), makeElement("p", "", task.detail));
      const status = makeElement("span", `task-status ${task.status}`, task.status.replaceAll("_", " "));
      card.append(info, status);
      if (task.status === "pending_approval") {
        const actions = makeElement("div", "approval-actions");
        const reject = makeElement("button", "danger-button", "Reject");
        reject.addEventListener("click", () => decideApproval(task.id, "reject"));
        const approve = makeElement("button", "primary-button", "Authorize");
        approve.addEventListener("click", () => decideApproval(task.id, "approve"));
        actions.append(reject, approve);
        card.append(actions);
      }
      list.append(card);
    }
  } catch (error) {
    toast(error.message);
  }
}

async function decideApproval(id, decision) {
  try {
    const result = await api(`/api/approvals/${id}`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    });
    toast(result.detail);
    await Promise.all([loadApprovals(), loadTasks(), loadActivity()]);
  } catch (error) {
    toast(error.message);
  }
}

async function loadActivity() {
  try {
    const events = await api("/api/activity");
    const list = $("#activity-list");
    const communication = $("#agent-communication");
    list.replaceChildren();
    communication.replaceChildren();
    if (!events.length) {
      list.append(makeElement("div", "empty-state", "Agent activity will appear here when you start a workflow."));
      communication.append(makeElement("div", "empty-pipeline", "Agent updates will appear here."));
      return;
    }
    for (const event of events.slice(0, 5)) {
      const update = makeElement("div", "communication-item");
      update.append(makeElement("span", `activity-mark ${event.status}`));
      const description = makeElement("span");
      description.append(makeElement("strong", "", `${event.agent}: ${event.action}`));
      description.append(makeElement("small", "", formatDate(event.created_at)));
      update.append(description);
      communication.append(update);
    }
    for (const event of events) {
      const card = makeElement("article", "activity-card");
      card.append(makeElement("span", `activity-mark ${event.status}`));
      const info = makeElement("div");
      info.append(makeElement("strong", "", `${event.agent} · ${event.action}`));
      info.append(makeElement("p", "", `Status: ${event.status.replaceAll("_", " ")}`));
      card.append(info, makeElement("time", "", formatDate(event.created_at)));
      list.append(card);
    }
  } catch (error) {
    toast(error.message);
  }
}

function formatDate(value) {
  return new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "Unavailable";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex ? 1 : 0)} ${units[unitIndex]}`;
}

function formatDuration(seconds) {
  const total = Math.floor(seconds || 0);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function setHeaderConnection(connected) {
  $("#header-connection-text").textContent = connected ? "LIVE · SYSTEM ONLINE" : "LIVE CONNECTION LOST";
  $("#header-connection-dot").className = `status-dot ${connected ? "online" : "offline"}`;
}

function renderMonitorStatus(snapshot) {
  const strip = $("#monitor-status-strip");
  strip.replaceChildren();
  const statuses = [
    ["Backend", snapshot.backend.status],
    ["Database", snapshot.database.status],
    ["Groq API", snapshot.groq.status],
    ["WebSocket", state.websocketConnected ? "connected" : "disconnected"],
  ];
  for (const [label, status] of statuses) {
    const healthy = ["healthy", "available", "connected"].includes(status);
    const chip = makeElement("span", `monitor-health-chip${healthy ? "" : " warning"}`);
    chip.append(makeElement("i", `status-dot ${healthy ? "online" : "offline"}`), makeElement("span", "", `${label} · ${status.replaceAll("_", " ")}`));
    strip.append(chip);
  }
}

function renderMetricGrid(snapshot) {
  const { system, api: apiMetrics, database: dbHealth, groq } = snapshot;
  const apiLatency = apiMetrics.requests ? `${apiMetrics.average_latency_ms} ms` : "No samples";
  const errorRate = apiMetrics.requests ? `${apiMetrics.error_rate_percent}% errors` : "No API samples yet";
  const metrics = [
    { label: "CPU utilization", value: `${system.cpu_percent}%`, detail: `Measured on this host · warning at ${snapshot.thresholds.cpu_percent}%`, bar: system.cpu_percent, threshold: snapshot.thresholds.cpu_percent, icon: "⌁" },
    { label: "Memory usage", value: `${system.memory_percent}%`, detail: `${formatBytes(system.memory_used_bytes)} of ${formatBytes(system.memory_total_bytes)} · warning at ${snapshot.thresholds.memory_percent}%`, bar: system.memory_percent, threshold: snapshot.thresholds.memory_percent, icon: "▤" },
    { label: "Disk utilization", value: `${system.disk_percent}%`, detail: `${formatBytes(system.disk_used_bytes)} of ${formatBytes(system.disk_total_bytes)} · warning at ${snapshot.thresholds.disk_percent}%`, bar: system.disk_percent, threshold: snapshot.thresholds.disk_percent, icon: "▱" },
    { label: "API response time", value: apiLatency, detail: `${errorRate} · rolling 5 min`, bar: null, icon: "↗" },
    { label: "Groq API probe", value: groq.latency_ms === null ? "Not measured" : `${groq.latency_ms} ms`, detail: groq.status === "not_configured" ? "Provider not configured" : groq.status, bar: null, icon: "✳" },
    { label: "Backend uptime", value: formatDuration(system.application_uptime_seconds), detail: "Since this process started", bar: null, icon: "◷" },
    { label: "Database health", value: dbHealth.status, detail: dbHealth.size_bytes === undefined ? dbHealth.error || dbHealth.check || "Health check unavailable" : `${formatBytes(dbHealth.size_bytes)} · integrity check`, bar: null, icon: "▦" },
    { label: "API error rate", value: apiMetrics.requests ? `${apiMetrics.error_rate_percent}%` : "No samples", detail: `${apiMetrics.errors} server errors / ${apiMetrics.requests} requests · rolling 5 min`, bar: null, icon: "◇" },
  ];
  const grid = $("#metric-grid");
  grid.replaceChildren();
  for (const metric of metrics) {
    const warning = metric.bar !== null && metric.bar >= metric.threshold;
    const card = makeElement("article", `metric-card${warning ? " warning" : ""}`);
    const top = makeElement("div", "metric-card-top");
    top.append(makeElement("span", "", metric.label), makeElement("span", "metric-icon", metric.icon));
    card.append(top, makeElement("strong", "", metric.value), makeElement("small", "", metric.detail));
    if (metric.bar !== null) {
      const bar = makeElement("div", "metric-bar");
      const fill = makeElement("span");
      fill.style.width = `${Math.max(0, Math.min(100, metric.bar))}%`;
      bar.append(fill);
      card.append(bar);
    }
    grid.append(card);
  }
}

function renderMonitorAgents(agents) {
  const list = $("#monitor-agent-list");
  list.replaceChildren();
  for (const agent of agents) {
    const row = makeElement("div", "monitor-agent-row");
    const identity = makeElement("div", "monitor-agent-name");
    identity.append(makeElement("span", "agent-icon", agentIcons[agent.agent] || "✳"), makeElement("span", "", agent.agent[0].toUpperCase() + agent.agent.slice(1)));
    const detail = agent.current_task ? `Working · ${agent.current_task}` : `${agent.status} · ${agent.average_duration_ms === null ? "No runs" : `${agent.average_duration_ms} ms avg`}`;
    const statsText = agent.successes + agent.failures === 0
      ? "No runs recorded"
      : `${agent.successes} ok · ${agent.failures} failed · ${agent.completion_rate_percent}% complete`;
    const stats = makeElement("div", "monitor-agent-stats");
    stats.append(makeElement("strong", "", statsText));
    if (agent.last_error) stats.append(makeElement("div", "", `Last error: ${agent.last_error}`));
    row.append(identity, makeElement("div", "monitor-agent-detail", detail), stats);
    list.append(row);
  }
}

function drawResourceChart(samples) {
  const canvas = $("#resource-chart");
  const empty = $("#chart-empty");
  const context = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  context.scale(ratio, ratio);
  context.clearRect(0, 0, rect.width, rect.height);
  if (!samples.length) {
    empty.hidden = false;
    $("#chart-footnote").textContent = "";
    return;
  }
  empty.hidden = true;
  const margin = { top: 10, right: 12, bottom: 23, left: 32 };
  const width = rect.width - margin.left - margin.right;
  const height = rect.height - margin.top - margin.bottom;
  context.font = "9px DM Sans, sans-serif";
  context.lineWidth = 1;
  for (let tick = 0; tick <= 100; tick += 25) {
    const y = margin.top + height * (1 - tick / 100);
    context.strokeStyle = "#252a35";
    context.beginPath();
    context.moveTo(margin.left, y);
    context.lineTo(rect.width - margin.right, y);
    context.stroke();
    context.fillStyle = "#737987";
    context.fillText(`${tick}%`, 2, y + 3);
  }
  const series = [
    ["cpu_percent", "#b599ff"],
    ["memory_percent", "#73c7eb"],
    ["disk_percent", "#e7b471"],
  ];
  for (const [key, color] of series) {
    context.strokeStyle = color;
    context.lineWidth = 1.8;
    context.beginPath();
    samples.forEach((sample, index) => {
      const x = margin.left + (samples.length === 1 ? width / 2 : width * index / (samples.length - 1));
      const y = margin.top + height * (1 - sample.system[key] / 100);
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.stroke();
  }
  context.fillStyle = "#737987";
  const first = new Date(samples[0].created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const last = new Date(samples[samples.length - 1].created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  context.fillText(first, margin.left, rect.height - 4);
  const endWidth = context.measureText(last).width;
  context.fillText(last, rect.width - margin.right - endWidth, rect.height - 4);
  const latest = samples[samples.length - 1];
  $("#chart-footnote").textContent = `${samples.length} stored measurements · latest ${formatDate(latest.created_at)} · CPU ${latest.system.cpu_percent}% / memory ${latest.system.memory_percent}% / disk ${latest.system.disk_percent}%`;
}

async function loadMonitorHistory() {
  try {
    const result = await api(`/api/monitor/history?period=${encodeURIComponent($("#history-period").value)}`);
    drawResourceChart(result.samples);
  } catch (error) {
    toast(error.message);
  }
}

async function loadMonitorAlerts() {
  try {
    const alerts = await api("/api/monitor/alerts?active_only=true");
    const list = $("#monitor-alert-list");
    list.replaceChildren();
    $("#active-alert-count").textContent = `${alerts.length} active`;
    $("#active-alert-count").classList.toggle("has-alerts", alerts.length > 0);
    $("#monitor-alert-count").textContent = alerts.length ? String(alerts.length) : "";
    if (!alerts.length) {
      list.append(makeElement("div", "empty-state", "No active alerts. Measurements will appear as the monitor samples the system."));
      return;
    }
    for (const alert of alerts) {
      const card = makeElement("article", "monitor-alert-card");
      card.append(makeElement("i", `alert-severity ${alert.severity}`));
      const body = makeElement("div", "alert-body");
      body.append(makeElement("strong", "", alert.title), makeElement("p", "", alert.message), makeElement("small", "", `${alert.severity} · ${formatDate(alert.updated_at)}`));
      const acknowledge = makeElement("button", "ack-button", "Acknowledge");
      acknowledge.addEventListener("click", async () => {
        try {
          await api(`/api/monitor/alerts/${alert.id}/acknowledge`, { method: "POST" });
          await loadMonitorAlerts();
          toast("Alert acknowledged and retained in history.");
        } catch (error) {
          toast(error.message);
        }
      });
      card.append(body, acknowledge);
      list.append(card);
    }
  } catch (error) {
    toast(error.message);
  }
}

async function loadMonitorErrors() {
  try {
    const errors = await api("/api/monitor/errors?period=24h");
    const list = $("#monitor-errors");
    list.replaceChildren();
    if (!errors.length) {
      list.append(makeElement("div", "empty-state", "No server-side application errors recorded in the last 24 hours."));
      return;
    }
    for (const error of errors) {
      const card = makeElement("article", "monitor-error-card");
      card.append(makeElement("i", "alert-severity critical"));
      const body = makeElement("div", "alert-body");
      body.append(makeElement("strong", "", error.source), makeElement("p", "", error.message), makeElement("small", "", formatDate(error.created_at)));
      card.append(body);
      list.append(card);
    }
  } catch (error) {
    toast(error.message);
  }
}

async function loadDiagnosticReports() {
  try {
    const reports = await api("/api/monitor/diagnostics");
    const list = $("#diagnostic-reports");
    list.replaceChildren();
    if (!reports.length) {
      list.append(makeElement("div", "empty-state", "No diagnostic reports yet."));
      return;
    }
    for (const report of reports) {
      const item = makeElement("article", "diagnostic-report", report.report);
      item.prepend(makeElement("small", "", `Report #${report.id} · ${formatDate(report.created_at)}\n\n`));
      list.append(item);
    }
  } catch (error) {
    toast(error.message);
  }
}

async function loadAlertSettings() {
  try {
    const settings = await api("/api/monitor/settings");
    const form = $("#alert-settings-form");
    for (const key of ["cpu_percent", "memory_percent", "disk_percent", "api_latency_ms", "websocket_disconnects"]) {
      form.elements.namedItem(key).value = settings[key];
    }
    form.elements.namedItem("notifications_enabled").checked = settings.notifications_enabled;
    state.notificationsEnabled = settings.notifications_enabled;
  } catch (error) {
    toast(error.message);
  }
}

async function loadMonitor() {
  const snapshotRequest = api("/api/monitor/overview");
  const results = await Promise.allSettled([
    snapshotRequest,
    loadMonitorHistory(),
    loadMonitorAlerts(),
    loadMonitorErrors(),
    loadDiagnosticReports(),
    loadAlertSettings(),
  ]);
  if (results[0].status === "rejected") {
    toast(results[0].reason.message);
    return;
  }
  state.monitorSnapshot = results[0].value;
  const snapshot = state.monitorSnapshot;
  renderMonitorStatus(snapshot);
  renderMetricGrid(snapshot);
  renderMonitorAgents(snapshot.agents);
  $("#monitor-updated").textContent = `Measured ${formatDate(snapshot.collected_at)}`;
}

async function runDiagnostics() {
  const button = $("#run-diagnostics");
  button.disabled = true;
  button.textContent = "Analyzing…";
  try {
    const result = await api("/api/monitor/diagnose", { method: "POST" });
    const list = $("#diagnostic-reports");
    list.prepend(makeElement("article", "diagnostic-report", `Report #${result.id} · ${formatDate(result.created_at)}\n\n${result.report}`));
    toast("Diagnostic report generated from the current monitoring snapshot.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Run diagnostics";
  }
}

async function saveAlertSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const value = Object.fromEntries(new FormData(form));
  for (const key of ["cpu_percent", "memory_percent", "disk_percent", "api_latency_ms", "websocket_disconnects"]) {
    value[key] = Number(value[key]);
  }
  value.notifications_enabled = form.elements.namedItem("notifications_enabled").checked;
  try {
    if (value.notifications_enabled && "Notification" in window && Notification.permission === "default") {
      await Notification.requestPermission();
    }
    await api("/api/monitor/settings", { method: "PUT", body: JSON.stringify(value) });
    state.notificationsEnabled = value.notifications_enabled;
    toast("Alert thresholds and notification preference saved.");
  } catch (error) {
    toast(error.message);
  }
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    const dot = $("#provider-dot");
    dot.classList.toggle("online", health.configured);
    dot.classList.toggle("offline", !health.configured);
    $("#provider-status").textContent = health.configured ? `${health.provider} connected` : "Groq key required";
    $("#settings-dot").className = `status-dot ${health.configured ? "online" : "offline"}`;
    $("#settings-model").textContent = `${health.provider} · ${health.model}`;
    $("#settings-connection").textContent = health.configured ? "Provider is configured on the server." : "No API key configured. Chat is unavailable until configured.";
    $("#settings-provider").textContent = health.provider.toUpperCase();
    if (!health.configured) toast("Set GROQ_API_KEY on the server to enable AI responses.");
  } catch (error) {
    $("#provider-status").textContent = "Server unavailable";
    toast(error.message);
  }
}


async function loadGroqConfig() {
  try {
    const config = await api("/api/groq/config");
    if (config.masked_key && $("#groq-api-key")) {
      $("#groq-api-key").placeholder = `Configured (${config.masked_key})`;
    }
    if (config.model && $("#groq-model-select")) {
      const select = $("#groq-model-select");
      let found = false;
      for (const opt of select.options) {
        if (opt.value === config.model) {
          select.value = config.model;
          found = true;
          break;
        }
      }
      if (!found) {
        const opt = document.createElement("option");
        opt.value = config.model;
        opt.textContent = config.model;
        select.prepend(opt);
        select.value = config.model;
      }
    }
  } catch {
    /* Optional prefetch */
  }
}

async function refreshGroqModels() {
  const btn = $("#refresh-models-btn");
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/groq/models");
    if (data.models && data.models.length) {
      const select = $("#groq-model-select");
      const currentVal = select.value;
      select.innerHTML = "";
      for (const m of data.models) {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m === "openai/gpt-oss-120b" ? `${m} (Recommended)` : (m === "openai/gpt-oss-20b" ? `${m} (Fast)` : m);
        select.appendChild(opt);
      }
      if (data.models.includes(currentVal)) {
        select.value = currentVal;
      }
      toast(`Loaded ${data.models.length} Groq models.`);
    }
  } catch (err) {
    toast(`Could not fetch models: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleGroqConfigure(event) {
  event.preventDefault();
  const keyInput = $("#groq-api-key");
  const modelSelect = $("#groq-model-select");
  const statusSpan = $("#groq-connect-status");
  const btn = $("#connect-groq-btn");

  const apiKey = keyInput.value.trim();
  const model = modelSelect.value;

  if (!apiKey && keyInput.placeholder.includes("Configured")) {
    toast("Groq is already configured. Enter a new key if you wish to change it.");
    return;
  }
  if (!apiKey) {
    toast("Please enter a Groq API key.");
    keyInput.focus();
    return;
  }

  btn.disabled = true;
  statusSpan.textContent = "Verifying & connecting...";
  statusSpan.style.color = "var(--amber)";

  try {
    const res = await api("/api/groq/configure", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey, model }),
    });
    statusSpan.textContent = "Connected successfully!";
    statusSpan.style.color = "var(--green)";
    keyInput.value = "";
    keyInput.placeholder = `Configured (${apiKey.slice(0, 4)}...${apiKey.slice(-4)})`;
    toast(res.message || "Groq connected successfully!");
    await loadHealth();
    await loadGroqConfig();
  } catch (err) {
    statusSpan.textContent = "Connection failed";
    statusSpan.style.color = "var(--red)";
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

function toggleKeyVisibility() {
  const input = $("#groq-api-key");
  const btn = $("#toggle-key-visibility");
  if (!input || !btn) return;
  if (input.type === "password") {
    input.type = "text";
    btn.textContent = "Hide";
  } else {
    input.type = "password";
    btn.textContent = "Show";
  }
}

async function loadPreferences() {
  try {
    const preferences = await api("/api/preferences");
    const name = preferences.display_name || "Your workspace";
    $("#profile-name").textContent = name;
    $("#greeting-name").textContent = name === "Your workspace" ? "you" : name;
    $("#display-name").value = preferences.display_name || "";
  } catch (error) {
    toast(error.message);
  }
}

function connectWebSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws`);
  socket.addEventListener("open", () => {
    state.websocketConnected = true;
    setHeaderConnection(true);
    if ($("#view-monitor").classList.contains("active") && state.monitorSnapshot) {
      renderMonitorStatus(state.monitorSnapshot);
    }
  });
  socket.addEventListener("message", (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "groq_configured") {
      loadHealth();
      loadGroqConfig();
      if ($("#view-monitor").classList.contains("active") && data.snapshot) {
        renderMonitorStatus(data.snapshot);
      }
      return;
    }
    if (data.type === "monitor_update") {
      state.monitorSnapshot = data.snapshot;
      if ($("#view-monitor").classList.contains("active")) {
        renderMonitorStatus(data.snapshot);
        renderMetricGrid(data.snapshot);
        renderMonitorAgents(data.snapshot.agents || []);
        $("#monitor-updated").textContent = `Measured ${formatDate(data.snapshot.collected_at)}`;
        loadMonitorHistory();
      }
      return;
    }
    if (data.type === "monitor_alert") {
      loadMonitorAlerts();
      $("#monitor-alert-count").textContent = "!";
      if (
        data.alert &&
        state.notificationsEnabled &&
        window.Notification &&
        Notification.permission === "granted"
      ) {
        new Notification(data.alert.title, { body: data.alert.message });
      }
      return;
    }
    if (data.type === "monitor_alert_acknowledged") {
      loadMonitorAlerts();
      return;
    }
    if (data.conversation_id && state.conversationId && data.conversation_id !== state.conversationId) return;
    if (data.type === "agent_progress") {
      const current = $(`.agent-state[data-agent="${CSS.escape(data.agent)}"]`);
      if (current) current.classList.toggle("active", data.status === "running");
      loadTasks(state.conversationId);
      loadActivity();
    }
    if (data.type === "workflow_completed" || data.type === "approval_updated") {
      loadTasks(state.conversationId);
      loadApprovals();
      loadActivity();
    }
    if (data.type === "workflow_error") toast(data.message);
    if (data.type === "workflow_completed") {
      const activeAgentStates = $$(".agent-state.active");
      activeAgentStates.forEach((element) => element.classList.remove("active"));
    }
    if (data.type === "history_cleared") {
      state.conversationId = null;
      messageList.replaceChildren();
      loadConversations();
      loadTasks();
    }
    if (data.type === "conversation_deleted" && data.conversation_id === state.conversationId) newConversation();
  });
  socket.addEventListener("close", () => {
    state.websocketConnected = false;
    setHeaderConnection(false);
    if (state.monitorSnapshot) renderMonitorStatus(state.monitorSnapshot);
    setTimeout(connectWebSocket, 2500);
  });
  socket.addEventListener("error", () => {
    state.websocketConnected = false;
    setHeaderConnection(false);
  });
}

$$(".nav-item[data-view], .text-button[data-view], .topbar-right [data-view]").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});
$("#new-chat").addEventListener("click", newConversation);
$("#delete-conversation").addEventListener("click", () => deleteConversation());
$("#clear-history").addEventListener("click", async () => {
  if (!state.conversations.length || !window.confirm("Delete all conversations and activity history? This cannot be undone.")) return;
  try {
    await api("/api/history", { method: "DELETE" });
    state.conversationId = null;
    messageList.replaceChildren();
    $("#suggestions").hidden = false;
    $("#delete-conversation").hidden = true;
    renderPipeline([]);
    await Promise.all([loadConversations(), loadTasks(), loadActivity(), loadApprovals()]);
    toast("Conversation history cleared.");
  } catch (error) {
    toast(error.message);
  }
});
$("#chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage();
});
input.addEventListener("input", updateCharacterCount);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});
$$(".suggestion-card").forEach((card) => card.addEventListener("click", () => sendMessage(card.dataset.prompt)));
$("#menu-toggle").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#save-name").addEventListener("click", async () => {
  try {
    const value = $("#display-name").value.trim();
    await api("/api/preferences", { method: "PUT", body: JSON.stringify({ key: "display_name", value }) });
    $("#profile-name").textContent = value || "Your workspace";
    $("#greeting-name").textContent = value || "you";
    toast("Workspace profile saved.");
  } catch (error) {
    toast(error.message);
  }
});
$("#refresh-monitor").addEventListener("click", loadMonitor);
$("#history-period").addEventListener("change", loadMonitorHistory);
$("#run-diagnostics").addEventListener("click", runDiagnostics);
$("#alert-settings-form").addEventListener("submit", saveAlertSettings);
$("#groq-config-form")?.addEventListener("submit", handleGroqConfigure);
$("#toggle-key-visibility")?.addEventListener("click", toggleKeyVisibility);
$("#refresh-models-btn")?.addEventListener("click", refreshGroqModels);
window.addEventListener("resize", () => {
  if ($("#view-monitor").classList.contains("active")) {
    clearTimeout(window.monitorChartResizeTimer);
    window.monitorChartResizeTimer = setTimeout(loadMonitorHistory, 120);
  }
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    newConversation();
  }
});

async function initialize() {
  $("#delete-conversation").hidden = true;
  setHeaderConnection(false);
  renderAgents(await api("/api/agents"));
  await Promise.all([loadConversations(), loadHealth(), loadPreferences(), loadApprovals(), loadActivity(), loadAlertSettings(), loadGroqConfig()]);
  connectWebSocket();
}

initialize().catch((error) => toast(error.message));
