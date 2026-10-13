# Payment Entry synchronization

The installed ERPNext app registers `express_tally.payment_entries_to_tally`
and `express_tally.payments_from_tally`. Both require an updated bridge with
`payment_voucher_v1` and `tally_payment_vouchers_v1` installed. Run
`bench --site <site> migrate` after updating the app to install the Tally Sync
User role's read access to Customer, Supplier, Account, Payment Entry, Sales
Invoice, and Purchase Invoice.
These reads are required by ERPNext's payment validation; the role does not
receive general create, write, or submit access.

Outbound vouchers use the submitted Payment Entry's posted GL entries in
company currency, including deductions and bill references. Pay, Receive, and
Internal Transfer become Payment, Receipt, and Contra respectively. Previously
delivered cancellations cancel the corresponding Tally voucher. An exact
source marker in the Tally narration allows retries to recover the destination
master ID even when Tally assigns its own voucher number and GUID.

Inbound payments and receipts become draft Payment Entries unless
`submit_documents` is enabled. The extractor has its own checkpoint, so payment
sync does not advance the all-voucher checkpoint. Select either the payment-only
inbound flow or the standard all-voucher inbound flow; they are exclusive.
Outbound-origin vouchers are skipped and imported Payment Entries are excluded
from outbound delivery to prevent loops.

Options belong under `flow_options` in the Control Centre configuration:

```json
{
  "express_tally.payment_entries_to_tally": {
    "document_names": ["ACC-PAY-2026-00001"]
  },
  "express_tally.payments_from_tally": {
    "submit_documents": false,
    "branch": "RAPL Unit-1",
    "ledger_accounts": {
      "ICICI Bank A/c-658905600890": "ICICI - RAPL"
    },
    "invoice_references": {
      "Tally bill number": "ERPNext invoice name"
    }
  }
}
```

Omit `document_names` for normal outbound synchronization. Branch is only needed
when the ERPNext site has a required Branch field. `ledger_accounts` resolves
Tally names that differ from ERPNext account names; mappings must belong to the
configured company. Customer/supplier names must resolve to an existing ERPNext
party. Bill references must resolve to submitted invoices. Unbalanced, ambiguous,
or foreign-account-currency inbound payments fail before document creation.
Use `voucher_ids` (Tally master IDs) to scope an inbound diagnostic or test;
Tally may automatically renumber imported vouchers.

## Portable Windows update

The regular Windows release workflow builds the updated executable. A portable
package can also be assembled with the official Windows embeddable Python ZIP:

```sh
python tally_plugin/build_portable.py \
  --python-runtime python-3.12.10-embed-amd64.zip \
  --output ERPNext-Tally-Control-Centre-Payment-Fix-Windows-x64.zip
```

Extract it into a new directory on Windows. Run `install-portable.ps1` with the
existing bridge configuration path and the branch to use. The installer runs
the bridge self-test and regression tests before stopping the old process,
backs up the configuration, and registers the portable bridge in the signed-in
desktop. The package contains its own Python runtime; it does not require a
system Python installation. Automatic sync remains disabled during the update.

For a QEMU VM, the installer can be invoked through the host VM project's
`scripts/guest-powershell.sh`. The host guest-agent socket must be accessible to
the process issuing that command; it is not mounted in the Frappe container.
