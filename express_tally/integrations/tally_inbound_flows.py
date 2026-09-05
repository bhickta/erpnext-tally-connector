"""Standard TDL-free Tally master and voucher imports for the Control Centre."""

import hashlib
import json
import uuid
from collections.abc import Mapping

import frappe
from frappe.utils import now_datetime

from express_tally.framework import InboundFlow


ALLOWED_ROLES = frozenset({"Tally Sync User", "Accounts Manager", "System Manager"})
SUCCESS_STATUSES = {"success", "already exists", "skipped"}


def _number(value):
	import re

	match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or "0").replace(",", ""))
	return float(match.group(0)) if match else 0.0


def _source_hash(record):
	stable_record = {key: value for key, value in record.items() if not key.startswith("_previous_")}
	return hashlib.sha256(
		json.dumps(stable_record, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()


def _success_result(record, doctype="", name="", status="Success", message=""):
	return {
		"source": record.get("_tally_key"),
		"source_type": record.get("_tally_type"),
		"source_alter_id": record.get("_tally_alter_id"),
		"status": status,
		"doctype": doctype,
		"name": name,
		"message": message,
	}


class InboundLog:
	def __init__(self, flow_key):
		self.flow_key = flow_key

	def previous_success(self, context, record):
		return frappe.db.get_value(
			"Tally Sync Log",
			{
				"flow_key": self.flow_key,
				"direction": "Tally to ERPNext",
				"target_id": context.target_id,
				"source_reference": record.get("_tally_key"),
				"source_hash": _source_hash(record),
				"status": "Success",
			},
			["target_reference", "source_doctype"],
			as_dict=True,
		)

	def previous_target(self, context, record):
		return frappe.db.get_value(
			"Tally Sync Log",
			{
				"flow_key": self.flow_key,
				"direction": "Tally to ERPNext",
				"target_id": context.target_id,
				"source_reference": record.get("_tally_key"),
				"status": "Success",
			},
			["target_reference", "source_doctype"],
			order_by="creation desc",
			as_dict=True,
		)

	def record(self, context, record, result, operation="Create"):
		status = "Success" if str(result.get("status", "")).lower() in SUCCESS_STATUSES else "Failed"
		frappe.get_doc(
			{
				"doctype": "Tally Sync Log",
				"flow_key": self.flow_key,
				"direction": "Tally to ERPNext",
				"company": context.company,
				"request_id": str(uuid.uuid4()),
				"status": status,
				"operation": operation,
				"source_system": "Tally",
				"source_type": record.get("_tally_type"),
				"source_reference": record.get("_tally_key"),
				"source_doctype": result.get("doctype"),
				"source_name": result.get("name"),
				"source_hash": _source_hash(record),
				"target_system": "ERPNext",
				"target_id": context.target_id,
				"tally_company": context.tally_company,
				"target_reference": result.get("name"),
				"synced_on": now_datetime(),
				"error": "" if status == "Success" else str(result.get("message") or "")[:4000],
			}
		).insert(ignore_permissions=True)

	def status(self, context):
		counts = dict(
			frappe.db.sql(
				"""
				SELECT status, COUNT(*)
				FROM `tabTally Sync Log`
				WHERE flow_key = %(flow)s
				  AND direction = 'Tally to ERPNext'
				  AND target_id = %(target)s
				GROUP BY status
				""",
				{"flow": self.flow_key, "target": context.target_id},
			)
		)
		return {"successful": counts.get("Success", 0), "failed": counts.get("Failed", 0)}


class LoggedInboundFlow(InboundFlow):
	allowed_roles = ALLOWED_ROLES

	def __init__(self):
		self.sync_log = InboundLog(self.key)

	def receive(self, context, records):
		results = []
		for index, record in enumerate(records):
			if not isinstance(record, Mapping) or not record.get("_tally_key"):
				results.append({"status": "Failed", "message": "Invalid Tally source identity"})
				continue
			previous = self.sync_log.previous_success(context, record)
			if previous:
				results.append(
					_success_result(
						record,
						previous.get("source_doctype") or "",
						previous.get("target_reference") or "",
						status="Skipped",
						message="This Tally version was already imported",
					)
				)
				continue
			previous_target = self.sync_log.previous_target(context, record)
			if previous_target:
				record = dict(record)
				record["_previous_doctype"] = previous_target.get("source_doctype")
				record["_previous_name"] = previous_target.get("target_reference")
			savepoint = f"tally_inbound_{index}"
			frappe.db.savepoint(savepoint)
			try:
				if record.get("cancelled") or record.get("deleted"):
					result = self._cancel_previous(record)
					operation = "Cancel"
				else:
					result = self.apply_record(context, record)
					operation = "Alter" if previous_target else "Create"
			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				result = _success_result(record, status="Failed", message=str(exc))
				operation = "Alter" if previous_target else "Create"
			self.sync_log.record(context, record, result, operation=operation)
			results.append(result)
		return results

	def _cancel_previous(self, record):
		doctype = record.get("_previous_doctype")
		name = record.get("_previous_name")
		if not doctype or not name or not frappe.db.exists(doctype, name):
			return _success_result(record, status="Skipped", message="Cancelled Tally record was not imported")
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus == 1:
			doc.cancel()
		elif doc.docstatus == 0:
			doc.delete(ignore_permissions=True)
		return _success_result(record, doctype, name, message=f"{doctype} cancelled")

	def status(self, context):
		return self.sync_log.status(context)

	def apply_record(self, context, record):
		raise NotImplementedError


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
			return _success_result(record, "Account", existing, status="Skipped", message="Account already exists")
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
		return self._upsert(record, "Account", {"account_name": record["name"], "company": context.company}, values)

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

	def apply_record(self, context, record):
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
		return _success_result(record, status="Skipped", message=f"Unsupported voucher type: {record.get('voucher_type')}")

	def _save_document(self, context, record, values):
		doctype = values["doctype"]
		previous_name = record.get("_previous_name")
		if previous_name and record.get("_previous_doctype") == doctype and frappe.db.exists(doctype, previous_name):
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
			}
		)

	def _payment(self, context, record, voucher_type):
		payment_type = "Pay" if voucher_type == "payment" else "Receive"
		party_type = "Supplier" if payment_type == "Pay" else "Customer"
		party = _party_name(party_type, record.get("party_ledger"))
		if not party:
			party_type = "Customer" if party_type == "Supplier" else "Supplier"
			party = _party_name(party_type, record.get("party_ledger"))
		if not party:
			raise ValueError(f"{party_type} not found for Tally ledger {record.get('party_ledger')}")
		other = next(
			(entry for entry in record.get("ledger_entries") or [] if entry.get("ledger") != record.get("party_ledger")),
			None,
		)
		bank_account = _account_name(context.company, (other or {}).get("ledger"))
		party_account = _party_account(context.company, party_type, party)
		if not bank_account or not party_account:
			raise ValueError("Could not resolve the party and bank/cash accounts for this payment")
		amount = max((abs(_number(entry.get("amount"))) for entry in record.get("ledger_entries") or []), default=0)
		values = {
			"doctype": "Payment Entry",
			"payment_type": payment_type,
			"company": context.company,
			"posting_date": record.get("date"),
			"party_type": party_type,
			"party": party,
			"paid_from": bank_account if payment_type == "Pay" else party_account,
			"paid_to": party_account if payment_type == "Pay" else bank_account,
			"paid_amount": amount,
			"received_amount": amount,
			"reference_no": record.get("reference") or record.get("voucher_number"),
			"reference_date": record.get("reference_date") or record.get("date"),
			"remarks": record.get("narration"),
		}
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
			}
		)


def _valid_values(doctype, values):
	meta = frappe.get_meta(doctype)
	return {
		fieldname: value
		for fieldname, value in values.items()
		if value not in (None, "") and (fieldname == "name" or meta.has_field(fieldname))
	}


def _group_name(doctype, title_field, value):
	if not value:
		return None
	return frappe.db.get_value(doctype, {title_field: value}, "name")


def _account_name(company, ledger):
	if not ledger:
		return None
	if frappe.db.exists("Account", ledger):
		return ledger
	return frappe.db.get_value("Account", {"company": company, "account_name": ledger}, "name")


def _fallback_account(company, primary_group):
	candidates = {
		"capital account": ("Equity", "Capital Account"),
		"current assets": ("Current Assets", "Assets"),
		"current liabilities": ("Current Liabilities", "Liabilities"),
		"direct expenses": ("Direct Expenses", "Expenses"),
		"direct incomes": ("Direct Income", "Income"),
		"fixed assets": ("Fixed Assets", "Assets"),
		"indirect expenses": ("Indirect Expenses", "Expenses"),
		"indirect incomes": ("Indirect Income", "Income"),
		"investments": ("Investments", "Assets"),
		"loans (liability)": ("Loans (Liabilities)", "Liabilities"),
		"purchase accounts": ("Cost of Goods Sold", "Expenses"),
		"sales accounts": ("Income",),
		"sundry debtors": ("Accounts Receivable", "Debtors", "Current Assets"),
		"sundry creditors": ("Accounts Payable", "Creditors", "Current Liabilities"),
	}.get(str(primary_group or "").casefold(), ())
	for candidate in candidates:
		account = _account_name(company, candidate)
		if account:
			return account
	return None


def _party_name(doctype, value):
	if not value:
		return None
	if frappe.db.exists(doctype, value):
		return value
	field = "customer_name" if doctype == "Customer" else "supplier_name"
	return frappe.db.get_value(doctype, {field: value}, "name")


def _party_account(company, party_type, party):
	account = frappe.db.get_value(
		"Party Account",
		{"parenttype": party_type, "parent": party, "company": company},
		"account",
	)
	if account:
		return account
	group_field = "customer_group" if party_type == "Customer" else "supplier_group"
	group = frappe.db.get_value(party_type, party, group_field)
	group_doctype = "Customer Group" if party_type == "Customer" else "Supplier Group"
	account = frappe.db.get_value(
		"Party Account",
		{"parenttype": group_doctype, "parent": group, "company": company},
		"account",
	)
	if account:
		return account
	default_field = "default_receivable_account" if party_type == "Customer" else "default_payable_account"
	return frappe.get_cached_value("Company", company, default_field)


def _item_code(value):
	if not value:
		return None
	if frappe.db.exists("Item", value):
		return value
	return frappe.db.get_value("Item", {"item_name": value}, "name")


def _warehouse_name(company, value):
	if not value:
		return None
	if frappe.db.exists("Warehouse", value):
		return value
	return frappe.db.get_value("Warehouse", {"warehouse_name": value, "company": company}, "name")


def _gst_category(value):
	mapping = {
		"regular": "Registered Regular",
		"composition": "Registered Composition",
		"consumer": "Unregistered",
		"unregistered/consumer": "Unregistered",
	}
	return mapping.get(str(value or "").casefold(), value or "Unregistered")


def _create_party_contact_and_address(party_type, party, record):
	lines = list(record.get("address_lines") or [])
	if lines and not frappe.db.exists(
		"Dynamic Link",
		{"parenttype": "Address", "link_doctype": party_type, "link_name": party},
	):
		address = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": record.get("name"),
				"address_type": "Billing",
				"address_line1": lines[0],
				"address_line2": " ".join(lines[1:]),
				"city": record.get("state") or "Unknown",
				"state": record.get("state"),
				"country": record.get("country") or "India",
				"pincode": record.get("pincode"),
				"gstin": record.get("gstin"),
				"links": [{"link_doctype": party_type, "link_name": party}],
			}
		)
		address.insert(ignore_permissions=True)
	if (record.get("email") or record.get("phone") or record.get("mobile")) and not frappe.db.exists(
		"Dynamic Link",
		{"parenttype": "Contact", "link_doctype": party_type, "link_name": party},
	):
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": record.get("name"),
				"email_ids": ([{"email_id": record["email"], "is_primary": 1}] if record.get("email") else []),
				"phone_nos": [
					{"phone": phone, "is_primary_mobile_no": int(index == 0)}
					for index, phone in enumerate(filter(None, (record.get("mobile"), record.get("phone"))))
				],
				"links": [{"link_doctype": party_type, "link_name": party}],
			}
		)
		contact.insert(ignore_permissions=True)
