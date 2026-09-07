const $ = (selector, root = document) => root.querySelector(selector);

let dependencies;
let details = {};
let previewRecords = [];

const table = (headers, rows) => {
  if (!rows.length) return '<div class="empty">No records found.</div>';
  return `<div class="table-scroll"><table class="history-table"><thead><tr>${headers.map((header) => `<th>${dependencies.escapeHtml(header)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
};

const cell = (value, className = "") => `<td class="${className}">${dependencies.escapeHtml(value ?? "")}</td>`;

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
  if (dependencies.state.config.flow_name) enabled.add(dependencies.state.config.flow_name);
  const flows = [...dependencies.state.flows].sort((left, right) => {
    const enabledDifference = Number(enabled.has(right.key)) - Number(enabled.has(left.key));
    return enabledDifference || (left.title || left.key).localeCompare(right.title || right.key);
  });
  select.innerHTML = flows.map((flow) => `<option value="${dependencies.escapeHtml(flow.key)}">${enabled.has(flow.key) ? "Enabled — " : ""}${dependencies.escapeHtml(flow.title || flow.key)}</option>`).join("");
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
    ...(reconciliation.matched !== undefined ? [["Matched", reconciliation.matched], ["Mismatched", reconciliation.mismatched]] : []),
  ].map(([label, value]) => `<article class="mini-stat"><span>${dependencies.escapeHtml(label)}</span><strong>${dependencies.escapeHtml(value)}</strong></article>`).join("");
}

function renderChecklist() {
  const flow = selectedFlow();
  const steps = details.configuration?.setup_steps || [];
  if (!steps.length) {
    $("#setup-checklist").innerHTML = '<div class="empty">No special first-run steps for this flow.</div>';
    return;
  }
  const storageKey = `express-tally-checklist:${flow.key}`;
  const checked = new Set(JSON.parse(localStorage.getItem(storageKey) || "[]"));
  $("#setup-checklist").innerHTML = steps.map((step, index) => `<label class="check checklist-row"><input type="checkbox" data-step="${index}" ${checked.has(index) ? "checked" : ""}/><span>${dependencies.escapeHtml(step)}</span></label>`).join("");
  $("#setup-checklist").querySelectorAll("input").forEach((input) => input.addEventListener("change", () => {
    const values = [...$("#setup-checklist").querySelectorAll("input:checked")].map((element) => Number(element.dataset.step));
    localStorage.setItem(storageKey, JSON.stringify(values));
  }));
}

function renderOptions() {
  const flow = selectedFlow();
  const schema = details.configuration?.option_schema || [];
  const defaults = flow?.default_options || {};
  const configured = dependencies.state.config.flow_options?.[flow?.key] || {};
  $("#flow-options").innerHTML = schema.length ? schema.map((field) => {
    const value = configured[field.fieldname] ?? defaults[field.fieldname] ?? "";
    if (field.type === "checkbox") {
      return `<label class="check wide"><input data-flow-option="${field.fieldname}" type="checkbox" ${value ? "checked" : ""} ${field.read_only ? "disabled" : ""}/><span>${dependencies.escapeHtml(field.label)}</span></label>`;
    }
    return `<label>${dependencies.escapeHtml(field.label)}<input data-flow-option="${field.fieldname}" type="${field.type || "text"}" value="${dependencies.escapeHtml(value)}" ${field.step ? `step="${field.step}"` : ""} ${field.min !== undefined ? `min="${field.min}"` : ""} ${field.read_only ? "readonly" : ""}/></label>`;
  }).join("") : '<div class="empty wide">This flow has no additional settings.</div>';
  $("#save-flow-options").classList.toggle("hidden", !schema.length && !details.configuration?.supports_ledger_mappings);
}

function mappingValue(value) {
  return typeof value === "string" ? { account: value, party_type: "", party: "" } : {
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
    <button class="icon-button remove-mapping" title="Remove mapping">×</button>
  </div>`;
}

function bindMappingRows() {
  $("#mapping-rows").querySelectorAll(".remove-mapping").forEach((button) => button.addEventListener("click", () => button.closest(".mapping-row").remove()));
  $("#mapping-rows").querySelectorAll('[data-map="party_type"]').forEach((select) => select.addEventListener("change", () => {
    const party = select.closest(".mapping-row").querySelector('[data-map="party"]');
    party.setAttribute("list", select.value === "Supplier" ? "supplier-options" : "customer-options");
  }));
}

function renderMappings() {
  const supports = Boolean(details.configuration?.supports_ledger_mappings);
  $("#mapping-panel").classList.toggle("hidden", !supports);
  if (!supports) return;
  const accounts = details.configuration.accounts || [];
  const parties = details.configuration.parties || {};
  $("#account-options").innerHTML = accounts.map((account) => `<option value="${dependencies.escapeHtml(account.name)}">${dependencies.escapeHtml(account.account_name || account.name)} — ${dependencies.escapeHtml(account.account_type || "")}</option>`).join("");
  ["Customer", "Supplier"].forEach((partyType) => {
    const titleField = partyType === "Customer" ? "customer_name" : "supplier_name";
    $(`#${partyType.toLowerCase()}-options`).innerHTML = (parties[partyType] || []).map((party) => `<option value="${dependencies.escapeHtml(party.name)}">${dependencies.escapeHtml(party[titleField] || party.name)}</option>`).join("");
  });
  const flow = selectedFlow();
  const mappings = dependencies.state.config.flow_options?.[flow.key]?.ledger_mappings || {};
  $("#mapping-rows").innerHTML = Object.entries(mappings).map(([ledger, value]) => mappingRow(ledger, value)).join("") || '<div class="empty mapping-empty">No explicit mappings. Exact account and party names resolve automatically.</div>';
  bindMappingRows();
}

function renderActivity() {
  const activity = details.diagnostics?.activity || [];
  const rows = activity.map((row) => `<tr>${cell(row.creation)}${cell(row.status, row.status === "Failed" ? "result-error" : "result-ok")}${cell(row.operation)}${cell(row.source_name || row.source_reference)}${cell(row.target_reference)}${cell(row.error, "error-text")}<td>${row.status === "Failed" ? '<button class="link-button retry-record">Retry</button>' : ""}</td></tr>`);
  $("#activity-results").innerHTML = table(["Time", "Status", "Operation", "Source", "Target", "Error", "Action"], rows);
  $("#activity-results").querySelectorAll(".retry-record").forEach((button) => button.addEventListener("click", () => retryFlow().catch((error) => showError(error.message))));
}

function renderReconciliation() {
  const reconciliation = details.diagnostics?.reconciliation || [];
  const rows = reconciliation.map((row, index) => `<tr>${cell(row.as_of_date)}${cell(row.tally_ledger)}${cell(row.erpnext_account)}${cell(row.party || "")}${cell(row.tally_balance)}${cell(row.erpnext_balance)}${cell(row.difference, row.status === "Mismatch" ? "result-error" : "result-ok")}${cell(row.status)}<td>${row.status === "Mismatch" ? `<button class="link-button map-mismatch" data-index="${index}">Map</button>` : ""}</td></tr>`);
  $("#reconciliation-results").innerHTML = table(["As of", "Tally ledger", "ERPNext account", "Party", "Tally", "ERPNext", "Difference", "Status", "Action"], rows);
  $("#reconciliation-results").querySelectorAll(".map-mismatch").forEach((button) => button.addEventListener("click", () => {
    const row = reconciliation[Number(button.dataset.index)];
    $(".mapping-empty")?.remove();
    $("#mapping-rows").insertAdjacentHTML("beforeend", mappingRow(row.tally_ledger, {
      account: row.erpnext_account,
      party_type: row.party_type,
      party: row.party,
    }));
    bindMappingRows();
    $("#mapping-panel").scrollIntoView({ behavior: "smooth" });
  }));
}

function renderPreview(response) {
  previewRecords = response.records || [];
  $("#preview-count").textContent = `${response.count || 0} pending`;
  const rows = previewRecords.map((row) => `<tr>${cell(row.source)}${cell(row.kind)}${cell(row.operation)}${cell(row.date)}${cell(row.party)}${cell(row.amount)}${cell((row.ledgers || []).join(", "))}</tr>`);
  $("#preview-results").innerHTML = table(["Source", "Kind", "Operation", "Date", "Party / ledger", "Amount", "Ledgers"], rows);
}

async function loadDetails() {
  const flow = selectedFlow();
  if (!flow) return;
  showError();
  try {
    details = await dependencies.api(`/api/v1/flow-details?flow=${encodeURIComponent(flow.key)}&limit=100`);
    renderStatus(); renderChecklist(); renderOptions(); renderMappings(); renderActivity(); renderReconciliation();
  } catch (error) {
    showError(error.message);
  }
}

async function preview() {
  const flow = selectedFlow();
  if (!flow) return;
  showError();
  try {
    renderPreview(await dependencies.api(`/api/v1/preview?flow=${encodeURIComponent(flow.key)}&limit=50`));
  } catch (error) {
    showError(error.message);
  }
}

function collectMappings() {
  const mappings = {};
  $("#mapping-rows").querySelectorAll(".mapping-row").forEach((row) => {
    const value = (name) => row.querySelector(`[data-map="${name}"]`).value.trim();
    const ledger = value("ledger");
    const account = value("account");
    const partyType = value("party_type");
    const party = value("party");
    if (!ledger && !account && !partyType && !party) return;
    if (!ledger || !account) throw new Error("Every mapping requires a Tally ledger and ERPNext account.");
    if (Boolean(partyType) !== Boolean(party)) throw new Error(`Mapping ${ledger} requires both party type and party.`);
    mappings[ledger] = partyType ? { account, party_type: partyType, party } : account;
  });
  return mappings;
}

async function saveOptions() {
  const flow = selectedFlow();
  const flowOptions = { ...(dependencies.state.config.flow_options || {}) };
  const options = { ...(flowOptions[flow.key] || {}) };
  $("#flow-options").querySelectorAll("[data-flow-option]").forEach((input) => {
    if (input.readOnly || input.disabled) return;
    options[input.dataset.flowOption] = input.type === "checkbox" ? input.checked : (input.type === "number" ? Number(input.value) : input.value);
  });
  if (details.configuration?.supports_ledger_mappings) options.ledger_mappings = collectMappings();
  flowOptions[flow.key] = options;
  const response = await dependencies.api("/api/v1/config", { method: "PUT", body: JSON.stringify({ flow_options: flowOptions }) });
  dependencies.state.config = response.config;
  dependencies.toast("Flow configuration saved.");
  await loadDetails();
}

function addPreviewLedgers() {
  const existing = new Set([...$("#mapping-rows").querySelectorAll('[data-map="ledger"]')].map((input) => input.value.trim()));
  const ledgers = [...new Set(previewRecords.flatMap((record) => [record.party, ...(record.ledgers || [])]).filter(Boolean))].filter((ledger) => !existing.has(ledger));
  $(".mapping-empty")?.remove();
  $("#mapping-rows").insertAdjacentHTML("beforeend", ledgers.map((ledger) => mappingRow(ledger)).join(""));
  bindMappingRows();
  dependencies.toast(`${ledgers.length} preview ledger(s) added for mapping.`);
}

async function retryFlow() {
  const flow = selectedFlow();
  await dependencies.api("/api/v1/sync", { method: "POST", body: JSON.stringify({ direction: flow.direction, flow_keys: [flow.key] }) });
  dependencies.toast("Flow started. Failed records remain pending and will be retried.");
  await dependencies.loadState();
}

export function initializeOperations(values) {
  dependencies = values;
  renderFlowChoices();
  $("#operation-flow").addEventListener("change", loadDetails);
  $("#refresh-operation").addEventListener("click", loadDetails);
  $("#preview-operation").addEventListener("click", preview);
  $("#retry-operation").addEventListener("click", () => retryFlow().catch((error) => showError(error.message)));
  $("#save-flow-options").addEventListener("click", () => saveOptions().catch((error) => showError(error.message)));
  $("#add-mapping").addEventListener("click", () => {
    $(".mapping-empty")?.remove();
    $("#mapping-rows").insertAdjacentHTML("beforeend", mappingRow());
    bindMappingRows();
  });
  $("#add-preview-ledgers").addEventListener("click", addPreviewLedgers);
  loadDetails();
}
