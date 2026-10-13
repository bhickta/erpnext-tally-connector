#!/usr/bin/env python3
"""Cancel archived accounting documents replaced by the verified Tally migration."""

import argparse
import json
from pathlib import Path

import frappe

from express_tally.bridge.migration_audit import snapshot_digest


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", required=True, type=Path)
	parser.add_argument("--site", default="rapl.localhost")
	parser.add_argument("--apply", action="store_true")
	args = parser.parse_args()
	root = args.root.resolve()
	archive = json.loads((root / "restored-financial-documents.json").read_text())
	verification = json.loads((root / "native-document-verification.json").read_text())
	if args.apply and not verification["all_financial_vouchers_verified"]:
		raise ValueError(
			"All replacement source vouchers must be verified before retiring restored accounting"
		)
	if args.apply and verification.get("source_snapshot_sha256") != snapshot_digest(
		[json.loads(path.read_text()) for path in (root / "voucher-cache").glob("*.json")]
	):
		raise ValueError("Replacement verification belongs to a different source snapshot")
	cfg = json.loads((root / "bridge-config-private.json").read_text())["config"]
	frappe.init(site=args.site)
	frappe.connect()
	frappe.set_user("Administrator")
	frappe.flags.mute_emails = True
	results = []
	try:
		# Remove bank movements before their opening journals so bank balance
		# restrictions remain valid throughout the replacement.
		order = {"Payment Entry": 0, "Sales Invoice": 1, "Purchase Invoice": 1, "Journal Entry": 2}
		for doctype, documents in sorted(archive.items(), key=lambda item: order.get(item[0], 3)):
			if doctype not in {"Payment Entry", "Journal Entry", "Sales Invoice", "Purchase Invoice"}:
				raise ValueError(f"Refusing to retire {doctype}")
			for original in documents:
				doc = frappe.get_doc(doctype, original["name"])
				if doc.company != cfg["erpnext_company"] or str(doc.creation) >= "2026-10-13":
					raise ValueError(f"Document is outside the archived restored baseline: {doc.name}")
				if frappe.db.exists("Stock Ledger Entry", {"voucher_type": doctype, "voucher_no": doc.name}):
					raise ValueError(f"Refusing to cancel a stock document: {doc.name}")
				if frappe.db.sql(
					"""SELECT gl.name FROM `tabGL Entry` gl JOIN `tabAccount` account
					ON account.name=gl.account WHERE gl.voucher_type=%s AND gl.voucher_no=%s
					AND gl.is_cancelled=0 AND account.account_type='Stock' LIMIT 1""",
					(doctype, doc.name),
				):
					raise ValueError(f"Refusing to cancel stock accounting: {doc.name}")
				has_gl = frappe.db.exists(
					"GL Entry",
					{
						"voucher_type": doctype,
						"voucher_no": doc.name,
						"is_cancelled": 0,
					},
				)
				if args.apply and doc.docstatus == 1 and has_gl:
					doc.flags.ignore_permissions = True
					doc._cancel()
					if doc.docstatus != 2:
						raise ValueError(f"Cancellation did not finish synchronously: {doc.name}")
					frappe.db.commit()
				results.append(
					{
						"doctype": doctype,
						"name": doc.name,
						"docstatus": doc.docstatus,
						"had_active_gl": bool(has_gl),
					}
				)
				(root / "retired-restored-accounting.json").write_text(
					json.dumps(
						{
							"applied": args.apply,
							"reason": "Replaced by Tally opening balances and complete source financial vouchers",
							"results": results,
						},
						indent=2,
					)
				)
		print("RESTORED ACCOUNTING", len(results), "APPLIED", args.apply)
	finally:
		frappe.db.rollback()
		frappe.destroy()


if __name__ == "__main__":
	main()
