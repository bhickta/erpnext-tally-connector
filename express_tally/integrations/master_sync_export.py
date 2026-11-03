"""Exhaustive ERPNext -> TallyPrime master sync payloads.

User-selectable master groups are Item, Warehouse, Customer and Supplier.
Item sync automatically includes its dependencies (UOM and Item Group).

Every pushed Tally master stores the ERPNext identity in the Tally alias list
(``LANGUAGENAME.LIST`` / ``NAME.LIST``):

* Item        - Tally name is the ERPNext item name; alias is the item code.
* Warehouse   - Tally name is the warehouse name; alias is the ERPNext ID.
* Customer    - Tally name is the customer name; alias is the ERPNext ID.
* Supplier    - Tally name is the supplier name; alias is the ERPNext ID.

Duplicate display names are handled explicitly. Items append the item code
when their display name is shared. Parties and warehouses either keep the
first record and report the rest, or append the ERPNext ID to every colliding
record, depending on ``duplicate_names``.

Customers and suppliers additionally carry the ERPNext ID in ``LEDGERCODE``.
The payload is returned in dependency order so the bridge can import it in
separate JSONEx requests: units, groups, warehouses, parties, then items.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import date

import frappe
from frappe import _
from frappe.utils import cint, flt

MASTER_SYNC_TYPES = ("Item", "Warehouse", "Customer", "Supplier")

MASTER_SYNC_LABELS = {
	"Item": "Items",
	"Warehouse": "Warehouses",
	"Customer": "Customers",
	"Supplier": "Suppliers",
}

TALLY_MASTER_TYPES = {
	"Item": "Stock Item",
	"Warehouse": "Godown",
	"Customer": "Ledger",
	"Supplier": "Ledger",
}

PARTY_PARENT = {
	"Customer": "Sundry Debtors",
	"Supplier": "Sundry Creditors",
}

GST_REGISTRATION_TYPE = {
	"Registered Regular": "Regular",
	"Registered Composition": "Composition",
	"Unregistered": "Unregistered/Consumer",
	"Unregistered/Consumer": "Unregistered/Consumer",
	"Consumer": "Consumer",
	"SEZ": "SEZ",
	"Overseas": "Overseas",
	"Deemed Export": "Deemed Export",
	"UIN Holders": "UIN Holders",
	"Tax Deductor": "Tax Deductor",
	"Tax Collector": "Tax Collector",
	"Input Service Distributor": "Input Service Distributor",
	"Input Service Distributor (ISD)": "Input Service Distributor",
	"Non-Resident": "Overseas",
}

TALLY_PRIMARY = "\x04 Primary"
TALLY_NOT_APPLICABLE = "\x04 Not Applicable"


def _clean_name(value, company_abbr=None):
	value = (value or "").strip()
	if company_abbr:
		value = re.sub(rf"\s+-\s+{re.escape(company_abbr)}$", "", value).strip()
	return value


def _text(value):
	value = re.sub(r"<[^>]+>", " ", str(value or ""))
	return re.sub(r"\s+", " ", value).strip()


def _alias_list(name, aliases=()):
	"""Return a Tally LANGUAGENAME.LIST-compatible alias list.

	The first name is the master name. Every following distinct value becomes
	an alias, which is how Tally stores alternate identifiers such as the
	ERPNext document name.
	"""
	values = [{"metadata": True, "type": "String"}, name]
	seen = {name.casefold()}
	for alias in aliases:
		alias = str(alias or "").strip()
		if alias and alias.casefold() not in seen:
			values.append(alias)
			seen.add(alias.casefold())
	return [{"name": values, "language id": " 1033"}]


def _identity_alias(tally_name, erp_name, doctype):
	"""Build aliases that always contain a stable ERPNext identity.

	The raw ERPNext ``name`` is used when it differs from the Tally master
	name. A namespaced alias is always added so Tally records remain mapped
	even when ERPNext names the document with the same display name.
	"""
	aliases = []
	erp_name = str(erp_name or "").strip()
	if erp_name and erp_name.casefold() != str(tally_name or "").casefold():
		aliases.append(erp_name)
	if erp_name:
		aliases.append(f"ERPNext:{doctype}:{erp_name}")
	return aliases


def _item_tally_names(items):
	"""Return unique Tally item names keyed by ERPNext item code.

	The display name is the normal Tally master name because that is how the
	existing Tally data is named. When several ERPNext items share a display
	name, every colliding record is suffixed with its item code.
	"""
	counts = Counter(
		_text(item.get("item_name")) or item.get("item_code") or item["name"] for item in items
	)
	names = {}
	for item in items:
		code = item.get("item_code") or item["name"]
		display = _text(item.get("item_name")) or code
		names[code] = f"{display} ({code})" if counts[display] > 1 else display
	return names


def _duplicate_name_report(master, name, rows):
	return [
		{
			"master": master,
			"name": name,
			"erp_name": row["name"],
			"reason": "Duplicate Tally name; first record kept",
		}
		for row in rows
	]


def _party_tally_names(parties, doctype, policy="skip"):
	groups = defaultdict(list)
	for row in parties:
		name = (row.get("customer_name") if doctype == "Customer" else row.get("supplier_name")) or row["name"]
		groups[name].append(row)
	names = {}
	skipped = []
	for name, rows in groups.items():
		rows = sorted(rows, key=lambda row: row["name"])
		if len(rows) == 1:
			names[rows[0]["name"]] = name
			continue
		if policy == "suffix":
			for row in rows:
				names[row["name"]] = f"{name} ({row['name']})"
		else:
			names[rows[0]["name"]] = name
			skipped.extend(_duplicate_name_report(doctype, name, rows[1:]))
	return names, skipped


def _warehouse_tally_names(warehouses, company_abbr, policy="skip"):
	groups = defaultdict(list)
	for row in warehouses:
		groups[_clean_name(row.get("warehouse_name") or row["name"], company_abbr)].append(row)
	names = {}
	skipped = []
	for name, rows in groups.items():
		rows = sorted(rows, key=lambda row: row["name"])
		if len(rows) == 1:
			names[rows[0]["name"]] = name
			continue
		if policy == "suffix":
			for row in rows:
				names[row["name"]] = f"{name} ({row['name']})"
		else:
			names[rows[0]["name"]] = name
			skipped.extend(_duplicate_name_report("Warehouse", name, rows[1:]))
	return names, skipped


def _is_warehouse_root(row):
	if row.get("parent_warehouse"):
		return False
	name = (row.get("warehouse_name") or row["name"] or "").strip().casefold()
	return name == "all warehouses" or name.startswith("all warehouses -")


def _string_list(values):
	return [{"metadata": True, "type": "String"}, *[str(value) for value in values if value]]


def _message(tally_type, name, **fields):
	message = {
		"metadata": {"type": tally_type, "action": "create", "name": name},
		"name": name,
	}
	message.update({key: value for key, value in fields.items() if value not in (None, "", [], {})})
	return message


def _gst_registration_type(value):
	return GST_REGISTRATION_TYPE.get(str(value or "").strip(), "Unregistered/Consumer")


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


def _has_column(doctype, fieldname):
	try:
		return frappe.db.has_column(doctype, fieldname)
	except Exception:
		return False


# ---------------------------------------------------------------------------
# Units and groups
# ---------------------------------------------------------------------------


def _unit_message(row):
	return _message(
		"Unit",
		row["name"],
		issimpleunit=True,
		originalname=row.get("uom_name") or row["name"],
		decimalplaces="0" if cint(row.get("must_be_whole_number")) else "3",
	)


def _stock_group_message(row):
	name = row["name"]
	parent = row.get("parent_item_group") or ""
	return _message(
		"Stock Group",
		name,
		parent=TALLY_PRIMARY if parent in ("", "All Item Groups") else parent,
		shouldquantitiesbeadded=True,
		**{
			"language name": _alias_list(
				name,
				_identity_alias(name, row.get("item_group_name") or name, "Item Group"),
			)
		},
	)


# ---------------------------------------------------------------------------
# Warehouse / Godown
# ---------------------------------------------------------------------------


def _godown_message(row, company_abbr, tally_name=None):
	name = tally_name or _clean_name(row.get("warehouse_name") or row["name"], company_abbr)
	parent = _clean_name(row.get("parent_warehouse"), company_abbr)
	if not parent or parent.casefold() == "all warehouses":
		parent = TALLY_PRIMARY
	return _message(
		"Godown",
		name,
		parent=parent,
		**{
			"language name": _alias_list(
				name, _identity_alias(name, row["name"], "Warehouse")
			)
		},
	)


# ---------------------------------------------------------------------------
# Customer / Supplier / Ledger
# ---------------------------------------------------------------------------


def _party_fieldnames(doctype):
	fields = [
		"name",
		"disabled",
		"tax_id",
		"tax_category",
		"default_currency",
		"default_price_list",
		"payment_terms",
		"website",
		"mobile_no",
		"email_id",
		"primary_address",
	]
	if doctype == "Customer":
		fields += [
			"customer_name",
			"customer_type",
			"customer_group",
			"territory",
			"customer_primary_address",
			"customer_primary_contact",
		]
	else:
		fields += [
			"supplier_name",
			"supplier_type",
			"supplier_group",
			"country",
			"supplier_primary_address",
			"supplier_primary_contact",
		]
	if _has_column(doctype, "gst_category"):
		fields.append("gst_category")
	if _has_column(doctype, "gstin"):
		fields.append("gstin")
	if _has_column(doctype, "pan"):
		fields.append("pan")
	return fields


def _fetch_parties(doctype, include_disabled=False):
	filters = {} if include_disabled else {"disabled": 0}
	return frappe.get_all(
		doctype,
		filters=filters,
		fields=_party_fieldnames(doctype),
		order_by="name",
	)


def _fetch_addresses(parties):
	names = sorted(
		{
			row.get("customer_primary_address") or row.get("supplier_primary_address")
			for row in parties
			if row.get("customer_primary_address") or row.get("supplier_primary_address")
		}
	)
	if not names:
		return {}
	return {
		row["name"]: row
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", names]},
			fields=[
				"name",
				"address_line1",
				"address_line2",
				"city",
				"state",
				"country",
				"pincode",
				"email_id",
				"phone",
			],
		)
	}


def _fetch_contacts(parties):
	names = sorted(
		{
			row.get("customer_primary_contact") or row.get("supplier_primary_contact")
			for row in parties
			if row.get("customer_primary_contact") or row.get("supplier_primary_contact")
		}
	)
	if not names:
		return {}
	return {
		row["name"]: row
		for row in frappe.get_all(
			"Contact",
			filters={"name": ["in", names]},
			fields=["name", "first_name", "last_name", "full_name", "email_id", "phone", "mobile_no"],
		)
	}


def _fetch_customer_credit_limits(customers, company):
	names = [row["name"] for row in customers]
	if not names:
		return {}
	limits = frappe.get_all(
		"Customer Credit Limit",
		filters={"parent": ["in", names], "company": company},
		fields=["parent", "credit_limit"],
	)
	result = {}
	for row in limits:
		result[row["parent"]] = result.get(row["parent"], 0) + flt(row.get("credit_limit"))
	return result


def _address_values(address):
	if not address:
		return [], "", "", ""
	lines = [
		address.get("address_line1"),
		address.get("address_line2"),
		address.get("city"),
	]
	lines = [_text(line) for line in lines if _text(line)]
	return lines, address.get("state") or "", address.get("pincode") or "", address.get("country") or ""


def _ledger_message(
	party,
	doctype,
	company,
	address,
	contact,
	credit_limit,
	posting_date=None,
	tally_name=None,
):
	name = (
		tally_name
		or (party.get("customer_name") if doctype == "Customer" else party.get("supplier_name"))
		or party["name"]
	)
	address_lines, state, pincode, address_country = _address_values(address)
	tax_id = _text(party.get("gstin") or party.get("tax_id"))
	pan = _text(party.get("pan"))
	if len(tax_id) == 10 and not pan:
		pan, tax_id = tax_id, ""
	elif len(tax_id) == 15 and not pan:
		# Indian GSTIN embeds the 10-character PAN at positions 3-12.
		pan = tax_id[2:12]
	country = _text(party.get("country") or address_country or frappe.get_cached_value("Company", company, "country"))
	email = _text(
		(contact or {}).get("email_id")
		or (address or {}).get("email_id")
		or party.get("email_id")
	)
	phone = _text((contact or {}).get("phone") or (address or {}).get("phone"))
	mobile = _text((contact or {}).get("mobile_no") or party.get("mobile_no"))
	contact_name = _text(
		(contact or {}).get("full_name")
		or " ".join(
			part for part in ((contact or {}).get("first_name"), (contact or {}).get("last_name")) if part
		)
	)
	fields = {
		"parent": PARTY_PARENT[doctype],
		"isbillwiseon": True,
		"iscostcentreson": False,
		"affectsstock": False,
		"isgstapplicable": bool(tax_id or party.get("gst_category") not in (None, "", "Unregistered")),
		"gstregistrationtype": _gst_registration_type(party.get("gst_category")),
		"partygstin": tax_id,
		"incometaxnumber": pan,
		"countryofresidence": country,
		"ledstatename": state,
		"pincode": pincode,
		"address": _string_list(address_lines) if address_lines else None,
		"email": email,
		"ledgerphone": phone,
		"ledgermobile": mobile,
		"ledgercontact": contact_name,
		"creditlimit": f"{flt(credit_limit):.2f}" if credit_limit else None,
		"ledgercode": party["name"],
		"website": _text(party.get("website")),
		"mailingname": _string_list([name]),
		"language name": _alias_list(
			name, _identity_alias(name, party["name"], doctype)
		),
	}
	if address_lines:
		fields["ledmailingdetails"] = [
			{
				"applicablefrom": str(posting_date or date.today().isoformat()).replace("-", ""),
				"mailingname": name,
				"address": _string_list(address_lines),
				"pincode": pincode,
				"state": state,
				"country": country,
			}
		]
	return _message("Ledger", name, **fields)


# ---------------------------------------------------------------------------
# Item / Stock Item
# ---------------------------------------------------------------------------


def _item_fieldnames():
	fields = [
		"name",
		"item_code",
		"item_name",
		"description",
		"item_group",
		"stock_uom",
		"disabled",
		"is_stock_item",
		"has_batch_no",
		"has_serial_no",
		"has_expiry_date",
		"shelf_life_in_days",
		"valuation_method",
		"valuation_rate",
		"standard_rate",
		"opening_stock",
		"is_sales_item",
		"is_purchase_item",
		"gst_hsn_code",
	]
	return [field for field in fields if _has_column("Item", field)]


def _fetch_items(include_disabled=False):
	filters = {} if include_disabled else {"disabled": 0}
	return frappe.get_all(
		"Item",
		filters=filters,
		fields=_item_fieldnames(),
		order_by="name",
	)


def _fetch_child_rows(doctype, item_names, fields):
	if not item_names:
		return []
	return frappe.get_all(
		doctype,
		filters={"parenttype": "Item", "parent": ["in", item_names]},
		fields=["parent", *fields],
	)


def _item_messages(
	items,
	company_abbr,
	item_names,
	include_opening_balances=False,
	posting_date=None,
):
	names = [row["name"] for row in items]
	barcodes = {}
	for row in _fetch_child_rows("Item Barcode", names, ["barcode", "uom"]):
		barcodes.setdefault(row["parent"], []).append(row)
	uoms = {}
	for row in _fetch_child_rows("UOM Conversion Detail", names, ["uom", "conversion_factor"]):
		uoms.setdefault(row["parent"], []).append(row)
	reorders = {}
	for row in _fetch_child_rows(
		"Item Reorder",
		names,
		["warehouse", "warehouse_reorder_level", "warehouse_reorder_qty"],
	):
		reorders.setdefault(row["parent"], []).append(row)

	messages = []
	for item in items:
		code = item.get("item_code") or item["name"]
		tally_name = item_names.get(code) or code
		uom = item.get("stock_uom") or "Nos"
		parent = item.get("item_group") or TALLY_PRIMARY
		if parent == "All Item Groups":
			parent = TALLY_PRIMARY
		description = _text(item.get("description")) or _text(item.get("item_name")) or code
		message = _message(
			"Stock Item",
			tally_name,
			parent=parent,
			**{
				"base units": uom,
				"description": description,
				"gstapplicable": "Applicable" if item.get("gst_hsn_code") else "Not Applicable",
				"gsttypeofsupply": "Goods" if cint(item.get("is_stock_item")) else "Services",
				"isbatchwiseon": bool(cint(item.get("has_batch_no"))),
				"isperishableon": bool(cint(item.get("has_expiry_date"))),
				"hasmfgdate": bool(cint(item.get("has_expiry_date"))),
				"ignorebatches": not bool(cint(item.get("has_batch_no"))),
				"ignoregodowns": not bool(cint(item.get("is_stock_item"))),
				"iscostcentreson": False,
				"iscosttrackingon": False,
				"language name": _alias_list(
					tally_name, [code, f"ERPNext:Item:{code}"]
				),
				"hsncode": item.get("gst_hsn_code") or None,
				"partno": code,
			},
		)
		if item.get("valuation_method"):
			message["valuationmethod"] = (
				"Avg. Price" if item["valuation_method"] == "Moving Average" else item["valuation_method"]
			)
		rate_date = str(posting_date or date.today().isoformat()).replace("-", "")
		if item.get("valuation_rate"):
			message["standardcostlist"] = [
				{
					"date": rate_date,
					"rate": f"{flt(item['valuation_rate']):.2f}/{uom}",
				}
			]
		if item.get("standard_rate"):
			message["standardpricelist"] = [
				{
					"date": rate_date,
					"rate": f"{flt(item['standard_rate']):.2f}/{uom}",
				}
			]
		if item.get("disabled"):
			message["isdeleted"] = True
		if reorders.get(item["name"]):
			levels = [
				flt(row.get("warehouse_reorder_level"))
				for row in reorders[item["name"]]
				if flt(row.get("warehouse_reorder_level"))
			]
			if levels:
				message["reorderlevel"] = f"{max(levels):.2f}"
			quantities = [
				flt(row.get("warehouse_reorder_qty"))
				for row in reorders[item["name"]]
				if flt(row.get("warehouse_reorder_qty"))
			]
			if quantities:
				message["reorderqty"] = f"{max(quantities):.2f}"
		if barcodes.get(item["name"]):
			message["barcodes"] = [
				{"barcode": row.get("barcode"), "uom": row.get("uom")}
				for row in barcodes[item["name"]]
				if row.get("barcode")
			]
		if uoms.get(item["name"]):
			message["uoms"] = [
				{
					"metadata": {"type": "UOM Conversion Detail", "name": row.get("uom")},
					"uom": row.get("uom"),
					"conversion_factor": flt(row.get("conversion_factor")),
				}
				for row in uoms[item["name"]]
				if row.get("uom")
			]
		if include_opening_balances and flt(item.get("opening_stock")):
			quantity = flt(item["opening_stock"])
			rate = flt(item.get("valuation_rate"))
			message["openingbalance"] = f" {quantity:g} {uom}"
			message["openingrate"] = f"{rate:.2f}/{uom}"
			message["openingvalue"] = f"-{quantity * rate:.2f}"
		messages.append(message)
	return messages


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def build_master_sync_summary(company):
	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist").format(frappe.bold(company)))
	return {
		"Item": frappe.db.count("Item", {"disabled": 0}),
		"Warehouse": frappe.db.count("Warehouse", {"company": company, "disabled": 0}),
		"Customer": frappe.db.count("Customer", {"disabled": 0}),
		"Supplier": frappe.db.count("Supplier", {"disabled": 0}),
		"Item Group": frappe.db.count("Item Group"),
		"UOM": frappe.db.count("UOM", {"enabled": 1}),
	}


def build_master_sync_payload(
	company,
	masters=None,
	include_disabled=False,
	include_opening_balances=False,
	posting_date=None,
	limit=None,
	duplicate_names="skip",
):
	"""Return dependency-ordered Tally JSONEx batches for the selected masters."""
	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist").format(frappe.bold(company)))
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

	if "Item" in selected:
		uom_rows = frappe.get_all(
			"UOM",
			filters={"enabled": 1},
			fields=["name", "uom_name", "must_be_whole_number"],
			order_by="name",
		)
		item_groups = frappe.get_all(
			"Item Group",
			fields=["name", "item_group_name", "parent_item_group"],
			order_by="lft",
		)
		batches.append(
			{
				"key": "units",
				"label": "Units of measure",
				"tallymessage": [_unit_message(row) for row in uom_rows],
			}
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
			# units and groups referenced by the selected items.
			if batch["key"] in {"warehouses", "customers", "suppliers", "items"}:
				batch["tallymessage"] = batch["tallymessage"][:limit]

	counts = {key: 0 for key in MASTER_SYNC_TYPES}
	for batch in batches:
		if batch["key"] == "items":
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
		"masters": selected,
		"include_disabled": bool(include_disabled),
		"limit": limit,
		"duplicate_names": duplicate_names,
		"batches": batches,
		"counts": counts,
		"skipped": skipped,
	}


@frappe.whitelist()
def get_master_sync_summary(company):
	frappe.only_for(("Accounts Manager", "System Manager", "Tally Sync User"))
	return build_master_sync_summary(company)


@frappe.whitelist()
def get_master_sync_payload(
	company,
	masters=None,
	include_disabled=0,
	include_opening_balances=0,
	posting_date=None,
	limit=None,
	duplicate_names="skip",
):
	frappe.only_for(("Accounts Manager", "System Manager", "Tally Sync User"))
	return build_master_sync_payload(
		company,
		masters,
		include_disabled=cint(include_disabled),
		include_opening_balances=cint(include_opening_balances),
		posting_date=posting_date,
		limit=cint(limit) if limit not in (None, "") else None,
		duplicate_names=duplicate_names,
	)
