#!/usr/bin/env python3
"""Read-only verification of every source financial voucher and its posted GL."""

import argparse
import csv
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import frappe

from express_tally.bridge.migration_audit import snapshot_digest
from express_tally.framework import FlowContext
from express_tally.integrations.native_transaction_flow import FLOW, NativeTransactionsFromTallyFlow, money
from express_tally.integrations.tally_inbound_flows import _source_hash

FINANCIAL_TYPES = {
	"Sales",
	"Purchase",
	"Credit Note",
	"Debit Note",
	"Journal",
	"Contra",
	"Receipt",
	"Payment",
}
DOCUMENT_TYPES = {"Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry"}


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", required=True, type=Path)
	parser.add_argument("--site", default="rapl.localhost")
	args = parser.parse_args()
	root = args.root.resolve()
	cfg = json.loads((root / "bridge-config-private.json").read_text())["config"]
	context = FlowContext(
		cfg["erpnext_company"],
		cfg["target_id"],
		cfg["tally_company"],
		options={
			"ledger_mappings": json.loads((root / "ledger-mappings.json").read_text()),
		},
	)
	records = [json.loads(path.read_text()) for path in (root / "voucher-cache").glob("*.json")]
	source_digest = snapshot_digest(records)
	records = [record for record in records if record["voucher_type"] in FINANCIAL_TYPES]
	frappe.init(site=args.site)
	frappe.connect()
	try:
		logs = frappe.get_all(
			"Tally Sync Log",
			filters={
				"flow_key": FLOW,
				"target_id": context.target_id,
				"direction": "Tally to ERPNext",
				"status": "Success",
			},
			fields=["source_reference", "source_hash", "source_doctype", "target_reference"],
		)
		by_source = defaultdict(list)
		for log in logs:
			by_source[(log.source_reference, log.source_hash)].append(log)
		documents = {}
		for doctype in DOCUMENT_TYPES:
			for doc in frappe.get_all(
				doctype, filters={"company": context.company}, fields=["name", "docstatus", "posting_date"]
			):
				documents[(doctype, doc.name)] = doc
		actual = defaultdict(lambda: defaultdict(Decimal))
		for row in frappe.db.sql(
			"""SELECT voucher_type, voucher_no, account,
			COALESCE(party_type,'') AS party_type, COALESCE(party,'') AS party,
			SUM(debit-credit) AS balance FROM `tabGL Entry`
			WHERE company=%s AND is_cancelled=0
			GROUP BY voucher_type, voucher_no, account, party_type, party""",
			context.company,
			as_dict=True,
		):
			actual[(row.voucher_type, row.voucher_no)][(row.account, row.party_type, row.party)] += money(
				row.balance
			)
		flow = NativeTransactionsFromTallyFlow()
		results = []
		used_documents = {}
		for record in sorted(records, key=lambda item: int(item["master_id"])):
			row = {
				"tally_id": record["master_id"],
				"voucher_type": record["voucher_type"],
				"date": record["date"],
				"status": "Verified",
				"doctype": "",
				"name": "",
				"issues": [],
			}
			matches = by_source[(record["_tally_key"], _source_hash(record))]
			if len(matches) != 1:
				row["issues"].append(f"Expected one current source-version success log; found {len(matches)}")
			else:
				log = matches[0]
				key = (log.source_doctype, log.target_reference)
				row.update(doctype=key[0] or "", name=key[1] or "")
				zero_value = bool(record.get("ledger_entries")) and all(
					money(entry["amount"]) == 0 for entry in record["ledger_entries"]
				)
				if record.get("optional") or zero_value:
					if any(key):
						row["issues"].append("Optional source voucher unexpectedly created a document")
					row["status"] = "Optional" if record.get("optional") else "Zero value"
				else:
					if key in used_documents:
						row["issues"].append(f"Document also belongs to source {used_documents[key]}")
					used_documents[key] = record["master_id"]
					doc = documents.get(key)
					if not doc or doc.docstatus != 1:
						row["issues"].append("Missing submitted standard accounting document")
					elif str(doc.posting_date) != record["date"]:
						row["issues"].append("Posting date differs from source")
					posted = {account: amount for account, amount in actual[key].items() if amount}
					if posted != flow.expected_gl(context, record):
						row["issues"].append("Posted account/party GL differs from source")
			if row["issues"]:
				row["status"] = "Failed"
			results.append(row)
		report = {
			"source_snapshot_sha256": source_digest,
			"financial_source_vouchers": len(records),
			"status_counts": dict(Counter(row["status"] for row in results)),
			"document_types": dict(Counter(row["doctype"] for row in results if row["doctype"])),
			"all_financial_vouchers_verified": bool(results) and all(not row["issues"] for row in results),
			"results": results,
		}
		(root / "native-document-verification.json").write_text(json.dumps(report, indent=2))
		with (root / "native-document-verification.csv").open("w", newline="") as output:
			writer = csv.DictWriter(output, fieldnames=list(results[0]) if results else [])
			writer.writeheader()
			writer.writerows(results)
		print(json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2))
		if not report["all_financial_vouchers_verified"]:
			raise SystemExit(1)
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
