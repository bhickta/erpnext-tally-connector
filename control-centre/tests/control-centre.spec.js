import { test as base, expect } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const outbound = { key: "test.items", title: "Items to Tally", direction: "erpnext_to_tally", available: true };
const inbound = { key: "test.ledgers", title: "Ledgers to ERPNext", direction: "tally_to_erpnext", available: true };
const config = {
  frappe_url: "https://erp.example.com", api_key: "key", api_secret: "********",
  erpnext_company: "ERP Company", target_id: "local-tally", tally_company: "Tally Company",
  tally_url: "http://127.0.0.1:9000", batch_size: 20, master_batch_size: 10,
  poll_interval_seconds: 60, request_timeout_seconds: 30,
  customer_parent: "Sundry Debtors", supplier_parent: "Sundry Creditors",
  enabled_flows: [outbound.key], auto_sync_directions: [outbound.direction], flow_options: {},
  verify_tls: true, open_browser_on_start: true, start_with_windows: true,
};
const masterResult = (status = "Success", message = "") => ({
  at: "2026-11-09T09:30:00Z", ok: status === "Success",
  results: [{ batch: "items", label: "Items", status, message, created: 3, altered: 1, ignored: 0, errors: status === "Failed" ? 1 : 0 }],
  summary: { Item: 4 },
});

const test = base.extend({
  backend: async ({ page }, use) => {
    const backend = {
      config: structuredClone(config), flows: [outbound, inbound],
      status: { running: false, masters_running: false, configuration_error: "", history: [], auto_sync_enabled: false },
      calls: [], errors: [], handlers: {},
    };
    page.on("pageerror", (error) => backend.errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const body = request.postDataJSON();
      backend.calls.push({ path: url.pathname, method: request.method(), body });
      const reply = (payload, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });
      if (backend.handlers[url.pathname]) {
        await backend.handlers[url.pathname]({ route, request, url, body, reply });
        return;
      }
      switch (url.pathname) {
        case "/api/v1/config":
          if (request.method() === "PUT") {
            backend.config = { ...backend.config, ...body };
            return reply({ config: backend.config });
          }
          return reply(backend.config);
        case "/api/v1/state": return reply(backend.status);
        case "/api/v1/health": return reply({ ok: true, erpnext: { ok: true, flow_count: backend.flows.length }, tally: { ok: true, loaded_tally_company: "Tally Company" } });
        case "/api/v1/flows": return reply({ flows: backend.flows });
        case "/api/v1/masters": return reply({ masters: [{ key: "Item", label: "Items", description: "Items, groups and units", tally_type: "Stock Item" }, { key: "Customer", label: "Customers", description: "Customer ledgers", tally_type: "Ledger" }], summary: { Item: 4, Customer: 2, "Item Group": 1, UOM: 1 } });
        case "/api/v1/masters/sync": return reply(masterResult());
        case "/api/v1/masters/cancel": return reply({ cancelled: true, message: "Cancelling after the current Tally request" });
        case "/api/v1/flow-details": {
          const flow = backend.flows.find((entry) => entry.key === url.searchParams.get("flow"));
          return reply({ flow, status: { pending: 2, successful: 4, failed: 0 }, configuration: { setup_steps: ["Open the correct company in Tally."], option_schema: [{ fieldname: "limit", label: "Record limit", type: "number", min: 1 }], supports_ledger_mappings: flow?.direction === "tally_to_erpnext", accounts: [], parties: {} }, diagnostics: { activity: [], reconciliation: [] } });
        }
        case "/api/v1/preview": return reply({ count: 1, records: [{ source: "ITEM-1", kind: "Item", operation: "Create", party: "Acme", ledgers: ["Sales"], amount: 10 }] });
        case "/api/v1/sync": return reply({ ok: true, message: "Synchronization started." });
        case "/api/v1/auto-sync": backend.status.auto_sync_enabled = body.enabled; return reply({ enabled: body.enabled });
        default: return reply({ error: `Unexpected endpoint ${url.pathname}` }, 404);
      }
    });
    await use(backend);
    expect(backend.errors, "The UI must handle errors without unhandled exceptions").toEqual([]);
  },
});

async function openPage(page, section) {
  await page.goto("/");
  await expect(page.locator("#erp-status")).toHaveText("Connected");
  await page.locator(`.nav-item[data-page="${section}"]`).click();
  await expect(page.locator(`[data-page-panel="${section}"]`)).toBeVisible();
}

test("navigation preserves the active screen after reload", async ({ page, backend }) => {
  await openPage(page, "masters");
  await page.reload();
  await expect(page.locator('[data-page-panel="masters"]')).toBeVisible();
  await expect(page.locator('.nav-item[data-page="masters"]')).toHaveAttribute("aria-current", "page");
});

test("flow selections survive searching and save only the chosen flows", async ({ page, backend }) => {
  await openPage(page, "flows");
  await page.locator(`.flow-check[value="${inbound.key}"]`).check();
  await page.locator("#flow-search").fill("Items");
  await expect(page.locator(".flow-check")).toHaveCount(1);
  await page.locator("#save-flows").click();
  await expect.poll(() => backend.config.enabled_flows).toEqual([outbound.key, inbound.key]);
  await page.locator("#flow-search").clear();
  await expect(page.locator(`.flow-check[value="${inbound.key}"]`)).toBeChecked();
});

test("settings save reports success and preserves masked credentials", async ({ page, backend }) => {
  await openPage(page, "settings");
  await page.locator('[name="erpnext_company"]').fill("New Company");
  await page.locator('#settings-form button[type="submit"]').click();
  await expect.poll(() => backend.config.erpnext_company).toBe("New Company");
  expect(backend.config.api_secret).toBe("********");
  await expect(page.locator("#toast")).toContainText("saved");
});

test("master refresh preserves selection and sync keeps the newest result", async ({ page, backend }) => {
  backend.handlers["/api/v1/masters"] = async ({ reply }) => reply({ masters: [{ key: "Item", label: "Items" }, { key: "Customer", label: "Customers" }], summary: { Item: 4, Customer: 2 }, last_result: masterResult("Failed", "Old failure") });
  await openPage(page, "masters");
  await page.locator('.master-check[value="Customer"]').uncheck();
  await page.locator("#refresh-masters").click();
  await expect(page.locator('.master-check[value="Customer"]')).not.toBeChecked();
  await page.locator("#sync-masters").click();
  await expect.poll(() => backend.calls.filter((call) => call.path === "/api/v1/masters/sync").length).toBe(1);
  expect(backend.calls.find((call) => call.path === "/api/v1/masters/sync").body.masters).toEqual(["Item"]);
  await expect(page.locator("#masters-results")).toContainText("Success");
  await expect(page.locator("#masters-results")).not.toContainText("Old failure");
});

test("master request failure remains visible and can be corrected", async ({ page, backend }) => {
  backend.handlers["/api/v1/masters/sync"] = async ({ reply }) => reply({ error: "Tally requests are paused. Check Tally before restarting the bridge." }, 502);
  await openPage(page, "masters");
  await page.locator("#sync-masters").click();
  await expect(page.locator("#masters-error")).toBeVisible();
  await expect(page.locator("#masters-error")).toContainText("Tally requests are paused");
  await expect(page.locator("#sync-masters")).toBeEnabled();
});

test("invalid master limits never send an import request", async ({ page, backend }) => {
  await openPage(page, "masters");
  // Advanced options may live inside a disclosure in the refactored UI.
  await page.locator("#masters-limit").evaluate((element) => { for (let parent = element.parentElement; parent; parent = parent.parentElement) if (parent.tagName === "DETAILS") parent.open = true; });
  await page.locator("#masters-limit").fill("-1");
  await page.locator("#sync-masters").click();
  await page.waitForTimeout(150);
  expect(backend.calls.filter((call) => call.path === "/api/v1/masters/sync")).toHaveLength(0);
});

test("flow preview is cleared when switching flows", async ({ page, backend }) => {
  await openPage(page, "operations");
  await page.locator("#preview-operation").click();
  await expect(page.locator("#preview-results")).toContainText("ITEM-1");
  await page.locator("#operation-flow").selectOption(inbound.key);
  await expect(page.locator("#preview-results")).not.toContainText("ITEM-1");
  await expect(page.locator("#mapping-panel")).toBeVisible();
});

test("flow options preserve edits while refreshing server diagnostics", async ({ page, backend }) => {
  await openPage(page, "operations");
  await page.locator('[data-flow-option="limit"]').fill("7");
  await page.locator("#refresh-operation").click();
  await expect(page.locator('[data-flow-option="limit"]')).toHaveValue("7");
  await page.locator("#save-flow-options").click();
  await expect.poll(() => backend.config.flow_options[outbound.key]?.limit).toBe(7);
});

test("connection failures show actionable status without unhandled errors", async ({ page, backend }) => {
  backend.handlers["/api/v1/health"] = async ({ route }) => route.abort("connectionrefused");
  await page.goto("/");
  await expect(page.locator("#tally-status")).not.toHaveText("Checking…");
  await page.locator("#test-connections").click();
  await expect(page.locator("#tally-detail")).not.toHaveText("—");
});

test("mobile navigation and content fit the viewport", async ({ page, backend }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openPage(page, "masters");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  expect(overflow).toBe(false);
  await page.locator('.nav-item[data-page="settings"]').click();
  await expect(page.locator('[data-page-panel="settings"]')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("mobile-settings.png"), fullPage: true });
});

test("invalid advanced settings open their section instead of failing silently", async ({ page, backend }) => {
  await openPage(page, "settings");
  await page.locator('[name="request_timeout_seconds"]').evaluate((input) => {
    input.value = "0";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.closest("details")?.removeAttribute("open");
  });
  await page.locator('#settings-form button[type="submit"]').click();
  await expect(page.locator('[name="request_timeout_seconds"]')).toBeVisible();
  expect(backend.calls.filter((call) => call.path === "/api/v1/config" && call.method === "PUT")).toHaveLength(0);
});

test("background connection checks never compete with an active sync", async ({ page, backend }) => {
  await page.clock.install();
  await openPage(page, "overview");
  const initialHealthCalls = backend.calls.filter((call) => call.path === "/api/v1/health").length;
  backend.status.running = true;
  backend.status.current = { direction: outbound.direction };
  await page.clock.fastForward(2100);
  await expect(page.locator("#sync-status")).toContainText(/Synchronizing|Syncing|Running/);
  await page.clock.fastForward(31000);
  await page.clock.runFor(100);
  expect(backend.calls.filter((call) => call.path === "/api/v1/health")).toHaveLength(initialHealthCalls);
  await expect(page.locator("#test-connections")).toBeDisabled();
});

test("master sync stays busy and cancellation waits for the current request", async ({ page, backend }) => {
  let release;
  backend.handlers["/api/v1/masters/sync"] = async ({ reply }) => {
    await new Promise((resolve) => { release = resolve; });
    await reply({ ...masterResult(), ok: false, cancelled: true, results: [{ batch: "items", label: "Items", status: "Cancelled", message: "Cancelled by operator", created: 1 }] });
  };
  await openPage(page, "masters");
  await page.locator("#sync-masters").click();
  await expect.poll(() => Boolean(release)).toBe(true);
  try {
    await expect(page.locator("#sync-masters")).toBeDisabled();
    await expect(page.locator("#cancel-masters")).toBeVisible();
    await page.locator("#cancel-masters").click();
    await expect.poll(() => backend.calls.some((call) => call.path === "/api/v1/masters/cancel")).toBe(true);
    await expect(page.locator("#sync-masters")).toBeDisabled();
  } finally {
    release();
  }
  await expect(page.locator("#masters-results")).toContainText("Cancelled");
  await expect(page.locator("#sync-masters")).toBeEnabled();
});

test("failed flow configuration save keeps edits and displays the server error", async ({ page, backend }) => {
  await openPage(page, "operations");
  await page.locator('[data-flow-option="limit"]').fill("9");
  backend.handlers["/api/v1/config"] = async ({ request, reply }) => request.method() === "PUT"
    ? reply({ error: "Flow configuration could not be saved" }, 500)
    : reply(backend.config);
  await page.locator("#save-flow-options").click();
  await expect(page.locator("#operation-error")).toContainText("could not be saved");
  await expect(page.locator('[data-flow-option="limit"]')).toHaveValue("9");
  await expect(page.locator("#save-flow-options")).toBeEnabled();
});

test("late details from a previous flow never overwrite the selected flow", async ({ page, backend }) => {
  await openPage(page, "operations");
  await expect(page.locator("#refresh-operation")).toBeEnabled();
  let release;
  backend.handlers["/api/v1/flow-details"] = async ({ url, reply }) => {
    const flow = backend.flows.find((entry) => entry.key === url.searchParams.get("flow"));
    if (flow.key === outbound.key) await new Promise((resolve) => { release = resolve; });
    await reply({ flow, status: { pending: flow.key === outbound.key ? 999 : 12 }, configuration: {}, diagnostics: {} });
  };
  await page.locator("#refresh-operation").click();
  await expect.poll(() => Boolean(release)).toBe(true);
  const oldResponse = page.waitForResponse((response) => response.url().includes("/flow-details?") && new URL(response.url()).searchParams.get("flow") === outbound.key);
  try {
    await page.locator("#operation-flow").selectOption(inbound.key);
    await expect(page.locator("#operation-status")).toContainText("Tally → ERPNext");
    await expect(page.locator("#operation-status")).toContainText("12");
  } finally {
    release();
  }
  await (await oldResponse).finished();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator("#operation-status")).not.toContainText("999");
  await expect(page.locator("#operation-flow")).toHaveValue(inbound.key);
});

test("unsaved flow options and mappings survive switching away and back", async ({ page, backend }) => {
  await openPage(page, "operations");
  await page.locator("#operation-flow").selectOption(inbound.key);
  await expect(page.locator("#mapping-panel")).toBeVisible();
  await page.locator('[data-flow-option="limit"]').fill("8");
  await page.locator("#add-mapping").click();
  await page.locator('[data-map="ledger"]').fill("Sales ledger");
  await page.locator('[data-map="account"]').fill("Sales - ERP");
  await page.locator("#operation-flow").selectOption(outbound.key);
  await expect(page.locator("#mapping-panel")).toBeHidden();
  await page.locator("#operation-flow").selectOption(inbound.key);
  await expect(page.locator('[data-flow-option="limit"]')).toHaveValue("8");
  await expect(page.locator('[data-map="ledger"]')).toHaveValue("Sales ledger");
  await expect(page.locator('[data-map="account"]')).toHaveValue("Sales - ERP");
  await page.locator("#save-flow-options").click();
  await expect.poll(() => backend.config.flow_options[inbound.key]?.ledger_mappings).toEqual({ "Sales ledger": "Sales - ERP" });
});

test("a pending start request cannot be submitted twice", async ({ page, backend }) => {
  let release;
  backend.handlers["/api/v1/sync"] = async ({ reply }) => {
    await new Promise((resolve) => { release = resolve; });
    await reply({ ok: true, message: "Synchronization started." });
  };
  await openPage(page, "overview");
  const button = page.locator('.sync-button[data-direction="erpnext_to_tally"]');
  await button.click();
  await expect.poll(() => Boolean(release)).toBe(true);
  try {
    await expect(button).toBeDisabled();
    await button.evaluate((element) => element.click());
    expect(backend.calls.filter((call) => call.path === "/api/v1/sync")).toHaveLength(1);
  } finally {
    release();
  }
});

test("failed connection settings save retains the form and explains the failure", async ({ page, backend }) => {
  await openPage(page, "settings");
  await page.locator('[name="erpnext_company"]').fill("Unsaved Company");
  backend.handlers["/api/v1/config"] = async ({ request, reply }) => request.method() === "PUT"
    ? reply({ error: "Settings rejected by the bridge" }, 400)
    : reply(backend.config);
  await page.locator('#settings-form button[type="submit"]').click();
  await expect(page.locator("#settings-error")).toContainText("Settings rejected");
  await expect(page.locator('[name="erpnext_company"]')).toHaveValue("Unsaved Company");
  await expect(page.locator('#settings-form button[type="submit"]')).toBeEnabled();
  expect(backend.config.erpnext_company).toBe("ERP Company");
});

test("sync actions wait until a connection check finishes", async ({ page, backend }) => {
  await openPage(page, "overview");
  let release;
  backend.handlers["/api/v1/health"] = async ({ reply }) => {
    await new Promise((resolve) => { release = resolve; });
    await reply({ ok: true, erpnext: { ok: true }, tally: { ok: true, loaded_tally_company: "Tally Company" } });
  };
  await page.locator("#test-connections").click();
  await expect.poll(() => Boolean(release)).toBe(true);
  try {
    await expect(page.locator('.sync-button[data-direction="erpnext_to_tally"]')).toBeDisabled();
    await page.locator('.nav-item[data-page="masters"]').click();
    await expect(page.locator("#sync-masters")).toBeDisabled();
  } finally {
    release();
  }
  await expect(page.locator("#sync-masters")).toBeEnabled();
});

test("desktop and mobile screens remain readable", async ({ page, backend }, testInfo) => {
  const output = process.env.UI_REVIEW_DIR || testInfo.outputDir;
  await mkdir(output, { recursive: true });
  await openPage(page, "overview");
  await page.screenshot({ path: resolve(output, "desktop-dashboard.png"), fullPage: true });
  await page.locator('.nav-item[data-page="masters"]').click();
  await expect(page.locator(".master-check")).toHaveCount(2);
  await page.screenshot({ path: resolve(output, "desktop-master-data.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.nav-item[data-page="operations"]').click();
  await expect(page.locator("#preview-operation")).toBeEnabled();
  await page.screenshot({ path: resolve(output, "mobile-flow-workspace.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)).toBe(false);
});

test("keyboard users keep focus when selecting a transaction flow", async ({ page, backend }) => {
  await openPage(page, "flows");
  const checkbox = page.locator(`.flow-check[value="${inbound.key}"]`);
  await checkbox.focus();
  await checkbox.press("Space");
  await expect(checkbox).toBeChecked();
  await expect(checkbox).toBeFocused();
  await checkbox.press("Space");
  await expect(checkbox).not.toBeChecked();
  await expect(checkbox).toBeFocused();
});

test("unavailable saved flows can be disabled without clearing other selections", async ({ page, backend }) => {
  backend.flows = [outbound, { ...inbound, available: false, unavailable_reason: "Profile unavailable" }];
  backend.config.enabled_flows = [outbound.key, inbound.key];
  await openPage(page, "flows");
  await page.locator(`.flow-check[value="${inbound.key}"]`).uncheck();
  await page.locator("#save-flows").click();
  await expect.poll(() => backend.config.enabled_flows).toEqual([outbound.key]);
});

test("select compatible never enables mutually exclusive alternatives together", async ({ page, backend }) => {
  backend.flows = [{ ...outbound, exclusive_group: "sales" }, { ...inbound, exclusive_group: "sales" }];
  await openPage(page, "flows");
  await page.locator("#clear-flows").click();
  await page.locator("#select-flows").click();
  await expect(page.locator(".flow-check:checked")).toHaveCount(1);
  await page.locator(`.flow-check[value="${inbound.key}"]`).check();
  await expect(page.locator(`.flow-check[value="${outbound.key}"]`)).not.toBeChecked();
  await page.locator("#save-flows").click();
  await expect.poll(() => backend.config.enabled_flows).toEqual([inbound.key]);
});

test("typing a new secret during an earlier save never loses the new edit", async ({ page, backend }) => {
  let release;
  backend.handlers["/api/v1/config"] = async ({ request, body, reply }) => {
    if (request.method() !== "PUT") return reply(backend.config);
    await new Promise((resolve) => { release = resolve; });
    backend.config = { ...backend.config, ...body, api_secret: "********" };
    return reply({ config: backend.config });
  };
  await openPage(page, "settings");
  await page.locator('[name="api_secret"]').fill("first-secret");
  await page.locator('#settings-form button[type="submit"]').click();
  await expect.poll(() => Boolean(release)).toBe(true);
  await page.locator('[name="api_secret"]').fill("new-secret");
  release();
  await expect(page.locator("#toast")).toContainText("saved");
  await expect(page.locator('[name="api_secret"]')).toHaveValue("new-secret");
  await expect(page.locator("#settings-save-status")).toContainText("Unsaved");
});

test("first-run setup stays usable without saved credentials", async ({ page, backend }) => {
  backend.config = { ...backend.config, frappe_url: "", api_key: "", api_secret: "", erpnext_company: "", tally_company: "", enabled_flows: [] };
  backend.status.configuration_error = "ERPNext URL is required";
  await page.goto("/");
  await expect(page.locator("#erp-status")).toHaveText("Not configured");
  await expect(page.locator("#next-step-action")).toContainText("Set up connections");
  expect(backend.calls.filter((call) => call.path === "/api/v1/health")).toHaveLength(0);
  await page.locator("#next-step-action").click();
  await expect(page.locator('[name="frappe_url"]')).toBeVisible();
  await expect(page.locator('[data-page-panel="settings"]')).toBeVisible();
});

test("an initial configuration read failure recovers on the next state poll", async ({ page, backend }) => {
  let reads = 0;
  backend.handlers["/api/v1/config"] = async ({ reply }) => {
    reads += 1;
    return reads === 1 ? reply({ error: "Temporary connection failure" }, 503) : reply(backend.config);
  };
  await page.goto("/");
  await expect(page.locator("#erp-status")).toHaveText("Connected", { timeout: 10000 });
  await expect(page.locator("#context-erp")).toHaveText("ERP Company");
  expect(reads).toBeGreaterThan(1);
});
