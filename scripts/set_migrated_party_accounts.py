#!/usr/bin/env python3
"""Set standard party account defaults to the verified migrated ledger accounts."""

import argparse
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import frappe
from erpnext.accounts.party import get_party_account


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", required=True, type=Path)
	parser.add_argument("--site", default="rapl.localhost")
	args = parser.parse_args()
	root = args.root.resolve()
	report = json.loads((root / "reconciliation-results.json").read_text())
	if not report["migration_complete"]:
		raise ValueError("Party defaults require complete financial migration verification")
	cfg = json.loads((root / "bridge-config-private.json").read_text())["config"]
	company = cfg["erpnext_company"]
	mapping = json.loads((root / "ledger-mappings.json").read_text())
	balances = {
		name: abs(Decimal(row["source_balance"]))
		for row in report["comparisons"]
		for name in row["tally_ledgers"]
	}
	usage = Counter(
		entry["ledger"]
		for path in (root / "voucher-cache").glob("*.json")
		for entry in json.loads(path.read_text()).get("ledger_entries", [])
	)
	parties = defaultdict(list)
	for name, value in mapping.items():
		if value.get("party"):
			parties[(value["party_type"], value["party"])].append(name)
	frappe.init(site=args.site)
	frappe.connect()
	frappe.set_user("Administrator")
	results = []
	try:
		for (party_type, party), names in sorted(parties.items()):
			existing = frappe.get_all(
				"Party Account",
				filters={"parenttype": party_type, "parent": party, "company": company},
				fields=["name", "account"],
			)
			current = existing[0].account if existing else None
			preferred_root = "Asset" if party_type == "Customer" else "Liability"
			chosen = min(
				names,
				key=lambda name: (
					mapping[name]["account"] != current,
					mapping[name]["root_type"] != preferred_root,
					not name.startswith(party),
					-usage[name],
					-balances.get(name, Decimal(0)),
					name,
				),
			)
			account = mapping[chosen]["account"]
			properties = frappe.db.get_value(
				"Account", account, ["company", "is_group", "disabled", "account_type"], as_dict=True
			)
			if (
				not properties
				or properties.company != company
				or properties.is_group
				or properties.disabled
				or properties.account_type != ("Receivable" if party_type == "Customer" else "Payable")
			):
				raise ValueError(f"Invalid default party account: {account}")
			if existing:
				for row in existing:
					frappe.db.set_value("Party Account", row.name, "account", account)
			else:
				frappe.get_doc(
					{
						"doctype": "Party Account",
						"parenttype": party_type,
						"parent": party,
						"parentfield": "accounts",
						"company": company,
						"account": account,
						"idx": frappe.db.count("Party Account", {"parenttype": party_type, "parent": party})
						+ 1,
					}
				).insert(ignore_permissions=True)
			frappe.clear_document_cache(party_type, party)
			if get_party_account(party_type, party, company) != account:
				raise ValueError(f"ERPNext did not resolve the migrated account for {party}")
			results.append(
				{
					"party_type": party_type,
					"party": party,
					"previous_accounts": [row.account for row in existing],
					"account": account,
					"source_ledger": chosen,
					"aliases": sorted(names),
				}
			)
		frappe.db.commit()
		(root / "migrated-party-defaults.json").write_text(
			json.dumps({"verified_parties": len(results), "results": results}, indent=2)
		)
		print("MIGRATED PARTY DEFAULTS VERIFIED", len(results))
	finally:
		frappe.db.rollback()
		frappe.destroy()


if __name__ == "__main__":
	main()
