"""Balanced accounting vouchers for payments, receipts, and bank transfers."""

import xml.etree.ElementTree as ET
from decimal import Decimal

from .xml_gateway import _amount, _date, _element, _import_envelope, _voucher_attributes, _yes_no


def validate_payment(document):
	if document.get("voucher_type") not in {"Payment", "Receipt", "Contra"}:
		raise ValueError("Unsupported payment voucher type")
	if document.get("operation") not in {"Create", "Alter", "Cancel"}:
		raise ValueError("Unsupported payment operation")
	if document["operation"] == "Cancel":
		if not document.get("tally_voucher_id"):
			raise ValueError("Cancelling a payment requires its Tally voucher identity")
		return
	entries = document.get("ledger_entries") or []
	if len(entries) < 2 or any(not row.get("ledger") for row in entries):
		raise ValueError("Payment requires at least two named ledger entries")
	amounts = [Decimal(_amount(row.get("amount"))) for row in entries]
	if not all(amount.is_finite() for amount in amounts) or sum(amounts) != 0 or not any(amounts):
		raise ValueError("Payment ledger entries must balance")
	for row, amount in zip(entries, amounts, strict=True):
		bills = row.get("bill_allocations") or []
		if bills and sum((Decimal(_amount(b.get("amount"))) for b in bills), Decimal(0)) != amount:
			raise ValueError("Payment bill allocations must match their ledger amount")


def build_payment_import(document, company, target_id):
	validate_payment(document)
	root, message = _import_envelope("Vouchers", company)
	attributes, remote_id = _voucher_attributes(document, target_id)
	voucher = _element(message, "VOUCHER", **attributes)
	_element(voucher, "GUID", remote_id)
	_element(voucher, "DATE", _date(document["transaction_date"]))
	_element(voucher, "VOUCHERTYPENAME", document["voucher_type"])
	_element(voucher, "VOUCHERNUMBER", document["name"])
	_element(voucher, "REFERENCE", document.get("reference") or document["name"])
	_element(voucher, "PERSISTEDVIEW", "Accounting Voucher View")
	_element(voucher, "ISINVOICE", "No")
	_element(voucher, "ISOPTIONAL", "No")
	if document["operation"] == "Cancel":
		_element(voucher, "ISCANCELLED", "Yes")
		return ET.tostring(root, encoding="unicode")
	if document.get("party_ledger"):
		_element(voucher, "PARTYLEDGERNAME", document["party_ledger"])
	narration = document.get("narration") or f"ERPNext Payment Entry {document['name']}"
	if document.get("origin_marker"):
		narration = f"[ExpressTallyOrigin:{document['origin_marker']}]\n{narration}"
	narration = f"[ExpressTallySource:{remote_id}]\n{narration}"
	_element(voucher, "NARRATION", narration)
	for row in document["ledger_entries"]:
		amount = Decimal(str(row["amount"]))
		entry = _element(voucher, "ALLLEDGERENTRIES.LIST")
		_element(entry, "LEDGERNAME", row["ledger"])
		_element(entry, "ISDEEMEDPOSITIVE", _yes_no(amount < 0))
		_element(entry, "ISPARTYLEDGER", _yes_no(row.get("is_party")))
		_element(entry, "AMOUNT", _amount(amount))
		for bill in row.get("bill_allocations") or []:
			allocation = _element(entry, "BILLALLOCATIONS.LIST")
			if bill.get("name"):
				_element(allocation, "NAME", bill["name"])
			_element(allocation, "BILLTYPE", bill["bill_type"])
			_element(allocation, "AMOUNT", _amount(bill["amount"]))
	return ET.tostring(root, encoding="unicode")


def build_payment_masters(document, company):
	return {
		"static_variables": [
			{"name": "svMstImportFormat", "value": "jsonex"},
			{"name": "svCurrentCompany", "value": company},
		],
		"tallymessage": [
			{
				"metadata": {"type": "Ledger", "action": "create", "name": master["name"]},
				"name": master["name"],
				"parent": master["parent"],
				"isbillwiseon": bool(master.get("billwise")),
				"affectsstock": False,
			}
			for master in document.get("masters") or []
		],
	}
