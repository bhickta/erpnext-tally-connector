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
from express_tally.framework import FlowContext, OutboundFlow


class SalesInvoiceToTally(OutboundFlow):
    key = "my_company.sales_invoice_to_tally"
    title = "Sales Invoices to Tally"
    agent_profile = "inventory_sales_voucher_v1"
    allowed_roles = frozenset({"Accounts Manager", "Tally Sync User"})

    def pull(self, context: FlowContext, limit: int):
        return find_and_map_pending_invoices(context, limit)

    def acknowledge(self, context: FlowContext, results):
        return persist_results_idempotently(context, results)
```

An inbound implementation receives records already extracted from Tally:

```python
from express_tally.framework import FlowContext, InboundFlow


class TallyReceiptToPaymentEntry(InboundFlow):
    key = "my_company.tally_receipt_to_payment_entry"
    allowed_roles = frozenset({"Accounts Manager", "Tally Sync User"})

    def receive(self, context: FlowContext, records):
        return [apply_receipt(context, record) for record in records]
```

Flow keys are stable API identifiers. Do not rename one after a client has been
configured without providing an alias or migration path.

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

The first implemented profile is `inventory_sales_voucher_v1`. It is consumed
by SRV's current Windows bridge and expects the existing version-1 sales document
payload. The profile maps Sales Orders and Delivery Notes to inventory-aware
Tally Sales vouchers.

Additional profiles should be introduced for other voucher types instead of
adding conditional company logic to the existing profile.

## State and idempotency

The framework deliberately does not use a shared `is_synced` field. Each flow
must maintain durable state using at least:

```text
flow key + source type + source ID + source version/hash + target ID
```

Acknowledgements must be idempotent by request ID. Destination identities must
also be deterministic so a lost HTTP acknowledgement cannot duplicate a Tally
voucher on retry.

The first draft leaves state storage with each flow. A generic event/outbox
DocType can be added after the SRV log and the legacy connector flags have a
tested data migration.

## Compatibility

SRV continues to expose its original API methods. Its bridge uses those methods
when `flow_name` is null, so existing installations and executables do not need
to change. With this connector installed, configuring
`srv.sales_documents_to_tally` switches the same bridge to the generic API while
retaining SRV's existing mapping, log, and deterministic Tally identity.
