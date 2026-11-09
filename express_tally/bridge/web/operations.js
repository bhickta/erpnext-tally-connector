const $ = (selector, root = document) => root.querySelector(selector);

let dependencies;
let details = {};
let previewRecords = [];
let loadedFlowKey = "";
let detailsRequest = 0;
let previewRequest = 0;
const drafts = new Map();
const pending = new Set();

function feedback(message = "") {
  const element = $("#operation-feedback");
  if (element) {
    element.textContent = message;
    element.classList.toggle("hidden", !message);
  }
}

function notifyBusyChange() {
  if (dependencies.onBusyChange) dependencies.onBusyChange();
  else document.dispatchEvent(new Event("bridge-state-change"));
}

function tallyUnavailable() {
  const state = dependencies.state;
  return Boolean(
    state.status?.running ||
      state.status?.masters_running ||
      state.syncPending ||
      state.mastersPending ||
      state.healthPending ||
      state.serviceOffline,
  );
}

function updateControls() {
  const flow = selectedFlow();
  const loaded = Boolean(flow && loadedFlowKey === flow.key);
  const busy = pending.size > 0;
  const syncing = tallyUnavailable();
  $("#operation-flow").disabled = pending.has("save") || pending.has("run");
  $("#refresh-operation").disabled = busy || !flow;
  $("#preview-operation").disabled =
    busy || syncing || !loaded || flow.available === false;
  $("#retry-operation").disabled =
    busy || syncing || !loaded || flow.available === false;
  $("#save-flow-options").disabled = busy || !loaded;
  $("#add-mapping").disabled = busy || !loaded;
  $("#add-preview-ledgers").disabled =
    busy || !loaded || !previewRecords.length;
  const notice = $("#operation-unsaved");
  if (notice) {
    notice.textContent = drafts.has(flow?.key)
      ? "Unsaved changes — save before running this flow."
      : "";
    notice.classList.toggle("hidden", !drafts.has(flow?.key));
  }
  for (const id of ["flow-options", "mapping-rows"]) {
    $("#" + id).inert = busy || !loaded;
  }
}

function rememberDraft() {
  if (!loadedFlowKey) return;
  drafts.set(loadedFlowKey, {
    options: [...$("#flow-options").querySelectorAll("[data-flow-option]")].map(
      (input) => ({
        name: input.dataset.flowOption,
        value: input.value,
        checked: input.checked,
      }),
    ),
    mappings: [...$("#mapping-rows").querySelectorAll(".mapping-row")].map(
      (row) =>
        Object.fromEntries(
          [...row.querySelectorAll("[data-map]")].map((input) => [
            input.dataset.map,
            input.value,
          ]),
        ),
    ),
  });
  updateControls();
}

function restoreDraft(flowKey) {
  const draft = drafts.get(flowKey);
  if (!draft) return;
  for (const input of $("#flow-options").querySelectorAll(
    "[data-flow-option]",
  )) {
    const value = draft.options.find(
      (option) => option.name === input.dataset.flowOption,
    );
    if (!value) continue;
    input.value = value.value;
    input.checked = value.checked;
  }
  if (details.configuration?.supports_ledger_mappings) {
    $("#mapping-rows").innerHTML = draft.mappings
      .map((row) => mappingRow(row.ledger, row))
      .join("");
  }
}

function clearPreview() {
  previewRequest += 1;
  previewRecords = [];
  $("#preview-count").textContent = "Not loaded";
  $("#preview-results").innerHTML =
    '<div class="empty">Select Preview records to inspect this flow before running it.</div>';
}

const table = (headers, rows) => {
  if (!rows.length) return '<div class="empty">No records found.</div>';
  return `<div class="table-scroll"><table class="history-table"><thead><tr>${headers.map((header) => `<th>${dependencies.escapeHtml(header)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
};

const cell = (value, className = "") =>
  `<td class="${className}">${dependencies.escapeHtml(value ?? "")}</td>`;

function selectedFlow() {
  const key = $("#operation-flow").value;
  return dependencies.state.flows.find((flow) => flow.key === key);
}

function showError(message = "") {
  const element = $("#operation-error");
  element.textContent = message;
  element.classList.toggle("hidden", !message);
}

function renderFlowChoices() {
  const select = $("#operation-flow");
  const previous = select.value;
  const enabled = new Set(dependencies.state.config.enabled_flows || []);
  if (dependencies.state.config.flow_name)
    enabled.add(dependencies.state.config.flow_name);
  const flows = [...dependencies.state.flows].sort((left, right) => {
    const enabledDifference =
      Number(enabled.has(right.key)) - Number(enabled.has(left.key));
    return (
      enabledDifference ||
      (left.title || left.key).localeCompare(right.title || right.key)
    );
  });
  select.innerHTML = flows.length
    ? flows
        .map(
          (flow) =>
            `<option value="${dependencies.escapeHtml(flow.key)}">${enabled.has(flow.key) ? "Enabled — " : ""}${dependencies.escapeHtml(flow.title || flow.key)}</option>`,
        )
        .join("")
    : '<option value="">No flows available — check Connections</option>';
  if (flows.some((flow) => flow.key === previous)) select.value = previous;
}

function renderStatus() {
  const status = details.status || {};
  const flow = details.flow || selectedFlow() || {};
  const reconciliation = status.reconciliation || {};
  $("#operation-status").innerHTML = [
    ["Direction", dependencies.directionLabel(flow.direction)],
    ["Pending", status.pending ?? "—"],
    ["Successful", status.successful ?? 0],
    ["Failed", status.failed ?? 0],
    ...(reconciliation.matched !== undefined
      ? [
          ["Matched", reconciliation.matched],
          ["Mismatched", reconciliation.mismatched],
        ]
      : []),
  ]
    .map(
      ([label, value]) =>
        `<article class="mini-stat"><span>${dependencies.escapeHtml(label)}</span><strong>${dependencies.escapeHtml(value)}</strong></article>`,
    )
    .join("");
}

function renderChecklist() {
  const flow = selectedFlow();
  const steps = details.configuration?.setup_steps || [];
  if (!steps.length) {
    $("#setup-checklist").innerHTML =
      '<div class="empty">No special first-run steps for this flow.</div>';
    return;
  }
  const storageKey = `express-tally-checklist:${flow.key}`;
  let saved = [];
  try {
    saved = JSON.parse(localStorage.getItem(storageKey) || "[]");
  } catch {
    /* Storage may be disabled or stale. */
  }
  const checked = new Set(Array.isArray(saved) ? saved : []);
  $("#setup-checklist").innerHTML = steps
    .map(
      (step, index) =>
        `<label class="check checklist-row"><input type="checkbox" data-step="${index}" ${checked.has(index) ? "checked" : ""}/><span>${dependencies.escapeHtml(step)}</span></label>`,
    )
    .join("");
  $("#setup-checklist")
    .querySelectorAll("input")
    .forEach((input) =>
      input.addEventListener("change", () => {
        const values = [
          ...$("#setup-checklist").querySelectorAll("input:checked"),
        ].map((element) => Number(element.dataset.step));
        try {
          localStorage.setItem(storageKey, JSON.stringify(values));
        } catch {
          /* Checklist remains usable without storage. */
        }
      }),
    );
}

function renderOptions() {
  const flow = selectedFlow();
  const schema = details.configuration?.option_schema || [];
  const defaults = flow?.default_options || {};
  const configured = dependencies.state.config.flow_options?.[flow?.key] || {};
  $("#flow-options").innerHTML = schema.length
    ? schema
        .map((field) => {
          const value =
            configured[field.fieldname] ?? defaults[field.fieldname] ?? "";
          if (field.type === "checkbox") {
            return `<label class="check wide"><input data-flow-option="${dependencies.escapeHtml(field.fieldname)}" type="checkbox" ${value ? "checked" : ""} ${field.read_only ? "disabled" : ""}/><span>${dependencies.escapeHtml(field.label)}</span></label>`;
          }
          return `<label>${dependencies.escapeHtml(field.label)}<input data-flow-option="${dependencies.escapeHtml(field.fieldname)}" type="${["number", "date", "text"].includes(field.type) ? field.type : "text"}" value="${dependencies.escapeHtml(value)}" ${field.step ? `step="${dependencies.escapeHtml(field.step)}"` : ""} ${field.min !== undefined ? `min="${dependencies.escapeHtml(field.min)}"` : ""} ${field.read_only ? "readonly" : ""}/></label>`;
        })
        .join("")
    : '<div class="empty wide">This flow has no additional settings.</div>';
  $("#save-flow-options").classList.toggle(
    "hidden",
    !schema.length && !details.configuration?.supports_ledger_mappings,
  );
}

function mappingValue(value) {
  return typeof value === "string"
    ? { account: value, party_type: "", party: "" }
    : {
        account: value?.account || "",
        party_type: value?.party_type || "",
        party: value?.party || "",
      };
}

function mappingRow(tallyLedger = "", value = {}) {
  const mapping = mappingValue(value);
  return `<div class="mapping-row">
    <label>Tally ledger<input data-map="ledger" value="${dependencies.escapeHtml(tallyLedger)}" placeholder="Exact Tally ledger name" /></label>
    <label>ERPNext account<input data-map="account" list="account-options" value="${dependencies.escapeHtml(mapping.account)}" placeholder="Leaf account" /></label>
    <label>Party type<select data-map="party_type"><option value="">None</option><option value="Customer" ${mapping.party_type === "Customer" ? "selected" : ""}>Customer</option><option value="Supplier" ${mapping.party_type === "Supplier" ? "selected" : ""}>Supplier</option></select></label>
    <label>Party<input data-map="party" list="${mapping.party_type === "Supplier" ? "supplier-options" : "customer-options"}" value="${dependencies.escapeHtml(mapping.party)}" placeholder="Customer or Supplier ID" /></label>
    <button type="button" class="icon-button remove-mapping" title="Remove mapping" aria-label="Remove mapping">×</button>
  </div>`;
}

function renderMappings() {
  const supports = Boolean(details.configuration?.supports_ledger_mappings);
  $("#mapping-panel").classList.toggle("hidden", !supports);
  if (!supports) return;
  const accounts = details.configuration.accounts || [];
  const parties = details.configuration.parties || {};
  $("#account-options").innerHTML = accounts
    .map(
      (account) =>
        `<option value="${dependencies.escapeHtml(account.name)}">${dependencies.escapeHtml(account.account_name || account.name)} — ${dependencies.escapeHtml(account.account_type || "")}</option>`,
    )
    .join("");
  ["Customer", "Supplier"].forEach((partyType) => {
    const titleField =
      partyType === "Customer" ? "customer_name" : "supplier_name";
    $(`#${partyType.toLowerCase()}-options`).innerHTML = (
      parties[partyType] || []
    )
      .map(
        (party) =>
          `<option value="${dependencies.escapeHtml(party.name)}">${dependencies.escapeHtml(party[titleField] || party.name)}</option>`,
      )
      .join("");
  });
  const flow = selectedFlow();
  const mappings =
    dependencies.state.config.flow_options?.[flow.key]?.ledger_mappings || {};
  $("#mapping-rows").innerHTML =
    Object.entries(mappings)
      .map(([ledger, value]) => mappingRow(ledger, value))
      .join("") ||
    '<div class="empty mapping-empty">No explicit mappings. Exact account and party names resolve automatically.</div>';
}

function renderActivity() {
  const activity = details.diagnostics?.activity || [];
  const rows = activity.map(
    (row) =>
      `<tr>${cell(row.creation)}${cell(row.status, row.status === "Failed" ? "result-error" : "result-ok")}${cell(row.operation)}${cell(row.source_name || row.source_reference)}${cell(row.target_reference)}${cell(row.error, "error-text")}<td>${row.status === "Failed" ? '<button class="link-button retry-record" title="Run this entire flow, including pending failed records">Retry flow</button>' : ""}</td></tr>`,
  );
  $("#activity-results").innerHTML = table(
    ["Time", "Status", "Operation", "Source", "Target", "Error", "Action"],
    rows,
  );
  $("#activity-results")
    .querySelectorAll(".retry-record")
    .forEach((button) =>
      button.addEventListener("click", () =>
        retryFlow().catch((error) => showError(error.message)),
      ),
    );
}

function renderReconciliation() {
  const reconciliation = details.diagnostics?.reconciliation || [];
  const rows = reconciliation.map(
    (row, index) =>
      `<tr>${cell(row.as_of_date)}${cell(row.tally_ledger)}${cell(row.erpnext_account)}${cell(row.party || "")}${cell(row.tally_balance)}${cell(row.erpnext_balance)}${cell(row.difference, row.status === "Mismatch" ? "result-error" : "result-ok")}${cell(row.status)}<td>${row.status === "Mismatch" && details.configuration?.supports_ledger_mappings ? `<button class="link-button map-mismatch" data-index="${index}">Map</button>` : ""}</td></tr>`,
  );
  $("#reconciliation-results").innerHTML = table(
    [
      "As of",
      "Tally ledger",
      "ERPNext account",
      "Party",
      "Tally",
      "ERPNext",
      "Difference",
      "Status",
      "Action",
    ],
    rows,
  );
  $("#reconciliation-results")
    .querySelectorAll(".map-mismatch")
    .forEach((button) =>
      button.addEventListener("click", () => {
        if (pending.size || loadedFlowKey !== selectedFlow()?.key) return;
        const row = reconciliation[Number(button.dataset.index)];
        $(".mapping-empty")?.remove();
        $("#mapping-rows").insertAdjacentHTML(
          "beforeend",
          mappingRow(row.tally_ledger, {
            account: row.erpnext_account,
            party_type: row.party_type,
            party: row.party,
          }),
        );
        rememberDraft();
        $("#mapping-panel").scrollIntoView({ behavior: "smooth" });
      }),
    );
}

function renderPreview(response) {
  previewRecords = response.records || [];
  $("#preview-count").textContent =
    `${response.count ?? previewRecords.length} shown (up to 50)`;
  const rows = previewRecords.map(
    (row) =>
      `<tr>${cell(row.source)}${cell(row.kind)}${cell(row.operation)}${cell(row.date)}${cell(row.party)}${cell(row.amount)}${cell((row.ledgers || []).join(", "))}</tr>`,
  );
  $("#preview-results").innerHTML = table(
    [
      "Source",
      "Kind",
      "Operation",
      "Date",
      "Party / ledger",
      "Amount",
      "Ledgers",
    ],
    rows,
  );
}

async function loadDetails() {
  const flow = selectedFlow();
  const request = ++detailsRequest;
  const switched = flow?.key !== loadedFlowKey;
  showError();
  if (switched) {
    loadedFlowKey = "";
    details = {};
    clearPreview();
    $("#operation-status").innerHTML = "";
    for (const id of [
      "setup-checklist",
      "flow-options",
      "activity-results",
      "reconciliation-results",
    ]) {
      $("#" + id).innerHTML =
        `<div class="empty">${flow ? "Loading flow details…" : "Connect ERPNext and refresh flows to get started."}</div>`;
    }
    $("#mapping-panel").classList.add("hidden");
  }
  if (!flow) {
    pending.delete("details");
    feedback("");
    updateControls();
    return;
  }
  pending.add("details");
  feedback("Loading flow details…");
  updateControls();
  try {
    const response = await dependencies.api(
      `/api/v1/flow-details?flow=${encodeURIComponent(flow.key)}&limit=100`,
    );
    if (request !== detailsRequest || selectedFlow()?.key !== flow.key) return;
    details = response;
    loadedFlowKey = flow.key;
    renderStatus();
    renderChecklist();
    renderOptions();
    renderMappings();
    renderActivity();
    renderReconciliation();
    restoreDraft(flow.key);
    feedback(
      drafts.has(flow.key)
        ? "Details refreshed. Your unsaved changes are preserved."
        : "",
    );
  } catch (error) {
    if (request === detailsRequest) {
      showError(error.message);
      feedback("");
    }
  } finally {
    if (request === detailsRequest) {
      pending.delete("details");
      updateControls();
    }
  }
}

async function preview() {
  const flow = selectedFlow();
  if (!flow || pending.size) return;
  if (drafts.has(flow.key)) {
    showError(
      "Save this flow’s configuration changes before previewing its records.",
    );
    return;
  }
  const request = ++previewRequest;
  if (tallyUnavailable()) {
    showError(
      "Wait for the current sync or connection test to finish, and check that the bridge is connected.",
    );
    return;
  }
  pending.add("preview");
  dependencies.state.previewPending = true;
  showError();
  feedback("Loading a preview of up to 50 records…");
  updateControls();
  notifyBusyChange();
  try {
    const response = await dependencies.api(
      `/api/v1/preview?flow=${encodeURIComponent(flow.key)}&limit=50`,
    );
    if (request !== previewRequest || selectedFlow()?.key !== flow.key) return;
    renderPreview(response);
    feedback(
      previewRecords.length
        ? "Preview only. No records have been imported."
        : "No pending records were returned for this preview.",
    );
  } catch (error) {
    if (request === previewRequest) {
      showError(error.message);
      feedback("");
    }
  } finally {
    pending.delete("preview");
    dependencies.state.previewPending = false;
    updateControls();
    notifyBusyChange();
  }
}

function collectMappings() {
  const mappings = Object.create(null);
  $("#mapping-rows")
    .querySelectorAll(".mapping-row")
    .forEach((row) => {
      const value = (name) =>
        row.querySelector(`[data-map="${name}"]`).value.trim();
      const ledger = value("ledger");
      const account = value("account");
      const partyType = value("party_type");
      const party = value("party");
      if (!ledger && !account && !partyType && !party) return;
      if (!ledger || !account)
        throw new Error(
          "Every mapping requires a Tally ledger and ERPNext account.",
        );
      if (Boolean(partyType) !== Boolean(party))
        throw new Error(
          `Mapping ${ledger} requires both party type and party.`,
        );
      if (
        Object.keys(mappings).some(
          (key) => key.toLowerCase() === ledger.toLowerCase(),
        )
      )
        throw new Error(`Tally ledger ${ledger} is mapped more than once.`);
      mappings[ledger] = partyType
        ? { account, party_type: partyType, party }
        : account;
    });
  return mappings;
}

async function saveOptions() {
  const flow = selectedFlow();
  if (!flow || pending.size || loadedFlowKey !== flow.key) return;
  const flowOptions = { ...(dependencies.state.config.flow_options || {}) };
  const options = { ...(flowOptions[flow.key] || {}) };
  for (const input of $("#flow-options").querySelectorAll(
    "[data-flow-option]",
  )) {
    if (input.readOnly || input.disabled) continue;
    if (!input.reportValidity()) return;
    options[input.dataset.flowOption] =
      input.type === "checkbox"
        ? input.checked
        : input.type === "number"
          ? Number(input.value)
          : input.value;
  }
  if (details.configuration?.supports_ledger_mappings)
    options.ledger_mappings = collectMappings();
  flowOptions[flow.key] = options;
  pending.add("save");
  showError();
  feedback("Saving flow configuration…");
  updateControls();
  try {
    const response = await dependencies.api("/api/v1/config", {
      method: "PUT",
      body: JSON.stringify({ flow_options: flowOptions }),
    });
    dependencies.state.config = response.config;
    drafts.delete(flow.key);
    dependencies.toast("Flow configuration saved.");
    feedback("Flow configuration saved. You can preview or run this flow.");
  } finally {
    pending.delete("save");
    updateControls();
  }
}

function addPreviewLedgers() {
  const existing = new Set(
    [...$("#mapping-rows").querySelectorAll('[data-map="ledger"]')].map(
      (input) => input.value.trim(),
    ),
  );
  const ledgers = [
    ...new Set(
      previewRecords
        .flatMap((record) => [record.party, ...(record.ledgers || [])])
        .filter(Boolean),
    ),
  ].filter((ledger) => !existing.has(ledger));
  $(".mapping-empty")?.remove();
  $("#mapping-rows").insertAdjacentHTML(
    "beforeend",
    ledgers.map((ledger) => mappingRow(ledger)).join(""),
  );
  if (ledgers.length) rememberDraft();
  dependencies.toast(
    ledgers.length
      ? `${ledgers.length} preview ledger(s) added. Choose accounts and save the configuration.`
      : "All preview ledgers already have mapping rows.",
  );
}

async function retryFlow() {
  const flow = selectedFlow();
  if (!flow || pending.size || loadedFlowKey !== flow.key) return;
  if (drafts.has(flow.key))
    throw new Error(
      "Save this flow's configuration changes before running it.",
    );
  if (tallyUnavailable())
    throw new Error(
      "Wait for the current sync or connection test to finish, and check that the bridge is connected.",
    );
  pending.add("run");
  dependencies.state.syncPending = true;
  showError();
  feedback("Starting this flow…");
  updateControls();
  notifyBusyChange();
  try {
    await dependencies.api("/api/v1/sync", {
      method: "POST",
      body: JSON.stringify({
        direction: flow.direction,
        flow_keys: [flow.key],
      }),
    });
    dependencies.toast(
      "Flow started. Check Dashboard or Run history for its result.",
    );
    feedback(
      "Flow started. This runs the entire flow, including pending failed records. Check Run history for the result.",
    );
    await dependencies.loadState();
  } finally {
    pending.delete("run");
    dependencies.state.syncPending = false;
    updateControls();
    notifyBusyChange();
  }
}

// Called after the flow list is refreshed or its enabled selection is saved.
export function refreshOperations() {
  if (!dependencies) return;
  renderFlowChoices();
  return loadDetails();
}

export async function openFlow(key) {
  if (!dependencies) return;
  renderFlowChoices();
  const select = $("#operation-flow");
  if (![...select.options].some((option) => option.value === key)) return;
  select.value = key;
  await loadDetails();
}

export function initializeOperations(values) {
  dependencies = values;
  renderFlowChoices();
  const handleError = (error) => {
    showError(error.message);
    feedback("");
  };
  $("#operation-flow").addEventListener("change", loadDetails);
  $("#refresh-operation").addEventListener("click", loadDetails);
  $("#preview-operation").addEventListener("click", preview);
  $("#retry-operation").addEventListener("click", () =>
    retryFlow().catch(handleError),
  );
  $("#save-flow-options").addEventListener("click", () =>
    saveOptions().catch(handleError),
  );
  $("#add-mapping").addEventListener("click", () => {
    $(".mapping-empty")?.remove();
    $("#mapping-rows").insertAdjacentHTML("beforeend", mappingRow());
    rememberDraft();
    $("#mapping-rows .mapping-row:last-child input")?.focus();
  });
  $("#mapping-rows").addEventListener("click", (event) => {
    const remove = event.target.closest(".remove-mapping");
    if (remove) {
      remove.closest(".mapping-row").remove();
      rememberDraft();
    }
  });
  $("#mapping-rows").addEventListener("change", (event) => {
    if (event.target.dataset.map === "party_type") {
      const party = event.target
        .closest(".mapping-row")
        .querySelector('[data-map="party"]');
      party.setAttribute(
        "list",
        event.target.value === "Supplier"
          ? "supplier-options"
          : "customer-options",
      );
    }
  });
  for (const id of ["flow-options", "mapping-rows"]) {
    $("#" + id).addEventListener("input", rememberDraft);
    $("#" + id).addEventListener("change", rememberDraft);
  }
  $("#add-preview-ledgers").addEventListener("click", addPreviewLedgers);
  document.addEventListener("bridge-state-change", updateControls);
  window.addEventListener("beforeunload", (event) => {
    if (drafts.size) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  loadDetails();
}
