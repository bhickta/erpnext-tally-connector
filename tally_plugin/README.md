# ERPNext-Tally Bridge

This Windows agent is the transport and Tally-side execution layer for flows
registered with the Express Tally Integration framework. It asks ERPNext for a
batch, selects the agent profile named by that flow, performs the Tally gateway
calls, and acknowledges the result. Company-specific selection and mapping stay
in the ERPNext app that registers the flow.

The built-in `inventory_sales_voucher_v1` profile creates required masters and
inventory-aware Sales vouchers. Additional profiles can be packaged through the
`agent_profiles` configuration list without changing the sync service.

## Requirements

- TallyPrime with the target company loaded and its HTTP server enabled.
- An ERPNext site with Express Tally Integration and at least one registered
  outbound flow.
- An API user with the **Tally Sync User** role, API key, and API secret.
- HTTPS when ERPNext is reached over a network.

The built-in inventory-sales profile also requires **Maintain Inventory**. If
zero-value rows are possible, enable **Allow zero-valued transactions** for the
Sales voucher type.

## Install on the Tally computer

1. Download and extract `ERPNext-Tally-Bridge-Windows-x64.zip` from
   [Tally Bridge Latest](https://github.com/bhickta/erpnext-tally-connector/releases/tag/tally-bridge-latest).
2. Copy `tally-bridge.example.json` to `tally-bridge.json`.
3. Set the ERPNext URL, credentials, companies, a stable `target_id`, and the
   registered `flow_name`. SRV's existing flow is
   `srv.sales_documents_to_tally`.
4. Start `start-bridge.cmd`. It validates the loaded Tally company before every
   batch.
5. Optional: load `ERPNextTallyBridge.tdl` through **F1 → TDL & Add-On → Manage
   Local TDL**. The Gateway menu will include **ERPNext Sync**.

The launcher runs in click-only mode. Keep it open, verify
`http://127.0.0.1:8765/health`, then use the Tally menu. Progress is available at
`http://127.0.0.1:8765/sync-status`.

For automatic polling or command-line operation:

```bat
ERPNextTallyBridge.exe --config tally-bridge.json status
ERPNextTallyBridge.exe --config tally-bridge.json sync --limit 5
ERPNextTallyBridge.exe --config tally-bridge.json serve
```

The executable can also call any Frappe endpoint allowed to its API user:

```bat
ERPNextTallyBridge.exe --config tally-bridge.json api GET /api/resource/Company
ERPNextTallyBridge.exe --config tally-bridge.json api POST /api/method/my_app.api.run --data "{\"name\":\"value\"}"
```

## Configuration extension

`agent_profiles` accepts dotted paths to additional `AgentProfile` classes that
are importable in the packaged agent. The ERPNext flow's `agent_profile` must
match the class's stable `key`.

```json
{
  "agent_profiles": ["my_bridge_profiles.CustomVoucherProfile"]
}
```

## Build

Push bridge/plugin changes to `master`, or run the workflow manually. GitHub
Actions tests the bridge, builds the Windows executable, uploads the ZIP, and
updates the rolling prerelease.

For a local Windows build:

```powershell
powershell -ExecutionPolicy Bypass -File .\tally_plugin\build-windows.ps1
```

The output is `dist\ERPNext-Tally-Bridge-Windows-x64.zip`.

## Retry and date behavior

The built-in profile imports masters in dependency order and vouchers through
Tally XML so detailed validation errors can be acknowledged. Its deterministic
GUID plus the stable `target_id` make a retry identifiable after a lost
acknowledgement. The GUID namespace remains compatible with the original SRV
bridge, preventing duplicates during migration.

TallyPrime Educational Mode restricts accepted transaction dates. For a test
company only, `voucher_date_override` can use an allowed date; leave it `null`
for licensed operation.

Protect `tally-bridge.json`. `ERPNEXT_TALLY_API_KEY` and
`ERPNEXT_TALLY_API_SECRET` override credentials in the file. The local trigger
listens on `127.0.0.1` by default.
