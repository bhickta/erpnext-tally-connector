# ERPNext-Tally Connector

A reusable base for bidirectional ERPNext ↔ Tally integrations. It provides
flow discovery and dispatch, stable HTTP contracts, shared synchronization
state, a manual ERPNext exporter, and a standalone Windows Control Centre with
manual and automatic synchronization plus pluggable Tally-side execution profiles. Company apps contribute only their selection,
mapping, eligibility, and document-creation policy.

The original Tally-to-ERPNext migration endpoints are retired and kept only for
historical reference in [`archive/legacy_tally_api/`](archive/legacy_tally_api/);
they are no longer installed or whitelisted. The supported API is the flow
framework endpoints documented below.

For a guided tour of the source tree, see the [Codebase map](#codebase-map).

## Prerequisites

- TallyPrime with its HTTP server enabled
- An active ERPNext site
- The India Compliance app when Indian tax fields are synchronized

## Installation

Once you've [set up a Frappe site](https://frappeframework.com/docs/v14/user/en/installation/), installing Express Tally Integration is simple:

1. Download the app using the Bench CLI.

    ```bash
    bench get-app https://github.com/bhickta/erpnext-tally-connector.git
    ```

2. Install the app on your site.

    ```bash
    bench --site [site name] install-app express_tally
    ```


## Configure synchronization

Generate an API key and secret for an ERPNext user with the **Tally Sync User**
role. Run the Windows Control Centre on the Tally computer, enter the connection
and company settings, select the registered flows, and use manual or automatic
sync. No TDL or TCP file is installed in Tally.

## Masters sync

The Control Centre includes a one-way **Master data** utility for Accounting
Groups, Ledgers, Units of measure, Item Groups, Items, Warehouses, Customers,
and Suppliers. Each type is selectable independently; sync Units and Item
Groups before Items. It writes the ERPNext identity into the Tally alias field
and maps the available GST, address, contact, credit-limit, barcode, UOM, and
price fields.

Each run writes a downloadable JSONL debug trace (versions, lookups, chunk
names, Tally results, and errors) that excludes secrets. See
[Masters sync](docs/masters-sync.md) for the alias contract, exhaustive field
mapping, API, trace, and limitations.


## Planned Features

- Sync Alternate (Multiple Units)
- Sync Price Lists

## Bidirectional flow framework

The `express_tally.framework` package provides a versioned API and extension
contract for company apps. The retired legacy migration modules are archived
under [`archive/legacy_tally_api/`](archive/legacy_tally_api/) and are not part
of the supported runtime.

An installed app can register one or more flows in its `hooks.py`:

```python
tally_integration_flows = [
    "my_company.tally.flows.SalesInvoiceToTally",
    "my_company.tally.flows.TallyReceiptToPaymentEntry",
]
```

ERPNext-to-Tally flows extend `OutboundFlow` and implement `pull` plus
`acknowledge`. Tally-to-ERPNext flows extend `InboundFlow` and implement
`receive`. Each flow owns its business mapping, eligibility rules, and
permissions. The connector owns discovery, validation, batch limits, direction
checks, the HTTP envelope, and reusable `OutboundSyncLog` state handling.

Version 1 endpoints are:

```text
GET  /api/method/express_tally.framework.api.get_flows
GET  /api/method/express_tally.framework.api.pull
POST /api/method/express_tally.framework.api.acknowledge
POST /api/method/express_tally.framework.api.receive
GET  /api/method/express_tally.framework.api.get_status
```

An outbound flow returns documents in a contract understood by its local Tally
agent profile. Payload mapping stays in the contributing app so different
companies can implement different accounting policies without forking the
connector. The bundled `inventory_sales_voucher_v1` profile supports SRV's
current Sales Order/Delivery Note contract.

## Using the Control Centre

Start on **Dashboard**. It shows the connected companies, current activity, and a
suggested next step. **Connections & settings** contains connection credentials;
optional scheduling and import behavior are under **Sync preferences**.

Use **Master data** to manually send items, customers, suppliers, warehouses, and
accounts from ERPNext to Tally. This is separate from transaction synchronization.
Selections stay unchanged when refreshing counts. Running jobs show a persistent
status and can be stopped after the current Tally request completes.

On **Transaction flows**, select compatible transaction types and save the
selection. Use **Configure & preview** to open a flow in **Flow workspace**. Save
any configuration edits, preview the pending records, then run the flow. Refresh
and flow switches preserve unsaved workspace edits while the page stays open.
The dashboard’s manual and automatic transaction actions use the saved flow
selection. **Run history** shows recent transaction results; master data results
remain on the Master data page.

The UI retains its active page on reload, reports failed requests inline, and
prevents overlapping sync, preview, and connection-test actions within the page.
The bridge’s transport gate remains the final protection against concurrent
requests from separate pages. See `control-centre/tests/README.md` for the mocked
browser regression suite.

## Windows Control Centre

End users run a single `ERPNextTallyControlCentre.exe`; Python and Node.js are
not required on the Tally computer. The executable opens a local browser UI for
ERPNext/Tally health, credentials, company and target settings, registered flow
selection, manual sync in either direction, automatic schedules, and recent run
history. Settings and logs are stored under the user's Local AppData directory.

The UI source is the private npm package in `control-centre/`. It has no runtime
dependencies and is built into the Python executable by the Windows release
workflow:

```bash
npm --prefix control-centre ci
npm --prefix control-centre run build
```

An ERPNext-to-Tally flow needs a delivery profile. A Tally-to-ERPNext flow needs
an extraction profile implementing `AgentProfile.collect`; the Control Centre
only enables flows whose matching local profile is installed.

Apps using standard ERPNext selling documents can subclass
`SalesDocumentsToTallyFlow` directly; only the stable key, title, and roles are
required. A custom `SalesDocumentMapper` subclass can override policy without
reimplementing synchronization state or Tally gateway work.

See [Tally flow framework](docs/flow-framework.md) for the extension contract,
agent profiles, state requirements, and an implementation example.

## Codebase map

This repository contains three cooperating runtimes, not a single app:

| Piece | Runs on | Entry point | Responsibility |
| --- | --- | --- | --- |
| **ERPNext app** (`express_tally/`) | Frappe bench server | `frappe.get_hooks("tally_integration_flows")` | Flow registry, HTTP API, business mapping, Tally Sync Log |
| **Local bridge** (`express_tally/bridge/`) | Tally Windows PC | `python -m express_tally.bridge serve` | Tally + Frappe transport, batching, scheduler, master sync |
| **Control Centre UI** (`control-centre/src/`) | Browser, served by the bridge | `express_tally/bridge/web/index.html` | Settings, dashboard, manual sync, previews, master data |

The rule of thumb: **ERPNext owns accounting policy and mapping; the bridge owns
transport and Tally execution.** Company apps extend flows, not the bridge.

### Annotated tree

```text
express_tally/                      # the installable Frappe app
├── hooks.py                        # ★ registers all flows + install hooks
├── install.py                      # creates "Tally Sync User" role + read perms
├── framework/                      # ★ stable, reusable orchestration
│   ├── contracts.py                # FlowContext, OutboundFlow, InboundFlow, FlowDirection
│   ├── registry.py                 # discovers flows from hooks
│   ├── engine.py                   # pull/acknowledge/receive/status orchestration, limits
│   ├── api.py                      # whitelisted /api/method/... endpoints
│   ├── sync_log.py                 # OutboundSyncLog, SourceSpec, idempotency
│   └── diagnostics.py              # read-only activity queries
│
├── integrations/                   # concrete flows + mapping policy
│   ├── sales_voucher_flow.py       # Sales Order/Delivery Note → Tally Sales
│   ├── payment_entry_flow.py       # Payment Entry → Tally Payment/Receipt
│   ├── ledger_flow.py              # full-ledger mirror (in + out)
│   ├── native_transaction_flow.py  # Tally voucher → ERPNext doc + GL verification
│   ├── tally_inbound_flows.py      # standard masters/vouchers/payments from Tally
│   ├── inbound_base.py             # LoggedInboundFlow, InboundLog
│   ├── erpnext_lookups.py          # resolve Tally names → ERPNext docs
│   ├── tally_export.py             # manual Desk export of SO JSON (older path)
│   ├── auto_push.py                # Stock Entry feed endpoint
│   ├── master_sync_export.py       # master-sync summary/payload endpoints
│   └── master_sync/                # server-side Tally master payload builders
│       ├── payload.py              # dependency-ordered assembly
│       ├── accounts.py  items.py  parties.py  names.py  constants.py
│
├── express_tally_integration/      # Frappe module assets
│   ├── doctype/tally_sync_log/     # ★ durable sync state (the DB table)
│   ├── page/tally_export/          # Desk page for manual SO export
│   └── workspace/tally_integration/
│
├── bridge/                         # ★ standalone Windows agent (Python)
│   ├── __main__.py                 # CLI: serve | sync | status | api | inspect-voucher
│   ├── config.py                   # BridgeConfig + thread-safe JSON ConfigStore
│   ├── controller.py               # ControlCentre: scheduler, state, history, masters
│   ├── http_server.py              # local UI server + /api/v1 routes
│   ├── service.py                  # SyncService: one sync batch, in/out
│   ├── clients.py                  # FrappeClient (REST) + TallyClient (XML/JSON)
│   ├── profiles.py                 # ★ AgentProfile classes: deliver/collect
│   ├── master_sync.py              # one-way ERPNext → Tally master push
│   ├── web/                        # built copy of control-centre/src
│   │
│   ├── xml_gateway.py              # Tally XML envelopes (vouchers, masters)
│   ├── json_gateway.py             # Tally JSONEx master imports
│   ├── collection_gateway.py       # TDL-free Tally collection exports
│   ├── ledger_gateway.py           # ledger import + opening balance readback
│   ├── payment_gateway.py          # payment/journal XML + verification
│   ├── voucher_readback.py         # read back saved vouchers
│   ├── normalization.py            # Tally records → connector records
│   ├── preview.py                  # safe previews for the UI
│   ├── master_trace.py             # JSONL debug trace
│   ├── tally_transport.py          # single-threaded Tally admission control
│   ├── migration_audit.py          # read-only migration checks
│   ├── balance_reconciliation.py   # source vs ERP GL comparison
│   ├── auto_push.py                # Stock Entry poller
│   ├── bill_references.py          # Tally bill → invoice resolution
│   └── inbound_profiles/           # Tally → ERPNext extraction
│       ├── base.py  checkpoints.py  constants.py
│       ├── masters.py  vouchers.py  ledger.py
│
├── tests/                          # ★ 15 unittest modules (server + bridge)
├── patches/                        # Frappe schema/data migrations
└── templates/

control-centre/                     # UI source (private npm package)
├── src/{index.html,app.js,operations.js,masters.js,styles.css}
├── scripts/build.mjs               # copies src/ → express_tally/bridge/web/
└── tests/                          # Playwright regression suite + mock server

tally_plugin/                       # Windows packaging
├── windows_entry.py                # frozen EXE entry point
├── build_package.py  build_portable.py  build-windows.ps1
└── start-*.cmd  install-portable.ps1

docs/                               # ★ read these before deep code
scripts/                            # deploy + migration/reconciliation CLIs
archive/legacy_tally_api/           # retired endpoints, reference only
.plans/                             # active engineering handoff/brief
.github/workflows/                  # deploy + Windows build
```

### Suggested reading order

1. **Conceptual** — `README.md`, [`docs/flow-framework.md`](docs/flow-framework.md),
   [`docs/development-handoff.md`](docs/development-handoff.md).
2. **Server orchestration** — `express_tally/hooks.py`,
   `framework/contracts.py`, `framework/engine.py`, `framework/api.py`,
   `framework/registry.py`, `framework/sync_log.py`.
3. **One flow end-to-end** — `integrations/sales_voucher_flow.py`,
   `bridge/profiles.py`, `bridge/service.py`, `bridge/controller.py`.
4. **Inbound and special cases** — `integrations/tally_inbound_flows.py`,
   `integrations/inbound_base.py`, `bridge/inbound_profiles/`,
   `integrations/native_transaction_flow.py`, `integrations/ledger_flow.py`.
5. **Master sync and UI** — `bridge/master_sync.py`,
   `integrations/master_sync/payload.py`, then `control-centre/src/app.js`,
   `operations.js`, `masters.js`, and `bridge/http_server.py`.
6. **Tests** — `express_tally/tests/` works as executable documentation.

### How a sync flows

Outbound (ERPNext → Tally):

```text
Control Centre / scheduler
  → bridge/controller.trigger()
  → bridge/service.SyncService._sync_outbound()
  → FrappeClient GET /api/method/express_tally.framework.api.pull
      → engine.pull() → flow.authorize() → OutboundFlow.pull()
      → OutboundSyncLog.pending_references()   [queries Tally Sync Log]
      → mapper.map_document()                  [payload + source_hash]
  → profiles.AgentProfile.deliver()             [Tally XML/JSON import]
  → voucher_readback / verify_*                 [readback before success]
  → FrappeClient POST .../acknowledge
      → engine.acknowledge() → flow.acknowledge()
      → sync_log.acknowledge()                  [writes Tally Sync Log]
```

Key idempotency idea: `flow key + source doctype + name + hash/version + target_id`,
plus deterministic Tally GUIDs.

Inbound (Tally → ERPNext):

```text
bridge profiles → profile.collect()             [Tally XML collection]
  → normalization.normalize_*()
  → FrappeClient POST .../receive
      → engine.receive() → InboundFlow.receive()
      → apply_record() → ERPNext docs (draft or submit)
      → InboundLog records Tally Sync Log
  → profile.acknowledge_collected()             [advance checkpoint]
```

Master sync (separate one-way path):

```text
Control Centre Master data page
  → controller.sync_masters()
  → bridge/master_sync.MasterSyncService.sync()
  → Frappe GET master_sync_export.get_master_sync_summary / payload
      → integrations/master_sync/* builds Tally JSON/XML
  → Tally import, existing-master detection, alias writes
  → master_trace.MasterTrace (JSONL, secrets excluded)
```

### Extension contracts

**Server side** — subclass in `integrations/`, register in `hooks.py`:

- `IntegrationFlow` — key, title, direction, `agent_profile`, roles, options
- `OutboundFlow` — implement `pull()` and `acknowledge()`
- `InboundFlow` — implement `receive()`
- Optional: `status()`, `configuration()`, `diagnostics()`
- Reuse `OutboundSyncLog` + `SourceSpec` instead of inventing state

**Tally side** — subclass in `bridge/profiles.py`:

- `AgentProfile` — stable `key`
- outbound: `deliver()` (plus `validate_environment()`)
- inbound: `collect()` (plus optional `acknowledge_collected()`)
- `supports_direction()` decides whether the UI enables a flow

A flow is usable only if **its `agent_profile` exists in the Windows package**.

### Where do I change …?

| I want to change… | Go to |
| --- | --- |
| Which flows exist / are enabled | `express_tally/hooks.py` + Control Centre config |
| API request/response shape | `framework/api.py`, `framework/engine.py` |
| Business mapping for sales | `integrations/sales_voucher_flow.py` |
| Payments mapping | `integrations/payment_entry_flow.py` |
| Tally → ERPNext document creation | `integrations/tally_inbound_flows.py` |
| Which docs are "pending" | `framework/sync_log.py` (`SourceSpec`, `pending_references`) |
| Tally import XML/JSON | `bridge/xml_gateway.py`, `json_gateway.py`, `payment_gateway.py` |
| Voucher readback checks | `bridge/voucher_readback.py`, `payment_gateway.py` |
| Tally extraction/checkpoints | `bridge/inbound_profiles/` |
| Master payload fields | `integrations/master_sync/` |
| Master push ordering/batching | `bridge/master_sync.py` |
| Scheduler / history / state | `bridge/controller.py` |
| Local UI routes | `bridge/http_server.py` |
| Dashboard / settings UI | `control-centre/src/app.js` |
| Flow workspace / run history UI | `control-centre/src/operations.js` |
| Master data UI | `control-centre/src/masters.js` |
| Windows EXE packaging | `tally_plugin/` |
| DB fields for sync state | `express_tally_integration/doctype/tally_sync_log/tally_sync_log.json` |
| Roles / permissions | `express_tally/install.py` |

### Tests, docs, and operations

- **Tests** — `express_tally/tests/` contains 15 modules. `test_framework.py`
  covers the registry/engine, `test_bridge.py` the gateways/service/HTTP,
  `test_master_sync.py` the largest surface, plus sales, payment, native
  transaction, ledger, verification, and safety suites. Run with
  `python -m unittest discover -s express_tally/tests -t .`
- **UI tests** — `control-centre/tests/` (Playwright with a mocked bridge).
- **Docs** — `flow-framework.md` (core), `masters-sync.md`, `payment-sync.md`,
  `full-ledger-sync.md`, `native-financial-migration.md`, `deployment.md`,
  `development-handoff.md`.
- **Ops scripts** — `scripts/deploy-server.sh`, `migrate_all_tally.py`,
  `validate_native_migration.py`, `verify_native_migration.py`,
  `check_migration_source.py`, `retire_restored_accounting.py`,
  `set_migrated_party_accounts.py`, `test_full_ledger_sync.py`.
- **CI** — `.github/workflows/deploy.yml` (Linux benches),
  `build-tally-bridge-windows.yml` (EXE + UI build), `control-centre.yml`.
- **Archive** — `archive/legacy_tally_api/` is retired and not installed; it is
  not live behavior.

## Contributing

- [Issue Guidelines](https://github.com/frappe/erpnext/wiki/Issue-Guidelines)
- [Pull Request Requirements](https://github.com/frappe/erpnext/wiki/Contribution-Guidelines)

## License

[GNU General Public License (v3)]
