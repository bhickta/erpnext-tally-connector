# Express Tally Control Centre for Windows

The Control Centre is the single Windows application for operating registered
ERPNext ↔ TallyPrime flows. It contains the local connector, scheduler, settings
screen, connection checks, manual controls, and synchronization history.

## Requirements

- TallyPrime with the target company loaded and its HTTP server enabled.
- An ERPNext site with Express Tally Integration and at least one registered flow.
- An API user with the **Tally Sync User** role, API key, and API secret.
- HTTPS when ERPNext is reached over a network.

The built-in inventory-sales profile also requires **Maintain Inventory**. If
zero-value rows are possible, enable **Allow zero-valued transactions** for the
Sales voucher type.

## Use on the Tally computer

1. Download and extract `ERPNext-Tally-Control-Centre-Windows-x64.zip` from
   [the latest release](https://github.com/bhickta/erpnext-tally-connector/releases/latest).
2. Double-click `ERPNextTallyControlCentre.exe`. The dashboard opens in the
   default browser; Python and Node.js are not required.
3. Open **Connections & settings**, enter the ERPNext URL/API credentials and
   the exact ERPNext and Tally company names, then choose **Save and test connections**.
4. Open **Master data** to send the items, customers, suppliers, and accounts
   your transactions need. Review **Import options** before including opening stock.
5. Open **Transaction flows**, enable compatible flows handled by this Tally
   computer, and choose **Save enabled flows**.
6. Select **Configure & preview** for a flow. In **Flow workspace**, review its
   configuration, save any changes, select **Preview records**, then **Run this flow**.
7. Use the **Dashboard** manual direction buttons or enable automatic transaction
   sync. Check **Run history** for transaction results and **Master data** for master
   results. Automatic transaction sync does not include master data.

The executable listens only on `127.0.0.1:8765` by default. Settings, history,
and the log are stored in `%LOCALAPPDATA%\Express Tally Control Centre`. Starting
the executable again opens the same Control Centre; only one instance can bind
the local port.

## Both sync directions

The Control Centre operates all registered directions:

- ERPNext → Tally flows use an agent profile that implements `deliver`.
- Tally → ERPNext flows use an agent profile that implements `collect`; ERPNext
  then validates and applies those records through the flow's `receive` method.

The UI disables any flow whose required profile is not installed in the Windows
package. This prevents a Tally payload from being interpreted using the wrong
accounting policy. The bundled `inventory_sales_voucher_v1` profile currently
handles SRV Sales Orders and Delivery Notes from ERPNext to Tally. Inbound data
types require their corresponding registered flow and extraction profile.

The bundled `tally_ledger_mirror_v1` profile supports staged financial-year
opening, voucher, and closing-balance records for company-specific ledger mirror
flows. The ERPNext app that registers the flow remains responsible for exact
ledger/party mapping and reconciliation policy.

## Masters sync

The **Master data** page pushes Accounting Groups, Ledgers, Item, Warehouse,
Customer, and Supplier masters from ERPNext to the loaded Tally company.
Ledger sync creates the accounting-group hierarchy; Item sync creates the
required Units and Item Groups. Each pushed master stores the ERPNext document
name in the Tally alias, so a Tally record can always be mapped back to its
ERPNext source.

The utility is one-way (ERPNext → Tally), never deletes Tally masters, and
leaves item opening stock off unless explicitly enabled. See
[`docs/masters-sync.md`](../docs/masters-sync.md) for the field mapping and
alias contract.

Selections survive count refreshes. A running import shows persistent progress;
**Stop after current request** lets Tally finish its current work before skipping
remaining batches. If a request times out, check Tally and the affected records
before restarting the bridge and rerunning the selected masters.

## Advanced command-line operation

The Control Centre UI is the normal interface. These commands remain available
for diagnostics and compatibility:

```bat
ERPNextTallyControlCentre.exe --config tally-bridge.json status
ERPNextTallyControlCentre.exe --config tally-bridge.json sync --limit 5
ERPNextTallyControlCentre.exe --config tally-bridge.json serve --no-browser
```

The executable can call any Frappe endpoint permitted for its API user:

```bat
ERPNextTallyControlCentre.exe --config tally-bridge.json api GET /api/resource/Company
```

## Build

Push bridge, Control Centre, or packaging changes to `master`, or run the GitHub
Actions workflow manually. It tests the bridge, builds the npm UI, embeds it in
the Windows executable, uploads the ZIP, and updates the rolling prerelease.

For a local Windows build:

```powershell
powershell -ExecutionPolicy Bypass -File .\tally_plugin\build-windows.ps1
```

The output is `dist\ERPNext-Tally-Control-Centre-Windows-x64.zip`.

## Retry and security behavior

Outbound profiles use deterministic Tally identities so retrying after a lost
acknowledgement does not create a second voucher. The local API rejects
cross-origin control requests, settings responses mask the API secret, and the
default listener is loopback-only.

TallyPrime Educational Mode restricts accepted transaction dates. For a test
company only, set the educational date override in Connections & settings; leave it blank for
licensed operation.
