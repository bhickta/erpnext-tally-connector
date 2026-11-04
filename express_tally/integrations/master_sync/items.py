"""Tally Stock Item field, fetch and message builders."""

from datetime import date

import frappe
from frappe.utils import cint, flt

from .constants import TALLY_PRIMARY
from .names import _alias_list, _has_column, _message, _text


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
