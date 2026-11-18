"""TallyPrime 7 native JSONEx payloads and import-result parsing."""

import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .xml_gateway import stock_item_name


def _amount(value):
	return str(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _number(value):
	return format(Decimal(str(value or 0)).normalize(), "f")


def _quantity(value, unit):
	return f" {_number(value)} {unit}"


def _metadata(object_type, name, action="create", **values):
	return {"type": object_type, "action": action.lower(), "name": name, **values}


def _master_payload(company, messages):
	return {
		"static_variables": [
			{"name": "svMstImportFormat", "value": "jsonex"},
			{"name": "svCurrentCompany", "value": company},
		],
		"tallymessage": messages,
	}


def _unit(name):
	formal_names = {
		"Bag": "Bags",
		"Box": "Boxes",
		"Nos": "Numbers",
		"Pcs": "Pieces",
		"Kg": "Kilograms",
		"Ltr": "Litres",
		"Mtr": "Metres",
	}
	return {
		"metadata": _metadata("Unit", name),
		"name": name,
		"issimpleunit": True,
		"originalname": formal_names.get(name, name),
		"decimalplaces": "3",
	}


def _stock_group(group):
	return {
		"metadata": _metadata("Stock Group", group["name"]),
		"name": group["name"],
		"parent": group.get("parent") or "\x04 Primary",
		"shouldquantitiesbeadded": True,
	}


def _godown(name):
	return {
		"metadata": _metadata("Godown", name),
		"name": name,
		"parent": "\x04 Primary",
	}


def _ledger(name, parent, billwise=False, gstin="", country="", *, affects_stock=False):
	ledger = {
		"metadata": _metadata("Ledger", name),
		"name": name,
		"parent": parent,
		"isbillwiseon": bool(billwise),
		"affectsstock": affects_stock,
	}
	if country:
		ledger["countryofresidence"] = country
	if gstin:
		ledger["partygstin"] = gstin
	return ledger


def _stock_item(item):
	tally_name = stock_item_name(item)
	names = [{"metadata": True, "type": "String"}, tally_name]
	for alias in (item["item_code"], f"ERPNext:Item:{item['item_code']}"):
		if alias and alias.casefold() != tally_name.casefold() and alias not in names:
			names.append(alias)
	stock_item = {
		"metadata": _metadata("Stock Item", tally_name),
		"name": tally_name,
		"parent": item.get("item_group") or "\x04 Primary",
		"base units": item["stock_uom"],
		"gsttypeofsupply": "Goods",
		"partno": item["item_code"],
		"language name": [{"name": names, "language id": " 1033"}],
	}
	if item.get("hsn_code"):
		stock_item["hsncode"] = item["hsn_code"]
	return stock_item


def build_master_imports(document, company, *, sales_ledger_exists=False):
	"""Return dependency-ordered native JSONEx master requests."""
	units = [_unit(name) for name in document["masters"]["units"]]
	general = [_stock_group(group) for group in document["masters"]["item_groups"]]
	general.extend(_godown(name) for name in document["masters"]["warehouses"])
	customer = document["customer"]
	general.append(
		_ledger(
			customer["name"],
			"Sundry Debtors",
			billwise=True,
			gstin=customer.get("gstin") or "",
			country=customer.get("country") or "",
		)
	)
	if not sales_ledger_exists:
		general.append(
			_ledger(document["sales_ledger"], "Sales Accounts", affects_stock=bool(document["items"]))
		)
	general.extend(_ledger(tax["ledger"], "Duties & Taxes") for tax in document["taxes"])
	if document.get("rounding_adjustment"):
		general.append(_ledger(document["round_off_ledger"], "Indirect Expenses"))
	items = [_stock_item(item) for item in document["items"]]
	return [
		_master_payload(company, units),
		_master_payload(company, general),
		_master_payload(company, items),
	]


def _remote_id(document, target_id):
	source_doctype = document.get("source_doctype", "Sales Order")
	return str(
		uuid.uuid5(
			uuid.NAMESPACE_URL,
			f"srv-erp:{target_id}:{source_doctype}:Sales Voucher:{document['name']}",
		)
	)


def build_voucher_import(document, company, target_id):
	remote_id = _remote_id(document, target_id)
	party_amount = _amount(-Decimal(str(document["grand_total"])))
	voucher = {
		"metadata": _metadata(
			"Voucher",
			document["name"],
			document["operation"],
			vchtype="Sales",
			objview="Invoice Voucher View",
			remoteid=remote_id,
		),
		"guid": remote_id,
		"date": str(document["transaction_date"]).replace("-", "")[:8],
		"effectivedate": str(document["transaction_date"]).replace("-", "")[:8],
		"vouchertypename": "Sales",
		"vouchernumber": document["name"],
		"reference": document.get("reference") or document["name"],
		"partyname": document["customer"]["name"],
		"partyledgername": document["customer"]["name"],
		"basicbuyername": document["customer"]["name"],
		"narration": document.get("narration")
		or f"ERPNext {document.get('source_doctype', 'Sales Order')} {document['name']}",
		"persistedview": "Invoice Voucher View",
		"vchentrymode": "Item Invoice",
		"isoptional": False,
		"isinvoice": True,
		"isorder": False,
		"ledgerentries": [
			{
				"ledgername": document["customer"]["name"],
				"isdeemedpositive": True,
				"ispartyledger": True,
				"islastdeemedpositive": True,
				"amount": party_amount,
				"billallocations": [
					{
						"name": document["name"],
						"billtype": "New Ref",
						"amount": party_amount,
					}
				],
			}
		],
		"allinventoryentries": [],
	}

	for tax in document["taxes"]:
		voucher["ledgerentries"].append(
			{
				"ledgername": tax["ledger"],
				"isdeemedpositive": False,
				"amount": _amount(tax["amount"]),
			}
		)
	if document.get("rounding_adjustment"):
		rounding = Decimal(str(document["rounding_adjustment"]))
		voucher["ledgerentries"].append(
			{
				"ledgername": document["round_off_ledger"],
				"isdeemedpositive": rounding < 0,
				"amount": _amount(rounding),
			}
		)

	for item in document["items"]:
		qty = _quantity(item["stock_qty"], item["stock_uom"])
		batch = {
			"batchname": "Primary Batch",
			"amount": _amount(item["amount"]),
			"actualqty": qty,
			"billedqty": qty,
		}
		if item.get("warehouse"):
			batch["godownname"] = item["warehouse"]
		voucher["allinventoryentries"].append(
			{
				"stockitemname": stock_item_name(item),
				"isdeemedpositive": False,
				"rate": f"{_amount(item['rate'])}/{item['stock_uom']}",
				"amount": _amount(item["amount"]),
				"actualqty": qty,
				"billedqty": qty,
				"batchallocations": [batch],
				"accountingallocations": [
					{
						"ledgername": document["sales_ledger"],
						"isdeemedpositive": False,
						"amount": _amount(item["amount"]),
					}
				],
			}
		)
	return {
		"static_variables": [
			{"name": "svVchImportFormat", "value": "jsonex"},
			{"name": "svCurrentCompany", "value": company},
		],
		"tallymessage": [voucher],
	}


@dataclass(frozen=True)
class ImportResult:
	success: bool
	created: int = 0
	altered: int = 0
	ignored: int = 0
	errors: int = 0
	last_voucher_id: str = ""
	message: str = ""
	combined: int = 0


def parse_import_response(payload, require_change=False, allow_ignored=False):
	if not isinstance(payload, dict):
		return ImportResult(False, message="Tally returned no import result")
	data = payload.get("data")
	result = data.get("import_result") if isinstance(data, dict) else None
	fields = ("created", "altered", "ignored", "errors", "exceptions", "combined", "cancelled")
	if not isinstance(result, dict) or not set(fields).intersection(result):
		return ImportResult(False, message=str(payload.get("error") or data or "Tally returned no import result"))
	try:
		created, altered, ignored, errors, exceptions, combined, cancelled = (
			int(result.get(field) or 0) for field in fields
		)
	except (ValueError, TypeError):
		return ImportResult(False, message="Tally returned an invalid import count")
	if min(created, altered, ignored, errors, exceptions, combined, cancelled) < 0:
		return ImportResult(False, message="Tally returned a negative import count")
	accepted = bool(created + altered + combined + cancelled or (allow_ignored and ignored))
	messages = result.get("line_errors") or result.get("lineerror") or payload.get("error") or ""
	if isinstance(messages, list):
		messages = "; ".join(str(message) for message in messages)
	if not messages and (errors or exceptions):
		messages = f"Tally reported errors={errors}, exceptions={exceptions}"
	success = (
		str(payload.get("status", "1")) == "1"
		and errors == 0
		and exceptions == 0
		and not messages
		and (not require_change or accepted)
	)
	if not success and not messages:
		messages = f"Tally did not accept the import (ignored={ignored})"
	return ImportResult(
		success=success,
		created=created,
		altered=altered,
		ignored=ignored,
		errors=errors,
		last_voucher_id=str(result.get("lastvchid") or ""),
		message=str(messages),
		combined=combined,
	)
