"""Posted ERPNext payment accounting exported as Tally accounting vouchers."""

import hashlib
import json
from decimal import Decimal

import frappe

from express_tally.framework import OutboundFlow, OutboundSyncLog, SourceSpec

PAYMENT_FLOW = "express_tally.payment_entries_to_tally"


def money(value):
	return Decimal(str(value or 0)).quantize(Decimal("0.01"))


class PaymentEntryMapper:
	def __init__(self, flow):
		self.flow = flow

	def map_document(self, doc, context):
		previous = self.flow.sync_log.previous_target_reference(doc.doctype, doc.name, context.target_id)
		operation = "Cancel" if doc.docstatus == 2 else ("Alter" if previous else "Create")
		if operation == "Cancel" and not previous:
			raise ValueError(f"Cannot cancel unsynced Payment Entry {doc.name}")
		voucher_type = {"Pay": "Payment", "Receive": "Receipt", "Internal Transfer": "Contra"}.get(
			doc.payment_type
		)
		if not voucher_type:
			raise ValueError(f"Unsupported payment type: {doc.payment_type}")
		payload = {
			"source_doctype": doc.doctype,
			"name": doc.name,
			"modified": str(doc.modified),
			"operation": operation,
			"identity_namespace": self.flow.key,
			"origin_marker": self.flow.key,
			"tally_voucher_id": previous,
			"voucher_type": voucher_type,
			"transaction_date": str(doc.posting_date),
			"reference": doc.reference_no or doc.name,
			"narration": doc.remarks or f"ERPNext Payment Entry {doc.name}",
			"ledger_entries": [],
			"masters": [],
		}
		if operation != "Cancel":
			rows = frappe.get_all(
				"GL Entry",
				filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
				fields=[
					"account",
					"debit",
					"credit",
					"party_type",
					"party",
					"against_voucher_type",
					"against_voucher",
				],
				order_by="creation, name",
			)
			payload.update(self.map_ledger_entries(rows, doc.company))
		payload["source_hash"] = hashlib.sha256(
			json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
		).hexdigest()
		return payload

	def map_ledger_entries(
		self, rows, company, *, preserve_lines=False, ledger_names=None, party_ledgers=None
	):
		entries = {}
		masters = {}
		party_ledger = ""
		for row in rows:
			amount = money(row.get("credit")) - money(row.get("debit"))
			if not amount:
				continue
			if row.get("party"):
				party_type = row.get("party_type")
				if party_type not in {"Customer", "Supplier"}:
					raise ValueError(f"Unsupported payment party type: {party_type}")
				field = "customer_name" if party_type == "Customer" else "supplier_name"
				ledger = frappe.db.get_value(party_type, row["party"], field)
				parent = "Sundry Debtors" if party_type == "Customer" else "Sundry Creditors"
				party_ledger = ledger
			else:
				account = frappe.db.get_value(
					"Account",
					row["account"],
					["account_name", "account_type", "root_type", "company"],
					as_dict=True,
				)
				if not account or account.company != company:
					raise ValueError(f"Account {row['account']} does not belong to {company}")
				ledger = account.account_name
				parent = {"Bank": "Bank Accounts", "Cash": "Cash-in-Hand", "Tax": "Duties & Taxes"}.get(
					account.account_type
				)
				parent = parent or {
					"Expense": "Indirect Expenses",
					"Income": "Indirect Incomes",
					"Asset": "Current Assets",
					"Liability": "Current Liabilities",
					"Equity": "Capital Account",
				}.get(account.root_type)
			if not ledger or not parent:
				raise ValueError(f"Could not map payment ledger {row['account']}")
			ledger = (
				row.get("tally_ledger")
				or (party_ledgers or {}).get(row.get("party"))
				or (ledger_names or {}).get(row["account"])
				or ledger
			)
			masters[ledger] = {"name": ledger, "parent": parent, "billwise": bool(row.get("party"))}
			entry = entries.setdefault(
				(ledger, len(entries)) if preserve_lines else ledger,
				{
					"ledger": ledger,
					"amount": Decimal(0),
					"is_party": bool(row.get("party")),
					"bill_allocations": [],
				},
			)
			entry["amount"] += amount
			if row.get("party"):
				invoice = (
					row.get("against_voucher")
					if row.get("against_voucher_type") in {"Sales Invoice", "Purchase Invoice"}
					else ""
				)
				entry["bill_allocations"].append(
					{
						"name": invoice or "",
						"bill_type": "Agst Ref" if invoice else "On Account",
						"amount": str(amount),
					}
				)
		if len(entries) < 2 or sum((e["amount"] for e in entries.values()), Decimal(0)) != 0:
			raise ValueError("Payment Entry has no balanced posted GL entries")
		for entry in entries.values():
			entry["amount"] = str(entry["amount"])
		return {
			"ledger_entries": list(entries.values()),
			"masters": list(masters.values()),
			"party_ledger": party_ledger,
		}


class PaymentEntriesToTallyFlow(OutboundFlow):
	key = PAYMENT_FLOW
	title = "Payment Entries to Tally"
	agent_profile = "payment_voucher_v1"
	accounting_scope = "payments"
	allowed_roles = frozenset({"Tally Sync User", "Accounts Manager", "System Manager"})
	source_specs = (
		SourceSpec(
			"Payment Entry",
			"posting_date",
			include_cancelled_if_synced=True,
			sync_once=True,
			exclude_tally_imports=True,
		),
	)

	def __init__(self):
		self.sync_log = OutboundSyncLog(self.key, self.source_specs)
		self.mapper = PaymentEntryMapper(self)

	def pull(self, context, limit):
		names = context.options.get("document_names")
		if names is not None and (not isinstance(names, list) or any(not isinstance(n, str) for n in names)):
			raise ValueError("document_names must be an array of Payment Entry names")
		refs = self.sync_log.pending_references(
			context.company,
			context.target_id,
			limit,
			from_date=context.from_date,
			to_date=context.to_date,
			source_names=names,
		)
		return [self.mapper.map_document(frappe.get_doc(doctype, name), context) for _, doctype, name in refs]

	def acknowledge(self, context, results):
		return self.sync_log.acknowledge(context, results)

	def status(self, context):
		return self.sync_log.status(context)
