"""Alias, display-name and duplicate handling helpers for master sync."""

import re
from collections import Counter, defaultdict

import frappe

from ...bridge.common import clean_company_suffix as _clean_name


def _has_column(doctype, fieldname):
	try:
		return frappe.db.has_column(doctype, fieldname)
	except Exception:
		return False


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
