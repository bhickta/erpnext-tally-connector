#!/usr/bin/env python3
"""Resumable, audited Tally master and native-document migration."""

import argparse
import json
import re
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

import frappe

from express_tally.bridge.bill_references import index_bill_sources
from express_tally.bridge.clients import TallyClient
from express_tally.bridge.collection_gateway import build_collection_export, parse_collection_export, scalar
from express_tally.bridge.inbound_profiles import NATIVE_TRANSACTION_FIELDS, _primary_group, normalize_voucher
from express_tally.bridge.migration_audit import active_business_vouchers, audit_snapshot, snapshot_digest
from express_tally.framework import FlowContext
from express_tally.integrations.native_transaction_flow import NativeTransactionsFromTallyFlow
from express_tally.integrations.tally_inbound_flows import _source_hash, _success_result

ROOTS = {
	"Bank Accounts": ("Asset", "Bank"),
	"Cash-in-Hand": ("Asset", "Cash"),
	"Bank OD A/c": ("Liability", "Bank"),
	"Capital Account": ("Liability", ""),
	"Current Assets": ("Asset", ""),
	"Current Liabilities": ("Liability", ""),
	"Deposits (Asset)": ("Asset", ""),
	"Direct Expenses": ("Expense", ""),
	"Direct Incomes": ("Income", ""),
	"Duties & Taxes": ("Liability", "Tax"),
	"Fixed Assets": ("Asset", ""),
	"Indirect Expenses": ("Expense", ""),
	"Indirect Incomes": ("Income", ""),
	"Investments": ("Asset", ""),
	"Loans & Advances (Asset)": ("Asset", ""),
	"Loans (Liability)": ("Liability", ""),
	"Misc. Expenses (ASSET)": ("Asset", ""),
	"Purchase Accounts": ("Expense", ""),
	"Reserves & Surplus": ("Liability", ""),
	"Sales Accounts": ("Income", ""),
	"Secured Loans": ("Liability", ""),
	"Stock-in-Hand": ("Asset", "Stock"),
	"Suspense A/c": ("Asset", ""),
	"Unsecured Loans": ("Liability", ""),
}


def main():
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument(
		"action", choices=["audit", "masters", "items", "export", "import", "openings", "reconcile"]
	)
	p.add_argument("--site", default="rapl.localhost")
	p.add_argument("--root", required=True, type=Path)
	p.add_argument("--limit", type=int, default=0)
	p.add_argument("--types", default="")
	p.add_argument(
		"--skip-ambiguous",
		action="store_true",
		help="Save partial master mappings and report unresolved parties; financial imports remain guarded",
	)
	args = p.parse_args()
	root = args.root.resolve()
	root.mkdir(parents=True, exist_ok=True)
	if args.action == "audit":
		report = audit_snapshot(root)
		(root / "migration-source-audit.json").write_text(json.dumps(report, indent=2))
		print(json.dumps({key: value for key, value in report.items() if key != "issues"}, indent=2))
		print("AUDIT ISSUES", Counter(issue["kind"] for issue in report["issues"]))
		return
	if args.action in {"import", "openings"}:
		report = audit_snapshot(root)
		(root / "migration-source-audit.json").write_text(json.dumps(report, indent=2))
		if not report["source_and_mapping_checks_passed"]:
			raise ValueError(
				"Financial import blocked by incomplete or ambiguous source mappings; see migration-source-audit.json"
			)
	private = json.loads((root / "bridge-config-private.json").read_text())
	cfg = private["config"]
	frappe.init(site=args.site)
	frappe.connect()
	frappe.set_user("Administrator")
	frappe.flags.mute_emails = True
	company = cfg["erpnext_company"]
	tally = TallyClient("http://172.17.0.1:19000", 120)

	def save(name, value):
		if name == "bill-reference-review.json" and (root / name).exists():
			previous = json.loads((root / name).read_text())
			value = list({json.dumps(row, sort_keys=True): row for row in [*previous, *value]}.values())
		(root / name).write_text(json.dumps(value, indent=2, default=str))

	def collect(kind, fields):
		path = root / f"all-{kind.lower()}.json"
		if path.exists():
			return json.loads(path.read_text())
		request = build_collection_export(
			cfg["tally_company"], "ETMigration" + kind.replace(" ", ""), kind, fields
		)
		rows = [
			r
			for r in parse_collection_export(tally.export_collection(request), [kind.replace(" ", "")])
			if scalar(r, "name", "_name")
		]
		save(path.name, rows)
		print("EXPORTED MASTER", kind, len(rows), flush=True)
		return rows

	if args.action == "masters":
		groups = collect("Group", ["Name", "Parent", "GUID", "AlterID", "IsRevenue", "IsDeemedPositive"])
		by_group = {
			scalar(g, "name", "_name").casefold(): {
				"name": scalar(g, "name", "_name"),
				"parent": scalar(g, "parent"),
				"is_revenue": scalar(g, "is_revenue"),
				"is_debit": scalar(g, "is_deemed_positive"),
			}
			for g in groups
		}
		ledgers = collect(
			"Ledger",
			["Name", "Parent", "GUID", "AlterID", "PartyGSTIN", "GSTRegistrationType", "IsBillWiseOn"],
		)
		mapping = {}
		unresolved = []
		from express_tally.bridge.inbound_profiles import tally_balance

		used_ledgers = {
			entry["ledger"]
			for path in (root / "voucher-cache").glob("*.json")
			for entry in json.loads(path.read_text()).get("ledger_entries", [])
		}
		invoice_roles = {}
		for path in (root / "voucher-cache").glob("*.json"):
			record = json.loads(path.read_text())
			if record["voucher_type"] in {"Sales", "Purchase"}:
				invoice_roles.setdefault(record["party_ledger"], set()).add(
					"Customer" if record["voucher_type"] == "Sales" else "Supplier"
				)
		zero_snapshot = {}
		for label in ("opening", "closing"):
			zero_snapshot[label] = {
				scalar(row, "name", "_name"): tally_balance(scalar(row, "closing_balance"))
				for row in json.loads((root / f"verified-{label}-ledgers.json").read_text())
			}
		account_groups = {}
		root_accounts = {
			r.root_type: r.name
			for r in frappe.get_all(
				"Account",
				filters={"company": company, "is_group": 1, "parent_account": ["is", "not set"]},
				fields=["name", "root_type"],
			)
		}

		def group_account(name):
			if name in account_groups:
				return account_groups[name]
			existing = frappe.db.get_value(
				"Account",
				{"company": company, "account_name": ["in", [name, name + " (Tally Group)"]], "is_group": 1},
				"name",
			)
			if existing:
				account_groups[name] = existing
				return existing
			g = by_group.get(name.casefold())
			if not g:
				raise ValueError(f"Missing Tally parent group {name}")
			parent = g["parent"]
			if parent in {"", "Primary"}:
				root_type, kind = ROOTS.get(
					name,
					(
						("Expense" if g["is_debit"] == "Yes" else "Income")
						if g["is_revenue"] == "Yes"
						else ("Asset" if g["is_debit"] == "Yes" else "Liability"),
						"",
					),
				)
				parent_account = root_accounts[root_type]
			else:
				parent_account = group_account(parent)
				kind = ""
			label = (
				name + " (Tally Group)"
				if frappe.db.exists("Account", {"company": company, "account_name": name})
				else name
			)
			doc = frappe.get_doc(
				{
					"doctype": "Account",
					"company": company,
					"account_name": label,
					"parent_account": parent_account,
					"is_group": 1,
					"account_type": kind,
				}
			)
			doc.insert(ignore_permissions=True)
			account_groups[name] = doc.name
			return doc.name

		def migration_group(doctype, parent):
			name = "Tally Migration"
			if not frappe.db.exists(doctype, name):
				field = frappe.get_meta(doctype).autoname.removeprefix("field:")
				parent_field = "parent_" + doctype.lower().replace(" ", "_")
				frappe.get_doc(
					{
						"doctype": doctype,
						field: name,
						parent_field: parent,
						"is_group": 0,
						"abbreviation": "TALLY",
					}
				).insert(ignore_permissions=True)
			return name

		def party_name(typ, name, raw):
			field = "customer" if typ == "Customer" else "supplier"
			if frappe.db.exists("DocType", "Tally Party Name"):
				value = frappe.db.get_value("Tally Party Name", {"party_name": name}, field)
				if value:
					return value
			match = re.match(r"^([A-Z]+-[A-Z0-9]+-[0-9]+)(?:\s|-|$)", name)
			if match and frappe.db.exists(typ, match.group(1)):
				return match.group(1)
			title = "customer_name" if typ == "Customer" else "supplier_name"
			existing = frappe.get_all(typ, filters={title: name}, pluck="name")
			if len(existing) > 1:
				raise ValueError(f"Ambiguous existing {typ} name: {name}")
			if existing:
				return existing[0]
			gstin = scalar(raw, "party_gstin")
			if gstin:
				matches = frappe.get_all(typ, filters={"gstin": gstin}, fields=["name", title])
				if len(matches) > 1:

					def canonical(value):
						return "".join(char for char in value.casefold() if char.isalnum())

					matches = [row for row in matches if canonical(row[title]) == canonical(name)]
					if len(matches) != 1:
						raise ValueError(f"Ambiguous existing {typ} GSTIN for {name}")
				if matches:
					return matches[0].name
			values = {"doctype": typ, title: name, "gst_category": "Unregistered"}
			if gstin:
				registration = scalar(raw, "gst_registration_type")
				if registration not in {"Regular", "Composition"}:
					raise ValueError(f"Unsupported GST registration type for {name}: {registration}")
				values.update(
					gstin=gstin,
					gst_category="Registered Composition"
					if registration == "Composition"
					else "Registered Regular",
				)
			if typ == "Customer":
				values.update(
					customer_type="Company",
					customer_group=migration_group("Customer Group", "All Customer Groups"),
					territory="India",
				)
			else:
				values.update(
					supplier_type="Company",
					supplier_group=migration_group("Supplier Group", "All Supplier Groups"),
					country="India",
				)
			doc = frappe.get_doc(values)
			doc.insert(ignore_permissions=True)
			return doc.name

		for index, raw in enumerate(ledgers):
			name = scalar(raw, "name", "_name")
			primary = _primary_group(scalar(raw, "parent"), by_group)
			if primary in {"Sundry Debtors", "Sundry Creditors"}:
				typ = "Customer" if primary == "Sundry Debtors" else "Supplier"
				frappe.db.savepoint("migration_party")
				try:
					party = party_name(typ, name, raw)
					if not frappe.db.exists(typ, party):
						raise ValueError(f"Party alias points to missing {typ}: {party}")
				except Exception as exc:
					frappe.db.rollback(save_point="migration_party")
					if (
						"gstin" in str(exc).casefold()
						and name not in used_ledgers
						and name in zero_snapshot["opening"]
						and name in zero_snapshot["closing"]
						and not zero_snapshot["opening"][name]
						and not zero_snapshot["closing"][name]
					):
						# A dormant ledger can be preserved as a standard zero-balance
						# Account without inventing a valid tax identity or a party.
						account = frappe.db.get_value(
							"Account", {"company": company, "account_name": name, "is_group": 0}, "name"
						)
						if not account:
							account = (
								frappe.get_doc(
									{
										"doctype": "Account",
										"company": company,
										"account_name": name,
										"parent_account": group_account(scalar(raw, "parent")),
										"account_type": "Receivable" if typ == "Customer" else "Payable",
										"is_group": 0,
									}
								)
								.insert(ignore_permissions=True)
								.name
							)
						mapping[name] = {
							"account": account,
							"primary_group": primary,
							"inactive_invalid_gstin": scalar(raw, "party_gstin"),
							**frappe.db.get_value(
								"Account", account, ["root_type", "account_type"], as_dict=True
							),
						}
						continue
					if not args.skip_ambiguous:
						raise
					unresolved.append({"ledger": name, "party_type": typ, "error": str(exc)})
					continue
				frappe.db.set_value(typ, party, "disabled", 0)
				account = frappe.db.get_value(
					"Account", {"company": company, "account_name": name, "is_group": 0}, "name"
				)
				if not account:
					account = (
						frappe.get_doc(
							{
								"doctype": "Account",
								"company": company,
								"account_name": name,
								"parent_account": group_account(scalar(raw, "parent")),
								"account_type": "Receivable" if typ == "Customer" else "Payable",
								"is_group": 0,
							}
						)
						.insert(ignore_permissions=True)
						.name
					)
				else:
					doc = frappe.get_doc("Account", account)
					doc.account_type = "Receivable" if typ == "Customer" else "Payable"
					doc.save(ignore_permissions=True)
				mapping[name] = {
					"account": account,
					"party_type": typ,
					"party": party,
					"primary_group": primary,
				}
			else:
				parent = (
					root_accounts["Liability"]
					if scalar(raw, "parent") == "Primary"
					else group_account(scalar(raw, "parent"))
				)
				account = frappe.db.get_value(
					"Account",
					{
						"company": company,
						"account_name": ["in", [name, name + " (Tally Ledger)"]],
						"is_group": 0,
					},
					"name",
				)
				if (
					not account
					and primary == "Cash-in-Hand"
					and re.sub(r"[^a-z]", "", name.lower()) in {"cash", "cashinhand"}
				):
					account = frappe.get_cached_value("Company", company, "default_cash_account")
				account_type = ROOTS.get(primary, ("", ""))[1]
				if "round" in name.casefold():
					account_type = "Round Off"
				if not account:
					label = (
						name + " (Tally Ledger)"
						if frappe.db.exists("Account", {"company": company, "account_name": name})
						else name
					)
					doc = frappe.get_doc(
						{
							"doctype": "Account",
							"company": company,
							"account_name": label,
							"parent_account": parent,
							"is_group": 0,
							"account_type": account_type,
						}
					)
					doc.insert(ignore_permissions=True)
					account = doc.name
				if account_type and frappe.db.get_value("Account", account, "account_type") != account_type:
					doc = frappe.get_doc("Account", account)
					doc.account_type = account_type
					doc.save(ignore_permissions=True)
				mapping[name] = {"account": account, "primary_group": primary}
				if name in invoice_roles and primary not in {"Cash-in-Hand", "Bank Accounts", "Bank OD A/c"}:
					typ = "Customer" if "Customer" in invoice_roles[name] else "Supplier"
					party = party_name(typ, name, raw)
					account_type = "Receivable" if typ == "Customer" else "Payable"
					doc = frappe.get_doc("Account", account)
					doc.account_type = account_type
					doc.save(ignore_permissions=True)
					frappe.db.set_value(typ, party, "disabled", 0)
					mapping[name].update(party_type=typ, party=party)
			mapping[name].update(
				frappe.db.get_value(
					"Account", mapping[name]["account"], ["root_type", "account_type"], as_dict=True
				)
			)
			if index % 100 == 0:
				frappe.db.commit()
				print("MAPPED LEDGERS", index + 1, len(ledgers), flush=True)
		frappe.db.commit()
		save("ledger-mappings.json", mapping)
		save("unresolved-master-mappings.json", unresolved)
		print("LEDGER MAPPING SAVED", len(mapping), "UNRESOLVED", len(unresolved), flush=True)

	if args.action in {"masters", "items"}:
		stock_items = collect("Stock Item", ["Name", "Parent", "BaseUnits", "PartNo", "GUID", "AlterID"])
		item_mapping = {}
		for index, raw in enumerate(stock_items):
			name = scalar(raw, "name", "_name")
			item = frappe.db.get_value("Item", {"item_name": name}, "name") or (
				name if frappe.db.exists("Item", name) else None
			)
			if not item:
				uom = scalar(raw, "base_units") or "Nos"
				if not frappe.db.exists("UOM", uom):
					frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)
				doc = frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": name,
						"item_name": name,
						"item_group": "All Item Groups",
						"stock_uom": uom,
						"is_stock_item": 1,
					}
				)
				doc.insert(ignore_permissions=True)
				item = doc.name
			frappe.db.set_value("Item", item, "disabled", 0)
			item_mapping[name] = item
			if index % 100 == 0:
				frappe.db.commit()
				print("MAPPED ITEMS", index + 1, len(stock_items), flush=True)
		accounting_item = frappe.db.get_value(
			"Item", {"item_name": "Tally Accounting Line", "is_stock_item": 0}, "name"
		)
		if not accounting_item:
			doc = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "Tally Accounting Line",
					"item_name": "Tally Accounting Line",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert(ignore_permissions=True)
			accounting_item = doc.name
		frappe.db.commit()
		save("item-mappings.json", item_mapping)
		save("accounting-item.json", {"item_code": accounting_item})
		print("ITEM MAPPING COMPLETE", len(item_mapping), flush=True)

	elif args.action == "export":
		inventory = json.loads((root / "tally-voucher-inventory.json").read_text())
		selected = active_business_vouchers(inventory)
		if args.types:
			selected = [r for r in selected if scalar(r, "vouchertypename") in args.types.split(",")]
		if args.limit:
			selected = selected[: args.limit]
		folder = root / "voucher-cache"
		folder.mkdir(exist_ok=True)
		for offset in range(0, len(selected), 50):
			batch = selected[offset : offset + 50]
			batch = [r for r in batch if not (folder / (scalar(r, "masterid") + ".json")).exists()]
			if not batch:
				continue
			request = build_collection_export(
				cfg["tally_company"],
				"ETNativeMigrationBatch",
				"Voucher",
				NATIVE_TRANSACTION_FIELDS,
				[" OR ".join(f"($MasterID = {int(scalar(r, 'masterid'))})" for r in batch)],
				static_variables={"SVFROMDATE": "20250401", "SVTODATE": "20270331"},
			)
			for raw in parse_collection_export(tally.export_collection(request), ["VOUCHER"]):
				record = normalize_voucher(raw)
				if record and record.get("master_id"):
					(folder / (record["master_id"] + ".json")).write_text(json.dumps(record))
			missing = [
				scalar(r, "masterid")
				for r in batch
				if not (folder / (scalar(r, "masterid") + ".json")).exists()
			]
			if missing:
				raise ValueError(f"Source vouchers missing from readback: {missing}")
			print("VOUCHER EXPORT", offset + len(batch), len(selected), flush=True)

	elif args.action == "openings":
		from decimal import Decimal

		from express_tally.bridge.inbound_profiles import _identity, master_name, tally_balance
		from express_tally.integrations.ledger_flow import FullLedgerFromTallyFlow

		mapping = json.loads((root / "ledger-mappings.json").read_text())
		request = build_collection_export(
			cfg["tally_company"],
			"ETMigrationOpeningBalances",
			"Ledger",
			["Name", "GUID", "AlterID", "ClosingBalance"],
			static_variables={"SVFROMDATE": "20250331", "SVTODATE": "20250331"},
		)
		opening_records = [
			r for r in parse_collection_export(tally.export_collection(request), ["LEDGER"]) if master_name(r)
		]
		temporary = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Temporary", "is_group": 0}, "name"
		)
		context = FlowContext(company, cfg["target_id"], cfg["tally_company"])
		flow = NativeTransactionsFromTallyFlow()
		from collections import defaultdict
		from hashlib import sha256

		grouped = defaultdict(list)
		for raw in opening_records:
			name = master_name(raw)
			m = mapping[name]
			if m["root_type"] in {"Income", "Expense"} or m["account_type"] == "Stock":
				continue
			key = (m["account"], m.get("party_type") or "", m.get("party") or "")
			grouped[key].append(raw)
		results = []
		for index, (key, sources) in enumerate(sorted(grouped.items())):
			account, party_type, party = key
			values = {"company": company, "account": account, "party_type": party_type, "party": party}
			where = "company=%(company)s AND account=%(account)s AND COALESCE(party_type,'')=%(party_type)s AND COALESCE(party,'')=%(party)s AND is_cancelled=0 AND (posting_date < '2025-04-01' OR (posting_date='2025-04-01' AND is_opening='Yes'))"
			actual = Decimal(
				str(
					frappe.db.sql(
						"SELECT COALESCE(SUM(debit-credit),0) FROM `tabGL Entry` WHERE " + where, values
					)[0][0]
				)
			).quantize(Decimal("0.01"))
			expected = sum(
				(Decimal(str(tally_balance(scalar(raw, "closing_balance")))) for raw in sources), Decimal(0)
			).quantize(Decimal("0.01"))
			delta = expected - actual
			if not delta:
				continue
			assert temporary, "Temporary Opening account is required"
			ledger_names = sorted(master_name(raw) for raw in sources)
			identity = sha256(json.dumps(key).encode()).hexdigest()
			record = {
				"_tally_type": "native_opening",
				"_tally_key": "native_opening_2025_04_01:" + identity,
				"ledger": ", ".join(ledger_names),
				"kind": "native_opening",
				"balance": str(expected),
			}
			lines = [
				FullLedgerFromTallyFlow.journal_line(
					{"account": account, "party": party, "party_type": party_type}, delta
				),
				{
					"account": temporary,
					"debit_in_account_currency": float(max(-delta, 0)),
					"credit_in_account_currency": float(max(delta, 0)),
				},
			]
			result = FullLedgerFromTallyFlow.save_journal(context, record, lines, "2025-04-01", opening=True)
			flow.sync_log.record(context, record, result)
			frappe.db.commit()
			results.append(
				{
					"ledgers": ledger_names,
					"account": account,
					"party": party,
					"original_erp": str(actual),
					"tally": str(expected),
					"opening_delta": str(delta),
					**result,
				}
			)
			if index % 100 == 0:
				save("opening-import-results.json", results)
				print("OPENING BALANCES", index + 1, len(grouped), "POSTED", len(results), flush=True)
		save("opening-import-results.json", results)
		print("OPENING IMPORT COMPLETE", len(results), flush=True)

	elif args.action == "import":
		context = FlowContext(
			company,
			cfg["target_id"],
			cfg["tally_company"],
			options={
				"ledger_mappings": json.loads((root / "ledger-mappings.json").read_text()),
				"item_mappings": json.loads((root / "item-mappings.json").read_text()),
				"accounting_item": json.loads((root / "accounting-item.json").read_text())["item_code"],
				"branch": "Real Appliances Private Limited",
				"historical_migration": True,
				"financial_only": True,
			},
		)
		flow = NativeTransactionsFromTallyFlow()
		order = {
			"Sales": 0,
			"Purchase": 0,
			"Credit Note": 1,
			"Debit Note": 1,
			"Journal": 2,
			"Contra": 2,
			"Receipt": 3,
			"Payment": 3,
			"Delivery Note": 4,
			"Receipt Note": 4,
		}
		records = [json.loads(p.read_text()) for p in (root / "voucher-cache").glob("*.json")]
		context.options["bill_sources"] = index_bill_sources(records)
		stock_matches = root / "verified-stock-matches.json"
		if stock_matches.exists():
			context.options["stock_matches"] = json.loads(stock_matches.read_text())
		if args.types:
			records = [r for r in records if r["voucher_type"] in args.types.split(",")]
		records.sort(key=lambda r: (order[r["voucher_type"]], r["date"], int(r["master_id"])))
		if args.limit:
			records = records[: args.limit]
		results = []
		started = time.monotonic()
		completed = {
			(row.source_reference, row.source_hash): row
			for row in frappe.get_all(
				"Tally Sync Log",
				filters={
					"flow_key": flow.key,
					"target_id": context.target_id,
					"direction": "Tally to ERPNext",
					"status": "Success",
				},
				fields=["source_reference", "source_hash", "source_doctype", "target_reference"],
			)
		}
		for index, record in enumerate(records):
			previous = completed.get((record["_tally_key"], _source_hash(record)))
			result = (
				_success_result(
					record,
					previous.source_doctype or "",
					previous.target_reference or "",
					status="Skipped",
					message="This Tally version was already imported",
				)
				if previous
				else flow.receive(context, [record])[0]
			)
			frappe.db.commit()
			results.append({"tally_id": record["master_id"], "tally_type": record["voucher_type"], **result})
			if index % 25 == 0 or result["status"] == "Failed":
				save("native-import-results.json", results)
				print(
					"NATIVE IMPORT",
					index + 1,
					len(records),
					Counter(r["status"] for r in results),
					"SECONDS",
					round(time.monotonic() - started, 1),
					flush=True,
				)
			if result["status"] == "Failed":
				save("bill-reference-review.json", context.options.get("bill_reference_review", []))
				print("FAILED", record["master_id"], record["voucher_type"], result["message"], flush=True)
				frappe.destroy()
				raise SystemExit(1)
		save("native-import-results.json", results)
		save("bill-reference-review.json", context.options.get("bill_reference_review", []))
	elif args.action == "reconcile":
		from express_tally.bridge.balance_reconciliation import compare_financial_posting_balances

		mapping = json.loads((root / "ledger-mappings.json").read_text())
		computed = {"ETPeriodFrom": "$$String:##SVFromDate", "ETPeriodTo": "$$String:##SVToDate"}
		request = build_collection_export(
			cfg["tally_company"],
			"ETMigrationReconciliation",
			"Ledger",
			["Name", "GUID", "AlterID", "ClosingBalance", *computed],
			static_variables={"SVFROMDATE": "20250401", "SVTODATE": "20270331"},
			computed_fields=computed,
		)
		rows = [
			row
			for row in parse_collection_export(tally.export_collection(request), ["LEDGER"])
			if scalar(row, "name", "_name")
		]
		if not rows or any(
			scalar(row, "etperiodfrom") != "1-Apr-25" or scalar(row, "etperiodto") != "31-Mar-27"
			for row in rows
		):
			raise ValueError("Tally did not return the requested reconciliation period")
		save("verified-closing-ledgers.json", rows)
		actual = frappe.db.sql(
			"SELECT account, COALESCE(party_type,'') AS party_type, COALESCE(party,'') AS party, SUM(debit-credit) AS balance FROM `tabGL Entry` WHERE company=%s AND is_cancelled=0 AND posting_date<=%s GROUP BY account, party_type, party",
			(company, "2027-03-31"),
			as_dict=True,
		)
		report = compare_financial_posting_balances(
			mapping,
			json.loads((root / "verified-opening-ledgers.json").read_text()),
			rows,
			[json.loads(path.read_text()) for path in (root / "voucher-cache").glob("*.json")],
			actual,
		)
		report["source_audit"] = audit_snapshot(root)
		verification_path = root / "native-document-verification.json"
		verification = json.loads(verification_path.read_text()) if verification_path.exists() else {}
		source_path = root / "final-source-verification.json"
		source_verification = json.loads(source_path.read_text()) if source_path.exists() else {}
		retirement_path = root / "retired-restored-accounting.json"
		retirement = json.loads(retirement_path.read_text()) if retirement_path.exists() else {}
		report["source_snapshot_sha256"] = snapshot_digest(
			[json.loads(path.read_text()) for path in (root / "voucher-cache").glob("*.json")]
		)
		report["all_financial_vouchers_verified"] = (
			verification.get("all_financial_vouchers_verified", False)
			and verification.get("source_snapshot_sha256") == report["source_snapshot_sha256"]
		)
		report["source_snapshot_current"] = (
			source_verification.get("source_snapshot_current", False)
			and source_verification.get("source_snapshot_sha256") == report["source_snapshot_sha256"]
		)
		report["restored_accounting_replacement_complete"] = not (
			root / "restored-financial-documents.json"
		).exists() or (
			retirement.get("applied", False)
			and all(
				row["docstatus"] == 2 or not row.get("had_active_gl") for row in retirement.get("results", [])
			)
			and len(retirement.get("results", []))
			== sum(
				len(rows)
				for rows in json.loads((root / "restored-financial-documents.json").read_text()).values()
			)
		)
		report["migration_complete"] = (
			report["source_audit"]["source_and_mapping_checks_passed"]
			and report["financial_ledger_balances_match"]
			and report["all_financial_vouchers_verified"]
			and report["source_snapshot_current"]
			and report["restored_accounting_replacement_complete"]
		)
		save("reconciliation-results.json", report)
		print(
			"RECONCILIATION",
			"TARGETS",
			len(report["comparisons"]),
			"MISMATCHES",
			sum(not row["matched"] for row in report["comparisons"]),
			"UNMAPPED",
			len(report["unmapped_tally_ledgers"]),
			flush=True,
		)
	frappe.destroy()


if __name__ == "__main__":
	main()
