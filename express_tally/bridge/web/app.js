import { initializeMasters, loadMasters } from "./masters.js";
import { initializeOperations } from "./operations.js";

const api = async (path, options = {}) => {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok && !options.allowError) {
    throw new Error(payload.error || payload.message || `Request failed (${response.status})`);
  }
  return payload;
};

const state = {
  config: {},
  flows: [],
  status: {},
  masters: {},
  toastTimer: null,
  flowFilter: "",
  flowSelection: null,
  historyFilter: "",
  historyDirection: "",
};
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[character]);

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => element.classList.remove("show"), 3600);
}

function showPage(page) {
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  $$(".page").forEach((item) => item.classList.toggle("active", item.dataset.pagePanel === page));
  $("#page-title").textContent = { overview: "Overview", flows: "Sync flows", masters: "Masters sync", operations: "Operations", history: "History", settings: "Settings" }[page];
  if (page === "masters") loadMasters();
}

function directionLabel(direction) {
  return direction === "erpnext_to_tally" ? "ERPNext → Tally" : direction === "tally_to_erpnext" ? "Tally → ERPNext" : "Both directions";
}

function formatTime(value) {
  if (!value) return "Not scheduled";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function historyTable(entries, compact = false, emptyMessage = "No synchronization runs have been recorded yet.") {
  if (!entries?.length) return `<div class="empty">${escapeHtml(emptyMessage)}</div>`;
  const rows = entries.slice(0, compact ? 5 : 100).map((entry) => {
    const success = !entry.error && !entry.failed;
    const result = success
      ? `<span class="result-ok">${entry.succeeded || 0} succeeded</span>`
      : `<span class="result-error">${entry.failed || 0} failed</span>`;
    return `<tr>
      <td>${escapeHtml(formatTime(entry.at))}</td>
      <td class="direction">${escapeHtml(directionLabel(entry.direction))}</td>
      <td><strong>${escapeHtml(entry.flow || "Control Centre")}</strong><br><small>${escapeHtml(entry.source || "manual")}</small></td>
      <td>${entry.fetched || 0} fetched</td>
      <td>${result}${entry.error ? `<div class="error-text">${escapeHtml(entry.error)}</div>` : ""}</td>
    </tr>`;
  }).join("");
  return `<table class="history-table"><thead><tr><th>Time</th><th>Direction</th><th>Flow</th><th>Records</th><th>Result</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderStatus() {
  const current = state.status.current;
  const running = Boolean(state.status.running);
  $("#sync-status").textContent = running ? "Synchronizing" : "Idle";
  $("#sync-detail").textContent = running ? directionLabel(current?.direction) : (state.status.last_error || "Ready");
  $("#running-badge").classList.toggle("hidden", !running);
  $$(".sync-button").forEach((button) => { button.disabled = running || Boolean(state.status.configuration_error); });
  $("#auto-sync").checked = Boolean(state.status.auto_sync_enabled);
  $("#auto-description").textContent = state.status.auto_sync_enabled
    ? `Runs every ${state.config.poll_interval_seconds || 60} seconds.`
    : "Automatic synchronization is off.";
  $("#next-auto-sync").textContent = formatTime(state.status.next_auto_sync);
  $("#auto-directions").innerHTML = (state.status.auto_sync_directions || []).map((direction) => `<span class="tag">${escapeHtml(directionLabel(direction))}</span>`).join("");
  const alert = $("#configuration-alert");
  alert.classList.toggle("hidden", !state.status.configuration_error);
  alert.textContent = state.status.configuration_error ? `${state.status.configuration_error}. Complete Settings and Sync flows before running.` : "";
  $("#recent-history").innerHTML = historyTable(state.status.history || [], true);
  renderHistory();
}

function filteredHistory() {
  const term = state.historyFilter.trim().toLowerCase();
  return (state.status.history || []).filter((entry) => {
    if (state.historyDirection && entry.direction !== state.historyDirection) return false;
    if (!term) return true;
    return [entry.flow, entry.direction, entry.source, entry.error, entry.at]
      .join(" ")
      .toLowerCase()
      .includes(term);
  });
}

function renderHistory() {
  const entries = filteredHistory();
  $("#history-count").textContent = `${entries.length} run${entries.length === 1 ? "" : "s"}`;
  $("#full-history").innerHTML = historyTable(
    entries,
    false,
    "No synchronization runs match the current filters.",
  );
}

async function loadState() {
  try {
    state.status = await api("/api/v1/state");
    renderStatus();
    $("#service-dot").classList.remove("error");
    $("#service-label").textContent = "Local service running";
  } catch (error) {
    $("#service-dot").classList.add("error");
    $("#service-label").textContent = "Service unavailable";
  }
}

async function loadHealth(showToast = false) {
  const health = await api("/api/v1/health", { allowError: true });
  const erp = health.erpnext || {};
  const tally = health.tally || {};
  $("#erp-status").textContent = erp.ok ? "Connected" : "Not connected";
  $("#erp-detail").textContent = erp.ok ? `${erp.flow_count || 0} flow(s) discovered` : (erp.error || "Check settings");
  $("#tally-status").textContent = tally.ok ? "Connected" : "Not connected";
  $("#tally-detail").textContent = tally.ok ? (tally.loaded_tally_company || "Company loaded") : (tally.error || (tally.loaded_tally_company ? `Loaded: ${tally.loaded_tally_company}` : "Check TallyPrime"));
  if (showToast) toast(health.ok ? "ERPNext and TallyPrime are connected." : "One or more connections failed. See the status cards.", !health.ok);
  return health;
}

async function loadConfig() {
  state.config = await api("/api/v1/config");
  const form = $("#settings-form");
  ["frappe_url", "api_key", "api_secret", "erpnext_company", "target_id", "tally_url", "tally_company", "voucher_date_override", "batch_size", "poll_interval_seconds", "request_timeout_seconds", "from_date", "to_date", "customer_parent", "supplier_parent", "master_batch_size", "ca_bundle"].forEach((name) => {
    form.elements[name].value = state.config[name] ?? "";
  });
  form.elements.verify_tls.checked = state.config.verify_tls !== false;
  form.elements.open_browser_on_start.checked = Boolean(state.config.open_browser_on_start);
	form.elements.start_with_windows.checked = Boolean(state.config.start_with_windows);
  form.elements.auto_outbound.checked = (state.config.auto_sync_directions || []).includes("erpnext_to_tally");
  form.elements.auto_inbound.checked = (state.config.auto_sync_directions || []).includes("tally_to_erpnext");
	form.elements.submit_inbound_documents.checked = Boolean(
	  state.config.flow_options?.["express_tally.standard_vouchers_from_tally"]?.submit_documents,
	);
	form.elements.update_stock_from_inbound.checked = Boolean(
	  state.config.flow_options?.["express_tally.standard_vouchers_from_tally"]?.update_stock,
	);
}

function settingsPayload() {
  const form = $("#settings-form");
  const formData = new FormData(form);
  const nullable = (name) => formData.get(name) || null;
  return {
    frappe_url: String(formData.get("frappe_url") || "").trim().replace(/\/$/, ""),
    api_key: String(formData.get("api_key") || "").trim(),
    api_secret: String(formData.get("api_secret") || ""),
    erpnext_company: String(formData.get("erpnext_company") || "").trim(),
    target_id: String(formData.get("target_id") || "").trim(),
    tally_url: String(formData.get("tally_url") || "").trim().replace(/\/$/, ""),
    tally_company: String(formData.get("tally_company") || "").trim(),
    voucher_date_override: nullable("voucher_date_override"),
    batch_size: Number(formData.get("batch_size")),
    poll_interval_seconds: Number(formData.get("poll_interval_seconds")),
    request_timeout_seconds: Number(formData.get("request_timeout_seconds")),
    from_date: nullable("from_date"),
    to_date: nullable("to_date"),
    customer_parent: String(formData.get("customer_parent") || "").trim(),
    supplier_parent: String(formData.get("supplier_parent") || "").trim(),
    master_batch_size: Number(formData.get("master_batch_size")),
    ca_bundle: String(formData.get("ca_bundle") || "").trim(),
    verify_tls: form.elements.verify_tls.checked,
    auto_sync_directions: [
      ...(form.elements.auto_outbound.checked ? ["erpnext_to_tally"] : []),
      ...(form.elements.auto_inbound.checked ? ["tally_to_erpnext"] : []),
    ],
	flow_options: {
	  ...(state.config.flow_options || {}),
	  "express_tally.standard_vouchers_from_tally": {
		...(state.config.flow_options?.["express_tally.standard_vouchers_from_tally"] || {}),
		submit_documents: form.elements.submit_inbound_documents.checked,
		update_stock: form.elements.update_stock_from_inbound.checked,
	  },
	},
    open_browser_on_start: form.elements.open_browser_on_start.checked,
	start_with_windows: form.elements.start_with_windows.checked,
  };
}

async function saveSettings(showSuccess = true) {
  const form = $("#settings-form");
  if (!form.reportValidity()) throw new Error("Complete the required settings first.");
  const response = await api("/api/v1/config", { method: "PUT", body: JSON.stringify(settingsPayload()) });
  state.config = response.config;
  form.elements.api_secret.value = state.config.api_secret;
  if (showSuccess) toast("Settings saved.");
  await loadState();
}

function configuredFlowSelection() {
  return new Set(state.config.enabled_flows || (state.config.flow_name ? [state.config.flow_name] : []));
}

function currentFlowSelection() {
  if (!state.flowSelection) state.flowSelection = configuredFlowSelection();
  return state.flowSelection;
}

function flowMatchesSearch(flow, term) {
  if (!term) return true;
  return [flow.title, flow.key, flow.direction, flow.agent_profile, flow.unavailable_reason]
    .join(" ")
    .toLowerCase()
    .includes(term);
}

function renderFlows(errorMessage = "") {
  const term = state.flowFilter.trim().toLowerCase();
  const selected = currentFlowSelection();
  const visible = state.flows.filter((flow) => flowMatchesSearch(flow, term));
  $("#flow-count").textContent = `${visible.length} of ${state.flows.length} flow${state.flows.length === 1 ? "" : "s"}`;
  if (!state.flows.length) {
    $("#flows-list").innerHTML = `<div class="empty">${escapeHtml(errorMessage || "No flows are registered in ERPNext.")}</div>`;
    return;
  }
  if (!visible.length) {
    $("#flows-list").innerHTML = '<div class="empty">No flows match your search.</div>';
    return;
  }
  $("#flows-list").innerHTML = visible.map((flow) => `<label class="flow-row ${flow.available ? "" : "unavailable"}">
    <input class="flow-check" type="checkbox" value="${escapeHtml(flow.key)}" ${selected.has(flow.key) ? "checked" : ""} ${flow.available ? "" : "disabled"} />
    <span><strong>${escapeHtml(flow.title || flow.key)}</strong><small>${escapeHtml(flow.available ? flow.key : flow.unavailable_reason)}</small></span>
    <span class="flow-direction">${escapeHtml(directionLabel(flow.direction))}</span>
  </label>`).join("");
  $$(".flow-check").forEach((input) => input.addEventListener("change", () => {
    if (input.checked) selected.add(input.value);
    else selected.delete(input.value);
  }));
}

async function loadFlows(showToast = false) {
  const response = await api("/api/v1/flows", { allowError: true });
  state.flows = response.flows || [];
  state.flowSelection = configuredFlowSelection();
  renderFlows(response.error || "");
  if (showToast) toast(response.error ? response.error : `${state.flows.length} flow(s) loaded.`, Boolean(response.error));
}

function selectAvailableFlows() {
  const selected = currentFlowSelection();
  state.flows.filter((flow) => flow.available).forEach((flow) => selected.add(flow.key));
  renderFlows();
}

function clearFlowSelection() {
  state.flowSelection = new Set();
  renderFlows();
}

async function saveFlows() {
  const enabled = [...currentFlowSelection()];
  const groups = new Map();
  enabled.forEach((key) => {
    const flow = state.flows.find((entry) => entry.key === key);
    if (!flow?.exclusive_group) return;
    if (groups.has(flow.exclusive_group)) {
      throw new Error(`${groups.get(flow.exclusive_group)} and ${flow.title || flow.key} cannot both be enabled.`);
    }
    groups.set(flow.exclusive_group, flow.title || flow.key);
  });
  const response = await api("/api/v1/config", { method: "PUT", body: JSON.stringify({ enabled_flows: enabled, flow_name: "" }) });
  state.config = response.config;
  state.flowSelection = configuredFlowSelection();
  renderFlows();
  toast("Enabled flows saved.");
  await loadState();
}

async function triggerSync(direction) {
  try {
    const response = await api("/api/v1/sync", { method: "POST", body: JSON.stringify({ direction }) });
    toast(response.message || "Synchronization started.");
    await loadState();
  } catch (error) {
    toast(error.message, true);
  }
}

async function toggleAutoSync(event) {
  const desired = event.target.checked;
  try {
    await api("/api/v1/auto-sync", { method: "POST", body: JSON.stringify({ enabled: desired }) });
    toast(desired ? "Automatic sync enabled." : "Automatic sync disabled.");
    await loadState();
  } catch (error) {
    event.target.checked = !desired;
    toast(error.message, true);
  }
}

function wireEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
  $$('[data-go]').forEach((button) => button.addEventListener("click", () => showPage(button.dataset.go)));
  $$(".sync-button").forEach((button) => button.addEventListener("click", () => triggerSync(button.dataset.direction)));
  $("#auto-sync").addEventListener("change", toggleAutoSync);
  $("#test-connections").addEventListener("click", () => loadHealth(true));
  $("#settings-test").addEventListener("click", async () => {
    try { await saveSettings(false); await loadHealth(true); await loadFlows(); } catch (error) { toast(error.message, true); }
  });
  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await saveSettings(); } catch (error) { toast(error.message, true); }
  });
  $("#refresh-flows").addEventListener("click", () => loadFlows(true));
  $("#flow-search").addEventListener("input", (event) => { state.flowFilter = event.target.value; renderFlows(); });
  $("#select-flows").addEventListener("click", selectAvailableFlows);
  $("#clear-flows").addEventListener("click", clearFlowSelection);
  $("#history-search").addEventListener("input", (event) => { state.historyFilter = event.target.value; renderHistory(); });
  $("#history-direction").addEventListener("change", (event) => { state.historyDirection = event.target.value; renderHistory(); });
	$("#stop-bridge").addEventListener("click", async () => {
	  if (!window.confirm("Stop the local bridge? The Control Centre will close until it is restarted.")) return;
	  try {
		await api("/api/v1/shutdown", { method: "POST", body: "{}" });
		toast("Bridge stopping. Restart it from start-control-centre.cmd or the scheduled task.");
	  } catch (error) { toast(error.message, true); }
	});
	$("#reset-checkpoints").addEventListener("click", async () => {
	  if (!window.confirm("Reset the inbound checkpoint for this target? Tally records will be offered to ERPNext again; existing sync logs still prevent duplicate versions.")) return;
	  try {
		await api("/api/v1/checkpoints/reset", { method: "POST", body: "{}" });
		toast("Inbound checkpoint reset.");
	  } catch (error) { toast(error.message, true); }
	});
  $("#save-flows").addEventListener("click", async () => {
    try { await saveFlows(); } catch (error) { toast(error.message, true); }
  });
}

async function initialize() {
  wireEvents();
  await Promise.all([loadConfig(), loadState()]);
  await Promise.all([loadHealth(), loadFlows()]);
  initializeOperations({ state, api, toast, escapeHtml, directionLabel, loadConfig, loadState });
  initializeMasters({ state, api, toast, escapeHtml });
  setInterval(loadState, 2000);
  setInterval(loadHealth, 30000);
}

initialize().catch((error) => toast(error.message, true));
