#!/usr/bin/env python3
"""Print the Tally XML the bridge would send, without ERPNext or TallyPrime.

The bridge's XML gateways use only the Python standard library, so this script
runs from a plain checkout with no ``bench`` installation:

    python scripts/preview_xml.py
    python scripts/preview_xml.py --payload my-order.json -o voucher.xml

The payload is the dictionary produced by the ERPNext flow (see
``express_tally/tests/test_bridge.py::sample_order`` for the full shape).
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Allow running from any directory without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from express_tally.bridge.xml_gateway import (  # noqa: E402
	build_delivery_note_import,
	build_master_import,
	build_unit_import,
	build_voucher_import,
)


def sample_order():
	return {
		"source_doctype": "Sales Order",
		"name": "SAL-ORD-2026-00001",
		"modified": "2026-08-28 12:00:00",
		"source_hash": "hash-1",
		"operation": "Create",
		"transaction_date": "2026-08-28",
		"delivery_date": "2026-09-05",
		"customer": {"id": "CUST-1", "name": "Customer One", "gstin": "", "country": "India"},
		"currency": "INR",
		"reference": "PO-1",
		"narration": "Test order",
		"net_total": 250,
		"grand_total": 295,
		"rounding_adjustment": 0,
		"sales_ledger": "Sales",
		"round_off_ledger": "Round Off",
		"taxes": [{"ledger": "Output IGST", "amount": 45}],
		"items": [
			{
				"item_code": "ITEM-1",
				"item_name": "Item One",
				"description": "Item One",
				"item_group": "Products",
				"hsn_code": "1234",
				"stock_uom": "Nos",
				"stock_qty": 2,
				"rate": 125,
				"amount": 250,
				"warehouse": "Stores",
				"delivery_date": "2026-09-05",
			}
		],
		"masters": {
			"item_groups": [{"name": "Products", "parent": ""}],
			"units": ["Nos"],
			"warehouses": ["Stores"],
		},
	}


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--payload",
		help="JSON file containing the flow payload; defaults to a built-in sample",
	)
	parser.add_argument("--company", default="Test Company")
	parser.add_argument("--target-id", default="target-1")
	parser.add_argument("--kind", choices=("sales", "delivery-note", "masters", "unit"), default="sales")
	parser.add_argument("--invoice", action="store_true", help="Use the Sales Invoice view")
	parser.add_argument("-o", "--output", help="Write XML to this file instead of stdout")
	args = parser.parse_args(argv)

	payload = json.loads(Path(args.payload).read_text(encoding="utf-8")) if args.payload else sample_order()
	if args.kind == "unit":
		unit_name = payload if isinstance(payload, str) else payload.get("unit", "Nos")
		xml = build_unit_import(args.company, [{"name": unit_name}])
	elif args.kind == "masters":
		xml = build_master_import(payload, args.company)
	elif args.kind == "delivery-note":
		xml = build_delivery_note_import(payload, args.company, args.target_id)
	else:
		payload = dict(payload, is_invoice=bool(args.invoice or payload.get("is_invoice")))
		xml = build_voucher_import(payload, args.company, args.target_id)

	# Indent for readability. The bridge sends the compact form; whitespace
	# between elements does not change Tally's interpretation.
	root = ET.fromstring(xml)
	ET.indent(root)
	xml = ET.tostring(root, encoding="unicode")

	if args.output:
		Path(args.output).write_text(xml, encoding="utf-8")
	else:
		print(xml)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
