# Financial ledger sync

Three flows mirror selected financial ledgers through the normal Frappe API and
Tally HTTP/XML gateway:

| Flow | Direction | Result |
| --- | --- | --- |
| `express_tally.full_ledger_masters_to_tally` | ERPNext to Tally | Ledger names, standard accounting groups, and fiscal-year opening balances |
| `express_tally.full_ledger_to_tally` | ERPNext to Tally | All posted GL lines of each selected source voucher, as an accounting Journal |
| `express_tally.full_ledger_from_tally` | Tally to ERPNext | Opening balances, submitted accounting Journal Entries, and closing-balance checks |

Run masters before outbound accounting. Use the same company, target, fiscal
year, and balanced ledger selection in all three flows. Choose the full-ledger
outbound flow instead of the payment/sales outbound flows; choose the full-ledger
inbound flow instead of the standard/payment inbound flows. Duplicate accounting
exporters are rejected when their metadata declares an overlapping scope.
Previously exported source documents and Tally-originated ERP documents are
excluded from the accounting export.

The scope must contain **every accounting counterpart of every selected
voucher**. A voucher that crosses the scope fails; its other lines are never
dropped. Select 1–1000 distinct, enabled leaf accounts. Company-currency financial
accounts are supported. Receivable/payable selections require a specific
Customer/Supplier. Stock accounts require an inventory migration policy and are
rejected. This mirrors financial GL accounting; it does not recreate invoice
items, inventory movements, GST returns, or foreign-currency settlements.

Submitted GL entries are the outbound amount source, including tax, discount,
rounding, and repeated account lines. Inbound vouchers must have at least two
nonzero lines and balance exactly. A successful inbound voucher acknowledgement
requires a submitted Journal Entry with posted GL entries. Existing Tally ledger
openings that conflict with ERPNext fail unless `allow_opening_update` is explicitly
enabled for the selected master scope. Metadata updates retain existing openings.

## Configuration

Set `from_date` to the fiscal-year start and `to_date` to the desired closing date.
Put the selections under `flow_options`, for example:

```json
{
  "from_date": "2026-04-01",
  "to_date": "2027-03-31",
  "enabled_flows": [
    "express_tally.full_ledger_masters_to_tally",
    "express_tally.full_ledger_to_tally",
    "express_tally.full_ledger_from_tally"
  ],
  "flow_options": {
    "express_tally.full_ledger_masters_to_tally": {
      "account_names": ["Cash - COMPANY", "Bank - COMPANY"]
    },
    "express_tally.full_ledger_to_tally": {
      "account_names": ["Cash - COMPANY", "Bank - COMPANY"]
    },
    "express_tally.full_ledger_from_tally": {
      "fiscal_year_start": "2026-04-01",
      "ledger_names": ["Cash", "Bank"],
      "ledger_mappings": {
        "Cash": "Cash - COMPANY",
        "Bank": "Bank - COMPANY"
      }
    }
  }
}
```

For party accounts, replace `account_names` with `ledgers` objects in both outbound
flows, and use the same objects as inbound mapping values:

```json
{"account": "Debtors - COMPANY", "party_type": "Customer", "party": "CUST-001", "ledger_name": "Customer One"}
```

The inbound bridge keeps checkpoints isolated by ledger selection and fiscal
year. It retains voucher IDs so cancellations remain visible after Tally removes
their ledger lines. Preserve `tally-inbound-checkpoints.json` with the bridge
configuration. ERPNext audit records also make retries idempotent after a local
checkpoint is lost. Changed external vouchers cancel the previous posted ERP
Journal and submit a replacement. Cancelled vouchers cancel their imported
Journal. Closing checkpoints identify the balance snapshot, so new movements
are reconciled even when the ledger master AlterID is unchanged.

## Programmatic live acceptance test

From the bench `sites` directory:

```sh
../env/bin/python ../apps/express_tally/scripts/test_full_ledger_sync.py \
  --site rapl.localhost \
  --frappe-url http://127.0.0.1:8000 \
  --bridge-url http://172.17.0.1:18765 \
  --tally-url http://172.17.0.1:19000 \
  --state /absolute/path/full-ledger-test.json
```

The test obtains the existing bridge API user's secret locally without printing
or storing it. It creates ten uniquely named financial accounts, a balanced
nonzero opening Journal, ten submitted ERP source Journals, and ten independent
Tally source Journals. It verifies actual Tally voucher lines and actual submitted
ERP GL lines, retries including a lost acknowledgement, all ten closing balances,
a changed Tally voucher, and cancellation cleanup. Unequal movement amounts make
the closing balances differ from the openings. The test uses 2 April, a date
accepted by this VM. Adjust the source dates when adapting to a different fiscal
year.

Every fixture identity is saved during execution. Cleanup runs even when a test
fails, cancels only that run's documents, clears only its openings, and verifies
zero balances in both systems. Zero-balance ledger masters and cancelled documents
remain as audit evidence. If interrupted, recover using the same arguments and
`--cleanup --state /absolute/path/full-ledger-test.json`.

The runner exercises the current source bridge against the real ERP API and
Tally server. Installing the updated Windows bridge is a separate deployment;
running this test does not replace the Windows executable or enable automatic
sync on existing business ledgers.

Protocol references: [Tally XML samples](https://help.tallysolutions.com/sample-xml/),
[ledger import duplicate handling](https://help.tallysolutions.com/article/DeveloperReference/integration-capabilities/case_study_1.htm/),
and [order voucher classification](https://help.tallysolutions.com/article/DeveloperReference/tdlreference/release_4_5.htm/).
