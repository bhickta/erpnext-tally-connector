# Development handoff

## What this project is

`express_tally` is a Frappe/ERPNext app with a Windows Control Centre and a
local bridge that pushes ERPNext master data to TallyPrime. The sync is
one-way: ERPNext is the source of truth and Tally masters are never deleted.

## Where things live

- Repository: `bhickta/erpnext-tally-connector`
- App root in the Linux bench: `frappe-bench/apps/express_tally`
- Bridge code: `express_tally/bridge/`
- Control Centre UI: `control-centre/src/`, built into
  `express_tally/bridge/web/`
- ERPNext payload builders: `express_tally/integrations/master_sync/`
- Tests: `express_tally/tests/`
- Release workflow: `.github/workflows/build-tally-bridge-windows.yml`
- Deploy workflow: `.github/workflows/deploy.yml`

## Current state

- `master` contains the downloadable trace feature (PR #14).
- Latest published release: `tally-control-centre-2026.11.11.3`
  - target commit `0e9fb83`
  - trace build for Windows x64
- `production` was fast-forwarded to `0e9fb83` and deployed successfully.
- Unit sync was split from Item and Item Group in PR #13.
- Working branch for the unit import fix: `fix/unit-xml-import`
  - commit `540808f`
  - not merged and not released
  - 140 tests pass

## Master types

Selectable independently:

```text
Group, Ledger, Unit, Item Group, Item, Warehouse, Customer, Supplier
```

Recommended order:

```text
units -> item groups -> items
```

Full dependency order:

```text
account groups -> account ledgers -> units -> item groups
-> warehouses -> customers -> suppliers -> items
```

Existing Tally units are always skipped, never altered.

## The current bug

TallyPrime rejects Unit masters sent through the JSONEX importer:

```text
Tally reported errors=0, exceptions=1
```

The failed JSON request can leave a Tally dialog open and block Tally's HTTP
server. Sending the old XML master envelope caused a Tally **memory access
violation (MAV)**.

`fix/unit-xml-import` switches Unit records to Tally's documented XML master
import:

```xml
<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>All Masters</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE>
          <UNIT Action="Create">
            <NAME>...</NAME>
            <ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
            <ORIGINALNAME>...</ORIGINALNAME>
            <DECIMALPLACES>...</DECIMALPLACES>
          </UNIT>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>
```

The documented envelope also timed out on the current Bhickta company, so we
still need to determine whether:

1. the import payload is still wrong, or
2. the Bhickta company data is corrupted (manual master creation may also fail).

Do not run another Unit sync against Bhickta until this is resolved.

## Next steps

1. On the Windows machine, create a unit manually in Tally (for example
   `ZZTEST`). If manual creation fails, repair the company data first.
2. Prefer testing imports against a blank test company.
3. Run the bridge from source and sync **Unit only**, **limit 1**,
   **master batch size 1**.
4. Download the trace from the result if anything fails.
5. If Unit import succeeds, sync Item Groups and Items next.
6. Only then merge `fix/unit-xml-import` and publish a Windows build.

## Running on Windows

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

The Control Centre opens at `http://127.0.0.1:8765/`.

Typical bridge settings:

```text
Tally URL:        http://127.0.0.1:9000   (confirm Tally's local HTTP port)
Tally company:    Bhickta
ERPNext URL:      your ERPNext site URL
ERPNext company:  RAPL (Estimate)
```

## Debug trace

Each sync writes JSONL to:

```text
%LOCALAPPDATA%\Express Tally Control Centre\traces\
```

The last 20 runs are kept. The result has a **Download debug trace** link. The
trace records versions, selected types, payload counts, Tally lookups, skipped
units, chunk names, request sizes, result counts, timings, and errors. API
keys and secrets are never written.

## Safety rules

- One master type at a time.
- Use batch size 1 when testing Units.
- Never alter existing Tally units; only create new ones.
- Do not retry after a timeout without checking Tally for a dialog and
  checking whether the records were created.
- Use **Resume Tally requests** only after Tally is responsive again.
- Run `npm run check` and the Python tests before committing.

## Useful commands

```powershell
# tests
python -m unittest discover -s express_tally/tests -t .

# UI checks and build
cd control-centre
npm run check
npm run build

# run bridge from source
python -m express_tally.bridge serve

# run one sync in the background process
python -m express_tally.bridge sync --limit 1
```

## Notes

- Default bridge port is `8765`; default Tally URL is `http://127.0.0.1:9000`.
- The Linux container reached Tally at `172.17.0.1:19000` and the dev ERPNext
  site at `172.17.0.1:8000`.
- The installed VM bridge on `172.17.0.1:18765` did not answer HTTP requests,
  so the last tests ran the same source code locally against the VM's Tally.
