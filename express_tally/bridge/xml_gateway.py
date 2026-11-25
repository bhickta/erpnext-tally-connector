import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

# TallyPrime accepts only letters and underscores in a Unit name. Names with
# digits, spaces, hyphens, slashes or brackets are refused with
# "BAD UNIT NAME"; verified against TallyPrime 1.1.7.1.
UNIT_NAME_PATTERN = re.compile(r"[A-Za-z_]+")

_DIGIT_WORDS = (
	"Zero",
	"One",
	"Two",
	"Three",
	"Four",
	"Five",
	"Six",
	"Seven",
	"Eight",
	"Nine",
)


def tally_unit_name(name):
	"""Translate an ERPNext UOM name into a name TallyPrime accepts.

	Letters and underscores are kept, digits become words, and every other
	character becomes an underscore. The ERPNext name is preserved separately
	so it can be recorded as the alias.
	"""
	text = str(name or "").strip()
	pieces = []
	for character in text:
		if character.isascii() and (character.isalpha() or character == "_"):
			pieces.append(character)
		elif character.isdigit():
			pieces.append(_DIGIT_WORDS[int(character)])
		else:
			pieces.append("_")
	candidate = re.sub(r"_+", "_", "".join(pieces)).strip("_")
	if not candidate:
		candidate = "Unit_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
	return candidate


def _alphabetic_suffix(value, length=4):
	"""Return a short letters-only suffix, so the name stays Tally-safe."""
	digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:length]
	return "".join(chr(ord("A") + int(character, 16)) for character in digest)


def resolve_unit_names(names):
	"""Map every original UOM name to a unique Tally-safe Unit name."""
	mapping = {}
	used = {}
	for original in dict.fromkeys(str(name or "").strip() for name in names):
		candidate = tally_unit_name(original)
		key = candidate.casefold()
		if key in used:
			suffix = _alphabetic_suffix(original)
			candidate = f"{candidate}_{suffix}"
			while candidate.casefold() in used:
				suffix = _alphabetic_suffix(suffix + original)
				candidate = f"{candidate.rsplit('_', 1)[0]}_{suffix}"
			key = candidate.casefold()
		used[key] = original
		if original:
			mapping[original] = candidate
	return mapping


def unit_name_error(name):
	"""Return why TallyPrime cannot accept this Unit name, or "" when it can."""
	text = str(name or "").strip()
	if not text:
		return "the name is empty"
	if not UNIT_NAME_PATTERN.fullmatch(text):
		return (
			"TallyPrime only accepts letters and underscores in a unit name"
		)
	return ""


def _element(parent, tag, value=None, **attributes):
	element = ET.SubElement(parent, tag, {key.upper(): str(value) for key, value in attributes.items()})
	if value is not None:
		element.text = str(value)
	return element


def _yes_no(value):
	return "Yes" if value else "No"


def _amount(value):
	return str(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _number(value):
	number = Decimal(str(value or 0)).normalize()
	return format(number, "f")


def _date(value):
	if isinstance(value, (date, datetime)):
		return value.strftime("%Y%m%d")
	return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%Y%m%d")


def _due_date(value):
	if isinstance(value, (date, datetime)):
		return value.strftime("%d-%b-%Y")
	return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d-%b-%Y")


def _quantity(value, unit):
	return f"{_number(value)} {unit}".strip()


def _envelope(request, request_type, report_id, company=None):
	root = ET.Element("ENVELOPE")
	header = _element(root, "HEADER")
	_element(header, "VERSION", "1")
	_element(header, "TALLYREQUEST", request)
	_element(header, "TYPE", request_type)
	_element(header, "ID", report_id)
	body = _element(root, "BODY")
	desc = _element(body, "DESC")
	if company:
		variables = _element(desc, "STATICVARIABLES")
		_element(variables, "SVCURRENTCOMPANY", company)
	return root, body, desc


def current_company_request():
	root, _, _ = _envelope("EXPORT", "FUNCTION", "$$CurrentCompany")
	return ET.tostring(root, encoding="unicode")


def function_request(function_name):
	if not function_name.startswith("$$"):
		raise ValueError("Only built-in Tally functions may be queried")
	root, _, _ = _envelope("EXPORT", "FUNCTION", function_name)
	return ET.tostring(root, encoding="unicode")


def _import_envelope(report_id, company):
	root, body, desc = _envelope("Import", "Data", report_id, company)
	variables = desc.find("STATICVARIABLES")
	_element(variables, "IMPORTDUPS", "@@DUPIGNORE")
	data = _element(body, "DATA")
	message = _element(data, "TALLYMESSAGE")
	return root, message


def _group(message, name, parent=""):
	group = _element(message, "STOCKGROUP", name=name, action="Create")
	_element(group, "NAME", name)
	_element(group, "PARENT", parent)
	_element(group, "SHOULDQUANTITIESBEADDED", "Yes")


def _unit(message, name):
	# TallyPrime treats NAME as a method (not an identifying XML attribute) for
	# Unit masters. Supplying NAME as an attribute makes 7.1 silently ignore it.
	# ORIGINALNAME is only valid for compound units; on a simple unit Tally
	# replies "DUPLICATE ORIGINAL NAME", so it is deliberately omitted here.
	unit = _element(message, "UNIT", action="Create")
	_element(unit, "NAME", name)
	_element(unit, "ISSIMPLEUNIT", "Yes")
	_element(unit, "DECIMALPLACES", "3")


def build_unit_import(company, messages):
	"""Build the documented Tally XML master import for Unit records.

	TallyPrime's JSON master importer reports an exception for Unit records,
	which can also leave an error dialog on the desktop and block further HTTP
	requests. Units use the XML master import format documented by Tally.
	"""
	root = ET.Element("ENVELOPE")
	header = _element(root, "HEADER")
	_element(header, "TALLYREQUEST", "Import Data")
	body = _element(root, "BODY")
	importdata = _element(body, "IMPORTDATA")
	requestdesc = _element(importdata, "REQUESTDESC")
	_element(requestdesc, "REPORTNAME", "All Masters")
	variables = _element(requestdesc, "STATICVARIABLES")
	_element(variables, "SVCURRENTCOMPANY", company)
	requestdata = _element(importdata, "REQUESTDATA")
	message = ET.SubElement(
		requestdata, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"}
	)
	for row in messages:
		name = str(
			row.get("name") or row.get("metadata", {}).get("name") or ""
		).strip()
		if not name:
			continue
		action = str(row.get("metadata", {}).get("action") or "create").title()
		unit = ET.SubElement(message, "UNIT", {"Action": action})
		_element(unit, "NAME", name)
		simple = _yes_no(row.get("issimpleunit", True))
		# ORIGINALNAME names the base unit of a compound unit. TallyPrime
		# rejects it on a simple unit create with "DUPLICATE ORIGINAL NAME",
		# so only send it when the unit is not simple.
		if simple != "Yes":
			_element(unit, "ORIGINALNAME", row.get("originalname") or name)
		_element(unit, "ISSIMPLEUNIT", simple)
		_element(unit, "DECIMALPLACES", row.get("decimalplaces") or "3")
	return ET.tostring(root, encoding="unicode")


def _godown(message, name):
	godown = _element(message, "GODOWN", name=name, action="Create")
	_element(godown, "NAME", name)
	_element(godown, "PARENT", "")


def _ledger(message, name, parent, billwise=False, gstin="", country="", *, affects_stock=False):
	ledger = _element(message, "LEDGER", name=name, action="Create")
	_element(ledger, "NAME", name)
	_element(ledger, "PARENT", parent)
	_element(ledger, "ISBILLWISEON", _yes_no(billwise))
	_element(ledger, "AFFECTSSTOCK", _yes_no(affects_stock))
	if country:
		_element(ledger, "COUNTRYOFRESIDENCE", country)
	if gstin:
		_element(ledger, "PARTYGSTIN", gstin)
		_element(ledger, "GSTREGISTRATIONTYPE", "Regular")


def _stock_item(message, item):
	stock_item = _element(message, "STOCKITEM", name=stock_item_name(item), action="Create")
	_element(stock_item, "NAME", stock_item_name(item))
	_element(stock_item, "PARENT", item.get("item_group") or "")
	_element(stock_item, "BASEUNITS", item["stock_uom"])
	_element(stock_item, "DESCRIPTION", item.get("description") or item.get("item_name") or "")
	_element(stock_item, "ISBATCHWISEON", "No")
	_element(stock_item, "ISGODOWNON", "Yes")
	if item.get("hsn_code"):
		_element(stock_item, "GSTAPPLICABLE", "Applicable")
		_element(stock_item, "HSNCODE", item["hsn_code"])


def build_master_import(order, company):
	"""Build one combined master request (primarily useful for inspection)."""
	root, message = _import_envelope("All Masters", company)
	for group in order["masters"]["item_groups"]:
		_group(message, group["name"], group.get("parent") or "")
	for unit in order["masters"]["units"]:
		_unit(message, unit)
	for warehouse in order["masters"]["warehouses"]:
		_godown(message, warehouse)

	customer = order["customer"]
	_ledger(
		message,
		customer["name"],
		"Sundry Debtors",
		billwise=True,
		gstin=customer.get("gstin") or "",
		country=customer.get("country") or "",
	)
	_ledger(message, order["sales_ledger"], "Sales Accounts", affects_stock=bool(order["items"]))
	for tax in order["taxes"]:
		_ledger(message, tax["ledger"], "Duties & Taxes")
	if order.get("rounding_adjustment"):
		_ledger(message, order["round_off_ledger"], "Indirect Expenses")
	for item in order["items"]:
		_stock_item(message, item)
	return ET.tostring(root, encoding="unicode")


def build_master_imports(order, company):
	"""Build dependency-ordered requests so Tally cannot skip dependent items.

	TallyPrime may report a missing Unit as an ignored object when Unit and Stock
	Item masters share one import request. Sending the three dependency layers
	separately gives every response an unambiguous result.
	"""
	requests = []

	root, message = _import_envelope("All Masters", company)
	for unit in order["masters"]["units"]:
		_unit(message, unit)
	requests.append(ET.tostring(root, encoding="unicode"))

	root, message = _import_envelope("All Masters", company)
	for group in order["masters"]["item_groups"]:
		_group(message, group["name"], group.get("parent") or "")
	for warehouse in order["masters"]["warehouses"]:
		_godown(message, warehouse)
	customer = order["customer"]
	_ledger(
		message,
		customer["name"],
		"Sundry Debtors",
		billwise=True,
		gstin=customer.get("gstin") or "",
		country=customer.get("country") or "",
	)
	_ledger(message, order["sales_ledger"], "Sales Accounts", affects_stock=bool(order["items"]))
	for tax in order["taxes"]:
		_ledger(message, tax["ledger"], "Duties & Taxes")
	if order.get("rounding_adjustment"):
		_ledger(message, order["round_off_ledger"], "Indirect Expenses")
	requests.append(ET.tostring(root, encoding="unicode"))

	root, message = _import_envelope("All Masters", company)
	for item in order["items"]:
		_stock_item(message, item)
	requests.append(ET.tostring(root, encoding="unicode"))
	return requests


def voucher_remote_id(order, target_id):
	source_doctype = order.get("source_doctype", "Sales Order")
	identity_namespace = order.get("identity_namespace") or "express-tally"
	voucher_type = order.get("voucher_type") or "Sales"
	return str(
		uuid.uuid5(
			uuid.NAMESPACE_URL,
			f"{identity_namespace}:{target_id}:{source_doctype}:{voucher_type} Voucher:{order['name']}",
		)
	)


def _voucher_attributes(order, target_id):
	remote_id = voucher_remote_id(order, target_id)
	voucher_type = order.get("voucher_type") or "Sales"
	attributes = {
		"REMOTEID": remote_id,
		"VCHTYPE": voucher_type,
		"ACTION": order["operation"],
		"OBJVIEW": "Accounting Voucher View",
	}
	if order["operation"] == "Alter":
		if order.get("tally_voucher_id"):
			reference = str(order["tally_voucher_id"])
			if reference.startswith("guid:"):
				attributes.update({"TAGNAME": "GUID", "TAGVALUE": reference.removeprefix("guid:")})
			else:
				attributes.update({"TAGNAME": "MASTER ID", "TAGVALUE": reference})
		else:
			attributes.update({"TAGNAME": "Voucher Number", "TAGVALUE": order["name"]})
	if order["operation"] == "Cancel" and order.get("tally_voucher_id"):
		reference = str(order["tally_voucher_id"])
		if reference.startswith("guid:"):
			attributes.update({"TAGNAME": "GUID", "TAGVALUE": reference.removeprefix("guid:")})
		else:
			attributes.update({"TAGNAME": "MASTER ID", "TAGVALUE": reference})
	return attributes, remote_id


def build_stock_journal_import(order, company, target_id):
	"""Build a Tally Stock Journal for one ERPNext Stock Entry.

	Each item contributes a source leg (issue) and a target leg (receipt) when
	both godowns are known, which covers Material Transfer. Material Issue and
	Material Receipt supply only one side. Amounts are signed so Tally sees the
	issue as negative and the receipt as positive.
	"""
	root, message = _import_envelope("Vouchers", company)
	attributes, remote_id = _voucher_attributes(order, target_id)
	attributes["OBJVIEW"] = "Inventory Voucher View"
	voucher = _element(message, "VOUCHER", **attributes)
	_element(voucher, "GUID", remote_id)
	_element(voucher, "DATE", _date(order["transaction_date"]))
	_element(voucher, "EFFECTIVEDATE", _date(order["transaction_date"]))
	_element(voucher, "VOUCHERTYPENAME", "Stock Journal")
	_element(voucher, "VOUCHERNUMBER", order.get("source_name") or "")
	_element(voucher, "REFERENCE", order.get("source_name") or "")
	_element(voucher, "NARRATION", order.get("narration") or "")
	_element(voucher, "PERSISTEDVIEW", "Inventory Voucher View")
	_element(voucher, "ISOPTIONAL", "No")

	for item in order.get("items") or ():
		quantity = Decimal(str(item.get("quantity") or 0))
		rate = Decimal(str(item.get("rate") or 0))
		amount = Decimal(str(item.get("amount") or 0))
		unit = str(item.get("uom") or "").strip()
		quantity_text = _quantity(quantity, unit)
		for godown, outgoing in (
			(item.get("source_godown"), True),
			(item.get("target_godown"), False),
		):
			if not godown:
				continue
			signed = -amount if outgoing else amount
			entry = _element(voucher, "INVENTORYENTRIESOUT.LIST" if outgoing else "INVENTORYENTRIESIN.LIST")
			_element(entry, "STOCKITEMNAME", item.get("item") or "")
			_element(entry, "ISDEEMEDPOSITIVE", _yes_no(outgoing))
			_element(entry, "ACTUALQTY", quantity_text)
			_element(entry, "BILLEDQTY", quantity_text)
			_element(entry, "RATE", f"{_amount(rate)}/{unit}")
			_element(entry, "AMOUNT", _amount(signed))
			batch = _element(entry, "BATCHALLOCATIONS.LIST")
			_element(batch, "GODOWNNAME", godown)
			_element(batch, "BATCHNAME", "Primary Batch")
			_element(batch, "ACTUALQTY", quantity_text)
			_element(batch, "BILLEDQTY", quantity_text)
			_element(batch, "AMOUNT", _amount(signed))
	return ET.tostring(root, encoding="unicode")


def build_delivery_note_import(order, company, target_id):
	"""Build a stock-only Tally Delivery Note for an ERPNext Delivery Note.

	Tally treats a Delivery Note as an inventory voucher, so it carries no
	ledger entries. That also makes it valid for zero-value deliveries, which a
	Sales voucher cannot be: Tally rejects a zero-amount accounting voucher.
	"""
	root, message = _import_envelope("Vouchers", company)
	attributes, remote_id = _voucher_attributes({**order, "voucher_type": "Delivery Note"}, target_id)
	attributes["OBJVIEW"] = "Inventory Voucher View"
	voucher = _element(message, "VOUCHER", **attributes)
	_element(voucher, "GUID", remote_id)
	_element(voucher, "DATE", _date(order["transaction_date"]))
	_element(voucher, "VOUCHERTYPENAME", "Delivery Note")
	_element(voucher, "VOUCHERNUMBER", order["name"])
	_element(voucher, "REFERENCE", order.get("reference") or order["name"])
	_element(
		voucher,
		"NARRATION",
		order.get("narration")
		or f"ERPNext {order.get('source_doctype', 'Delivery Note')} {order['name']}",
	)
	_element(voucher, "PERSISTEDVIEW", "Inventory Voucher View")
	_element(voucher, "ISOPTIONAL", "No")

	for item in order["items"]:
		amount = Decimal(str(item.get("amount") or 0))
		quantity = _quantity(item["stock_qty"], item["stock_uom"])
		entry = _element(voucher, "ALLINVENTORYENTRIES.LIST")
		_element(entry, "STOCKITEMNAME", stock_item_name(item))
		_element(entry, "ISDEEMEDPOSITIVE", _yes_no(Decimal(str(item["stock_qty"])) < 0))
		_element(entry, "ACTUALQTY", quantity)
		_element(entry, "BILLEDQTY", quantity)
		_element(entry, "RATE", f"{_amount(item.get('rate'))}/{item['stock_uom']}")
		_element(entry, "AMOUNT", _amount(amount))
		batch = _element(entry, "BATCHALLOCATIONS.LIST")
		_element(batch, "GODOWNNAME", item.get("warehouse") or "Main Location")
		_element(batch, "BATCHNAME", "Primary Batch")
		_element(batch, "ACTUALQTY", quantity)
		_element(batch, "BILLEDQTY", quantity)
		_element(batch, "AMOUNT", _amount(amount))
	return ET.tostring(root, encoding="unicode")


def stock_item_name(item):
	# Tally collapses repeated spaces in master names. Match that so a payload
	# like "FC 4" 23/30 PC  - Proend" resolves to the existing Tally item.
	name = item.get("tally_name") or item.get("item_name") or item["item_code"]
	return re.sub(r"\s+", " ", str(name)).strip()


def _sales_inventory_entry(parent, tag, item, sales_ledger=None):
	amount = Decimal(str(item["amount"] or 0))
	entry = _element(parent, tag)
	_element(entry, "STOCKITEMNAME", stock_item_name(item))
	_element(entry, "ISDEEMEDPOSITIVE", _yes_no(Decimal(str(item["stock_qty"])) < 0))
	if amount or sales_ledger:
		_element(entry, "RATE", f"{_amount(item['rate'])}/{item['stock_uom']}")
		_element(entry, "AMOUNT", _amount(amount))
	qty = _quantity(item["stock_qty"], item["stock_uom"])
	_element(entry, "ACTUALQTY", qty)
	_element(entry, "BILLEDQTY", qty)
	if item.get("warehouse"):
		batch = _element(entry, "BATCHALLOCATIONS.LIST")
		_element(batch, "GODOWNNAME", item["warehouse"])
		_element(batch, "BATCHNAME", "Primary Batch")
		_element(batch, "DESTINATIONGODOWNNAME", item["warehouse"])
		if amount or sales_ledger:
			_element(batch, "AMOUNT", _amount(amount))
		_element(batch, "ACTUALQTY", qty)
		_element(batch, "BILLEDQTY", qty)
	if sales_ledger:
		_ledger_entry(entry, "ACCOUNTINGALLOCATIONS.LIST", sales_ledger, amount, include_zero=True)


def _ledger_entry(parent, tag, ledger, amount, *, party=False, include_zero=False):
	entry = _element(parent, tag)
	_element(entry, "LEDGERNAME", ledger)
	debit = _yes_no(amount < 0)
	_element(entry, "ISDEEMEDPOSITIVE", debit)
	if party:
		_element(entry, "ISPARTYLEDGER", "Yes")
		_element(entry, "ISLASTDEEMEDPOSITIVE", debit)
	if amount or include_zero:
		_element(entry, "AMOUNT", _amount(amount))
	return entry


def build_voucher_import(order, company, target_id):
	root, message = _import_envelope("Vouchers", company)
	attributes, remote_id = _voucher_attributes(order, target_id)
	is_invoice = bool(order.get("is_invoice"))
	items = order["items"]
	# Tally stores stock lines only when the voucher uses the item-invoice view.
	# The accounting view accepts the import but drops the inventory block, which
	# is why Delivery Notes posted without items. Use it for every stock voucher.
	item_invoice = bool(items)
	view = "Invoice Voucher View" if item_invoice or is_invoice else "Accounting Voucher View"
	attributes["OBJVIEW"] = view
	voucher = _element(message, "VOUCHER", **attributes)
	_element(voucher, "GUID", remote_id)
	_element(voucher, "DATE", _date(order["transaction_date"]))
	_element(voucher, "VOUCHERTYPENAME", attributes["VCHTYPE"])
	_element(voucher, "VOUCHERNUMBER", order["name"])
	if order["operation"] == "Cancel":
		return ET.tostring(root, encoding="unicode")

	_element(voucher, "REFERENCE", order.get("reference") or order["name"])
	_element(voucher, "PARTYNAME", order["customer"]["name"])
	_element(voucher, "PARTYLEDGERNAME", order["customer"]["name"])
	source_doctype = order.get("source_doctype", "Sales Order")
	narration = order.get("narration") or f"ERPNext {source_doctype} {order['name']}"
	if order.get("origin_marker"):
		narration = f"[ExpressTallyOrigin:{order['origin_marker']}]\n{narration}"
	_element(voucher, "NARRATION", narration)
	_element(voucher, "PERSISTEDVIEW", view)
	_element(voucher, "OBJVIEW", view)
	if item_invoice or is_invoice:
		_element(voucher, "VCHENTRYMODE", "Item Invoice")
	_element(voucher, "ISINVOICE", _yes_no(is_invoice))
	_element(voucher, "ISORDER", "No")
	_element(voucher, "ISOPTIONAL", "No")

	party_amount = -Decimal(str(order["grand_total"] or 0))
	party_entry = _ledger_entry(
		voucher, "LEDGERENTRIES.LIST", order["customer"]["name"], party_amount, party=True
	)
	if party_amount:
		bill = _element(party_entry, "BILLALLOCATIONS.LIST")
		_element(bill, "NAME", order["name"])
		_element(bill, "BILLTYPE", "New Ref")
		_element(bill, "AMOUNT", _amount(party_amount))

	sales_ledger = order["sales_ledger"]
	# Item-invoice vouchers derive the sales posting from each stock line's
	# accounting allocation, so they must not also carry a Sales ledger entry.
	if not item_invoice:
		net_total = Decimal(
			str(order.get("net_total") or sum(Decimal(str(item["amount"] or 0)) for item in items))
		)
		sales_entry = _ledger_entry(voucher, "LEDGERENTRIES.LIST", sales_ledger, net_total)
		for item in items:
			_sales_inventory_entry(sales_entry, "INVENTORYALLOCATIONS.LIST", item)

	for tax in order["taxes"]:
		_ledger_entry(
			voucher, "LEDGERENTRIES.LIST", tax["ledger"], Decimal(str(tax["amount"] or 0)),
			include_zero=True,
		)
	if order.get("rounding_adjustment"):
		_ledger_entry(
			voucher, "LEDGERENTRIES.LIST", order["round_off_ledger"],
			Decimal(str(order["rounding_adjustment"])),
		)

	if item_invoice:
		for item in items:
			# Zero-value lines keep quantities but omit zero allocations.
			allocation_ledger = sales_ledger if Decimal(str(item["amount"] or 0)) else None
			_sales_inventory_entry(
				voucher, "ALLINVENTORYENTRIES.LIST", item, allocation_ledger
			)
	return ET.tostring(root, encoding="unicode")


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


def _text(root, name, default=""):
	element = root.find(f".//{name}")
	return (element.text or default).strip() if element is not None else default


def parse_import_response(xml, require_change=False, allow_ignored=False):
	try:
		root = ET.fromstring(xml)
	except ET.ParseError as exc:
		raise ValueError(f"Tally returned invalid XML: {xml[:500]}") from exc
	tally_fields = ("CREATED", "ALTERED", "IGNORED", "ERRORS", "EXCEPTIONS", "COMBINED", "CANCELLED")
	elements = {field: root.find(f".//{field}") for field in tally_fields}
	message = "; ".join(
		element.text.strip() for element in root.iter()
		if element.tag in {"LINEERROR", "ERROR", "ERRORMSG"} and (element.text or "").strip()
	)
	data = root.find(".//DATA")
	if not message and data is not None and not len(data):
		message = (data.text or "").strip()
	if not message and root.tag == "RESPONSE" and not len(root):
		message = (root.text or "").strip()
	if not any(element is not None for element in elements.values()):
		return ImportResult(False, message=message or "Tally returned no import result")
	try:
		counts = {
			field: int(element.text or "0") if element is not None else 0
			for field, element in elements.items()
		}
	except ValueError:
		return ImportResult(False, message="Tally returned an invalid import count")
	if any(value < 0 for value in counts.values()):
		return ImportResult(False, message="Tally returned a negative import count")
	created, altered, ignored, errors, exceptions, combined, cancelled = counts.values()
	if not message and (errors or exceptions):
		message = f"Tally reported errors={errors}, exceptions={exceptions}"
	accepted = bool(created + altered + combined + cancelled or (allow_ignored and ignored))
	success = _text(root, "STATUS", "1") == "1" and not message and (not require_change or accepted)
	if not success and not message:
		message = f"Tally did not accept the import (ignored={ignored})"
	return ImportResult(
		success=success, created=created, altered=altered, ignored=ignored, errors=errors,
		last_voucher_id=_text(root, "LASTVCHID"), message=message, combined=combined,
	)


def parse_current_company(xml):
	try:
		root = ET.fromstring(xml)
	except ET.ParseError as exc:
		raise ValueError("Tally returned invalid XML") from exc
	return _text(root, "RESULT")


def parse_logical_result(xml):
	return parse_current_company(xml).strip().lower() in {"yes", "true", "1"}
