"""Balanced accounting vouchers for payments, receipts, and bank transfers."""

import xml.etree.ElementTree as ET
from collections import Counter
from decimal import Decimal

from .collection_gateway import nested_records, scalar
from .ledger_gateway import debit_balance
from .voucher_readback import IDENTITY_FIELDS, read_voucher, read_vouchers, tally_xml_date
from .xml_gateway import _amount, _date, _element, _import_envelope, _voucher_attributes, _yes_no, voucher_remote_id


def validate_accounting_voucher(document):
	if document.get("voucher_type") not in {"Payment", "Receipt", "Contra", "Journal"}:
		raise ValueError("Unsupported accounting voucher type")
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


def validate_payment(document):
	if document.get("voucher_type") not in {"Payment", "Receipt", "Contra"}:
		raise ValueError("Unsupported payment voucher type")
	validate_accounting_voucher(document)


def build_payment_import(document, company, target_id):
	validate_payment(document)
	return build_accounting_import(document, company, target_id)


def build_accounting_import(document, company, target_id):
	validate_accounting_voucher(document)
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


ACCOUNTING_FIELDS = (
	*IDENTITY_FIELDS,
	*(
		f"{entries}.{field}"
		for entries in ("AllLedgerEntries", "LedgerEntries")
		for field in ("LedgerName", "Amount", "BillAllocations.Name", "BillAllocations.BillType", "BillAllocations.Amount")
	),
)


def read_accounting_voucher(tally, company, document, target_id):
	reference = document.get("tally_voucher_id")
	if reference:
		return read_voucher(tally, company, str(reference), fields=ACCOUNTING_FIELDS)
	marker = f"[ExpressTallySource:{voucher_remote_id(document, target_id)}]"
	rows = read_vouchers(tally, company, [f'$Narration Contains "{marker}"'], fields=ACCOUNTING_FIELDS)
	if len(rows) > 1 or (rows and marker not in scalar(rows[0], "narration").splitlines()):
		raise ValueError("Tally returned an ambiguous accounting voucher identity")
	return rows[0] if rows else None


def verify_accounting_voucher(row, document):
	if not row:
		raise ValueError("Tally did not return the saved accounting voucher")
	master_id = scalar(row, "masterid")
	is_cancelled = scalar(row, "iscancelled").casefold() in {"yes", "true", "1"}
	if (
		not master_id.isascii() or not master_id.isdecimal() or int(master_id) < 1
		or scalar(row, "vouchertypename").casefold() != document["voucher_type"].casefold()
	):
		raise ValueError("Tally returned an unexpected accounting voucher identity")
	if document["operation"] == "Cancel":
		if not is_cancelled:
			raise ValueError(f"Tally voucher {master_id} is still active")
		return master_id
	if is_cancelled or scalar(row, "isoptional").casefold() in {"yes", "true", "1"}:
		raise ValueError(f"Tally voucher {master_id} is cancelled or optional")
	if tally_xml_date(scalar(row, "date")) != _date(document["transaction_date"]):
		raise ValueError(f"Tally voucher {master_id} has a different posting date")

	expected = Counter(
		(
			line["ledger"].strip().casefold(), Decimal(_amount(line["amount"])),
			tuple(sorted(
				(str(bill.get("name") or "").strip(), bill["bill_type"].strip().casefold(), Decimal(_amount(bill["amount"])))
				for bill in line.get("bill_allocations") or ()
			)),
		)
		for line in document["ledger_entries"]
	)
	lines = nested_records(row, "AllLedgerEntries.LIST") or nested_records(row, "LedgerEntries.LIST")
	actual = Counter(
		(
			scalar(line, "ledgername").casefold(), -debit_balance(scalar(line, "amount")),
			tuple(sorted(
				(scalar(bill, "name"), scalar(bill, "billtype").casefold(), -debit_balance(scalar(bill, "amount")))
				for bill in nested_records(line, "BillAllocations.LIST")
			)),
		)
		for line in lines
	)
	if actual != expected:
		raise ValueError(f"Tally voucher {master_id} did not retain the requested ledger entries and bill allocations")
	return master_id
