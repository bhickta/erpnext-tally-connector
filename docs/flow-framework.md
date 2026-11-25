# Tally flow framework

The framework separates reusable orchestration from company accounting policy.
It does not assume that a Sales Order, Delivery Note, or Tally voucher has the
same meaning for every company.

## Register a flow

Expose flow classes through the contributing Frappe app's `hooks.py`:

```python
tally_integration_flows = [
    "my_company.tally.flows.SalesInvoiceToTally",
]
```

An outbound implementation supplies mapped records and records acknowledgements:

```python
from express_tally.framework import FlowContext, OutboundFlow, OutboundSyncLog, SourceSpec


sync_log = OutboundSyncLog(
    "my_company.sales_invoice_to_tally",
    [SourceSpec("Sales Invoice", "posting_date")],
)


class SalesInvoiceToTally(OutboundFlow):
    key = "my_company.sales_invoice_to_tally"
    title = "Sales Invoices to Tally"
    agent_profile = "inventory_sales_voucher_v1"
    allowed_roles = frozenset({"Accounts Manager", "Tally Sync User"})

    def pull(self, context: FlowContext, limit: int):
        return find_and_map_pending_invoices(context, limit)

    def acknowledge(self, context: FlowContext, results):
        return sync_log.acknowledge(context, results)

    def status(self, context: FlowContext):
        return sync_log.status(context)
```

An inbound implementation receives records already extracted from Tally:

```python
from express_tally.framework import FlowContext, InboundFlow


class TallyReceiptToPaymentEntry(InboundFlow):
	key = "my_company.tally_receipt_to_payment_entry"
	agent_profile = "my_tally_receipts_v1"
    allowed_roles = frozenset({"Accounts Manager", "Tally Sync User"})

	def receive(self, context: FlowContext, records):
		return [apply_receipt(context, record) for record in records]
```

The matching Windows `AgentProfile` implements `collect(config, tally_client,
limit, options)` and may implement `acknowledge_collected(...)` to persist its
checkpoint only after ERPNext accepts the batch. The connector includes
`tally_masters_v1` and `tally_vouchers_v1`, which query Tally through inline XML
collections and therefore do not require an installed TDL or TCP file.

Two standard inbound flows are registered by the connector:

```text
express_tally.standard_masters_from_tally
express_tally.standard_vouchers_from_tally
```

Master sync should be enabled before voucher sync. Voucher imports are drafts by
default. Set `submit_documents: true` in that flow's options only after mappings
have been verified in a test company; the Control Centre exposes this as a
settings checkbox.

Flow keys are stable API identifiers. Do not rename one after a client has been
configured without providing an alias or migration path.

## Reusable sales-voucher preset

For standard submitted Sales Orders and Delivery Notes, a company app can be as
small as:

```python
from express_tally.integrations.sales_voucher_flow import SalesDocumentsToTallyFlow


class CompanySalesToTally(SalesDocumentsToTallyFlow):
    key = "my_company.sales_documents_to_tally"
    title = "Sales documents to Tally"
    allowed_roles = frozenset({"Accounts Manager", "Tally Sync User"})
```

Override `mapper_class` with a `SalesDocumentMapper` subclass when narration,
references, ledgers, parties, lines, taxes, or eligibility differ. Override
`source_specs` to change source DocTypes/date fields. The connector continues to
own pending-version selection, sync logging, payload hashing, agent transport,
master ordering, and Tally import/response handling.

The sales-document flow keeps its existing voucher types and IDs. Any voucher
with stock lines uses the item invoice layout, including Sales Orders and
Delivery Notes; the accounting layout is used only when there are no stock lines.
Tally accepts an accounting-view import but stores its inventory block empty, so
stock lines would not appear in the posted voucher. For vouchers with stock
lines, enable **Inventory values are affected** on the configured Tally sales
ledger. The bridge enables this when it creates the ledger and asks the operator
to review the setting on existing ledgers. [Tally XML samples](https://help.tallysolutions.com/sample-xml/)
show both voucher layouts.

The bridge looks up each stock voucher through an inline Tally collection and
checks the stored item names, quantities and godowns before acknowledging it.
If a write is accepted and readback fails, the rest of the batch stays pending.
A retry checks the existing voucher and source version before creating any
masters or sending another voucher. Reconcile older or incomplete vouchers in
Tally before retrying them.

On the Tally computer, the bridge can inspect a previously imported voucher
using its configured Tally endpoint and company. Use the number shown in Tally,
since Tally may assign a different number at import.

```text
python -m express_tally.bridge --config tally-bridge.json inspect-voucher --number DN-FULL --date 2026-07-01
```

The command returns the voucher's master ID and stored stock lines. If more
than one voucher matches, add `--voucher-type` with the type shown in Tally.
This command uses collection export and works independently of the named
report exports. If the collection request itself fails, the command reports
the error.

Payment, Receipt, Contra, and Journal profiles verify the saved posting date,
ledger entries, amounts, and bill allocations. Cancellation is acknowledged
after Tally returns the voucher as cancelled. Ledger opening sync also reads
back the ledger name, group, bill-wise setting, and opening balance.

Inbound collection errors stop the batch and leave its checkpoint available
for retry. Acknowledgements use the Tally source identity when ERPNext returns
it. An accepted item above a pending older version is recorded separately
until the gap closes. Date and voucher-type filters keep separate voucher
checkpoints. A newly separated scope starts with no checkpoint and may read
records already seen by the older scope.

The standalone Stock Entry poller uses a cursor scoped to the ERPNext company
and destination target ID. It compares Stock Journal source and destination
movements before acknowledging and stops at the first failed entry. Update
the server app and standalone poller together because the feed now requires
company and target parameters. On the first scan after upgrade, the poller may
encounter older journals without a matching version marker. Review those
journals before resuming the feed. Delivery Notes use the registered sales
flow.

## Context

Every operation receives a `FlowContext` containing:

- `company`: ERPNext company
- `target_id`: stable identifier for one destination data set
- `tally_company`: exact Tally company name
- `from_date` and `to_date`: optional selection window
- `options`: flow-specific JSON settings

The `target_id` must stay stable. A different target ID represents a different
replication destination and must not share acknowledgement state.

## Agent profiles

An outbound flow advertises an `agent_profile`. The local agent must reject an
unknown profile before writing anything to Tally. This prevents a purchase,
payroll, or company-specific payload from accidentally being interpreted as a
Sales voucher.

The outbound inventory-sales profile is `inventory_sales_voucher_v1`. It expects the
version-1 sales-document payload and maps those records to inventory-aware Tally
Sales vouchers. SRV uses this profile for its current Sales Order and Delivery
Note flow.

Additional profiles should be introduced for other voucher types instead of
adding conditional company logic to the existing profile.

The built-in inbound profiles are `tally_masters_v1` and `tally_vouchers_v1`.
They fetch standard Tally masters and vouchers over the localhost XML gateway,
normalize the response, and use per-target Alter ID checkpoints stored by the
Control Centre. ERPNext performs the final validation and document mapping.

The connector also includes `tally_ledger_mirror_v1` for company-owned financial
migration flows. It stages previous-day ledger closing balances as fiscal-year
openings, then accounting vouchers, then per-ledger closing balances for
reconciliation. Such a flow must provide `fiscal_year_start` in its
`default_options`; company-specific account and party policy remains on ERPNext.

## State and idempotency

The framework deliberately does not use a shared `is_synced` field. State is
scoped using at least:

```text
flow key + source type + source ID + source version/hash + target ID
```

`OutboundSyncLog` implements this pattern against the connector-owned **Tally
Sync Log** DocType. It finds source versions without a successful result,
records acknowledgements idempotently by request ID, returns the latest target
reference for Alter operations, and reports counts. A flow still owns mapping
and any eligibility rules beyond submitted/company/date filtering.

Successful inbound and outbound versions also receive a deterministic unique
idempotency key. This protects the server when the same batch is delivered by
more than one agent. Failed attempts do not consume that key and remain
retryable.

`SourceSpec(include_cancelled_if_synced=True)` includes cancelled documents only
when the same source was previously acknowledged successfully. This supports a
single cancellation event without ever exporting a document that was cancelled
before its first sync.

`SourceSpec(sync_once=True)` makes the source lifecycle-based instead of
modified-version-based: one successful Create/Alter and at most one later
Cancel are delivered for each source document and Target ID.

Inbound flows which consume the same accounting-voucher stream should publish
the same `exclusive_group`. The Control Centre refuses to run two selected flows
from one exclusive group together.

Destination identities must also be deterministic so a lost HTTP
acknowledgement cannot duplicate a Tally voucher on retry.

## Compatibility

SRV keeps its original Python API and bridge import paths as thin compatibility
wrappers, but new agents use the generic framework API and require a
`flow_name`. Configure `srv.sales_documents_to_tally` for SRV. Existing
unscoped SRV log rows and its deterministic Tally identity remain recognized,
so migration does not resend already acknowledged vouchers.
