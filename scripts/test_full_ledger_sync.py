#!/usr/bin/env python3
"""Live financial-ledger acceptance test. Run from the bench sites directory.

Creates ten isolated financial accounts, nonzero balanced openings, and ten
posted vouchers in each system. Uses real HTTP bridge credentials and Tally XML.
State is saved after every fixture so --cleanup can recover interrupted runs.
No credentials are written to the state or report.
"""

import argparse
import json
import tempfile
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import frappe
from frappe.utils.password import get_decrypted_password

from express_tally.bridge.clients import FrappeClient, TallyClient
from express_tally.bridge.collection_gateway import scalar
from express_tally.bridge.config import BridgeConfig
from express_tally.bridge.inbound_profiles import PAYMENT_VOUCHER_FIELDS, TallyVouchersProfile
from express_tally.bridge.ledger_gateway import debit_balance, read_ledger
from express_tally.bridge.profiles import GeneralLedgerVoucherProfile
from express_tally.bridge.service import SyncService
from express_tally.framework import FlowContext
from express_tally.integrations.ledger_flow import (
	INBOUND_FLOW,
	MASTER_FLOW,
	OUTBOUND_FLOW,
	FullLedgerFromTallyFlow,
	FullLedgerToTallyFlow,
	balance,
	ledger_specs,
)


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", default="rapl.localhost")
	parser.add_argument("--bridge-url", default="http://172.17.0.1:18765")
	parser.add_argument("--tally-url", default="http://172.17.0.1:19000")
	parser.add_argument("--frappe-url", default="http://127.0.0.1:8000")
	parser.add_argument("--fiscal-start", default="2026-04-01")
	parser.add_argument("--through", default="2026-04-02")
	parser.add_argument("--state", required=True)
	parser.add_argument("--cleanup", action="store_true")
	args = parser.parse_args()
	state_path = Path(args.state).resolve()
	if state_path.exists() and not args.cleanup:
		raise ValueError("State already exists; use --cleanup or choose a new state file")
	state = (
		json.loads(state_path.read_text())
		if args.cleanup
		else {
			"tag": "ETGL-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
			"accounts": [],
			"erp_sources": [],
			"tally_sources": [],
			"checks": [],
		}
	)

	def save():
		state_path.parent.mkdir(parents=True, exist_ok=True)
		state_path.write_text(json.dumps(state, indent=2, default=str))

	def check(label, actual, expected):
		assert actual == expected, f"{label}: {actual!r} != {expected!r}"
		state["checks"].append(label)
		save()
		print("PASS", label, flush=True)

	frappe.init(site=args.site)
	frappe.connect()
	frappe.set_user("Administrator")
	with urllib.request.urlopen(args.bridge_url + "/api/v1/config", timeout=15) as response:
		values = json.load(response)
	user = frappe.db.get_value("User", {"api_key": values["api_key"]}, "name")
	values["api_secret"] = get_decrypted_password("User", user, "api_secret")
	values.update(
		frappe_url=args.frappe_url,
		tally_url=args.tally_url,
		flow_name=OUTBOUND_FLOW,
		enabled_flows=(MASTER_FLOW, OUTBOUND_FLOW, INBOUND_FLOW),
		from_date=args.fiscal_start,
		to_date=args.through,
		voucher_date_override=None,
		request_timeout_seconds=60,
		runtime_directory=state.setdefault("runtime_directory", tempfile.mkdtemp(prefix="et-ledger-test-")),
		auto_sync_enabled=False,
	)
	state["company"] = values["erpnext_company"]
	state["target_id"] = values["target_id"]
	state["tally_company"] = values["tally_company"]
	config = BridgeConfig(**values)
	client = FrappeClient(config.frappe_url, config.api_key, config.api_secret, 60)
	tally = TallyClient(config.tally_url, 60)
	service = SyncService(config, client, tally)
	check("Tally company matches configured company", service.health()["company_matches"], True)
	available = {flow["key"] for flow in client.get_flows()["flows"]}
	check(
		"All three ledger flows available on ERPNext",
		{MASTER_FLOW, OUTBOUND_FLOW, INBOUND_FLOW} <= available,
		True,
	)
	save()

	def configure():
		names = [row["account"] for row in state["accounts"]]
		options = {
			MASTER_FLOW: {"account_names": names, "allow_opening_update": bool(args.cleanup)},
			OUTBOUND_FLOW: {"account_names": names, "document_names": state["erp_sources"]},
			INBOUND_FLOW: {
				**FullLedgerFromTallyFlow.default_options,
				"fiscal_year_start": args.fiscal_start,
				"ledger_names": [row["ledger"] for row in state["accounts"]],
				"ledger_mappings": {row["ledger"]: row["account"] for row in state["accounts"]},
			},
		}
		nonlocal config, service
		config = replace(config, flow_options=options)
		service = SyncService(config, client, tally)

	def sync(flow, direction):
		result = service.sync_flow(
			flow,
			direction,
			agent_profile="tally_ledger_mirror_v1" if direction == "tally_to_erpnext" else None,
			limit=100,
		).to_dict()
		state.setdefault("sync_runs", []).append(result)
		save()
		print("SYNC", json.dumps(result), flush=True)
		if result["failed"] or result["error"]:
			frappe.db.rollback()
			errors = frappe.get_all(
				"Tally Sync Log",
				filters={"target_id": config.target_id, "flow_key": flow, "status": "Failed"},
				fields=["source_reference", "error"],
				order_by="creation desc",
				limit=10,
			)
			raise AssertionError(f"Sync failed: {result}; {errors}")
		return result

	def journal(lines, posting_date, *, opening=False):
		doc = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": config.erpnext_company,
				"posting_date": posting_date,
				"voucher_type": "Opening Entry" if opening else "Journal Entry",
				"is_opening": "Yes" if opening else "No",
				"user_remark": state["tag"],
				"accounts": lines,
			}
		)
		doc.insert(ignore_permissions=True)
		doc._submit()
		frappe.db.commit()
		return doc.name

	def line(account, amount):
		return {
			"account": account,
			"debit_in_account_currency": float(max(amount, 0)),
			"credit_in_account_currency": float(max(-amount, 0)),
		}

	def drain():
		for _ in range(12):
			if sync(INBOUND_FLOW, "tally_to_erpnext")["fetched"] == 0:
				return
		raise AssertionError("Inbound stages did not drain")

	def cleanup():
		nonlocal config
		if not state["accounts"]:
			return
		configure()
		for name in state["erp_sources"]:
			frappe.db.rollback()
			doc = frappe.get_doc("Journal Entry", name)
			if doc.docstatus == 1:
				doc._cancel()
				frappe.db.commit()
		sync(OUTBOUND_FLOW, "erpnext_to_tally")
		for row in state["tally_sources"]:
			document = {**row["document"], "operation": "Cancel", "tally_voucher_id": row["reference"]}
			GeneralLedgerVoucherProfile().deliver(document, config, tally)
		if state["tally_sources"]:
			drain()
		frappe.db.rollback()
		if state.get("opening_journal"):
			doc = frappe.get_doc("Journal Entry", state["opening_journal"])
			if doc.docstatus == 1:
				doc._cancel()
				frappe.db.commit()
		config = replace(
			config,
			flow_options={
				**config.flow_options,
				MASTER_FLOW: {
					"account_names": [r["account"] for r in state["accounts"]],
					"allow_opening_update": True,
				},
			},
		)
		service.config = config
		sync(MASTER_FLOW, "erpnext_to_tally")
		frappe.db.rollback()
		ctx = FlowContext(
			config.erpnext_company,
			config.target_id,
			config.tally_company,
			options=config.flow_options[MASTER_FLOW],
		)
		for spec in ledger_specs(ctx):
			check(
				"Cleanup ERP zero balance " + spec["ledger_name"],
				balance(config.erpnext_company, spec),
				Decimal(0),
			)
			raw = read_ledger(tally, config.tally_company, spec["ledger_name"])
			check(
				"Cleanup Tally zero balance " + spec["ledger_name"],
				debit_balance(scalar(raw, "closingbalance", "closing_balance")),
				Decimal(0),
			)
		state["cleanup_complete"] = True
		save()

	if args.cleanup:
		cleanup()
		frappe.destroy()
		return

	try:
		parent = frappe.db.get_value(
			"Account",
			{"company": config.erpnext_company, "account_name": "Current Assets", "is_group": 1},
			"name",
		)
		assert parent, "Current Assets parent required"
		for index in range(10):
			name = f"{state['tag']}-{index + 1:02d}"
			assert read_ledger(tally, config.tally_company, name) is None, "Fixture ledger already exists"
			doc = frappe.get_doc(
				{
					"doctype": "Account",
					"company": config.erpnext_company,
					"account_name": name,
					"parent_account": parent,
					"is_group": 0,
				}
			)
			doc.insert(ignore_permissions=True)
			frappe.db.commit()
			opening = (index % 5 + 1) * 10 * (1 if index < 5 else -1)
			state["accounts"].append({"account": doc.name, "ledger": name, "opening": opening})
			save()
		state["opening_journal"] = journal(
			[line(r["account"], r["opening"]) for r in state["accounts"]], args.fiscal_start, opening=True
		)
		save()
		configure()
		check("Ten ledger masters exported", sync(MASTER_FLOW, "erpnext_to_tally")["succeeded"], 10)
		check("Master retry creates nothing", sync(MASTER_FLOW, "erpnext_to_tally")["fetched"], 0)
		for row in state["accounts"]:
			raw = read_ledger(tally, config.tally_company, row["ledger"])
			check(
				"Opening readback " + row["ledger"],
				debit_balance(scalar(raw, "opening_balance", "openingbalance")),
				Decimal(row["opening"]),
			)
		for index, row in enumerate(state["accounts"]):
			other = state["accounts"][(index + 1) % 10]
			state["erp_sources"].append(
				journal([line(row["account"], index + 1), line(other["account"], -(index + 1))], "2026-04-02")
			)
			save()
		configure()
		check("Ten posted ERP vouchers exported", sync(OUTBOUND_FLOW, "erpnext_to_tally")["succeeded"], 10)
		check("Outbound retry creates nothing", sync(OUTBOUND_FLOW, "erpnext_to_tally")["fetched"], 0)
		frappe.db.rollback()
		state["outbound_references"] = [
			frappe.db.get_value(
				"Tally Sync Log",
				{
					"flow_key": OUTBOUND_FLOW,
					"target_id": config.target_id,
					"source_name": name,
					"status": "Success",
				},
				"target_reference",
			)
			for name in state["erp_sources"]
		]
		save()
		extractor = TallyVouchersProfile()
		extractor.voucher_fields = PAYMENT_VOUCHER_FIELDS
		readback = extractor.collect(config, tally, 100, {"voucher_ids": state["outbound_references"]})
		check("All ten outbound vouchers present in Tally", len(readback), 10)
		for index, reference in enumerate(state["outbound_references"]):
			record = next(row for row in readback if row["master_id"] == reference)
			check(
				"Exact outbound ledger amounts " + reference,
				[(r["ledger"], Decimal(str(r["amount"]))) for r in record["ledger_entries"]],
				[
					(state["accounts"][index]["ledger"], Decimal(-(index + 1))),
					(state["accounts"][(index + 1) % 10]["ledger"], Decimal(index + 1)),
				],
			)
			check(
				"Balanced outbound Tally voucher " + record["master_id"],
				sum(Decimal(str(r["amount"])) for r in record["ledger_entries"]),
				Decimal(0),
			)
		ctx = FlowContext(
			config.erpnext_company,
			config.target_id,
			config.tally_company,
			options=config.flow_options[MASTER_FLOW],
		)
		payload = FullLedgerToTallyFlow().map_document(
			frappe.get_doc("Journal Entry", state["erp_sources"][0]), ctx, ledger_specs(ctx)
		)
		check(
			"Lost acknowledgement retry reuses original Tally identity",
			GeneralLedgerVoucherProfile().deliver(payload, config, tally),
			state["outbound_references"][0],
		)
		for index, row in enumerate(state["accounts"]):
			other = state["accounts"][(index + 1) % 10]
			document = {
				"name": f"{state['tag']}-TALLY-{index + 1:02d}",
				"operation": "Create",
				"identity_namespace": state["tag"],
				"origin_marker": "",
				"voucher_type": "Journal",
				"transaction_date": "2026-04-02",
				"narration": state["tag"] + " independent Tally source",
				"ledger_entries": [
					{"ledger": row["ledger"], "amount": (index + 2) * 2},
					{"ledger": other["ledger"], "amount": -(index + 2) * 2},
				],
			}
			reference = GeneralLedgerVoucherProfile().deliver(document, config, tally)
			state["tally_sources"].append({"document": document, "reference": reference})
			save()
			print("TALLY SOURCE", reference, flush=True)
		drain()
		inbound_readback = extractor.collect(
			config, tally, 100, {"voucher_ids": [r["reference"] for r in state["tally_sources"]]}
		)
		inbound_keys = {"ledger_mirror_voucher:" + r["_tally_guid"] for r in inbound_readback}
		frappe.db.rollback()
		imported = frappe.get_all(
			"Tally Sync Log",
			filters={
				"flow_key": INBOUND_FLOW,
				"target_id": config.target_id,
				"source_type": "ledger_mirror_voucher",
				"status": "Success",
				"target_reference": ["is", "set"],
			},
			fields=["target_reference", "source_reference"],
		)
		imported = [r for r in imported if r.source_reference in inbound_keys and r.target_reference]
		state["inbound_journals"] = sorted({r.target_reference for r in imported})
		check("Ten independent Tally vouchers imported", len(state["inbound_journals"]), 10)
		for record in inbound_readback:
			name = next(
				r.target_reference
				for r in imported
				if r.source_reference == "ledger_mirror_voucher:" + record["_tally_guid"]
			)
			actual = frappe.get_all(
				"GL Entry",
				filters={"voucher_type": "Journal Entry", "voucher_no": name, "is_cancelled": 0},
				fields=["account", "debit", "credit"],
			)
			mapping = config.flow_options[INBOUND_FLOW]["ledger_mappings"]
			check(
				"Exact inbound posted GL amounts " + name,
				sorted((r.account, Decimal(str(r.debit)) - Decimal(str(r.credit))) for r in actual),
				sorted((mapping[r["ledger"]], -Decimal(str(r["amount"]))) for r in record["ledger_entries"]),
			)
		for name in state["inbound_journals"]:
			check(
				"Inbound journal fully submitted " + name,
				frappe.db.get_value("Journal Entry", name, "docstatus"),
				1,
			)
			check(
				"Inbound journal posted two GL entries " + name,
				frappe.db.count(
					"GL Entry", {"voucher_type": "Journal Entry", "voucher_no": name, "is_cancelled": 0}
				),
				2,
			)
		ctx = FlowContext(
			config.erpnext_company,
			config.target_id,
			config.tally_company,
			options=config.flow_options[MASTER_FLOW],
		)
		state["ledger_balances"] = []
		for spec in ledger_specs(ctx):
			actual = balance(config.erpnext_company, spec, through=args.through)
			raw = read_ledger(tally, config.tally_company, spec["ledger_name"])
			expected = debit_balance(scalar(raw, "closingbalance", "closing_balance"))
			check("Closing balance reconciled " + spec["ledger_name"], actual, expected)
			state["ledger_balances"].append(
				{
					"ledger": spec["ledger_name"],
					"erp_closing": str(actual),
					"tally_closing": str(expected),
					"difference": str(actual - expected),
				}
			)
		first = state["tally_sources"][0]
		old_journal = next(
			r.target_reference
			for r in imported
			if r.source_reference == "ledger_mirror_voucher:" + inbound_readback[0]["_tally_guid"]
		)
		changed = {
			**first["document"],
			"operation": "Alter",
			"tally_voucher_id": first["reference"],
			"ledger_entries": [
				{**r, "amount": r["amount"] + (3 if r["amount"] > 0 else -3)}
				for r in first["document"]["ledger_entries"]
			],
		}
		GeneralLedgerVoucherProfile().deliver(changed, config, tally)
		first["document"] = changed
		save()
		drain()
		frappe.db.rollback()
		check(
			"Changed Tally voucher cancels previous posted ERP version",
			frappe.db.get_value("Journal Entry", old_journal, "docstatus"),
			2,
		)
		check(
			"Changed voucher reconciles every selected ledger",
			sync(INBOUND_FLOW, "tally_to_erpnext")["fetched"],
			0,
		)
		state["sync_test_passed"] = True
		save()
	finally:
		try:
			cleanup()
		except Exception as exc:
			state["cleanup_error"] = str(exc)
			save()
			print("CLEANUP FAILED; saved state supports --cleanup:", str(exc), flush=True)
			raise
		finally:
			frappe.destroy()
	print("PASS full-ledger test and cleanup:", state_path, flush=True)


if __name__ == "__main__":
	main()
