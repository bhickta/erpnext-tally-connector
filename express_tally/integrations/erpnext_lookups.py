"""ERPNext lookups used while applying Tally records."""

import re

import frappe


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
	# Duplicate item names are stored as "Display Name (ITEM-CODE)".
	match = re.search(r"\(([^()]+)\)\s*$", value)
	if match and frappe.db.exists("Item", match.group(1)):
		return match.group(1)
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
				"email_ids": (
					[{"email_id": record["email"], "is_primary": 1}] if record.get("email") else []
				),
				"phone_nos": [
					{"phone": phone, "is_primary_mobile_no": int(index == 0)}
					for index, phone in enumerate(filter(None, (record.get("mobile"), record.get("phone"))))
				],
				"links": [{"link_doctype": party_type, "link_name": party}],
			}
		)
		contact.insert(ignore_permissions=True)
