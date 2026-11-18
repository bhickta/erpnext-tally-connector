"""Targeted readback of an outbound voucher through Tally's inline collection."""

import json
import re
from collections import Counter
from decimal import Decimal

from .clients import BridgeRequestError
from .collection_gateway import build_collection_export, nested_records, parse_collection_export, scalar
from .xml_gateway import _date, stock_item_name

INVENTORY_FIELDS = (
	"StockItemName",
	"ActualQty",
	"BilledQty",
	"GodownName",
	"BatchAllocations.GodownName",
	"BatchAllocations.ActualQty",
	"BatchAllocations.BilledQty",
)
VOUCHER_FIELDS = (
	"MasterID", "GUID", "VoucherNumber", "Date", "VoucherTypeName", "Narration", "IsCancelled", "IsOptional",
	*(
		f"{collection}.{field}"
		for collection in (
			"AllInventoryEntries",
			"InventoryEntries",
			"AllLedgerEntries.InventoryAllocations",
			"LedgerEntries.InventoryAllocations",
		)
		for field in INVENTORY_FIELDS
	),
)


class VoucherReadbackError(BridgeRequestError):
	"""Stop the outbound batch when Tally cannot confirm voucher inventory."""


def read_collection(tally, request, tag):
	try:
		return parse_collection_export(tally.export_collection(request), (tag,), raise_on_error=True)
	except (BridgeRequestError, ValueError) as exc:
		raise BridgeRequestError(f"Tally {tag.lower()} collection read failed: {exc}") from exc


def _read_vouchers(tally, company, filters, date=None):
	request = build_collection_export(
		company, "ETOutboundVoucherReadback", "Voucher", VOUCHER_FIELDS, filters,
		static_variables={"SVFROMDATE": date, "SVTODATE": date} if date else None,
	)
	return read_collection(tally, request, "VOUCHER")


def read_voucher(tally, company, reference):
	if reference.startswith("guid:"):
		guid = reference.removeprefix("guid:")
		field, value, formula_value = "GUID", guid, json.dumps(guid)
	else:
		if not re.fullmatch(r"[1-9][0-9]*", reference):
			raise BridgeRequestError(f"Invalid Tally voucher master ID: {reference}")
		field, value, formula_value = "MasterID", reference, reference
	rows = _read_vouchers(tally, company, [f"${field} = {formula_value}"])
	if len(rows) > 1 or (rows and scalar(rows[0], field.lower()).casefold() != value.casefold()):
		raise BridgeRequestError("Tally returned an ambiguous voucher identity")
	return rows[0] if rows else None


def inspect_voucher(tally, company, number, date, voucher_type=None):
	"""Return stored lines without importing or changing a voucher."""
	date = _date(date)
	filters = [f"$VoucherNumber = {json.dumps(number)}", "$Date = ##SVFromDate"]
	if voucher_type:
		filters.append(f"$VoucherTypeName = {json.dumps(voucher_type)}")
	rows = _read_vouchers(tally, company, filters, date)
	if not rows:
		raise BridgeRequestError(f"Tally did not return voucher {number} dated {date}")
	if len(rows) != 1:
		raise BridgeRequestError(f"Multiple Tally vouchers match {number}; specify the voucher type")
	row = rows[0]
	if (
		scalar(row, "vouchernumber") != number
		or scalar(row, "date") != date
		or (voucher_type and scalar(row, "vouchertypename").casefold() != voucher_type.casefold())
	):
		raise BridgeRequestError("Tally returned a voucher that does not match the requested number, date and type")
	return {
		"master_id": scalar(row, "masterid"),
		"voucher_number": scalar(row, "vouchernumber"),
		"voucher_type": scalar(row, "vouchertypename"),
		"date": scalar(row, "date"),
		"cancelled": scalar(row, "iscancelled"),
		"optional": scalar(row, "isoptional"),
		"stock_lines": [
			{
				"item": scalar(entry, "stockitemname"),
				"actual_quantity": scalar(entry, "actualqty"),
				"billed_quantity": scalar(entry, "billedqty"),
				"godowns": [
					scalar(batch, "godownname")
					for batch in nested_records(entry, "BatchAllocations.LIST")
				] or ([scalar(entry, "godownname")] if scalar(entry, "godownname") else []),
			}
			for entry in _inventory_entries(row)
		],
	}


def _inventory_entries(row):
	# Tally can expose the same stock lines through multiple view collections.
	return (
		nested_records(row, "AllInventoryEntries.LIST")
		or nested_records(row, "InventoryEntries.LIST")
		or nested_records(row, "InventoryAllocations.LIST")
	)


def _quantity(value):
	# Exports can include the equivalent quantity in an alternate unit.
	match = re.fullmatch(r"([+-]?\d+(?:,\d+)*(?:\.\d+)?)\s*(.*)", value.partition(" = ")[0].strip())
	if not match:
		return None
	return Decimal(match[1].replace(",", "")), match[2].strip().casefold()


def _matches_inventory(row, items):
	expected = Counter()
	for item in items:
		name = stock_item_name(item).casefold()
		qty = (Decimal(str(item["stock_qty"])), item["stock_uom"].strip().casefold())
		warehouse = (item.get("warehouse") or "").strip().casefold()
		expected[name, qty, qty, warehouse] += 1

	unallocated = Counter()
	for entry in _inventory_entries(row):
		name = scalar(entry, "stockitemname").casefold()
		qty = _quantity(scalar(entry, "actualqty"))
		billed = _quantity(scalar(entry, "billedqty"))
		batches = nested_records(entry, "BatchAllocations.LIST")
		godown = scalar(batches[0] if batches else entry, "godownname").casefold()
		key = (name, qty, billed, godown)
		if godown and expected[key]:
			if batches and (
				len(batches) != 1
				or _quantity(scalar(batches[0], "actualqty")) != qty
				or _quantity(scalar(batches[0], "billedqty")) != billed
			):
				return False
			expected[key] -= 1
		else:
			unallocated[name, qty, billed, ""] += 1
	return expected == unallocated


def verify_voucher(row, document, version_marker):
	name = document["name"]
	if not row:
		raise BridgeRequestError(f"Tally did not return a voucher for {name}")
	master_id = scalar(row, "masterid")
	if (
		not re.fullmatch(r"[1-9][0-9]*", master_id)
		or scalar(row, "vouchertypename").casefold() != (document.get("voucher_type") or "Sales").casefold()
		or scalar(row, "date") != _date(document["transaction_date"])
		or scalar(row, "iscancelled").casefold() in {"yes", "true", "1"}
		or scalar(row, "isoptional").casefold() in {"yes", "true", "1"}
	):
		raise BridgeRequestError(f"Tally voucher for {name} has an unexpected identity or posting state")
	if not _matches_inventory(row, document["items"]):
		raise BridgeRequestError(
			f"Tally voucher {master_id} for {name} did not retain the expected stock items, quantities and godowns"
		)
	if version_marker not in scalar(row, "narration").splitlines():
		raise BridgeRequestError(
			f"Tally voucher {master_id} for {name} has no matching ERPNext version marker. "
			"Reconcile the existing voucher before retrying."
		)
	return master_id
