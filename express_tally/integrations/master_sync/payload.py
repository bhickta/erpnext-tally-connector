"""Dependency-ordered master sync summary and payload builders."""

import json
from datetime import date

import frappe
from frappe import _
from frappe.utils import cint

from express_tally import __version__ as connector_version

from .accounts import _account_messages, _stock_group_message, _unit_message
from .constants import MASTER_SYNC_LABELS, MASTER_SYNC_TYPES
from .items import _fetch_items, _item_messages
from .names import (
	_has_column,
	_is_warehouse_root,
	_item_tally_names,
	_party_tally_names,
	_warehouse_tally_names,
)
from .parties import (
	_fetch_addresses,
	_fetch_contacts,
	_fetch_customer_credit_limits,
	_fetch_parties,
	_godown_message,
	_ledger_message,
)


def _parse_master_types(masters):
	if masters in (None, ""):
		return list(MASTER_SYNC_TYPES)
	if isinstance(masters, str):
		try:
			masters = json.loads(masters)
		except json.JSONDecodeError:
			frappe.throw(_("masters must be a JSON array"))
	if not isinstance(masters, (list, tuple)) or not masters:
		frappe.throw(_("Select at least one master type"))
	normalized = []
	for value in masters:
		value = str(value or "").strip()
		if value not in MASTER_SYNC_TYPES:
			frappe.throw(
				_("Unsupported master type {0}. Choose from {1}.").format(
					frappe.bold(value), ", ".join(MASTER_SYNC_TYPES)
				)
			)
		if value not in normalized:
			normalized.append(value)
	return normalized


def build_master_sync_summary(company):
	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist").format(frappe.bold(company)))
	return {
		"Group": frappe.db.count(
			"Account", {"company": company, "is_group": 1, "disabled": 0}
		),
		"Ledger": frappe.db.count(
			"Account", {"company": company, "is_group": 0, "disabled": 0}
		),
		"Unit": frappe.db.count("UOM"),
		"Item Group": frappe.db.count("Item Group"),
		"Item": frappe.db.count("Item", {"disabled": 0}),
		"Warehouse": frappe.db.count("Warehouse", {"company": company, "disabled": 0}),
		"Customer": frappe.db.count("Customer", {"disabled": 0}),
		"Supplier": frappe.db.count("Supplier", {"disabled": 0}),
		# Kept for the Item count subtitle.
		"UOM": frappe.db.count("UOM"),
	}


def build_master_sync_payload(
	company,
	masters=None,
	include_disabled=False,
	include_opening_balances=False,
	posting_date=None,
	limit=None,
	duplicate_names="skip",
	customer_parent="Sundry Debtors",
	supplier_parent="Sundry Creditors",
):
	"""Return dependency-ordered Tally JSONEx batches for the selected masters."""
	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist").format(frappe.bold(company)))
	customer_parent = str(customer_parent or "").strip()
	supplier_parent = str(supplier_parent or "").strip()
	if not customer_parent or not supplier_parent:
		frappe.throw(_("Customer and supplier parent groups are required"))
	posting_date = str(posting_date or date.today().isoformat())
	try:
		date.fromisoformat(posting_date)
	except ValueError:
		frappe.throw(_("posting_date must use YYYY-MM-DD format"))
	limit = cint(limit) if limit not in (None, "") else 0
	if limit < 0:
		frappe.throw(_("limit must be zero or a positive number"))
	duplicate_names = str(duplicate_names or "skip").strip().lower()
	if duplicate_names not in {"skip", "suffix"}:
		frappe.throw(_("duplicate_names must be skip or suffix"))
	selected = _parse_master_types(masters)
	company_abbr = frappe.get_cached_value("Company", company, "abbr")
	batches = []
	item_messages = []
	skipped = []

	if "Group" in selected or "Ledger" in selected:
		account_fields = [
			"name",
			"account_name",
			"parent_account",
			"is_group",
			"root_type",
			"account_type",
			"account_currency",
			"disabled",
		]
		if _has_column("Account", "account_number"):
			account_fields.append("account_number")
		accounts = frappe.get_all(
			"Account",
			filters={"company": company},
			fields=account_fields,
			order_by="lft",
		)
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
		account_groups, account_ledgers = _account_messages(
			accounts,
			company_abbr,
			include_disabled=include_disabled,
			company_currency=company_currency,
		)
		batches.append(
			{
				"key": "account_groups",
				"label": "Accounting groups",
				"tallymessage": account_groups,
			}
		)
		if "Ledger" in selected:
			batches.append(
				{
					"key": "account_ledgers",
					"label": "Ledgers",
					"tallymessage": account_ledgers,
				}
			)

	if "Unit" in selected:
		# Include every UOM, including disabled ones that an active item may
		# still reference. Tally needs the unit before the stock item.
		uom_rows = frappe.get_all(
			"UOM",
			fields=["name", "uom_name", "must_be_whole_number"],
			order_by="name",
		)
		batches.append(
			{
				"key": "units",
				"label": "Units of measure",
				"tallymessage": [_unit_message(row) for row in uom_rows],
			}
		)

	if "Item Group" in selected:
		item_groups = frappe.get_all(
			"Item Group",
			fields=["name", "item_group_name", "parent_item_group"],
			order_by="lft",
		)
		batches.append(
			{
				"key": "item_groups",
				"label": "Item groups",
				"tallymessage": [
					_stock_group_message(row)
					for row in item_groups
					if (row.get("item_group_name") or row["name"]) != "All Item Groups"
				],
			}
		)

	if "Warehouse" in selected:
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"company": company},
			fields=["name", "warehouse_name", "parent_warehouse", "is_group", "disabled"],
			order_by="lft",
		)
		if not include_disabled:
			warehouses = [row for row in warehouses if not cint(row.get("disabled"))]
		warehouses = [row for row in warehouses if not _is_warehouse_root(row)]
		warehouse_names, warehouse_skipped = _warehouse_tally_names(
			warehouses, company_abbr, duplicate_names
		)
		skipped.extend(warehouse_skipped)
		batches.append(
			{
				"key": "warehouses",
				"label": "Warehouses",
				"tallymessage": [
					_godown_message(
						row, company_abbr, tally_name=warehouse_names.get(row["name"])
					)
					for row in warehouses
					if row["name"] in warehouse_names
				],
			}
		)

	for doctype in ("Customer", "Supplier"):
		if doctype not in selected:
			continue
		parties = _fetch_parties(doctype, include_disabled=include_disabled)
		party_names, party_skipped = _party_tally_names(parties, doctype, duplicate_names)
		skipped.extend(party_skipped)
		parties = [party for party in parties if party["name"] in party_names]
		addresses = _fetch_addresses(parties)
		contacts = _fetch_contacts(parties)
		credit_limits = _fetch_customer_credit_limits(parties, company) if doctype == "Customer" else {}
		messages = []
		for party in parties:
			address_name = party.get("customer_primary_address") or party.get("supplier_primary_address")
			contact_name = party.get("customer_primary_contact") or party.get("supplier_primary_contact")
			messages.append(
				_ledger_message(
					party,
					doctype,
					company,
					addresses.get(address_name),
					contacts.get(contact_name),
					credit_limits.get(party["name"], 0),
					posting_date=posting_date,
					tally_name=party_names[party["name"]],
					parent=customer_parent if doctype == "Customer" else supplier_parent,
				)
			)
		batches.append(
			{
				"key": doctype.lower() + "s",
				"label": MASTER_SYNC_LABELS[doctype],
				"tallymessage": messages,
			}
		)

	if "Item" in selected:
		items = _fetch_items(include_disabled=include_disabled)
		item_messages = _item_messages(
			items,
			company_abbr,
			_item_tally_names(items),
			include_opening_balances=include_opening_balances,
			posting_date=posting_date,
		)
		batches.append(
			{
				"key": "items",
				"label": "Items",
				"tallymessage": item_messages,
			}
		)

	if limit:
		for batch in batches:
			# Keep dependency batches complete so a limited test still has the
			# units/groups referenced by the selected masters.
			if batch["key"] == "account_groups" and "Ledger" in selected:
				continue
			if batch["key"] in {"units", "item_groups"} and "Item" in selected:
				continue
			if batch["key"] in {
				"account_groups",
				"account_ledgers",
				"units",
				"item_groups",
				"warehouses",
				"customers",
				"suppliers",
				"items",
			}:
				batch["tallymessage"] = batch["tallymessage"][:limit]

	counts = {key: 0 for key in MASTER_SYNC_TYPES}
	for batch in batches:
		if batch["key"] == "account_groups":
			counts["Group"] = len(batch["tallymessage"])
		elif batch["key"] == "account_ledgers":
			counts["Ledger"] = len(batch["tallymessage"])
		elif batch["key"] == "units":
			counts["Unit"] = len(batch["tallymessage"])
		elif batch["key"] == "item_groups":
			counts["Item Group"] = len(batch["tallymessage"])
		elif batch["key"] == "items":
			counts["Item"] = len(batch["tallymessage"])
		elif batch["key"] == "warehouses":
			counts["Warehouse"] = len(batch["tallymessage"])
		elif batch["key"] == "customers":
			counts["Customer"] = len(batch["tallymessage"])
		elif batch["key"] == "suppliers":
			counts["Supplier"] = len(batch["tallymessage"])

	return {
		"company": company,
		"tally_company": frappe.get_cached_value("Company", company, "company_name"),
		"connector_version": connector_version,
		"masters": selected,
		"include_disabled": bool(include_disabled),
		"limit": limit,
		"duplicate_names": duplicate_names,
		"batches": batches,
		"counts": counts,
		"skipped": skipped,
	}
