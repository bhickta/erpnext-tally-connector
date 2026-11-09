import {
  initializeMasters,
  loadMasters,
  refreshMastersState,
} from "./masters.js";
import {
  initializeOperations,
  refreshOperations,
  openFlow,
} from "./operations.js";

const api = async (path, options = {}) => {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers },
    });
  } catch {
    throw new Error(
      "Cannot reach the local bridge. Check that it is running, then try again.",
    );
  }
  const payload = await response.json().catch(() => {
    throw new Error(
      `The bridge returned an unreadable response (${response.status}).`,
    );
  });
  if (!response.ok && !options.allowError)
    throw new Error(
      payload.error || payload.message || `Request failed (${response.status})`,
    );
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
  health: null,
  settingsDirty: false,
  settingsFields: new Set(),
  serviceOffline: false,
  syncPending: false,
  initialized: false,
  page: "overview",
};
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>'"]/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "'": "&#39;",
        '"': "&quot;",
      })[character],
  );

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => element.classList.remove("show"), 3600);
}

const pages = {
  overview: [
    "Dashboard",
    "Connect your companies, sync master data, then run transactions.",
  ],
  flows: [
    "Transaction flows",
    "Choose the transaction types and directions this connector handles.",
  ],
  masters: [
    "Master data",
    "Create or update ERPNext masters in Tally before sending transactions.",
  ],
  operations: [
    "Flow workspace",
    "Configure a transaction flow, preview its records, and run it.",
  ],
  history: [
    "Run history",
    "Review recent transaction results and find records that need attention.",
  ],
  settings: [
    "Connections & settings",
    "Manage company connections and synchronization preferences.",
  ],
};

function showPage(page, focus = true) {
  if (!pages[page]) page = "overview";
  state.page = page;
  $$(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.page === page);
    if (item.dataset.page === page) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $$(".page").forEach((item) => {
    const active = item.dataset.pagePanel === page;
    item.classList.toggle("active", active);
    item.hidden = !active;
  });
  $("#page-title").textContent = pages[page][0];
  $("#page-description").textContent = pages[page][1];
  if (location.hash !== `#${page}`) history.pushState(null, "", `#${page}`);
  if (focus) $("#page-title").focus({ preventScroll: true });
  if (state.initialized && page === "masters") loadMasters();
  if (state.initialized && page === "operations") refreshOperations();
}

function setMessage(selector, message = "", error = false) {
  const element = $(selector);
  element.textContent = message;
  element.classList.toggle("hidden", !message);
  element.classList.toggle("error", error);
}

async function action(button, work, errorTarget) {
  if (button?.dataset.pending) return;
  if (button) {
    button.dataset.pending = "true";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  if (errorTarget) setMessage(errorTarget);
  try {
    return await work();
  } catch (error) {
    if (errorTarget) setMessage(errorTarget, error.message, true);
    toast(error.message, true);
  } finally {
    if (button) {
      delete button.dataset.pending;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
    renderStatus();
  }
}

function isBusy() {
  return Boolean(
    state.syncPending ||
      state.mastersPending ||
      state.previewPending ||
      state.status.running ||
      state.status.masters_running,
  );
}

function hasConnections() {
  return [
    "frappe_url",
    "api_key",
    "api_secret",
    "erpnext_company",
    "target_id",
    "tally_url",
    "tally_company",
  ].every((key) => Boolean(state.config[key]));
}

function enabledFlows() {
  const selected = configuredFlowSelection();
  return state.flows.filter((flow) => flow.available && selected.has(flow.key));
}

function renderNextStep() {
  let step;
  if (!hasConnections())
    step = [
      "Set up your connections",
      "Enter your ERPNext credentials and choose the company open in Tally.",
      "Set up connections",
      "settings",
    ];
  else if (state.health && !state.health.ok)
    step = [
      "Check your connections",
      "Review the connection message above, correct your settings, then test again.",
      "Review connections",
      "settings",
    ];
  else if (!state.status.last_masters?.ok)
    step = [
      "Send your master data",
      "Create or update the items, customers and accounts that your transactions reference.",
      "Choose master data",
      "masters",
    ];
  else if (!enabledFlows().length)
    step = [
      "Choose your transaction flows",
      "Enable the transaction types you need, save your selection, then preview them in the workspace.",
      "Choose transactions",
      "flows",
    ];
  else
    step = [
      "Ready to review transactions",
      "Preview a flow’s pending records before starting a sync. Automatic sync uses your saved selection.",
      "Open flow workspace",
      "operations",
    ];
  $("#next-step-title").textContent = step[0];
  $("#next-step-detail").textContent = step[1];
  $("#next-step-action").textContent = step[2];
  $("#next-step-action").dataset.go = step[3];
}

function directionLabel(direction) {
  return direction === "erpnext_to_tally"
    ? "ERPNext → Tally"
    : direction === "tally_to_erpnext"
      ? "Tally → ERPNext"
      : "Both directions";
}

function formatTime(value) {
  if (!value) return "Not scheduled";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function historyTable(
  entries,
  compact = false,
  emptyMessage = "No synchronization runs have been recorded yet.",
) {
  if (!entries?.length)
    return `<div class="empty">${escapeHtml(emptyMessage)}</div>`;
  const rows = entries
    .slice(0, compact ? 5 : 100)
    .map((entry) => {
      const success = !entry.error && !entry.failed;
      const flow = state.flows.find((item) => item.key === entry.flow);
      const result = success
        ? `<span class="result-ok">${entry.succeeded || 0} succeeded</span>`
        : `<span class="result-error">${entry.failed ? `${entry.failed} failed` : "Run failed"}</span>`;
      return `<tr>
      <td>${escapeHtml(formatTime(entry.at))}</td>
      <td class="direction">${escapeHtml(directionLabel(entry.direction))}</td>
      <td><strong>${escapeHtml(flow?.title || entry.flow || "Control Centre")}</strong><br><small>${escapeHtml(entry.source || "manual")}</small></td>
      <td>${entry.fetched || 0} fetched</td>
      <td>${result}${entry.error ? `<div class="error-text">${escapeHtml(entry.error)}</div>` : ""}</td>
    </tr>`;
    })
    .join("");
  return `<div class="table-scroll"><table class="history-table"><thead><tr><th>Time</th><th>Direction</th><th>Flow</th><th>Records</th><th>Result</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderStatus() {
  const busy = isBusy();
  const mastersRunning = state.mastersPending || state.status.masters_running;
  $("#sync-status").textContent = state.healthPending
    ? "Checking connections"
    : state.previewPending
      ? "Loading preview"
      : busy
        ? mastersRunning
          ? "Sending master data"
          : "Syncing transactions"
        : state.status.last_error
          ? "Needs attention"
          : "Idle";
  $("#sync-detail").textContent = busy
    ? "Let the current request finish before starting another action."
    : state.status.last_error || "Ready for the next run";
  $("#running-badge").classList.toggle("hidden", !busy);
  const flows = enabledFlows();
  $$(".sync-button").forEach((button) => {
    const matching = flows.filter(
      (flow) =>
        button.dataset.direction === "all" ||
        flow.direction === button.dataset.direction,
    );
    button.disabled =
      busy ||
      state.healthPending ||
      state.serviceOffline ||
      Boolean(state.status.configuration_error) ||
      !matching.length;
    button.title = state.healthPending
      ? "Wait for the connection test to finish"
      : busy
        ? "Wait for the current sync to finish"
        : !matching.length
          ? "Enable and save a flow in this direction first"
          : `Run ${matching.length} enabled flow(s)`;
  });
  $("#sync-availability").textContent = busy
    ? "A sync is running. Other sync actions will be available when it finishes."
    : flows.length
      ? `${flows.length} enabled flow(s). Runs use your saved selection.`
      : "No available flows are enabled. Choose transaction flows and save your selection first.";
  if (!$("#auto-sync").dataset.pending)
    $("#auto-sync").checked = Boolean(state.status.auto_sync_enabled);
  $("#auto-sync").disabled =
    state.serviceOffline ||
    Boolean($("#auto-sync").dataset.pending) ||
    (!state.status.auto_sync_enabled &&
      (busy ||
        state.healthPending ||
        !flows.length ||
        Boolean(state.status.configuration_error)));
  $("#auto-description").textContent = state.status.auto_sync_enabled
    ? `Enabled · runs every ${state.config.poll_interval_seconds || 60} seconds. Master data is not included.`
    : "Off. Turn on to run your saved transaction flows on a schedule.";
  $("#next-auto-sync").textContent = formatTime(state.status.next_auto_sync);
  $("#auto-directions").innerHTML = (state.status.auto_sync_directions || [])
    .map(
      (direction) =>
        `<span class="tag">${escapeHtml(directionLabel(direction))}</span>`,
    )
    .join("");
  setMessage(
    "#configuration-alert",
    state.status.configuration_error
      ? `${state.status.configuration_error}. Review Connections & settings and choose your transaction flows.`
      : "",
  );
  $("#context-erp").textContent =
    state.config.erpnext_company || "Not configured";
  $("#context-tally").textContent =
    state.config.tally_company || "Not configured";
  $("#test-connections").disabled =
    busy ||
    state.healthPending ||
    Boolean($("#test-connections").dataset.pending);
  $("#test-connections").title = busy
    ? "Connection tests wait until the current sync finishes"
    : "Check saved ERPNext and Tally connections";
  $("#settings-test").disabled =
    state.configSaving ||
    busy ||
    state.healthPending ||
    Boolean($("#settings-test").dataset.pending);
  const settingsSave = $('#settings-form button[type="submit"]');
  settingsSave.disabled = Boolean(
    state.configSaving || settingsSave.dataset.pending,
  );
  for (const [name, key] of [
    ["submit_inbound_documents", "submit_documents"],
    ["update_stock_from_inbound", "update_stock"],
  ]) {
    if (!state.settingsFields.has(name))
      $("#settings-form").elements[name].checked = Boolean(
        state.config.flow_options?.[
          "express_tally.standard_vouchers_from_tally"
        ]?.[key],
      );
  }
  $("#recent-history").innerHTML = historyTable(
    state.status.history || [],
    true,
  );
  renderHistory();
  renderNextStep();
  if (state.initialized) refreshMastersState();
  document.dispatchEvent(new Event("bridge-state-change"));
}

function filteredHistory() {
  const term = state.historyFilter.trim().toLowerCase();
  return (state.status.history || []).filter((entry) => {
    if (state.historyDirection && entry.direction !== state.historyDirection)
      return false;
    if (!term) return true;
    return [entry.flow, entry.direction, entry.source, entry.error, entry.at]
      .join(" ")
      .toLowerCase()
      .includes(term);
  });
}

function renderHistory() {
  const entries = filteredHistory();
  $("#history-count").textContent =
    `${entries.length} run${entries.length === 1 ? "" : "s"}`;
  $("#full-history").innerHTML = historyTable(
    entries,
    false,
    "No synchronization runs match the current filters.",
  );
}

let stateLoading = false;
async function loadState() {
  if (stateLoading) return;
  stateLoading = true;
  try {
    const wasOffline = state.serviceOffline;
    state.status = await api("/api/v1/state");
    state.serviceOffline = false;
    setMessage("#service-alert");
    $("#service-dot").classList.remove("error");
    $("#service-label").textContent = "Bridge running";
    if (state.initialized && (wasOffline || !state.configLoaded)) {
      await loadConfig();
      await loadFlows();
      if (hasConnections() && !isBusy()) await loadHealth();
    }
  } catch (error) {
    state.serviceOffline = true;
    setMessage(
      "#service-alert",
      `${error.message} This page will reconnect automatically.`,
      true,
    );
    $("#service-dot").classList.add("error");
    $("#service-label").textContent = "Bridge unavailable";
  } finally {
    stateLoading = false;
    renderStatus();
  }
}

let healthLoading = false;
async function loadHealth(showToast = false) {
  if (healthLoading || isBusy()) {
    if (!state.health && isBusy()) {
      $("#erp-status").textContent = $("#tally-status").textContent =
        "Check after current operation";
      $("#erp-detail").textContent = $("#tally-detail").textContent =
        "Connection checks wait while Tally is in use.";
    }
    if (showToast)
      toast("Wait for the current sync or connection test to finish.");
    return state.health;
  }
  healthLoading = true;
  state.healthPending = true;
  renderStatus();
  try {
    const health = await api("/api/v1/health", { allowError: true });
    state.health = health;
    const erp = health.erpnext || {};
    const tally = health.tally || {};
    $("#erp-status").textContent = erp.ok ? "Connected" : "Not connected";
    $("#erp-detail").textContent = erp.ok
      ? `${erp.flow_count || 0} transaction flows discovered`
      : erp.error || "Review connection settings";
    $("#tally-status").textContent = tally.ok ? "Connected" : "Not connected";
    $("#tally-detail").textContent = tally.ok
      ? tally.loaded_tally_company || "Company loaded"
      : tally.error ||
        (tally.loaded_tally_company
          ? `Different company loaded: ${tally.loaded_tally_company}`
          : "Open your company in Tally and check its HTTP server");
    const errors = [
      !erp.ok && `ERPNext: ${$("#erp-detail").textContent}`,
      !tally.ok && `Tally: ${$("#tally-detail").textContent}`,
    ].filter(Boolean);
    setMessage("#connection-feedback", errors.join("\n"), !health.ok);
    if (showToast)
      toast(
        health.ok
          ? "Both connections are ready."
          : "Connection check needs attention. See the message above.",
        !health.ok,
      );
    return health;
  } catch (error) {
    state.health = { ok: false };
    $("#erp-status").textContent = "Check failed";
    $("#tally-status").textContent = "Check failed";
    $("#erp-detail").textContent = $("#tally-detail").textContent =
      error.message;
    setMessage("#connection-feedback", error.message, true);
    if (showToast) toast(error.message, true);
    return state.health;
  } finally {
    healthLoading = false;
    state.healthPending = false;
    renderStatus();
  }
}

async function loadConfig() {
  if (state.configLoading) return;
  state.configLoading = true;
  try {
    state.config = await api("/api/v1/config");
    state.configLoaded = true;
    if (state.configLoadError) {
      setMessage("#settings-error");
      state.configLoadError = false;
    }
    if (state.settingsDirty) return;
    const form = $("#settings-form");
    [
      "frappe_url",
      "api_key",
      "api_secret",
      "erpnext_company",
      "target_id",
      "tally_url",
      "tally_company",
      "voucher_date_override",
      "batch_size",
      "poll_interval_seconds",
      "request_timeout_seconds",
      "from_date",
      "to_date",
      "customer_parent",
      "supplier_parent",
      "master_batch_size",
      "ca_bundle",
    ].forEach((name) => {
      form.elements[name].value = state.config[name] ?? "";
    });
    form.elements.verify_tls.checked = state.config.verify_tls !== false;
    form.elements.open_browser_on_start.checked = Boolean(
      state.config.open_browser_on_start,
    );
    form.elements.start_with_windows.checked = Boolean(
      state.config.start_with_windows,
    );
    form.elements.auto_outbound.checked = (
      state.config.auto_sync_directions || []
    ).includes("erpnext_to_tally");
    form.elements.auto_inbound.checked = (
      state.config.auto_sync_directions || []
    ).includes("tally_to_erpnext");
    form.elements.submit_inbound_documents.checked = Boolean(
      state.config.flow_options?.["express_tally.standard_vouchers_from_tally"]
        ?.submit_documents,
    );
    form.elements.update_stock_from_inbound.checked = Boolean(
      state.config.flow_options?.["express_tally.standard_vouchers_from_tally"]
        ?.update_stock,
    );
  } finally {
    state.configLoading = false;
  }
}

function settingsPayload() {
  const form = $("#settings-form");
  const formData = new FormData(form);
  const nullable = (name) => formData.get(name) || null;
  return {
    frappe_url: String(formData.get("frappe_url") || "")
      .trim()
      .replace(/\/$/, ""),
    api_key: String(formData.get("api_key") || "").trim(),
    api_secret: String(formData.get("api_secret") || ""),
    erpnext_company: String(formData.get("erpnext_company") || "").trim(),
    target_id: String(formData.get("target_id") || "").trim(),
    tally_url: String(formData.get("tally_url") || "")
      .trim()
      .replace(/\/$/, ""),
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
        ...(state.config.flow_options?.[
          "express_tally.standard_vouchers_from_tally"
        ] || {}),
        submit_documents: state.settingsFields.has("submit_inbound_documents")
          ? form.elements.submit_inbound_documents.checked
          : Boolean(
              state.config.flow_options?.[
                "express_tally.standard_vouchers_from_tally"
              ]?.submit_documents,
            ),
        update_stock: state.settingsFields.has("update_stock_from_inbound")
          ? form.elements.update_stock_from_inbound.checked
          : Boolean(
              state.config.flow_options?.[
                "express_tally.standard_vouchers_from_tally"
              ]?.update_stock,
            ),
      },
    },
    open_browser_on_start: form.elements.open_browser_on_start.checked,
    start_with_windows: form.elements.start_with_windows.checked,
  };
}

async function saveSettings(showSuccess = true) {
  const form = $("#settings-form");
  if (!form.reportValidity())
    throw new Error("Complete the required settings first.");
  if (state.configSaving)
    throw new Error(
      "A configuration save is in progress. Wait for it to finish, then save your changes.",
    );
  state.configSaving = true;
  renderStatus();
  const revision = state.settingsRevision || 0;
  let response;
  try {
    response = await api("/api/v1/config", {
      method: "PUT",
      body: JSON.stringify(settingsPayload()),
    });
  } finally {
    state.configSaving = false;
    renderStatus();
  }
  state.config = response.config;
  if (revision === (state.settingsRevision || 0)) {
    form.elements.api_secret.value = state.config.api_secret;
    state.settingsDirty = false;
    state.settingsFields.clear();
    $("#settings-save-status").textContent = "All changes saved";
  }
  if (showSuccess) toast("Settings saved.");
  setMessage("#settings-error");
  await loadState();
}

function configuredFlowSelection() {
  return new Set(
    state.config.enabled_flows ||
      (state.config.flow_name ? [state.config.flow_name] : []),
  );
}

function currentFlowSelection() {
  if (!state.flowSelection) state.flowSelection = configuredFlowSelection();
  return state.flowSelection;
}

function flowMatchesSearch(flow, term) {
  if (!term) return true;
  return [
    flow.title,
    flow.key,
    flow.direction,
    flow.agent_profile,
    flow.unavailable_reason,
  ]
    .join(" ")
    .toLowerCase()
    .includes(term);
}

function updateFlowSaveStatus() {
  const selected = currentFlowSelection();
  const saved = configuredFlowSelection();
  const dirty =
    selected.size !== saved.size ||
    [...selected].some((key) => !saved.has(key));
  $("#flows-save-status").textContent =
    `${selected.size} selected · ${dirty ? "Unsaved changes" : "Selection saved"}`;
  $("#save-flows").disabled =
    !dirty || Boolean($("#save-flows").dataset.pending);
}

function renderFlows(errorMessage = "") {
  const focusedFlow = document.activeElement?.classList.contains("flow-check")
    ? document.activeElement.value
    : null;
  const selected = currentFlowSelection();
  const visible = state.flows.filter((flow) =>
    flowMatchesSearch(flow, state.flowFilter.trim().toLowerCase()),
  );
  ["#select-flows", "#clear-flows", "#refresh-flows"].forEach((id) => {
    $(id).disabled = Boolean(state.flowsSaving);
  });
  $("#flow-count").textContent =
    `${visible.length} of ${state.flows.length} flows`;
  setMessage("#flows-error", errorMessage, Boolean(errorMessage));
  updateFlowSaveStatus();
  if (!visible.length) {
    $("#flows-list").innerHTML =
      `<div class="empty">${escapeHtml(state.flows.length ? "No flows match your search." : "No flows loaded. Save your ERPNext connection settings, then refresh.")}</div>`;
    return;
  }
  $("#flows-list").innerHTML = ["erpnext_to_tally", "tally_to_erpnext"]
    .map((direction) => {
      const group = visible.filter((flow) => flow.direction === direction);
      if (!group.length) return "";
      return (
        `<h3 class="flow-group-heading">${escapeHtml(directionLabel(direction))}</h3>` +
        group
          .map(
            (
              flow,
            ) => `<div class="flow-row ${flow.available ? "" : "unavailable"}">
      <label><input class="flow-check" type="checkbox" value="${escapeHtml(flow.key)}" ${selected.has(flow.key) ? "checked" : ""} ${(flow.available || selected.has(flow.key)) && !state.flowsSaving ? "" : "disabled"} /><span><strong>${escapeHtml(flow.title || flow.key)}</strong><small>${escapeHtml(flow.available ? flow.description || "Configure this flow and preview its pending records in the workspace." : flow.unavailable_reason)}</small>${flow.exclusive_group ? "<small>Choose one flow in this group.</small>" : ""}</span></label>
      <button class="link-button" data-open-flow="${escapeHtml(flow.key)}">Configure &amp; preview</button>
    </div>`,
          )
          .join("")
      );
    })
    .join("");
  $$(".flow-check").forEach((input) =>
    input.addEventListener("change", () => {
      const flow = state.flows.find((entry) => entry.key === input.value);
      if (input.checked) {
        if (flow.exclusive_group)
          state.flows
            .filter(
              (entry) =>
                entry.exclusive_group === flow.exclusive_group &&
                entry.key !== flow.key,
            )
            .forEach((entry) => selected.delete(entry.key));
        selected.add(input.value);
      } else selected.delete(input.value);
      renderFlows();
    }),
  );
  if (focusedFlow)
    $$(".flow-check")
      .find((input) => input.value === focusedFlow)
      ?.focus();
  $$("[data-open-flow]").forEach((button) =>
    button.addEventListener("click", () => {
      showPage("operations");
      action(
        button,
        () => openFlow(button.dataset.openFlow),
        "#operation-error",
      );
    }),
  );
}

async function loadFlows(showToast = false) {
  try {
    const response = await api("/api/v1/flows");
    state.flows = response.flows || [];
    renderFlows();
    if (state.initialized) refreshOperations();
    renderStatus();
    if (showToast)
      toast(
        `${state.flows.length} transaction flows loaded. Your selection is preserved.`,
      );
  } catch (error) {
    renderFlows(error.message);
    if (showToast) toast(error.message, true);
  }
}

function selectAvailableFlows() {
  const selected = currentFlowSelection();
  const groups = new Set(
    state.flows
      .filter((flow) => selected.has(flow.key))
      .map((flow) => flow.exclusive_group)
      .filter(Boolean),
  );
  state.flows
    .filter((flow) => flow.available)
    .forEach((flow) => {
      if (flow.exclusive_group && groups.has(flow.exclusive_group)) return;
      selected.add(flow.key);
      if (flow.exclusive_group) groups.add(flow.exclusive_group);
    });
  renderFlows();
}

function clearFlowSelection() {
  state.flowSelection = new Set();
  renderFlows();
}

async function saveFlows() {
  if (state.flowsSaving) return;
  const enabled = [...currentFlowSelection()];
  const groups = new Map();
  enabled.forEach((key) => {
    const flow = state.flows.find((entry) => entry.key === key);
    if (!flow?.exclusive_group) return;
    if (groups.has(flow.exclusive_group)) {
      throw new Error(
        `${groups.get(flow.exclusive_group)} and ${flow.title || flow.key} cannot both be enabled.`,
      );
    }
    groups.set(flow.exclusive_group, flow.title || flow.key);
  });
  if (state.configSaving)
    throw new Error(
      "A configuration save is in progress. Wait for it to finish.",
    );
  state.flowsSaving = true;
  state.configSaving = true;
  renderFlows();
  renderStatus();
  try {
    const response = await api("/api/v1/config", {
      method: "PUT",
      body: JSON.stringify({ enabled_flows: enabled, flow_name: "" }),
    });
    state.config = response.config;
    state.flowSelection = configuredFlowSelection();
    toast("Enabled flows saved.");
    refreshOperations();
    await loadState();
  } finally {
    state.flowsSaving = false;
    state.configSaving = false;
    renderFlows();
    renderStatus();
  }
}

async function triggerSync(direction) {
  if (isBusy() || state.healthPending) return;
  state.syncPending = true;
  renderStatus();
  try {
    const response = await api("/api/v1/sync", {
      method: "POST",
      body: JSON.stringify({ direction }),
    });
    toast(
      response.message ||
        "Transaction sync started. Follow its results in Run history.",
    );
    await loadState();
  } catch (error) {
    setMessage("#connection-feedback", error.message, true);
    toast(error.message, true);
  } finally {
    state.syncPending = false;
    renderStatus();
  }
}

async function toggleAutoSync(event) {
  const desired = event.target.checked;
  if (desired && (isBusy() || state.healthPending)) {
    event.target.checked = false;
    toast("Wait for the current operation before enabling automatic sync.");
    return;
  }
  event.target.dataset.pending = "true";
  event.target.disabled = true;
  try {
    await api("/api/v1/auto-sync", {
      method: "POST",
      body: JSON.stringify({ enabled: desired }),
    });
    toast(desired ? "Automatic sync enabled." : "Automatic sync disabled.");
    await loadState();
  } catch (error) {
    event.target.checked = !desired;
    toast(error.message, true);
  } finally {
    delete event.target.dataset.pending;
    renderStatus();
  }
}

function wireEvents() {
  $$(".nav-item").forEach((button) =>
    button.addEventListener("click", () => showPage(button.dataset.page)),
  );
  $$("[data-go]").forEach((button) =>
    button.addEventListener("click", () => showPage(button.dataset.go)),
  );
  $$(".sync-button").forEach((button) =>
    button.addEventListener("click", () =>
      triggerSync(button.dataset.direction),
    ),
  );
  $("#auto-sync").addEventListener("change", toggleAutoSync);
  $("#test-connections").addEventListener("click", (event) =>
    action(event.currentTarget, () => loadHealth(true), "#connection-feedback"),
  );
  $("#settings-test").addEventListener("click", (event) =>
    action(
      event.currentTarget,
      async () => {
        await saveSettings(false);
        await loadHealth(true);
        await loadFlows();
      },
      "#settings-error",
    ),
  );
  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    action(event.submitter, () => saveSettings(), "#settings-error");
  });
  $("#refresh-flows").addEventListener("click", (event) =>
    action(event.currentTarget, () => loadFlows(true), "#flows-error"),
  );
  $("#flow-search").addEventListener("input", (event) => {
    state.flowFilter = event.target.value;
    renderFlows();
  });
  $("#select-flows").addEventListener("click", selectAvailableFlows);
  $("#clear-flows").addEventListener("click", clearFlowSelection);
  $("#history-search").addEventListener("input", (event) => {
    state.historyFilter = event.target.value;
    renderHistory();
  });
  $("#history-direction").addEventListener("change", (event) => {
    state.historyDirection = event.target.value;
    renderHistory();
  });
  $("#stop-bridge").addEventListener("click", async () => {
    if (
      !window.confirm(
        "Stop the local bridge? The Control Centre will close until it is restarted.",
      )
    )
      return;
    try {
      await api("/api/v1/shutdown", { method: "POST", body: "{}" });
      toast(
        "Bridge stopping. Restart it from start-control-centre.cmd or the scheduled task.",
      );
    } catch (error) {
      toast(error.message, true);
    }
  });
  $("#reset-checkpoints").addEventListener("click", async () => {
    if (
      !window.confirm(
        "Reset the inbound checkpoint for this target? Tally records will be offered to ERPNext again; existing sync logs still prevent duplicate versions.",
      )
    )
      return;
    try {
      await api("/api/v1/checkpoints/reset", { method: "POST", body: "{}" });
      toast("Inbound checkpoint reset.");
    } catch (error) {
      toast(error.message, true);
    }
  });
  $("#save-flows").addEventListener("click", (event) =>
    action(event.currentTarget, saveFlows, "#flows-error").then(
      updateFlowSaveStatus,
    ),
  );
  $("#settings-form").addEventListener("input", (event) => {
    state.settingsDirty = true;
    if (event.target.name) state.settingsFields.add(event.target.name);
    state.settingsRevision = (state.settingsRevision || 0) + 1;
    $("#settings-save-status").textContent = "Unsaved changes";
  });
  $("#settings-form").addEventListener(
    "invalid",
    (event) => {
      for (
        let parent = event.target.parentElement;
        parent;
        parent = parent.parentElement
      )
        if (parent.tagName === "DETAILS") parent.open = true;
    },
    true,
  );
  window.addEventListener("hashchange", () => showPage(location.hash.slice(1)));
  window.addEventListener("beforeunload", (event) => {
    const selected = currentFlowSelection();
    const saved = configuredFlowSelection();
    if (
      state.settingsDirty ||
      selected.size !== saved.size ||
      [...selected].some((key) => !saved.has(key))
    ) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

async function initialize() {
  wireEvents();
  showPage(location.hash.slice(1) || "overview", false);
  const results = await Promise.allSettled([loadConfig(), loadState()]);
  if (results[0].status === "rejected") {
    state.configLoadError = true;
    setMessage("#settings-error", results[0].reason.message, true);
  }
  initializeOperations({
    state,
    api,
    toast,
    escapeHtml,
    directionLabel,
    loadConfig,
    loadState,
    onBusyChange: renderStatus,
  });
  initializeMasters({
    state,
    api,
    toast,
    escapeHtml,
    loadState,
    onBusyChange: renderStatus,
  });
  state.initialized = true;
  renderStatus();
  await loadFlows();
  setInterval(loadState, 2000);
  setInterval(() => {
    if (
      !document.hidden &&
      hasConnections() &&
      !state.serviceOffline &&
      !isBusy()
    )
      loadHealth();
  }, 30000);
  if (hasConnections()) await loadHealth();
  else {
    $("#erp-status").textContent = $("#tally-status").textContent =
      "Not configured";
    $("#erp-detail").textContent = $("#tally-detail").textContent =
      "Start in Connections & settings";
  }
}

initialize().catch((error) => {
  setMessage("#service-alert", error.message, true);
  toast(error.message, true);
});
