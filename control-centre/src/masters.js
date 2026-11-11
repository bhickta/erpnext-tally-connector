const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

let dependencies;
let selectedKeys = null;
let loading = false;
let cancelling = false;
let loadVersion = 0;
let latestResult = null;
let remoteResultSignature = "";

function traceUrl(traceId) {
  return `/api/v1/masters/trace?id=${encodeURIComponent(traceId)}`;
}

function showError(message = "", traceId = "") {
  const element = $("#masters-error");
  if (!element) return;
  element.textContent = message;
  if (message && traceId) {
    const link = document.createElement("a");
    link.href = traceUrl(traceId);
    link.download = "";
    link.textContent = "Download debug trace";
    element.append(" ", link);
  }
  element.classList.toggle("hidden", !message);
}

function showProgress(message = "") {
  const element = $("#masters-progress");
  if (!element) return;
  if (element.textContent !== message) element.textContent = message;
  element.classList.toggle("hidden", !message);
}

function formatTime(value) {
  if (!value) return "Not run";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function countText(master, summary) {
  const count = summary?.[master.key];
  if (count === undefined) return "Count unavailable";
  if (master.key !== "Item") return `${count} record${count === 1 ? "" : "s"}`;
  return `${count} item${count === 1 ? "" : "s"} · ${summary?.["Item Group"] ?? "—"} groups · ${summary?.UOM ?? "—"} units`;
}

function mastersRunning() {
  return Boolean(
    dependencies.state.mastersPending ||
      dependencies.state.status?.masters_running,
  );
}

function connectionConfigured() {
  const config = dependencies.state.config || {};
  return [
    "frappe_url",
    "api_key",
    "api_secret",
    "erpnext_company",
    "target_id",
    "tally_url",
    "tally_company",
  ].every((key) => Boolean(String(config[key] || "").trim()));
}

function renderMasters(payload) {
  const masters = payload?.masters || [];
  const container = $("#masters-list");
  if (!container) return;
  // Counts and result refreshes must not silently change the user's selection.
  if (selectedKeys === null && masters.length)
    selectedKeys = new Set(masters.map((master) => master.key));
  if (!masters.length) {
    container.innerHTML =
      '<div class="empty">No master types loaded. Save your connection settings, then refresh counts.</div>';
    updateControls();
    return;
  }
  container.innerHTML = masters
    .map(
      (master) => `<label class="master-row">
    <input class="master-check" type="checkbox" value="${dependencies.escapeHtml(master.key)}" ${selectedKeys.has(master.key) ? "checked" : ""} />
    <span class="master-copy"><strong>${dependencies.escapeHtml(master.label || master.key)}</strong><small>${dependencies.escapeHtml(master.description || master.key)}</small></span>
    <span class="tag">${dependencies.escapeHtml(master.tally_type || "")}</span>
    <span class="master-count">${dependencies.escapeHtml(countText(master, payload.summary))}</span>
  </label>`,
    )
    .join("");
  $$(".master-check").forEach((input) =>
    input.addEventListener("change", () => {
      if (input.checked) selectedKeys.add(input.value);
      else selectedKeys.delete(input.value);
      updateControls();
    }),
  );
  updateControls();
}

function updateControls() {
  if (!dependencies) return;
  const running = mastersRunning();
  const flowRunning = Boolean(
    dependencies.state.status?.running || dependencies.state.syncPending,
  );
  const checking = Boolean(
    dependencies.state.healthPending || dependencies.state.previewPending,
  );
  const offline = Boolean(dependencies.state.serviceOffline);
  const busy = running || flowRunning;
  const selected = $$(".master-check:checked").length;
  const total = $$(".master-check").length;
  const counter = $("#masters-selected-count");
  if (counter) counter.textContent = `${selected} of ${total} selected`;
  const help = $("#masters-selection-help");
  if (help)
    help.textContent = offline
      ? "The local bridge is unavailable. Wait for it to reconnect before starting a sync."
      : checking
        ? "Wait for the connection check or record preview to finish before syncing masters."
        : !connectionConfigured()
          ? "Save the ERPNext and Tally connection settings before syncing masters."
          : !selected
            ? "Select the master types you want to send."
            : `${selected} master type${selected === 1 ? "" : "s"} will be sent to ${dependencies.state.config.tally_company}. Sync Units and Item Groups before Items.`;
  const button = $("#sync-masters");
  if (button) {
    button.disabled =
      busy ||
      checking ||
      offline ||
      loading ||
      !selected ||
      !connectionConfigured();
    button.textContent = running
      ? "Syncing to Tally…"
      : "Sync selected to Tally";
  }
  $$(
    ".master-check, #select-masters, #clear-masters, #masters-include-disabled, #masters-include-openings, #masters-limit, #masters-duplicate-names",
  ).forEach((input) => {
    input.disabled = busy;
  });
  const refresh = $("#refresh-masters");
  if (refresh) {
    refresh.disabled = busy || offline || loading;
    refresh.textContent = loading ? "Refreshing…" : "Refresh counts";
  }
  const cancelButton = $("#cancel-masters");
  if (cancelButton) {
    cancelButton.classList.toggle("hidden", !running);
    cancelButton.disabled = cancelling || offline;
    cancelButton.textContent = cancelling
      ? "Stopping after current request…"
      : "Stop after current request";
  }
  $("#masters-list")?.setAttribute("aria-busy", String(loading || running));
  if (offline)
    showProgress(
      "The local bridge is unavailable. This page will reconnect automatically. If a sync was running, check Tally before starting again.",
    );
  else if (running)
    showProgress(
      cancelling
        ? "Stop requested. Tally will finish the current request; remaining batches will be skipped."
        : "Sending selected masters to Tally in small batches. Keep Tally open. You can stop after the current request.",
    );
  else if (flowRunning)
    showProgress(
      "A transaction sync is running. Wait for it to finish before syncing masters.",
    );
  else if (checking)
    showProgress(
      "A connection check or record preview is using Tally. Master sync will be available when it finishes.",
    );
  else showProgress();
}

function setMastersSelected(checked) {
  if (mastersRunning()) return;
  $$(".master-check").forEach((input) => {
    input.checked = checked;
    if (checked) selectedKeys.add(input.value);
    else selectedKeys.delete(input.value);
  });
  updateControls();
}

function renderResults(result) {
  const container = $("#masters-results");
  if (!container || !result) return;
  latestResult = result;
  const lastRun = $("#masters-last-run");
  if (lastRun) lastRun.textContent = formatTime(result.at);
  if (!result.results?.length) {
    container.innerHTML =
      '<div class="empty">No master batch results were returned. Refresh counts and check the connection before trying again.</div>';
    return;
  }
  const escape = dependencies.escapeHtml;
  const totals = result.results.reduce(
    (sum, row) => ({
      created: sum.created + Number(row.created || 0),
      altered: sum.altered + Number(row.altered || 0),
      ignored: sum.ignored + Number(row.ignored || 0),
    }),
    { created: 0, altered: 0, ignored: 0 },
  );
  const title = result.cancelled
    ? "Sync stopped"
    : result.ok
      ? "Sync complete"
      : "Sync needs attention";
  const summary = `<div class="result-summary ${result.ok ? "result-ok" : ""}" role="status"><strong>${title}</strong><p>${totals.created} created · ${totals.altered} updated · ${totals.ignored} ignored by Tally</p></div>`;
  const messages = result.results.map((row) => row.message || "").join(" ");
  const uncertain =
    /outcome unknown|paused|timed? out|timeout|connection failure|Cannot reach Tally/i.test(
      messages,
    );
  const recovery = uncertain
    ? '<div class="alert">The last request may still be running in Tally. Check Tally and the affected records. Once Tally is responsive, clear the pause and test the connection. <button type="button" class="button secondary" id="resume-tally">Resume Tally requests</button></div>'
    : result.cancelled
      ? '<p class="muted">Completed changes are kept. Review the results before starting another sync.</p>'
      : !result.ok
        ? '<p class="muted">Review the message for the failed batch and correct it before running again. Completed changes are kept.</p>'
        : "";
  const countNote = result.summary_error
    ? `<p class="muted">Sync results are available, but source counts could not be refreshed: ${escape(result.summary_error)}</p>`
    : "";
  const traceNote = result.trace_id
    ? `<p class="muted">Send this run’s debug trace when reporting a problem: <a href="${traceUrl(result.trace_id)}" download>download debug trace</a></p>`
    : "";
  const skipped = result.skipped || [];
  const skippedNote = skipped.length
    ? `<div class="alert">${skipped.length} duplicate name${skipped.length === 1 ? " was" : "s were"} skipped. Choose “Add ERPNext ID” to include them on the next run. ${escape(
        skipped
          .slice(0, 3)
          .map((row) => `${row.master}: ${row.name}`)
          .join(", "),
      )}${skipped.length > 3 ? "…" : ""}</div>`
    : "";
  const rows = result.results
    .map(
      (row) => `<tr>
    <td><strong>${escape(row.label || row.batch)}</strong></td>
    <td><span class="${row.status === "Success" ? "result-ok" : row.status === "Failed" ? "result-error" : "tag"}">${escape(row.status)}</span></td>
    <td>${escape(row.created ?? 0)}</td><td>${escape(row.altered ?? 0)}</td><td>${escape(row.ignored ?? 0)}</td><td>${escape(row.errors ?? 0)}</td>
    <td class="error-text">${escape(row.message || "—")}</td>
  </tr>`,
    )
    .join("");
  container.innerHTML = `${summary}${recovery}${countNote}${traceNote}${skippedNote}<div class="table-scroll"><table class="history-table">
    <caption class="visually-hidden">Results of the latest master sync</caption>
    <thead><tr><th scope="col">Master type</th><th scope="col">Status</th><th scope="col">Created</th><th scope="col">Updated</th><th scope="col">Ignored</th><th scope="col">Errors</th><th scope="col">Details</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
  container
    .querySelector("#resume-tally")
    ?.addEventListener("click", resumeTally);
}

async function resumeTally() {
  try {
    const result = await dependencies.api("/api/v1/tally/resume", {
      method: "POST",
      body: "{}",
    });
    dependencies.toast(
      result.message ||
        "Tally request pause cleared. Test the connection before retrying.",
    );
    await dependencies.loadHealth?.(true);
  } catch (error) {
    dependencies.toast(error.message, true);
  }
}

export function refreshMastersState() {
  if (!dependencies) return;
  const result = dependencies.state.status?.last_masters;
  const signature = result ? JSON.stringify(result) : "";
  if (!mastersRunning() && result && signature !== remoteResultSignature) {
    remoteResultSignature = signature;
    renderResults(result);
  }
  if (!mastersRunning()) cancelling = false;
  updateControls();
}

export async function loadMasters(showToast = false) {
  if (!dependencies || loading || dependencies.state.mastersPending) return;
  const version = ++loadVersion;
  loading = true;
  showError();
  updateControls();
  try {
    const payload = await dependencies.api("/api/v1/masters");
    if (version !== loadVersion) return;
    dependencies.state.masters = payload;
    renderMasters(payload);
    if (!latestResult && payload.last_result)
      renderResults(payload.last_result);
    if (showToast)
      dependencies.toast(
        "Master counts refreshed. Your selection is unchanged.",
      );
  } catch (error) {
    if (version !== loadVersion) return;
    showError(error.message);
    if (showToast) dependencies.toast(error.message, true);
  } finally {
    if (version === loadVersion) loading = false;
    updateControls();
  }
}

async function syncMasters() {
  if (
    mastersRunning() ||
    loading ||
    dependencies.state.status?.running ||
    dependencies.state.syncPending ||
    dependencies.state.healthPending ||
    dependencies.state.previewPending ||
    dependencies.state.serviceOffline
  )
    return;
  const masters = $$(".master-check:checked").map((input) => input.value);
  if (!masters.length)
    return dependencies.toast("Select at least one master type.", true);
  const limitInput = $("#masters-limit");
  const rawLimit = limitInput?.value.trim() || "";
  const limit = rawLimit ? Number(rawLimit) : null;
  if (limit !== null && (!Number.isSafeInteger(limit) || limit < 1)) {
    showError(
      "Enter a whole number of at least 1 for the limit, or leave it blank to sync all records.",
    );
    limitInput?.focus();
    return;
  }
  if (limitInput && !limitInput.reportValidity()) return;
  const request = {
    masters,
    include_disabled: $("#masters-include-disabled")?.checked || false,
    include_opening_balances: $("#masters-include-openings")?.checked || false,
    limit,
    duplicate_names: $("#masters-duplicate-names")?.value || "skip",
  };
  // Invalidate count requests that began before this run so old data cannot
  // overwrite the completed result or selection.
  loadVersion += 1;
  dependencies.state.mastersPending = true;
  // Remember the pre-run server result. A later poll may still contain it;
  // compare server versions rather than browser/server clocks.
  remoteResultSignature = dependencies.state.status?.last_masters
    ? JSON.stringify(dependencies.state.status.last_masters)
    : "";
  cancelling = false;
  showError();
  updateControls();
  dependencies.onBusyChange?.();
  try {
    const result = await dependencies.api("/api/v1/masters/sync", {
      method: "POST",
      body: JSON.stringify(request),
    });
    const completed = { ...result, at: result.at || new Date().toISOString() };
    dependencies.state.masters = {
      ...(dependencies.state.masters || {}),
      summary: result.summary_error
        ? dependencies.state.masters?.summary
        : result.summary || dependencies.state.masters?.summary,
      last_result: completed,
    };
    renderMasters(dependencies.state.masters);
    renderResults(completed);
    const failed = result.results?.find((row) => row.status === "Failed");
    dependencies.toast(
      result.cancelled
        ? "Master sync stopped. Completed changes are kept."
        : result.ok
          ? "Masters synced to Tally."
          : failed?.message ||
            "Some master batches need attention. Review the results.",
      !result.ok && !result.cancelled,
    );
  } catch (error) {
    showError(
      `${error.message} If the connection was lost while syncing, check Tally and the imported records before starting again.`,
      error.traceId,
    );
    dependencies.toast(error.message, true);
  } finally {
    dependencies.state.mastersPending = false;
    cancelling = false;
    updateControls();
    dependencies.onBusyChange?.();
    await dependencies.loadState?.();
  }
}

async function cancelSync() {
  if (cancelling || !mastersRunning() || dependencies.state.serviceOffline)
    return;
  cancelling = true;
  updateControls();
  try {
    const result = await dependencies.api("/api/v1/masters/cancel", {
      method: "POST",
      body: "{}",
    });
    if (!result.cancelled) cancelling = false;
    dependencies.toast(
      result.message ||
        (result.cancelled
          ? "Stop requested. Waiting for the current Tally request."
          : "No master sync is running."),
    );
  } catch (error) {
    cancelling = false;
    showError(error.message);
    dependencies.toast(error.message, true);
  }
  updateControls();
}

export function initializeMasters(values) {
  dependencies = values;
  $("#refresh-masters")?.addEventListener("click", () => loadMasters(true));
  $("#sync-masters")?.addEventListener("click", syncMasters);
  $("#cancel-masters")?.addEventListener("click", cancelSync);
  $("#select-masters")?.addEventListener("click", () =>
    setMastersSelected(true),
  );
  $("#clear-masters")?.addEventListener("click", () =>
    setMastersSelected(false),
  );
  refreshMastersState();
  loadMasters();
}
