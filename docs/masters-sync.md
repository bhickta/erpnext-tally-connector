# Masters sync (ERPNext → TallyPrime)

The Control Centre **Master data** page pushes ERPNext master data to the
TallyPrime company loaded on the same Windows machine. It is deliberately
one-way: ERPNext is the source of truth, and Tally masters are created or
altered by name.

## Selectable master groups

| Control Centre group | ERPNext source | Tally destination |
| --- | --- | --- |
| Accounting groups | `Account` where `is_group = 1` | `Group` |
| Ledgers | `Account` where `is_group = 0` | `Ledger` |
| Units of measure | `UOM` | `Unit` |
| Item groups | `Item Group` | `Stock Group` |
| Items | `Item` | `Stock Item` |
| Warehouses | `Warehouse` | `Godown` |
| Customers | `Customer` | `Ledger` under `Sundry Debtors` |
| Suppliers | `Supplier` | `Ledger` under `Sundry Creditors` |

Each type is selectable independently so you can sync one doctype at a time.
For a complete sync, run them in this order:

```text
account groups -> account ledgers -> units -> item groups
-> warehouses -> customers -> suppliers -> items
```

`Ledger` includes the accounting-group hierarchy as a dependency. Units and
Item Groups are separate master types; sync them before Items. Existing Tally
units are skipped, never altered.

## Alias contract

Every pushed master carries a stable ERPNext identity in Tally's alias list
(`LANGUAGENAME.LIST` / `NAME.LIST`). The first name is the Tally master name;
the remaining names are aliases.

| Master | Tally name | Alias values |
| --- | --- | --- |
| Group | ERPNext `account_name` without the company abbreviation | raw ERPNext `name`, then `ERPNext:Account:<name>` |
| Ledger | ERPNext `account_name` without the company abbreviation | raw ERPNext `name`, then `ERPNext:Account:<name>` |
| Unit | ERPNext `uom_name` | Tally units do not support aliases |
| Item Group | ERPNext item group name | raw ERPNext `name`, then `ERPNext:Item Group:<name>` |
| Item | ERPNext `item_name` (or `item_code` when blank) | item `item_code`, then `ERPNext:Item:<item_code>` |
| Warehouse | `warehouse_name` without the company abbreviation | raw ERPNext `name`, then `ERPNext:Warehouse:<name>` |
| Customer | `customer_name` | raw ERPNext `name` when different, then `ERPNext:Customer:<name>` |
| Supplier | `supplier_name` | raw ERPNext `name` when different, then `ERPNext:Supplier:<name>` |

Customers and suppliers also write the ERPNext `name` to `LEDGERCODE`. The
raw alias is omitted only when it would duplicate the Tally master name; the
namespaced alias is always present, so a Tally record can always be mapped
back to its ERPNext document.

Existing Tally stock items are normally named by the ERPNext item name, not by
the item code, so the item master name follows that convention.

Duplicate display names are resolved as follows:

- **Items:** every colliding item is named `<item_name> (<item_code>)`, so all
  items remain distinct.
- **Parties and warehouses:** choose `duplicate_names=skip` to keep the first
  record and report the rest, or `duplicate_names=suffix` to append the
  ERPNext ID to every colliding record. `skip` is the default.

## Field mapping

### Group → Tally Group

- `name`: ERPNext `account_name` with the company abbreviation removed
- `parent`: parent account group; standard Tally groups such as
  `Current Assets`, `Bank Accounts`, `Sundry Debtors`, `Duties & Taxes` are
  reused instead of duplicated
- `isrevenue`: true for income groups
- `isaddable`: true
- `iscostcentreson`, `affectsstock`: false
- `isdeleted`: disabled groups, when **Include disabled records** is enabled

### Ledger → Tally Ledger

- `name`: ERPNext `account_name` with the company abbreviation removed
- `parent`: parent account group, or the mapped standard Tally group
- `isbillwiseon`: true for Receivable and Payable accounts
- `iscostcentreson`: false
- `ledgercode`: ERPNext `account_number` when present, otherwise the ERPNext `name`
- `currency`: non-company account currency when present
- `isdeleted`: disabled ledgers, when **Include disabled records** is enabled

### Unit → Tally Unit

- `name`: ERPNext `uom_name`
- `originalname`: ERPNext `uom_name`
- `decimalplaces`: `0` when `must_be_whole_number`, otherwise `3`
- existing Tally units are **skipped**, not altered

### Item Group → Stock Group

- `name`: ERPNext `Item Group` name
- `parent`: parent item group; `All Item Groups` becomes Tally Primary
- `shouldquantitiesbeadded`: true

### Item → Stock Item

- `name`: `item_name`, or `<item_name> (<item_code>)` when the name is not unique
- `parent`: ERPNext item group name; `All Item Groups` becomes Tally Primary
- `base units`: `stock_uom`
- `description`: `description` with HTML removed, falling back to `item_name`
- `hsncode`: `gst_hsn_code`
- `gstapplicable`: `Applicable` when an HSN code is present
- `gsttypeofsupply`: `Goods` for stock items, `Services` otherwise
- `isbatchwiseon`: `has_batch_no`
- `isperishableon`, `hasmfgdate`: `has_expiry_date`
- `ignorebatches`: inverse of `has_batch_no`
- `ignoregodowns`: inverse of `is_stock_item`
- `valuationmethod`: `FIFO` or `Avg. Price` from `valuation_method`
- `standardcostlist`: `valuation_rate`
- `standardpricelist`: `standard_rate`
- `reorderlevel`: highest `Item Reorder.warehouse_reorder_level`
- `reorderqty`: highest `Item Reorder.warehouse_reorder_qty`
- `barcodes`: `Item Barcode` rows
- `uoms`: `UOM Conversion Detail` rows
- `openingbalance`, `openingrate`, `openingvalue`: only when the
  **Include item opening stock** checkbox is enabled
- `isdeleted`: disabled items, when **Include disabled records** is enabled

### Warehouse → Godown

- `name`: `warehouse_name` with the company abbreviation removed
- `parent`: `parent_warehouse` with the company abbreviation removed;
  the root and `All Warehouses` become Tally Primary
- the `All Warehouses` root group itself is not imported
- aliases: raw `name` and `ERPNext:Warehouse:<name>`

### Customer / Supplier → Ledger

- `name`: `customer_name` / `supplier_name`
- `parent`: `Sundry Debtors` / `Sundry Creditors`
- `isbillwiseon`: `Yes`
- `iscostcentreson`, `affectsstock`: `No`
- `ledgercode`: ERPNext `name`
- `partygstin`: `gstin` or `tax_id`
- `incometaxnumber`: `pan`, or the PAN embedded in a 15-character GSTIN
- `gstregistrationtype`: mapped from `gst_category`
- `isgstapplicable`: true when a GSTIN or a non-unregistered GST category exists
- `countryofresidence`: `country`, primary address country, or company country
- `ledstatename`, `pincode`: primary address
- `address`: primary address lines, with the city appended because Tally has
  no separate city field
- `email`, `ledgerphone`, `ledgermobile`, `ledgercontact`: primary contact,
  address, or party contact fields
- `creditlimit`: sum of `Customer Credit Limit` for the configured company
- `website`: party website
- `mailingname`, `ledmailingdetails`: mailing name, address, state, country,
  and pincode. `applicablefrom` uses the configured **From date** or today.
- aliases: raw ERPNext `name` when different, then
  `ERPNext:Customer:<name>` or `ERPNext:Supplier:<name>`

## Running a sync

1. Open **Master data** in the Control Centre.
2. Select one or more groups. Items, Warehouses, Customers, and Suppliers are
   enabled by default.
3. Optionally enable **Include disabled records** or
   **Include item opening stock**.
4. Optionally set **Limit per group** to test a small batch. When Unit or
   Item Group is synced alone, the limit applies. When Items is selected with
   those dependencies, their batches remain complete.
5. Select **Sync selected to Tally**.

The bridge imports dependency batches in this order:

```text
account groups -> account ledgers -> units -> item groups -> warehouses -> customers -> suppliers -> items
```

Master requests default to 10 records and are capped at 25 records and 64 KiB
of encoded JSON, including when older settings specify a larger batch size.
Slow successful responses reduce the remaining batch size. An individual
master exceeding 64 KiB is rejected locally; reduce its oversized fields before
retrying. A failed request stops the run so later batches do not fail against
missing dependencies. The results table shows confirmed created, altered,
ignored, and error counts per group; a transport failure has an unknown outcome
and the affected records may already have been imported.

All XML and JSON calls to the same Tally endpoint share one process-wide
request gate. Concurrent calls fail with a busy message rather than queueing
more work in Tally. Calls have a minimum 250 ms pause, increasing with response
time up to 2 seconds. Empty batches do not perform master lookups.

A timeout or connection failure pauses further calls to that endpoint for the
lifetime of the bridge process, including health checks and automatic sync.
There is no automatic retry: disconnecting HTTP cannot cancel work inside Tally.
Wait until Tally is responsive, inspect the imported records, and only then
restart the bridge and rerun the sync. Existing master names are read again on
each run and updated after each successful chunk so confirmed prior imports
can be altered rather than created again. A rejected name lookup stops the run
instead of treating the company as empty.
Keep only one bridge process connected to a Tally instance; this gate cannot
coordinate other processes or other applications. Name lookups still read the
existing master names once per Tally object type per run. These safeguards
reduce gateway pressure but cannot guarantee Tally responsiveness for every
data set, plugin, storage system, or individual master.

The same operation is available directly:

```text
GET  /api/v1/masters
POST /api/v1/masters/sync
```

Example body:

```json
{
  "masters": ["Group", "Ledger", "Unit", "Item Group", "Item", "Warehouse", "Customer", "Supplier"],
  "include_disabled": false,
  "include_opening_balances": false,
  "limit": null,
  "duplicate_names": "skip"
}
```

`limit` is optional and applies to the selected unit, item-group, warehouse,
customer, supplier, and item batches. When Items is selected together with
Units or Item Groups, those dependency batches remain complete.
`duplicate_names` accepts `skip` or `suffix`.

The bridge calls these Frappe methods:

```text
get_master_sync_summary
get_master_sync_payload
```

Both allow the `Tally Sync User` role. Installation/migration grants that role
read access to Company, Item, Item Group, Warehouse, UOM, Customer Group,
Supplier Group, Address, Contact, Customer Credit Limit, Item Barcode,
Item Reorder, and UOM Conversion Detail.

## Limitations

- Masters are created or altered; the utility never deletes Tally masters.
- Opening stock is opt-in because repeated imports must not create duplicate
  stock openings.
- Customer and supplier groups are not recreated in Tally; parties use the
  standard `Sundry Debtors` and `Sundry Creditors` groups, matching the
  sales-voucher flow.
- Item names remain the ERPNext `item_code` because the sales-voucher flow
  references that name. The item display name is stored as an alias.
- The Windows executable must be rebuilt or updated to include this utility;
  the Frappe-side methods alone are not enough for the Control Centre page.
