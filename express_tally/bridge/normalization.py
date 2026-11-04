"""Pure normalization of Tally collection records into connector records."""

import hashlib

from .collection_gateway import nested_records, scalar
from .common import parse_number


def master_name(record):
	return scalar(record, "_name", "name", "name_list.name", "language_name_list.name")


def master_depth(record, by_type_and_name):
	depth = 0
	parent = str(record.get("parent") or "").casefold()
	visited = set()
	while parent and parent not in visited:
		visited.add(parent)
		parent_record = by_type_and_name.get((record["_tally_type"], parent))
		if not parent_record:
			break
		depth += 1
		parent = str(parent_record.get("parent") or "").casefold()
	return depth


def integer(value):
	try:
		return int(float(str(value or "0").replace(",", "")))
	except ValueError:
		return 0


def number(value):
	return parse_number(value)


def tally_balance(value):
	"""Normalize Tally balances to ERPNext's debit-minus-credit convention."""
	text = str(value or "0").strip()
	amount = number(text)
	if "dr" in text.casefold():
		return abs(amount)
	if "cr" in text.casefold():
		return -abs(amount)
	# Native Tally amounts are negative for debit and positive for credit.
	return -amount


def tally_date(value):
	value = str(value or "").strip()
	if len(value) == 8 and value.isdigit():
		return f"{value[:4]}-{value[4:6]}-{value[6:]}"
	return value


def _primary_group(parent, groups):
	current = str(parent or "").strip()
	classification_groups = {
		"sundry debtors",
		"sundry creditors",
		"bank accounts",
		"cash-in-hand",
		"bank od a/c",
		"duties & taxes",
		"stock-in-hand",
		"sales accounts",
		"purchase accounts",
		"direct expenses",
		"direct incomes",
		"indirect expenses",
		"indirect incomes",
		"fixed assets",
		"deposits (asset)",
		"loans & advances (asset)",
		"secured loans",
		"unsecured loans",
	}
	visited = set()
	last = current
	while current and current.casefold() not in visited:
		if current.casefold() in classification_groups:
			return current
		visited.add(current.casefold())
		last = current
		group = groups.get(current.casefold())
		if not group or not group.get("parent") or group["parent"].casefold() == "primary":
			break
		current = group["parent"]
	return last


def _identity(collection_type, raw, name):
	alter_id = integer(scalar(raw, "alter_id", "alterid"))
	guid = scalar(raw, "guid")
	if not guid:
		guid = hashlib.sha256(f"{collection_type}:{name}".encode()).hexdigest()
	return {
		"_tally_type": collection_type,
		"_tally_alter_id": alter_id,
		"_tally_guid": guid,
		"_tally_key": f"{collection_type}:{guid}",
	}


def normalize_master(collection_type, raw, groups):
	name = master_name(raw)
	if not name:
		return None
	parent = scalar(raw, "parent")
	base = {**_identity(collection_type, raw, name), "name": name, "parent": parent}
	if collection_type == "unit":
		return {**base, "kind": "uom", "must_be_whole_number": integer(scalar(raw, "decimal_places")) == 0}
	if collection_type == "stock_group":
		return {**base, "kind": "item_group"}
	if collection_type == "godown":
		return {**base, "kind": "warehouse"}
	if collection_type == "stock_item":
		return {
			**base,
			"kind": "item",
			"item_code": scalar(raw, "part_no", "partno") or name,
			"stock_uom": scalar(raw, "base_units", "baseunits") or "Nos",
			"description": scalar(raw, "description") or name,
			"opening_stock": number(scalar(raw, "opening_balance", "openingbalance")),
			"opening_value": number(scalar(raw, "opening_value", "openingvalue")),
			"hsn_code": scalar(raw, "gst_details_list.hsn_code", "hsn_code"),
		}
	if collection_type == "group":
		primary = _primary_group(parent, groups)
		if primary.casefold() == "sundry debtors":
			return {**base, "kind": "customer_group", "primary_group": primary}
		if primary.casefold() == "sundry creditors":
			return {**base, "kind": "supplier_group", "primary_group": primary}
		return {**base, "kind": "account_group", "primary_group": primary}
	if collection_type == "ledger":
		primary = _primary_group(parent, groups)
		kind = "account"
		if primary.casefold() == "sundry debtors":
			kind = "customer"
		elif primary.casefold() == "sundry creditors":
			kind = "supplier"
		addresses = raw.get("address_list") or raw.get("address") or []
		if isinstance(addresses, dict):
			addresses = list(addresses.values())
		if not isinstance(addresses, list):
			addresses = [addresses]
		return {
			**base,
			"kind": kind,
			"primary_group": primary,
			"code": scalar(raw, "ledger_code", "ledgercode") or name,
			"gst_category": scalar(raw, "gst_registration_type", "gstregistrationtype"),
			"gstin": scalar(raw, "party_gstin", "partygstin"),
			"pan": scalar(raw, "income_tax_number", "incometaxnumber"),
			"state": scalar(raw, "state_name", "statename"),
			"country": scalar(raw, "country_name", "countryname") or "India",
			"pincode": scalar(raw, "pin_code", "pincode"),
			"email": scalar(raw, "email"),
			"phone": scalar(raw, "ledger_phone", "ledgerphone"),
			"mobile": scalar(raw, "ledger_mobile", "ledgermobile"),
			"credit_limit": number(scalar(raw, "credit_limit", "creditlimit")),
			"address_lines": [str(value).strip() for value in addresses if str(value).strip()],
		}
	return None


def normalize_voucher(raw):
	voucher_number = scalar(raw, "voucher_number", "vouchernumber", "_name", "name")
	master_id = scalar(raw, "master_id", "masterid")
	name = voucher_number or master_id
	if not name:
		return None
	ledger_entries = []
	raw_ledger_entries = nested_records(raw, "all_ledger_entries_list") or nested_records(
		raw, "ledger_entries_list"
	)
	for entry in raw_ledger_entries:
		ledger_entries.append(
			{
				"ledger": scalar(entry, "ledger_name", "ledgername"),
				"amount": number(scalar(entry, "amount")),
				"is_deemed_positive": scalar(entry, "is_deemed_positive", "isdeemedpositive"),
				"bill_allocations": nested_records(entry, "bill_allocations_list"),
			}
		)
	inventory_entries = []
	raw_inventory_entries = nested_records(raw, "all_inventory_entries_list") or nested_records(
		raw, "inventory_entries_list"
	)
	for entry in raw_inventory_entries:
		allocations = nested_records(entry, "accounting_allocations_list")
		batches = nested_records(entry, "batch_allocations_list")
		inventory_entries.append(
			{
				"item": scalar(entry, "stock_item_name", "stockitemname"),
				"actual_qty": scalar(entry, "actual_qty", "actualqty"),
				"billed_qty": scalar(entry, "billed_qty", "billedqty"),
				"rate": scalar(entry, "rate"),
				"amount": number(scalar(entry, "amount")),
				"godown": scalar(entry, "godown_name", "godownname")
				or (scalar(batches[0], "godown_name") if batches else ""),
				"account": scalar(allocations[0], "ledger_name")
				if allocations
				else scalar(entry, "ledger_name"),
				"accounting_allocations": [
					{"ledger": scalar(row, "ledger_name"), "amount": number(scalar(row, "amount"))}
					for row in allocations
					if scalar(row, "ledger_name")
				],
				"tracking_numbers": [
					scalar(row, "tracking_number") for row in batches if scalar(row, "tracking_number")
				],
			}
		)
	return {
		**_identity("voucher", raw, name),
		"kind": "voucher",
		"voucher_number": voucher_number,
		"voucher_type": scalar(raw, "voucher_type_name", "vouchertypename"),
		"master_id": master_id,
		"date": tally_date(scalar(raw, "date")),
		"reference": scalar(raw, "reference"),
		"reference_date": tally_date(scalar(raw, "reference_date", "referencedate")),
		"narration": scalar(raw, "narration"),
		"party_ledger": scalar(raw, "party_ledger_name", "partyledgername"),
		"optional": scalar(raw, "is_optional", "isoptional").casefold() == "yes",
		"cancelled": scalar(raw, "is_cancelled", "iscancelled").casefold() == "yes",
		"deleted": scalar(raw, "is_deleted", "isdeleted").casefold() == "yes",
		"ledger_entries": [entry for entry in ledger_entries if entry["ledger"]],
		"inventory_entries": [entry for entry in inventory_entries if entry["item"]],
	}
