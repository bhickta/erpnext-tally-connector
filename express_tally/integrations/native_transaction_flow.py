"""Tally vouchers as posted ERPNext business documents, with GL verification."""

from collections import defaultdict
from decimal import Decimal
from typing import ClassVar

import frappe

from express_tally.bridge.bill_references import dated_bill_key
from express_tally.bridge.collection_gateway import scalar
from express_tally.integrations.tally_inbound_flows import (
	TallyVouchersToERPNextFlow,
	_success_result,
)

FLOW = "express_tally.native_transactions_from_tally"


class UnresolvedBillReference(ValueError):
	"""The source bill has no unambiguous imported invoice counterpart."""


def money(value):
	return Decimal(str(value or 0)).quantize(Decimal("0.01"))


class NativeTransactionsFromTallyFlow(TallyVouchersToERPNextFlow):
	key = FLOW
	title = "Posted native transactions from Tally with GL verification"
	agent_profile = "tally_native_transactions_v1"
	default_options: ClassVar[dict] = {"submit_documents": True, "strict_ledger_verification": True}

	def resolve(self, context, name):
		mapping = (context.options.get("ledger_mappings") or {}).get(name)
		if not isinstance(mapping, dict) or not mapping.get("account"):
			raise ValueError(f"No verified ERP ledger mapping for {name}")
		return mapping

	def apply_record(self, context, record):
		if record.get("optional"):
			return _success_result(record, status="Skipped", message="Optional Tally voucher")
		entries = record.get("ledger_entries") or []
		if entries and all(money(entry["amount"]) == 0 for entry in entries):
			return _success_result(
				record, status="Skipped", message="Source voucher has no financial movement"
			)
		kind = str(record.get("voucher_type") or "").casefold()
		for entry in record.get("ledger_entries") or []:
			for bill in entry.get("bill_allocations") or []:
				bill_type = scalar(bill, "bill_type").casefold()
				if bill_type not in {"on account", "new ref", "agst ref", "advance"}:
					raise ValueError(f"Unsupported bill allocation: {bill_type}")
		if kind in {"sales", "purchase", "credit note", "debit note"}:
			party = self.resolve(context, record.get("party_ledger"))
			sales = party.get("party_type") == "Customer"
			if party.get("party_type") not in {"Customer", "Supplier"}:
				return self.journal(
					context, record, "Cash Entry" if party.get("account_type") == "Cash" else "Journal Entry"
				)
			if (kind == "sales" and not sales) or (kind == "purchase" and sales):
				return self.journal(context, record)
			party_amount = sum(
				money(entry["amount"])
				for entry in record["ledger_entries"]
				if entry["ledger"] == record["party_ledger"]
			)
			return self.invoice(
				context,
				record,
				sales=sales,
				is_return=party_amount > 0 if sales else party_amount < 0,
			)
		if kind in {"payment", "receipt"}:
			return self.payment(context, record, kind)
		if kind in {"journal", "contra"}:
			return self.journal(context, record, "Contra Entry" if kind == "contra" else "Journal Entry")
		if kind in {"delivery note", "receipt note"}:
			return self.stock_document(context, record, outgoing=kind == "delivery note")
		raise ValueError(f"Unsupported native transaction type: {record.get('voucher_type')}")

	def expected_gl(self, context, record):
		rows = defaultdict(Decimal)
		for entry in record.get("ledger_entries") or []:
			mapping = self.resolve(context, entry["ledger"])
			key = (mapping["account"], mapping.get("party_type") or "", mapping.get("party") or "")
			rows[key] -= money(entry["amount"])
		if sum(rows.values()) != 0 or not rows:
			raise ValueError("Source accounting lines do not balance")
		return {key: value for key, value in rows.items() if value}

	def verify_gl(self, context, record, doctype, name):
		expected = self.expected_gl(context, record)
		actual = defaultdict(Decimal)
		for row in frappe.get_all(
			"GL Entry",
			filters={"voucher_type": doctype, "voucher_no": name, "is_cancelled": 0},
			fields=["account", "party_type", "party", "debit", "credit"],
		):
			actual[(row.account, row.party_type or "", row.party or "")] += money(row.debit) - money(
				row.credit
			)
		actual = {key: value for key, value in actual.items() if value}
		if actual != expected:
			differences = {
				str(key): str(actual.get(key, 0) - expected.get(key, 0))
				for key in actual.keys() | expected.keys()
				if actual.get(key, 0) != expected.get(key, 0)
			}
			raise ValueError(f"Posted ERP GL differs from Tally: {differences}")

	def _save_document(self, context, record, values):
		if context.options.get("historical_migration") and values["doctype"] in {
			"Sales Invoice",
			"Purchase Invoice",
		}:
			original = frappe.db.get_single_value("Accounts Settings", "enable_common_party_accounting")
			frappe.db.set_single_value("Accounts Settings", "enable_common_party_accounting", 0)
			try:
				return self._save_with_purchase_policy(context, record, values)
			finally:
				frappe.db.set_single_value("Accounts Settings", "enable_common_party_accounting", original)
		return self._save_with_purchase_policy(context, record, values)

	def _save_with_purchase_policy(self, context, record, values):
		# Historical source invoices have no ERP purchase orders. Apply the existing
		# supplier exception within this transaction, restoring it before any commit.
		if values["doctype"] == "Purchase Invoice" and context.options.get("historical_migration"):
			field = "allow_purchase_invoice_creation_without_purchase_order"
			supplier = values["supplier"]
			original = frappe.db.get_value("Supplier", supplier, field)
			frappe.db.set_value("Supplier", supplier, field, 1, update_modified=False)
			try:
				return self._save_native_document(context, record, values)
			finally:
				frappe.db.set_value("Supplier", supplier, field, original, update_modified=False)
		return self._save_native_document(context, record, values)

	def _save_native_document(self, context, record, values):
		doctype = values["doctype"]
		previous = record.get("_previous_name")
		if previous and record.get("_previous_doctype") == doctype and frappe.db.exists(doctype, previous):
			old = frappe.get_doc(doctype, previous)
			old.flags.ignore_permissions = True
			if old.docstatus == 1:
				old._cancel()
			elif old.docstatus == 0:
				old.delete(ignore_permissions=True)
		meta = frappe.get_meta(doctype)
		if meta.has_field("branch"):
			values.setdefault("branch", context.options.get("branch"))
		doc = frappe.get_doc(values)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		doc._submit()
		if doc.docstatus != 1:
			raise ValueError("Imported transaction did not submit synchronously")
		if str(doc.posting_date) != record["date"]:
			raise ValueError("Imported transaction did not preserve the Tally posting date")
		if doctype in {"Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"}:
			self.verify_gl(context, record, doctype, doc.name)
		return _success_result(record, doctype, doc.name, message="Posted and verified native transaction")

	def journal(self, context, record, voucher_type="Journal Entry"):
		self.expected_gl(context, record)
		lines = []
		for entry in record["ledger_entries"]:
			mapping = self.resolve(context, entry["ledger"])
			remaining = -money(entry["amount"])
			allocations = []
			for bill in entry.get("bill_allocations") or []:
				if scalar(bill, "bill_type").casefold() == "agst ref":
					try:
						doctype, doc = self.bill_target(context, entry, mapping, bill)
					except UnresolvedBillReference:
						if not context.options.get("financial_only"):
							raise
						context.options.setdefault("bill_reference_review", []).append(
							{"tally_id": record["master_id"], "ledger": entry["ledger"], "bill": dict(bill)}
						)
						continue
					amount = -money(scalar(bill, "amount"))
					if context.options.get("financial_only"):
						settlement = money(doc.outstanding_amount) * (-1 if doctype == "Sales Invoice" else 1)
						if amount * settlement <= 0 or abs(amount) > abs(settlement):
							allocated = (
								min(abs(amount), abs(settlement)) * (1 if amount > 0 else -1)
								if amount * settlement > 0
								else Decimal(0)
							)
							context.options.setdefault("bill_reference_review", []).append(
								{
									"tally_id": record["master_id"],
									"ledger": entry["ledger"],
									"bill": dict(bill),
									"erp_allocated_gl_amount": str(allocated),
									"reason": "Source bill redistribution retained in unallocated journal lines",
								}
							)
							amount = allocated
							if not amount:
								continue
					allocations.append((amount, {"reference_type": doctype, "reference_name": doc.name}))
					remaining -= amount
			if remaining:
				allocations.append((remaining, {}))
			for amount, reference in allocations:
				row = {
					"account": mapping["account"],
					"debit_in_account_currency": float(max(amount, 0)),
					"credit_in_account_currency": float(max(-amount, 0)),
					"cost_center": frappe.get_cached_value("Company", context.company, "cost_center"),
					**reference,
				}
				if mapping.get("party"):
					row.update(party_type=mapping["party_type"], party=mapping["party"])
				lines.append(row)
		return self._save_document(
			context,
			record,
			{
				"doctype": "Journal Entry",
				"company": context.company,
				"posting_date": record["date"],
				"voucher_type": voucher_type,
				"cheque_no": record.get("reference")
				or record.get("voucher_number")
				or record.get("master_id")
				if voucher_type == "Bank Entry"
				else None,
				"cheque_date": record.get("reference_date") or record["date"]
				if voucher_type == "Bank Entry"
				else None,
				"user_remark": self.remark(record),
				"accounts": lines,
			},
		)

	def payment(self, context, record, kind):
		parties = []
		banks = []
		others = []
		for row in record["ledger_entries"]:
			mapping = self.resolve(context, row["ledger"])
			if mapping.get("party"):
				parties.append((row, mapping))
			elif mapping.get("account_type") in {"Bank", "Cash"}:
				banks.append((row, mapping))
			else:
				others.append((row, mapping))
		if len(parties) != 1 or len(banks) != 1:
			# ERPNext Payment Entry requires one party and one cash/bank account.
			# Direct expense and multi-party payments belong in Bank/Cash Entry journals.
			return self.journal(
				context,
				record,
				"Cash Entry" if banks and banks[0][1].get("account_type") == "Cash" else "Bank Entry",
			)
		party_row, party = parties[0]
		bank_row, bank = banks[0]
		pay = money(bank_row["amount"]) > 0
		party_amount, bank_amount = abs(money(party_row["amount"])), abs(money(bank_row["amount"]))
		try:
			references = self.bill_references(
				context, party_row, party, pay=pay, source_id=record["master_id"]
			)
		except UnresolvedBillReference:
			if not context.options.get("financial_only"):
				raise
			return self.journal(
				context, record, "Cash Entry" if bank.get("account_type") == "Cash" else "Bank Entry"
			)
		if abs(sum((money(row["allocated_amount"]) for row in references), Decimal(0))) > party_amount:
			return self.journal(
				context, record, "Cash Entry" if bank.get("account_type") == "Cash" else "Bank Entry"
			)
		values = {
			"doctype": "Payment Entry",
			"company": context.company,
			"posting_date": record["date"],
			"payment_type": "Pay" if pay else "Receive",
			"party_type": party["party_type"],
			"party": party["party"],
			"paid_from": bank["account"] if pay else party["account"],
			"paid_to": party["account"] if pay else bank["account"],
			"paid_amount": float(bank_amount if pay else party_amount),
			"received_amount": float(party_amount if pay else bank_amount),
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
			"reference_no": record.get("reference") or record["voucher_number"],
			"reference_date": record.get("reference_date") or record["date"],
			"remarks": self.remark(record),
			"references": references,
			"deductions": [
				{
					"account": m["account"],
					"amount": float(-money(r["amount"])),
					"cost_center": frappe.get_cached_value("Company", context.company, "cost_center"),
				}
				for r, m in others
			],
		}
		return self._save_document(context, record, values)

	def bill_target(self, context, entry, mapping, bill):
		bill_name = scalar(bill, "name")
		refs = (context.options.get("bill_sources") or {}).get(entry["ledger"], {})
		keys = [
			"id:" + scalar(bill, "bill_id"),
			bill_name,
			dated_bill_key(bill_name, scalar(bill, "bill_date")),
		]
		source = next((refs[key] for key in keys if key in refs), None)
		if not source:
			raise UnresolvedBillReference(f"No unique source invoice for bill {bill_name}")
		previous = self.sync_log.previous_target(context, source)
		doctype = "Sales Invoice" if mapping["party_type"] == "Customer" else "Purchase Invoice"
		if not previous or previous.get("source_doctype") != doctype:
			raise UnresolvedBillReference(f"Bill {bill_name} has no imported {doctype}")
		doc = frappe.get_doc(doctype, previous["target_reference"])
		party_field = "customer" if doctype == "Sales Invoice" else "supplier"
		if doc.docstatus != 1 or doc.company != context.company or doc.get(party_field) != mapping["party"]:
			raise ValueError(f"Bill {bill_name} resolves to an invalid invoice")
		return doctype, doc

	def bill_references(self, context, entry, mapping, *, pay=False, source_id=""):
		allocations = defaultdict(Decimal)
		documents = {}
		for bill in entry.get("bill_allocations") or []:
			if scalar(bill, "bill_type").casefold() != "agst ref":
				continue
			doctype, doc = self.bill_target(context, entry, mapping, bill)
			key = (doctype, doc.name)
			allocations[key] += money(scalar(bill, "amount")) * (-1 if pay else 1)
			documents[key] = doc
		references = []
		for (doctype, name), amount in allocations.items():
			outstanding = money(documents[(doctype, name)].outstanding_amount)
			if not amount or amount * outstanding <= 0 or abs(amount) > abs(outstanding):
				if not context.options.get("financial_only"):
					raise ValueError(
						f"Bill {name} allocation exceeds or conflicts with the invoice outstanding amount"
					)
				allocated = (
					(min(abs(amount), abs(outstanding)) * (1 if amount > 0 else -1))
					if amount * outstanding > 0
					else Decimal(0)
				)
				context.options.setdefault("bill_reference_review", []).append(
					{
						"tally_id": source_id,
						"ledger": entry["ledger"],
						"erp_invoice": name,
						"source_requested_allocation": str(amount),
						"erp_allocated_amount": str(allocated),
						"reason": "Excess or conflicting source bill allocation retained as an unallocated party payment",
					}
				)
				amount = allocated
				if not amount:
					continue
			references.append(
				{"reference_doctype": doctype, "reference_name": name, "allocated_amount": float(amount)}
			)
		return references

	def _cancel_previous(self, record):
		doctype, name = record.get("_previous_doctype"), record.get("_previous_name")
		if not doctype or not name or not frappe.db.exists(doctype, name):
			return _success_result(
				record, status="Skipped", message="Cancelled Tally record was not imported"
			)
		if doctype not in {"Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"}:
			raise ValueError("A restored stock document cannot be cancelled by the financial migration")
		doc = frappe.get_doc(doctype, name)
		doc.flags.ignore_permissions = True
		if doc.docstatus == 1:
			doc._cancel()
			if doc.docstatus != 2:
				raise ValueError("Cancellation did not complete synchronously")
		elif doc.docstatus == 0:
			doc.delete(ignore_permissions=True)
		return _success_result(record, doctype, name, message="Cancelled native transaction")

	def invoice(self, context, record, *, sales, is_return):
		party = self.resolve(context, record.get("party_ledger"))
		party_type = "Customer" if sales else "Supplier"
		if party.get("party_type") != party_type:
			raise ValueError(f"{record['voucher_type']} requires a mapped {party_type}")
		if any(
			self.resolve(context, entry["ledger"]).get("party") and entry["ledger"] != record["party_ledger"]
			for entry in record["ledger_entries"]
		):
			return self.journal(context, record)
		items = []
		allocated = set()
		inventory = record.get("inventory_entries") or []
		for entry in inventory:
			if len(entry.get("accounting_allocations") or []) > 1:
				return self.journal(context, record)
			item = (context.options.get("item_mappings") or {}).get(entry["item"])
			if not item:
				raise ValueError(f"Item not mapped: {entry['item']}")
			from express_tally.integrations.tally_inbound_flows import _number

			qty = abs(_number(entry.get("billed_qty") or entry.get("actual_qty")))
			if not qty and not context.options.get("financial_only"):
				raise ValueError("Invoice inventory quantity is zero")
			account_name = entry.get("account")
			if not account_name:
				choices = [
					r["ledger"]
					for r in record["ledger_entries"]
					if self.resolve(context, r["ledger"]).get("root_type")
					== ("Income" if sales else "Expense")
					and self.resolve(context, r["ledger"]).get("account_type") not in {"Tax", "Round Off"}
				]
				if len(choices) != 1:
					raise ValueError("Inventory item needs an unambiguous accounting allocation")
				account_name = choices[0]
			allocated.add(account_name)
			account = self.resolve(context, account_name)["account"]
			items.append(
				{
					"item_code": context.options["accounting_item"]
					if context.options.get("financial_only") or not sales
					else item,
					"description": entry["item"]
					+ (
						f" | Tally quantity: {entry.get('billed_qty') or entry.get('actual_qty')} | Rate: {entry.get('rate')}"
						if context.options.get("financial_only")
						else ""
					),
					"uom": "Nos"
					if context.options.get("financial_only")
					else frappe.get_cached_value("Item", item, "stock_uom"),
					"conversion_factor": 1,
					"qty": (-1 if is_return else 1)
					if context.options.get("financial_only")
					else (-qty if is_return else qty),
					"rate": float(abs(money(entry["amount"])))
					if context.options.get("financial_only")
					else float(abs(money(entry["amount"])) / Decimal(str(qty))),
					"income_account" if sales else "expense_account": account,
					"cost_center": frappe.get_cached_value("Company", context.company, "cost_center"),
				}
			)
		if not inventory:
			for entry in record["ledger_entries"]:
				mapping = self.resolve(context, entry["ledger"])
				if mapping.get("party") or mapping.get("account_type") in {"Tax", "Round Off"}:
					continue
				if mapping.get("root_type") != ("Income" if sales else "Expense"):
					continue
				allocated.add(entry["ledger"])
				items.append(
					{
						"item_code": context.options["accounting_item"],
						"description": entry["ledger"],
						"qty": -1 if is_return else 1,
						"rate": float(abs(money(entry["amount"]))),
						"income_account" if sales else "expense_account": mapping["account"],
						"cost_center": frappe.get_cached_value("Company", context.company, "cost_center"),
					}
				)
		if not items:
			return self.journal(context, record)
		taxes = []
		for entry in record["ledger_entries"]:
			if entry["ledger"] in allocated or entry["ledger"] == record["party_ledger"]:
				continue
			mapping = self.resolve(context, entry["ledger"])
			amount = money(entry["amount"]) * (1 if sales else -1)
			if not amount:
				continue
			row = {
				"charge_type": "Actual",
				"account_head": mapping["account"],
				"description": entry["ledger"],
				"tax_amount": float(amount),
				"cost_center": frappe.get_cached_value("Company", context.company, "cost_center"),
			}
			if not sales:
				row.update(category="Total", add_deduct_tax="Add")
			taxes.append(row)
		values = {
			"doctype": "Sales Invoice" if sales else "Purchase Invoice",
			"company": context.company,
			"customer" if sales else "supplier": party["party"],
			"debit_to" if sales else "credit_to": party["account"],
			"posting_date": record["date"],
			"set_posting_time": 1,
			"due_date": record["date"],
			"is_return": int(is_return),
			"update_stock": 0,
			"disable_rounded_total": 1,
			"ignore_pricing_rule": 1,
			"items": items,
			"taxes": taxes,
			"remarks": self.remark(record),
		}
		against = [
			(entry, bill)
			for entry in record["ledger_entries"]
			for bill in entry.get("bill_allocations") or []
			if scalar(bill, "bill_type").casefold() == "agst ref"
		]
		if against:
			if not is_return or len(against) != 1 or against[0][0]["ledger"] != record["party_ledger"]:
				raise ValueError("Invoice adjustment must have one unambiguous return reference")
			doctype, original = self.bill_target(context, against[0][0], party, against[0][1])
			if doctype != values["doctype"] or money(against[0][1]["amount"]) != sum(
				money(entry["amount"])
				for entry in record["ledger_entries"]
				if entry["ledger"] == record["party_ledger"]
			):
				raise ValueError("Return allocation must cover the exact source party amount")
			values["return_against"] = original.name
		if not sales:
			values.update(
				bill_no=record.get("reference") or record["voucher_number"],
				bill_date=record.get("reference_date") or record["date"],
			)
		return self._save_document(context, record, values)

	def stock_document(self, context, record, *, outgoing):
		match = (context.options.get("stock_matches") or {}).get(str(record["master_id"]))
		if not match:
			raise ValueError(
				"No verified stock-document match; importing this note could duplicate inventory"
			)
		doctype, name = match["doctype"], match["name"]
		if doctype not in (
			{"Delivery Note", "Stock Entry"} if outgoing else {"Purchase Receipt", "Stock Entry"}
		):
			raise ValueError("Stock match has the wrong document direction")
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus != 1 or doc.company != context.company or str(doc.posting_date) != record["date"]:
			raise ValueError("Stock match must be a submitted document with the same company and date")
		from express_tally.integrations.tally_inbound_flows import _number

		expected = defaultdict(Decimal)
		for entry in record.get("inventory_entries") or []:
			item = (context.options.get("item_mappings") or {}).get(entry["item"])
			if not item:
				raise ValueError(f"No stock item mapping for {entry['item']}")
			qty = Decimal(str(_number(entry.get("actual_qty") or entry.get("billed_qty"))))
			expected[item] += abs(qty) * (-1 if outgoing else 1)
		actual = defaultdict(Decimal)
		for row in frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": doctype, "voucher_no": name, "is_cancelled": 0},
			fields=["item_code", "actual_qty"],
		):
			actual[row.item_code] += Decimal(str(row.actual_qty))
		if not expected or dict(actual) != dict(expected):
			raise ValueError("Stock match does not have the exact source item quantities and direction")
		if doctype != "Stock Entry":
			party_type = "Customer" if outgoing else "Supplier"
			party = self.resolve(context, record["party_ledger"])
			allowed = {party.get("party"), *(party.get("stock_party_aliases") or [])}
			if party.get("party_type") != party_type or doc.get(party_type.lower()) not in allowed:
				raise ValueError("Stock match does not belong to the source party")
		return _success_result(
			record, doctype, name, message="Verified and reused restored stock transaction"
		)

	@staticmethod
	def remark(record):
		bills = [
			f"{entry['ledger']}: {scalar(bill, 'bill_type')} {scalar(bill, 'name')} {scalar(bill, 'amount')}"
			for entry in record.get("ledger_entries") or []
			for bill in entry.get("bill_allocations") or []
		]
		return (
			f"Tally {record['voucher_type']} {record['voucher_number']} (ID {record['master_id']})\n{record.get('narration') or ''}"
			+ ("\nSource bill allocations:\n" + "\n".join(bills) if bills else "")
		)
