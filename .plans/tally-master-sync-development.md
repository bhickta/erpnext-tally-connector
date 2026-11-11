# Tally master sync: engineering brief and execution plan

**Status:** active handoff
**Last updated:** 2026-11-11
**Scope:** Windows bridge and ERPNext master payloads

## 1. Purpose

This document is the starting point for an engineer taking over the
ERPNext-to-TallyPrime master sync. It records the architecture, the current
implementation state, the evidence gathered from live Tally tests, the open
defect, and a controlled plan for resolving it.

The immediate objective is to make **Unit** master creation reliable against
TallyPrime. The broader objective is to finish the one-way master sync for:

```text
Group, Ledger, Unit, Item Group, Item, Warehouse, Customer, Supplier
```

Each type must remain independently selectable.

## 2. Non-negotiable requirements

1. ERPNext is the source of truth.
2. The sync is one-way. Never delete Tally masters.
3. Sync one master type at a time.
4. Unit and Item Group must remain separate from Item.
5. Existing Tally units are skipped, never altered.
6. Every Tally master stores the ERPNext identity in its alias field where
   aliases are supported.
7. Duplicate names use `skip` by default or `suffix` with the ERPNext ID.
8. Every run produces a downloadable debug trace that excludes secrets.
9. No environment-specific values may be hardcoded in application code.
10. A failed or timed-out Tally request must stop the run and leave the
    operator in control. Do not silently retry.

### Design decisions and rationale

- **One-way only.** Tally is an accounting destination, not a second source of
  truth. Bidirectional master sync would create conflict resolution and
  deletion semantics that the business has not asked for.
- **Independent master types.** A failure in Unit must not block Item or
  Warehouse. Operationally, the team also needs to run one doctype at a time.
- **Skip existing units.** TallyPrime can become unstable when an in-use unit
  is altered. Skipping is safer than updating and still satisfies the
  create-only requirement.
- **Aliases for identity.** Tally aliases provide a stable back-reference to
  the ERPNext document without modifying Tally's primary key.
- **Append-only JSONL trace.** A crash or timeout still leaves the events
  written before the failure. A single JSON document would be lost exactly
  when it is most needed.
- **Small, capped requests.** Large JSON imports increase the chance of a
  timeout and make failure recovery ambiguous. Requests are capped at 25
  records and 64 KiB.
- **Stop after the first failure.** Continuing after a dependency failure
  produces misleading errors and can leave Tally in an inconsistent state.

## 3. Repository and current branch state

Repository: `bhickta/erpnext-tally-connector`

Important revisions:

| Ref | Commit | Meaning |
| --- | --- | --- |
| `master` | `0e9fb83` | Trace feature merged (PR #14) |
| `production` | `0e9fb83` | Fast-forwarded and deployed |
| `fix/unit-xml-import` | `7e59257` | Unit XML import fix, handoff docs; not merged or released |
| latest release | `tally-control-centre-2026.11.11.3` | Windows build from `0e9fb83` |

The branch `fix/unit-xml-import` contains:

```text
540808f fix(masters): create units through Tally XML master import
7e59257 docs: add Windows development handoff
```

It is based on the released trace build. It has not been validated against a
working Tally Unit import.

Application version: `0.5.1`
Control Centre version: `1.3.1`

## 4. Runtime topology

Production deployment:

```text
ERPNext site -> Windows bridge -> TallyPrime HTTP gateway
```

The bridge is a local HTTP service. The Control Centre UI is served from the
same process.

Default bridge settings:

```text
listen_host: 127.0.0.1
listen_port: 8765
tally_url: http://127.0.0.1:9000
```

Configuration file on Windows:

```text
%LOCALAPPDATA%\Express Tally Control Centre\tally-bridge.json
```

Runtime trace directory:

```text
%LOCALAPPDATA%\Express Tally Control Centre\traces\
```

The bridge process accepts these environment variables for credentials:

```text
ERPNEXT_TALLY_API_KEY
ERPNEXT_TALLY_API_SECRET
```

Never commit credentials or trace files containing customer data.

## 5. Code map

| Area | Path |
| --- | --- |
| Bridge HTTP server | `express_tally/bridge/http_server.py` |
| Control Centre coordinator | `express_tally/bridge/controller.py` |
| Master sync orchestration | `express_tally/bridge/master_sync.py` |
| Tally HTTP client | `express_tally/bridge/clients.py` |
| Tally transport gate and pause | `express_tally/bridge/tally_transport.py` |
| Tally XML gateway | `express_tally/bridge/xml_gateway.py` |
| Tally collection export | `express_tally/bridge/collection_gateway.py` |
| JSON import response parser | `express_tally/bridge/json_gateway.py` |
| Trace writer | `express_tally/bridge/master_trace.py` |
| Master payload builders | `express_tally/integrations/master_sync/` |
| Control Centre UI | `control-centre/src/` |
| Built UI assets | `express_tally/bridge/web/` |
| Tests | `express_tally/tests/` |
| Masters documentation | `docs/masters-sync.md` |
| Windows handoff | `docs/development-handoff.md` |

## 6. Data flow

1. The operator selects one or more master types in the Control Centre.
2. The bridge calls the ERPNext method
   `express_tally.integrations.master_sync_export.get_master_sync_payload`.
3. ERPNext returns dependency-ordered batches.
4. The bridge reads existing Tally master names through a collection export.
5. The bridge marks each record as `create` or `alter`.
6. Existing units are removed from the Unit batch.
7. The bridge sends small chunks to Tally.
8. Tally results are aggregated per batch.
9. The result and a JSONL trace are returned to the UI.

Dependency order:

```text
account groups
-> account ledgers
-> units
-> item groups
-> warehouses
-> customers
-> suppliers
-> items
```

When Item is selected together with Unit or Item Group, the dependency batches
remain complete. When Unit or Item Group is selected alone, the limit applies.

## 7. Master contract

| Master | ERPNext source | Tally type |
| --- | --- | --- |
| Group | `Account` where `is_group = 1` | `Group` |
| Ledger | `Account` where `is_group = 0` | `Ledger` |
| Unit | `UOM` | `Unit` |
| Item Group | `Item Group` | `Stock Group` |
| Item | `Item` | `Stock Item` |
| Warehouse | `Warehouse` | `Godown` |
| Customer | `Customer` | `Ledger` under `Sundry Debtors` |
| Supplier | `Supplier` | `Ledger` under `Sundry Creditors` |

Naming rules:

- Item Tally name is the ERPNext `item_name`.
- `item_code` is written to Part No and aliases.
- Duplicate item names become `(<item_code>)`.
- Unit Tally name is the ERPNext UOM name.
- Existing units are skipped because altering an in-use unit can crash
  TallyPrime.

## 8. Safety invariants

These are not optional:

1. Send one master type at a time during recovery and validation.
2. Use master batch size 1 while testing Units.
3. Read existing Tally names before every batch.
4. Never alter an existing Tally unit.
5. Do not retry a timed-out request until Tally has been inspected.
6. Stop the run after the first failed chunk.
7. Do not run write tests against the live company until manual master
   creation in that company is proven to work.
8. Prefer a blank test company for payload experiments.
9. Capture and attach the trace for every failure.
10. Keep the Tally UI visible when testing. A modal dialog blocks Tally's HTTP
    server even though the TCP port still accepts connections.

## 9. Debug trace

Event names:

```text
sync_start
payload
lookup_start
lookup_cached
lookup_result
lookup_error
skip_existing
chunk_start
chunk_result
chunk_error
batch_result
sync_end
sync_error
```

The trace records bridge and ERPNext connector versions, selected types,
limits, batch size, payload counts, existing-name counts, skipped units, chunk
names, create/alter actions, request size, result counts, timing, and errors.

It intentionally excludes API keys and secrets.

Retention: latest 20 runs.

## 10. Completed work

The following is already implemented and tested:

- One-way master sync.
- Independent master types, including Unit and Item Group.
- Item sync sends Stock Items only.
- Existing Tally unit skip.
- Attribute-only unit name parsing.
- Duplicate-name policy.
- Alias and `LEDGERCODE` identity.
- Tally pause and resume.
- Stop bridge and cancel sync.
- Streaming JSONL trace and download endpoint.
- UI trace link, including for pre-batch failures.
- 140 Python tests pass.
- Control Centre `npm run check` and `npm run build` pass.

## 11. Observed failures and evidence

### 11.1 `HTTP 417 Unsupported master type Item Group`

Cause: the Windows bridge was new, but the ERPNext site was running old
application code. The old site accepted only:

```text
Group, Ledger, Item, Warehouse, Customer, Supplier
```

Resolution: `production` was fast-forwarded to `0e9fb83` and deployed. The
production ping endpoint returns HTTP 200. No further 417 should occur with the
current site code.

### 11.2 VM bridge unreachable on port 18765

From the Linux environment, `172.17.0.1:18765` accepts TCP but never returns an
HTTP response. Static paths and API paths both time out. The connection is
reset after roughly 75 seconds.

Interpretation: the host port forward exists, but the bridge process is not
answering. The bridge may not be running, may have crashed, or may be listening
only on `127.0.0.1` without a working forward.

This is separate from the Unit import defect. Validation was therefore done by
running the same source code locally against the VM's Tally and the dev
ERPNext site.

### 11.3 Unit import through JSONEX

Test conditions:

```text
Unit only
limit = 1
master batch size = 1
Tally company = Bhickta
```

Bridge result:

```text
Tally reported errors=0, exceptions=1
```

The trace showed a single 304-byte request for `Abampere`. The request had one
`create` action. Tally returned `exceptions=1` with no line-level error. Tally's
HTTP server then stopped responding, consistent with a modal dialog blocking the
gateway.

### 11.4 Unit import through the old XML envelope

The first XML implementation reused `_import_envelope`, which produces:

```xml
<HEADER><TALLYREQUEST>Import</TALLYREQUEST></HEADER>
<BODY><DESC>...</DESC><DATA><TALLYMESSAGE>...</TALLYMESSAGE></DATA></BODY>
```

Tally displayed a **memory access violation (MAV)**. Tally's HTTP server was
blocked until the dialog was dismissed.

### 11.5 Unit import through the documented XML envelope

The implementation was corrected to Tally's documented master import format:

```xml
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY>
  <IMPORTDATA>
    <REQUESTDESC>
      <REPORTNAME>All Masters</REPORTNAME>
      <STATICVARIABLES><SVCURRENTCOMPANY>Bhickta</SVCURRENTCOMPANY></STATICVARIABLES>
    </REQUESTDESC>
    <REQUESTDATA>
      <TALLYMESSAGE>
        <UNIT Action="Create">
          <NAME>Abampere</NAME>
          <ORIGINALNAME>Abampere</ORIGINALNAME>
          <ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
          <DECIMALPLACES>3</DECIMALPLACES>
        </UNIT>
      </TALLYMESSAGE>
    </REQUESTDATA>
  </IMPORTDATA>
</BODY>
```

The request used the documented field order and values. It also timed out and
left Tally unresponsive.

### 11.6 Current Tally state

Observed through the HTTP gateway:

```text
Company: Bhickta
Units: NOS only
Stock Groups: Testing only
Stock Items: Testing only
```

A unit named `Abampere` was not created by any failed request.

## 12. Root-cause hypotheses

Ranked by likelihood:

1. **The Bhickta company data is unhealthy.** MAV on a well-formed master
   import can indicate corrupted company data, especially after earlier
   duplicate-master dialogs. Manual unit creation may also fail.
2. **Tally's HTTP import path is in a bad state after the first failure.** Once
   a modal/MAV appears, every later request times out. Each experiment must
   start from a known-responsive Tally.
3. **The XML payload still differs from what this Tally build expects.**
   Candidate differences: `STATICVARIABLES` in `REQUESTDESC`, the
   `xmlns:UDF` namespace, `DECIMALPLACES=3`, `Action` versus `ACTION`, or
   `ORIGINALNAME` on a simple unit.
4. **TallyPrime Server restricts or mishandles master imports.** Confirm whether
   the endpoint is desktop TallyPrime or TallyPrime Server, and whether the
   company is opened in a mode that permits imports.
5. **Unit masters are unsupported by the JSONEX importer.** This is strongly
   suggested by the clean `exceptions=1` response and by Tally's documented XML
   master import. JSONEX should not be used for Units.

## 13. Investigation plan

Each step changes one variable only. Do not batch experiments.

### Phase 0: Establish a safe baseline

1. Dismiss any Tally dialog.
2. Restart Tally if the HTTP server remains unresponsive.
3. Confirm a read-only request succeeds:
   - export the company list;
   - export existing Unit names.
4. Record the Tally version and mode:
   - TallyPrime or TallyPrime Server;
   - version from Help -> About.
5. Create a unit manually in Tally, for example `ZZTEST`.
6. Record whether manual creation succeeds.

Decision gate:

- If manual creation fails or MAVs, stop. Repair the company data or use a
  blank company. Do not test bridge imports against this company.
- If manual creation succeeds, continue to Phase 1.

### Phase 1: Use a blank test company

Create a new Tally company with the minimum required features and load it.
Point the bridge at that company.

Run:

```text
Master type: Unit
Limit: 1
Master batch size: 1
```

Expected result: one created unit and no dialog.

If this fails, the payload is still wrong. Continue to Phase 2.

### Phase 2: Isolate the XML payload

Test variants against the blank company, one at a time, verifying Tally is
responsive before and after each:

1. Exact TallyHelp sample, without `STATICVARIABLES`.
2. Same, with `SVCURRENTCOMPANY` in `STATICVARIABLES`.
3. `DECIMALPLACES` values 0, 2, and 3.
4. `Action="Create"` versus `ACTION="Create"`.
5. With and without `ORIGINALNAME`.
6. With and without the `xmlns:UDF` declaration.
7. A single record versus two records in one `TALLYMESSAGE`.

After each attempt:

- read the raw HTTP response;
- export the Unit list to verify creation;
- inspect the Tally exception report or event log;
- dismiss any dialog before the next attempt.

Do not use the live Bhickta company for this phase.

### Phase 3: Validate the full Unit batch

Only after a single unit is created reliably:

1. Sync Unit with limit 5.
2. Verify created names and skipped names in the trace.
3. Increase to limit 25, then the full set.
4. Confirm existing units are skipped and never altered.

### Phase 4: Validate the remaining master types

Run in this order, one type at a time:

```text
Unit -> Item Group -> Item
```

Then validate the accounting and party masters separately:

```text
Group -> Ledger -> Warehouse -> Customer -> Supplier
```

Record the trace for each run.

### Phase 5: Merge and release

Only after Phase 4 passes:

1. Run all Python tests.
2. Run `npm run check` and `npm run build`.
3. Merge `fix/unit-xml-import`.
4. Build the Windows EXE and portable package.
5. Publish a release with checksums.
6. Deploy the ERPNext app if server-side code changed.

## 14. Acceptance criteria

The Unit fix is complete when:

- A new unit is created in Tally through the bridge.
- The same unit is skipped on a second run.
- Existing units are not altered.
- No Tally dialog or MAV appears.
- The trace records `chunk_result` with `success: true` and `created: 1`.
- A second run reports `All Tally units already exist`.
- Item Group and Item sync then succeed with the created units.
- The full test suite passes.
- The Windows build installs and runs against the target machine.

## 15. Windows development setup

Prerequisites: Git, Python 3.12, Node.js 22.

```powershell
git clone git@github.com:bhickta/erpnext-tally-connector.git
cd erpnext-tally-connector
git checkout fix/unit-xml-import

cd control-centre
npm ci
npm run build
cd ..

python -m unittest discover -s express_tally/tests -t .
python -m express_tally.bridge serve
```

Control Centre:

```text
http://127.0.0.1:8765/
```

Configuration:

```text
Tally URL:        http://127.0.0.1:9000   (confirm the local Tally gateway port)
Tally company:    target company
ERPNext URL:      target ERPNext site
ERPNext company:  target ERPNext company
ERPNext API key:  configure in the UI or environment
```

## 16. Release process

The Windows build workflow is:

```text
.github/workflows/build-tally-bridge-windows.yml
```

It runs on Windows, executes the bridge tests, builds the Control Centre,
builds the standalone executable, creates the package, updates the rolling
prerelease, and uploads artifacts.

The deploy workflow is:

```text
.github/workflows/deploy.yml
```

`master` deploys to dev. `production` deploys to production. Production was
fast-forwarded to `0e9fb83` for the 417 fix.

## 17. What not to do

- Do not alter existing Tally units.
- Do not send a second request after a timeout without inspecting Tally.
- Do not test imports against the live company before manual master creation
  is proven.
- Do not batch multiple payload changes into one experiment.
- Do not assume a TCP connection means Tally or the bridge is healthy. HTTP
  must return a response.
- Do not commit API keys, secrets, customer data, or trace files.
- Do not treat passing unit tests as proof that Tally accepts the payload.
  Live Tally validation is required.

## 18. First action for the next engineer

Start with Phase 0. Determine whether Tally can create a unit manually. That
single result separates a payload defect from a company-data defect and decides
the rest of the plan.

## 19. Operating instruction for the next agent

Treat this as an engineering investigation, not a code-generation task.

1. Read `docs/development-handoff.md` and this document before changing code.
2. Assume `fix/unit-xml-import` is unvalidated. Do not describe the Unit fix as
   working until a live Tally import succeeds.
3. Establish the Tally baseline first: responsiveness, version, mode, and
   manual unit creation.
4. Change one variable per experiment. Record the exact payload, raw response,
   trace ID, and observed Tally dialog for each attempt.
5. Do not write to the live company until manual creation and a blank-company
   import both succeed.
6. If Tally stops responding, stop the experiment. Do not send another request
   until the dialog is dismissed and a read-only export succeeds.
7. Add a failing test before changing payload generation or import routing.
8. Run the complete test suite and the Control Centre checks before committing.
9. Report findings with evidence, not conclusions alone. Distinguish confirmed
   behaviour from hypotheses.
10. Preserve the safety invariants in section 8 even when a shortcut appears
    faster.
