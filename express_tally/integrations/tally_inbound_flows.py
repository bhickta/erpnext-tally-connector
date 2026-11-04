"""Standard TDL-free Tally master and voucher imports for the Control Centre."""

from typing import ClassVar

import frappe

from express_tally.bridge.common import SUCCESS_STATUSES, parse_number

from .erpnext_lookups import (
	_account_name,
	_create_party_contact_and_address,
	_fallback_account,
	_group_name,
	_gst_category,
	_item_code,
	_party_account,
	_party_name,
	_valid_values,
	_warehouse_name,
)
from .inbound_base import ALLOWED_ROLES, InboundLog, LoggedInboundFlow, _success_result


def _number(value):
	return parse_number(value)


class TallyMastersToERPNextFlow(LoggedInboundFlow):
	key = "express_tally.standard_masters_from_tally"
	title = "Standard masters from Tally"
	agent_profile = "tally_masters_v1"

	def apply_record(self, context, record):
		kind = record.get("kind")
		handler = {
			"uom": self._uom,
			"customer_group": self._customer_group,
			"supplier_group": self._supplier_group,
			"item_group": self._item_group,
			"warehouse": self._warehouse,
			"account_group": self._account,
			"account": self._account,
			"customer": self._customer,
			"supplier": self._supplier,
			"item": self._item,
		}.get(kind)
		if not handler:
			return _success_result(record, status="Skipped", message=f"Unsupported master kind: {kind}")
		return handler(context, record)

	def _upsert(self, record, doctype, filters, values, set_name=None):
		name = frappe.db.get_value(doctype, filters, "name")
		operation = "updated" if name else "created"
		if name:
			doc = frappe.get_doc(doctype, name)
			for fieldname, value in _valid_values(doctype, values).items():
				doc.set(fieldname, value)
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc({"doctype": doctype, **_valid_values(doctype, values)})
			doc.insert(ignore_permissions=True, set_name=set_name)
		return _success_result(record, doctype, doc.name, message=f"{doctype} {operation}")

	def _uom(self, context, record):
		return self._upsert(
			record,
			"UOM",
			{"uom_name": record["name"]},
			{"uom_name": record["name"], "must_be_whole_number": int(record["must_be_whole_number"])},
		)

	def _customer_group(self, context, record):
		parent = _group_name("Customer Group", "customer_group_name", record.get("parent"))
		return self._upsert(
			record,
			"Customer Group",
			{"customer_group_name": record["name"]},
			{
				"customer_group_name": record["name"],
				"parent_customer_group": parent or "All Customer Groups",
				"is_group": int(record.get("is_group", True)),
			},
		)

	def _supplier_group(self, context, record):
		parent = _group_name("Supplier Group", "supplier_group_name", record.get("parent"))
		return self._upsert(
			record,
			"Supplier Group",
			{"supplier_group_name": record["name"]},
			{
				"supplier_group_name": record["name"],
				"parent_supplier_group": parent or "All Supplier Groups",
				"is_group": int(record.get("is_group", True)),
			},
		)

	def _item_group(self, context, record):
		parent = _group_name("Item Group", "item_group_name", record.get("parent"))
		return self._upsert(
			record,
			"Item Group",
			{"item_group_name": record["name"]},
			{
				"item_group_name": record["name"],
				"parent_item_group": parent or "All Item Groups",
				"is_group": int(record.get("is_group", True)),
			},
		)

	def _warehouse(self, context, record):
		company_abbr = frappe.get_cached_value("Company", context.company, "abbr")
		parent = frappe.db.get_value(
			"Warehouse",
			{"warehouse_name": record.get("parent"), "company": context.company},
			"name",
		)
		return self._upsert(
			record,
			"Warehouse",
			{"warehouse_name": record["name"], "company": context.company},
			{
				"warehouse_name": record["name"],
				"company": context.company,
				"parent_warehouse": parent or f"All Warehouses - {company_abbr}",
				"is_group": int(record.get("is_group", False)),
			},
		)

	def _account(self, context, record):
		existing = _account_name(context.company, record["name"])
		if existing:
			return _success_result(
				record, "Account", existing, status="Skipped", message="Account already exists"
			)
		parent = _account_name(context.company, record.get("parent")) or _fallback_account(
			context.company, record.get("primary_group")
		)
		if not parent:
			if str(record.get("parent") or "").casefold() in {"", "primary"}:
				return _success_result(
					record,
					status="Skipped",
					message="Tally primary account groups are represented by the ERPNext chart roots",
				)
			return _success_result(
				record,
				status="Failed",
				message=f"No ERPNext parent account matches Tally group {record.get('parent')}",
			)
		values = {
			"account_name": record["name"],
			"company": context.company,
			"parent_account": parent,
			"is_group": int(record.get("kind") == "account_group"),
		}
		return self._upsert(
			record, "Account", {"account_name": record["name"], "company": context.company}, values
		)

	def _customer(self, context, record):
		group = _group_name("Customer Group", "customer_group_name", record.get("parent"))
		values = {
			"customer_name": record["name"],
			"customer_type": "Company",
			"customer_group": group or "All Customer Groups",
			"territory": "All Territories",
			"gst_category": _gst_category(record.get("gst_category")),
			"tax_id": record.get("pan"),
		}
		result = self._upsert(record, "Customer", {"customer_name": record["name"]}, values)
		_create_party_contact_and_address("Customer", result["name"], record)
		return result

	def _supplier(self, context, record):
		group = _group_name("Supplier Group", "supplier_group_name", record.get("parent"))
		values = {
			"supplier_name": record["name"],
			"supplier_type": "Company",
			"supplier_group": group or "All Supplier Groups",
			"country": record.get("country") or "India",
			"tax_id": record.get("pan"),
		}
		result = self._upsert(record, "Supplier", {"supplier_name": record["name"]}, values)
		_create_party_contact_and_address("Supplier", result["name"], record)
		return result

	def _item(self, context, record):
		item_code = record.get("item_code") or record["name"]
		group = _group_name("Item Group", "item_group_name", record.get("parent"))
		return self._upsert(
			record,
			"Item",
			{"item_code": item_code},
			{
				"item_code": item_code,
				"item_name": record["name"],
				"description": record.get("description") or record["name"],
				"item_group": group or "All Item Groups",
				"stock_uom": record.get("stock_uom") or "Nos",
				"is_stock_item": 1,
				"include_item_in_manufacturing": 1,
				"gst_hsn_code": record.get("hsn_code"),
			},
			set_name=item_code,
		)


class TallyVouchersToERPNextFlow(LoggedInboundFlow):
	key = "express_tally.standard_vouchers_from_tally"
	title = "Standard vouchers from Tally"
	agent_profile = "tally_vouchers_v1"
	exclusive_group = "tally_accounting_inbound"
	default_options: ClassVar[dict] = {"excluded_origins": ["express_tally.payment_entries_to_tally"]}

	def apply_record(self, context, record):
		if any(
			f"[ExpressTallyOrigin:{origin}]" in str(record.get("narration") or "")
			for origin in context.options.get("excluded_origins") or []
		):
			return _success_result(record, status="Skipped", message="Voucher originated from ERPNext")
		voucher_type = str(record.get("voucher_type") or "").casefold()
		if "sales" in voucher_type or "credit note" in voucher_type:
			return self._invoice(context, record, sales=True, is_return="credit note" in voucher_type)
		if "purchase" in voucher_type or "debit note" in voucher_type:
			return self._invoice(context, record, sales=False, is_return="debit note" in voucher_type)
		if voucher_type in {"payment", "receipt"}:
			return self._payment(context, record, voucher_type)
		if voucher_type in {"journal", "contra"}:
			return self._journal(context, record, voucher_type)
		if "stock journal" in voucher_type:
			return self._stock_entry(context, record)
		return _success_result(
			record, status="Skipped", message=f"Unsupported voucher type: {record.get('voucher_type')}"
		)

	def _save_document(self, context, record, values):
		doctype = values["doctype"]
		previous_name = record.get("_previous_name")
		if (
			previous_name
			and record.get("_previous_doctype") == doctype
			and frappe.db.exists(doctype, previous_name)
		):
			doc = frappe.get_doc(doctype, previous_name)
			if doc.docstatus != 0:
				raise ValueError(f"Cannot alter submitted ERPNext {doctype} {doc.name}; cancel it first")
			doc.update({key: value for key, value in values.items() if key != "doctype"})
			doc.save(ignore_permissions=True)
			operation = "updated"
		else:
			doc = frappe.get_doc(values)
			doc.insert(ignore_permissions=True)
			operation = "created"
		if context.options.get("submit_documents") and doc.docstatus == 0:
			doc.submit()
		return _success_result(record, doctype, doc.name, message=f"{doctype} {operation}")

	def _invoice(self, context, record, *, sales, is_return):
		doctype = "Sales Invoice" if sales else "Purchase Invoice"
		party_type = "Customer" if sales else "Supplier"
		party_field = "customer" if sales else "supplier"
		party = _party_name(party_type, record.get("party_ledger"))
		if not party:
			raise ValueError(f"{party_type} not found for Tally ledger {record.get('party_ledger')}")
		items = []
		for entry in record.get("inventory_entries") or []:
			item_code = _item_code(entry.get("item"))
			if not item_code:
				raise ValueError(f"Item not found for Tally stock item {entry.get('item')}")
			qty = abs(_number(entry.get("billed_qty") or entry.get("actual_qty"))) or 1
			if is_return:
				qty *= -1
			rate = abs(_number(entry.get("rate")))
			if not rate and qty:
				rate = abs(_number(entry.get("amount")) / qty)
			row = {"item_code": item_code, "qty": qty, "rate": rate}
			warehouse = _warehouse_name(context.company, entry.get("godown"))
			if warehouse:
				row["warehouse"] = warehouse
			account = _account_name(context.company, entry.get("account"))
			if account:
				row["income_account" if sales else "expense_account"] = account
			items.append(row)
		if not items:
			raise ValueError(f"Tally {record.get('voucher_number')} has no inventory lines")
		allocated_ledgers = {entry.get("account") for entry in record.get("inventory_entries") or []}
		taxes = []
		for entry in record.get("ledger_entries") or []:
			ledger = entry.get("ledger")
			if ledger == record.get("party_ledger") or ledger in allocated_ledgers:
				continue
			account = _account_name(context.company, ledger)
			if account and _number(entry.get("amount")):
				taxes.append(
					{
						"charge_type": "Actual",
						"account_head": account,
						"description": ledger,
						"tax_amount": _number(entry.get("amount")),
					}
				)
		values = {
			"doctype": doctype,
			"company": context.company,
			party_field: party,
			"posting_date": record.get("date"),
			"due_date": record.get("date"),
			"remarks": record.get("narration"),
			"is_return": int(is_return),
			"update_stock": int(bool(context.options.get("update_stock"))),
			"items": items,
			"taxes": taxes,
		}
		if not sales:
			values.update(
				{
					"bill_no": record.get("reference") or record.get("voucher_number"),
					"bill_date": record.get("reference_date") or record.get("date"),
				}
			)
		return self._save_document(context, record, values)

	def _journal(self, context, record, voucher_type):
		accounts = []
		for entry in record.get("ledger_entries") or []:
			ledger = entry.get("ledger")
			party_type = ""
			party = _party_name("Customer", ledger)
			if party:
				party_type = "Customer"
			else:
				party = _party_name("Supplier", ledger)
				if party:
					party_type = "Supplier"
			account = (
				_party_account(context.company, party_type, party)
				if party_type
				else _account_name(context.company, ledger)
			)
			if not account:
				raise ValueError(f"Account not found for Tally ledger {ledger}")
			amount = abs(_number(entry.get("amount")))
			is_debit = str(entry.get("is_deemed_positive") or "").casefold() == "yes"
			if not entry.get("is_deemed_positive"):
				is_debit = _number(entry.get("amount")) < 0
			row = {
				"account": account,
				"debit_in_account_currency": amount if is_debit else 0,
				"credit_in_account_currency": 0 if is_debit else amount,
			}
			if party_type:
				row.update({"party_type": party_type, "party": party})
			accounts.append(row)
		if len(accounts) < 2:
			raise ValueError("A Journal Entry needs at least two mapped ledger lines")
		return self._save_document(
			context,
			record,
			{
				"doctype": "Journal Entry",
				"voucher_type": "Contra Entry" if voucher_type == "contra" else "Journal Entry",
				"company": context.company,
				"posting_date": record.get("date"),
				"user_remark": record.get("narration"),
				"accounts": accounts,
			},
		)

	def _payment(self, context, record, voucher_type):
		payment_type = "Pay" if voucher_type == "payment" else "Receive"
		entries = record.get("ledger_entries") or []
		preferred = "Supplier" if payment_type == "Pay" else "Customer"
		parties = []
		banks = []
		other_entries = []
		for entry in entries:
			ledger = entry.get("ledger")
			account = (context.options.get("ledger_accounts") or {}).get(ledger) or _account_name(
				context.company, ledger
			)
			if account and frappe.db.get_value("Account", account, "company") != context.company:
				raise ValueError(f"Payment account {account} does not belong to {context.company}")
			account_type = frappe.db.get_value("Account", account, "account_type") if account else ""
			if account_type in {"Bank", "Cash"}:
				banks.append((entry, account))
				continue
			for party_type in (preferred, "Customer" if preferred == "Supplier" else "Supplier"):
				party = _party_name(party_type, ledger)
				if party:
					parties.append((entry, party_type, party))
					break
			else:
				if not account:
					raise ValueError(f"Account not found for Tally ledger {ledger}")
				other_entries.append((entry, account))
		if len(parties) != 1 or len(banks) != 1:
			raise ValueError("Payment needs exactly one mapped party ledger and one bank/cash ledger")
		party_entry, party_type, party = parties[0]
		bank_entry, bank_account = banks[0]
		party_account = _party_account(context.company, party_type, party)
		if not bank_account or not party_account:
			raise ValueError("Could not resolve the party and bank/cash accounts for this payment")
		company_currency = frappe.db.get_value("Company", context.company, "default_currency")
		for account in (bank_account, party_account):
			currency = frappe.db.get_value("Account", account, "account_currency") or company_currency
			if currency != company_currency:
				raise ValueError("Inbound payments require bank and party accounts in the company currency")
		party_amount = _number(party_entry.get("amount"))
		bank_amount = _number(bank_entry.get("amount"))
		if abs(sum(_number(e.get("amount")) for e in entries)) > 0.005:
			raise ValueError("Tally payment ledger entries do not balance")
		if (payment_type == "Pay" and (party_amount >= 0 or bank_amount <= 0)) or (
			payment_type == "Receive" and (party_amount <= 0 or bank_amount >= 0)
		):
			raise ValueError("Tally payment ledger signs do not match its voucher type")
		values = {
			"doctype": "Payment Entry",
			"payment_type": payment_type,
			"company": context.company,
			"posting_date": record.get("date"),
			"party_type": party_type,
			"party": party,
			"paid_from": bank_account if payment_type == "Pay" else party_account,
			"paid_to": party_account if payment_type == "Pay" else bank_account,
			"paid_amount": abs(bank_amount if payment_type == "Pay" else party_amount),
			"received_amount": abs(party_amount if payment_type == "Pay" else bank_amount),
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
			"deductions": [
				{
					"account": account,
					"amount": -_number(entry.get("amount")),
					"cost_center": frappe.db.get_value("Company", context.company, "cost_center"),
				}
				for entry, account in other_entries
			],
			"reference_no": record.get("reference") or record.get("voucher_number"),
			"reference_date": record.get("reference_date") or record.get("date"),
			"remarks": record.get("narration"),
		}
		if frappe.get_meta("Payment Entry").has_field("branch"):
			values["branch"] = context.options.get("branch")
			if frappe.get_meta("Payment Entry").get_field("branch").reqd and not values["branch"]:
				raise ValueError("Configure the inbound payment flow's branch option")
		references = []
		invoice_doctype = "Sales Invoice" if party_type == "Customer" else "Purchase Invoice"
		for bill in party_entry.get("bill_allocations") or []:
			bill_type = bill.get("billtype") or bill.get("bill_type") or ""
			if str(bill_type).casefold() not in {"agst ref", "against ref"}:
				continue
			bill_name = bill.get("name") or ""
			invoice = (context.options.get("invoice_references") or {}).get(bill_name) or bill_name
			if not invoice or not frappe.db.exists(
				invoice_doctype, {"name": invoice, "company": context.company, "docstatus": 1}
			):
				raise ValueError(f"Map Tally bill reference {bill_name} to a submitted {invoice_doctype}")
			references.append(
				{
					"reference_doctype": invoice_doctype,
					"reference_name": invoice,
					"allocated_amount": abs(_number(bill.get("amount"))),
				}
			)
		if references:
			values["references"] = references
		return self._save_document(context, record, values)

	def _stock_entry(self, context, record):
		items_by_code = {}
		for entry in record.get("inventory_entries") or []:
			item_code = _item_code(entry.get("item"))
			warehouse = _warehouse_name(context.company, entry.get("godown"))
			qty = _number(entry.get("actual_qty") or entry.get("billed_qty"))
			if not item_code or not warehouse or not qty:
				continue
			row = items_by_code.setdefault(item_code, {"item_code": item_code, "qty": abs(qty)})
			if qty < 0:
				row["s_warehouse"] = warehouse
			else:
				row["t_warehouse"] = warehouse
		items = [row for row in items_by_code.values() if row.get("s_warehouse") and row.get("t_warehouse")]
		if not items:
			raise ValueError("No balanced source/target stock lines with mapped warehouses were found")
		return self._save_document(
			context,
			record,
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Transfer",
				"company": context.company,
				"posting_date": record.get("date"),
				"remarks": record.get("narration"),
				"items": items,
			},
		)


class TallyPaymentsToERPNextFlow(TallyVouchersToERPNextFlow):
	key = "express_tally.payments_from_tally"
	title = "Payment Entries from Tally"
	agent_profile = "tally_payment_vouchers_v1"

	def apply_record(self, context, record):
		if str(record.get("voucher_type") or "").casefold() not in {"payment", "receipt"}:
			return _success_result(record, status="Skipped", message="Not a payment or receipt voucher")
		return super().apply_record(context, record)

	def configuration(self, context):
		schema = [{"fieldname": "submit_documents", "label": "Submit imported payments", "type": "checkbox"}]
		if frappe.get_meta("Payment Entry").has_field("branch"):
			schema.append(
				{
					"fieldname": "branch",
					"label": "Branch",
					"type": "select",
					"options": frappe.get_all("Branch", pluck="name", order_by="name"),
				}
			)
		return {"option_schema": schema}
