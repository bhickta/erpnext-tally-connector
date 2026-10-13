#!/usr/bin/env python3
"""Exercise real native documents and their GL, rolling back every test case."""

import argparse
import json
from collections import Counter
from pathlib import Path

import frappe

from express_tally.bridge.bill_references import index_bill_sources
from express_tally.framework import FlowContext
from express_tally.integrations.native_transaction_flow import NativeTransactionsFromTallyFlow


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", required=True, type=Path)
	parser.add_argument("--site", default="rapl.localhost")
	parser.add_argument("--per-type", type=int, default=2)
	args = parser.parse_args()
	root = args.root.resolve()
	cfg = json.loads((root / "bridge-config-private.json").read_text())["config"]
	mapping = json.loads((root / "ledger-mappings.json").read_text())
	records = [json.loads(path.read_text()) for path in (root / "voucher-cache").glob("*.json")]
	records.sort(key=lambda record: (record["date"], int(record["master_id"])))
	context = FlowContext(
		cfg["erpnext_company"],
		cfg["target_id"],
		cfg["tally_company"],
		options={
			"ledger_mappings": mapping,
			"item_mappings": json.loads((root / "item-mappings.json").read_text()),
			"accounting_item": json.loads((root / "accounting-item.json").read_text())["item_code"],
			"historical_migration": True,
			"branch": "Real Appliances Private Limited",
			"bill_sources": index_bill_sources(records),
		},
	)
	frappe.init(site=args.site)
	frappe.connect()
	frappe.set_user("Administrator")
	frappe.flags.mute_emails = True
	flow = NativeTransactionsFromTallyFlow()
	seen = Counter()
	results = []
	try:
		for record in records:
			kind = record["voucher_type"]
			if (
				kind
				not in {
					"Sales",
					"Purchase",
					"Payment",
					"Receipt",
					"Journal",
					"Contra",
					"Credit Note",
					"Debit Note",
				}
				or seen[kind] >= args.per_type
			):
				continue
			if any(
				entry["ledger"] not in mapping or entry.get("bill_allocations")
				for entry in record["ledger_entries"]
			):
				continue
			seen[kind] += 1
			try:
				result = flow.receive(context, [record])[0]
				results.append({"id": record["master_id"], "type": kind, **result})
				print(json.dumps(results[-1]), flush=True)
			finally:
				frappe.db.rollback()
	finally:
		frappe.db.rollback()
		frappe.destroy()
	(root / "native-rollback-validation.json").write_text(
		json.dumps({"rolled_back": True, "tested_types": dict(seen), "results": results}, indent=2)
	)
	if len(seen) != 8 or any(result["status"] != "Success" for result in results):
		raise SystemExit(1)


if __name__ == "__main__":
	main()
