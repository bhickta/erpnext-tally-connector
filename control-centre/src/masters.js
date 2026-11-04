const $ = (selector, root = document) => root.querySelector(selector);

let dependencies;

function showError(message = "") {
  const element = $("#masters-error");
  if (!element) return;
  element.textContent = message;
  element.classList.toggle("hidden", !message);
}

function formatTime(value) {
  if (!value) return "Not run";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function countText(master, summary) {
  const count = summary?.[master.key];
  if (count === undefined) return "—";
  if (master.key !== "Item") return `${count} record${count === 1 ? "" : "s"}`;
  const groups = summary?.["Item Group"];
  const units = summary?.["UOM"];
  return `${count} item${count === 1 ? "" : "s"} · ${groups ?? "—"} groups · ${units ?? "—"} units`;
}

function renderMasters(payload) {
  const masters = payload?.masters || [];
  const summary = payload?.summary || {};
  const container = $("#masters-list");
  if (!container) return;
  if (!masters.length) {
    container.innerHTML = '<div class="empty">No master types are available.</div>';
    return;
  }
  container.innerHTML = masters.map((master) => `<label class="master-row">
    <input class="master-check" type="checkbox" value="${dependencies.escapeHtml(master.key)}" checked />
    <span class="master-copy">
      <strong>${dependencies.escapeHtml(master.label || master.key)}</strong>
      <small>${dependencies.escapeHtml(master.description || master.key)}</small>
    </span>
    <span class="tag">${dependencies.escapeHtml(master.tally_type || "")}</span>
    <span class="master-count">${dependencies.escapeHtml(countText(master, summary))}</span>
  </label>`).join("");
  container.querySelectorAll(".master-check").forEach((input) => input.addEventListener("change", updateMasterSelectionCount));
  updateMasterSelectionCount();
  if (payload.last_result) renderResults(payload.last_result);
}

function updateMasterSelectionCount() {
  const total = document.querySelectorAll(".master-check").length;
  const selected = document.querySelectorAll(".master-check:checked").length;
  const counter = $("#masters-selected-count");
  if (counter) counter.textContent = `${selected} of ${total} selected`;
}

function setMastersSelected(checked) {
  document.querySelectorAll(".master-check").forEach((input) => { input.checked = checked; });
  updateMasterSelectionCount();
}

function renderResults(result) {
  const container = $("#masters-results");
  const lastRun = $("#masters-last-run");
  if (!container) return;
  if (!result?.results?.length) {
    container.innerHTML = '<div class="empty">No master batch results were returned.</div>';
    if (lastRun) lastRun.textContent = formatTime(result?.at);
    return;
  }
  if (lastRun) lastRun.textContent = formatTime(result.at);
  const skipped = result.skipped || [];
  const skippedNote = skipped.length
    ? `<div class="alert">${skipped.length} duplicate master name(s) skipped. ${dependencies.escapeHtml(
        skipped.slice(0, 3).map((row) => `${row.master}: ${row.name}`).join(", "),
      )}${skipped.length > 3 ? "…" : ""}</div>`
    : "";
  const rows = result.results.map((row) => {
    const statusClass = row.status === "Success" ? "result-ok" : "result-error";
    return `<tr>
      <td><strong>${dependencies.escapeHtml(row.label || row.batch)}</strong><br><small>${dependencies.escapeHtml(row.batch)}</small></td>
      <td><span class="${statusClass}">${dependencies.escapeHtml(row.status)}</span></td>
      <td>${row.created ?? 0}</td>
      <td>${row.altered ?? 0}</td>
      <td>${row.ignored ?? 0}</td>
      <td>${row.errors ?? 0}</td>
      <td class="error-text">${dependencies.escapeHtml(row.message || "")}</td>
    </tr>`;
  }).join("");
  container.innerHTML = `${skippedNote}<div class="table-scroll"><table class="history-table">
    <thead><tr><th>Batch</th><th>Status</th><th>Created</th><th>Altered</th><th>Ignored</th><th>Errors</th><th>Message</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

export async function loadMasters(showToast = false) {
  showError();
  try {
    const payload = await dependencies.api("/api/v1/masters");
    dependencies.state.masters = payload;
    renderMasters(payload);
    if (showToast) dependencies.toast("Master counts refreshed.");
  } catch (error) {
    showError(error.message);
    if (showToast) dependencies.toast(error.message, true);
  }
}

async function syncMasters() {
  const masters = [...document.querySelectorAll(".master-check:checked")].map((input) => input.value);
  if (!masters.length) {
    dependencies.toast("Select at least one master type.", true);
    return;
  }
  const button = $("#sync-masters");
  const cancelButton = $("#cancel-masters");
  const previousLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Syncing…";
  cancelButton?.classList.remove("hidden");
  showError();
  try {
    const result = await dependencies.api("/api/v1/masters/sync", {
      method: "POST",
      body: JSON.stringify({
        masters,
        include_disabled: $("#masters-include-disabled")?.checked || false,
        include_opening_balances: $("#masters-include-openings")?.checked || false,
        limit: Number($("#masters-limit")?.value) || null,
        duplicate_names: $("#masters-duplicate-names")?.value || "skip",
      }),
    });
    renderResults({ at: new Date().toISOString(), ...result });
    if (result.summary) {
      dependencies.state.masters = { ...(dependencies.state.masters || {}), summary: result.summary };
      renderMasters(dependencies.state.masters);
    }
    const failed = result.results?.filter((row) => row.status !== "Success") || [];
    if (result.ok) {
      dependencies.toast("Masters synced to Tally.");
    } else {
      dependencies.toast(failed[0]?.message || "Some master batches failed. See the results.", true);
    }
  } catch (error) {
    showError(error.message);
    dependencies.toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = previousLabel;
    cancelButton?.classList.add("hidden");
  }
}

async function cancelSync() {
  try {
    const result = await dependencies.api("/api/v1/masters/cancel", {
      method: "POST",
      body: "{}",
    });
    dependencies.toast(result.message || (result.cancelled ? "Cancelling…" : "No sync is running."), !result.cancelled);
  } catch (error) {
    dependencies.toast(error.message, true);
  }
}

export function initializeMasters(values) {
  dependencies = values;
  $("#refresh-masters")?.addEventListener("click", () => loadMasters(true));
  $("#sync-masters")?.addEventListener("click", syncMasters);
  $("#cancel-masters")?.addEventListener("click", cancelSync);
  $("#select-masters")?.addEventListener("click", () => setMastersSelected(true));
  $("#clear-masters")?.addEventListener("click", () => setMastersSelected(false));
  loadMasters();
}
