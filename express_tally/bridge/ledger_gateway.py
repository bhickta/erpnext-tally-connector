"""Financial ledger master payloads and opening-balance verification."""

import json
import re
import xml.etree.ElementTree as ET
from decimal import Decimal

from .collection_gateway import build_collection_export, parse_collection_export, scalar
from .xml_gateway import _amount, _element, _import_envelope, _yes_no


def debit_balance(value):
	text = str(value or "0").replace(",", "")
	match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
	amount = Decimal(match.group(0)) if match else Decimal(0)
	if text.strip().lower().endswith("dr"):
		return abs(amount)
	if text.strip().lower().endswith("cr"):
		return -abs(amount)
	return -amount


def read_ledger(tally_client, company, name, *, from_date=None, to_date=None):
	request = build_collection_export(
		company,
		"ETFinancialLedgerIdentity",
		"Ledger",
		("Name", "Parent", "OpeningBalance", "ClosingBalance", "GUID"),
		[f"$Name = {json.dumps(name)}"],
		static_variables={
			key: str(value).replace("-", "")
			for key, value in (("SVFROMDATE", from_date), ("SVTODATE", to_date))
			if value
		},
	)
	rows = [
		row
		for row in parse_collection_export(tally_client.export_collection(request), ("LEDGER",))
		if scalar(row, "name", "_name")
	]
	if len(rows) > 1:
		raise ValueError("Multiple Tally ledgers matched the selected name")
	return rows[0] if rows else None


def build_ledger_import(document, company, *, existing=False, update_opening=True):
	amount = Decimal(str(document["opening_balance"]))
	if not amount.is_finite() or not document.get("ledger_name") or not document.get("parent"):
		raise ValueError("A ledger needs a name, group, and finite opening balance")
	root, message = _import_envelope("All Masters", company)
	if existing:
		root.find(".//IMPORTDUPS").text = "@@DUPMODIFY"
	ledger = _element(
		message,
		"LEDGER",
		name=document.get("target_ledger") or document["ledger_name"],
		action="Alter" if existing else "Create",
	)
	_element(ledger, "NAME", document["ledger_name"])
	_element(ledger, "PARENT", document["parent"])
	_element(ledger, "ISBILLWISEON", _yes_no(document.get("billwise")))
	_element(ledger, "AFFECTSSTOCK", "No")
	if update_opening:
		_element(ledger, "OPENINGBALANCE", _amount(-amount))
	return ET.tostring(root, encoding="unicode")
