import tempfile
import xml.etree.ElementTree as ET
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from express_tally.bridge.config import BridgeConfig
from express_tally.bridge.controller import validate_exclusive_flows
from express_tally.bridge.inbound_profiles import TallyLedgerMirrorProfile
from express_tally.bridge.payment_gateway import build_accounting_import, validate_payment
from express_tally.bridge.profiles import GeneralLedgerVoucherProfile, LedgerMasterProfile
from express_tally.framework import FlowContext
from express_tally.integrations.ledger_flow import FullLedgerFromTallyFlow
from express_tally.integrations.payment_entry_flow import PaymentEntryMapper


class TestLedgerAccounting(TestCase):
	def test_journal_preserves_repeated_account_lines(self):
		rows = [
			frappe._dict(account="Cash", tally_ledger="Selected cash", debit=10, credit=0),
			frappe._dict(account="Cash", tally_ledger="Selected cash", debit=5, credit=0),
			frappe._dict(account="Bank", tally_ledger="Selected bank", debit=0, credit=15),
		]
		with patch("frappe.db", new=Mock()) as db:
			db.get_value.return_value = frappe._dict(
				account_name="ignored", account_type="", root_type="Asset", company="Company"
			)
			mapped = PaymentEntryMapper(SimpleNamespace(key="full-ledger")).map_ledger_entries(
				rows, "Company", preserve_lines=True
			)
		self.assertEqual(
			[r["ledger"] for r in mapped["ledger_entries"]],
			["Selected cash", "Selected cash", "Selected bank"],
		)
		self.assertEqual(
			[Decimal(r["amount"]) for r in mapped["ledger_entries"]], [Decimal(-10), Decimal(-5), Decimal(15)]
		)
		payload = {
			**mapped,
			"name": "J1",
			"operation": "Create",
			"transaction_date": "2026-04-02",
			"voucher_type": "Journal",
		}
		root = ET.fromstring(build_accounting_import(payload, "Company", "target"))
		self.assertEqual(len(root.findall(".//ALLLEDGERENTRIES.LIST")), 3)
		with self.assertRaises(ValueError):
			validate_payment(payload)

	def test_general_ledger_profile_never_prepares_masters(self):
		self.assertFalse(GeneralLedgerVoucherProfile.prepare_masters)

	def test_existing_opening_conflict_does_not_write(self):
		config = BridgeConfig(tally_company="Company")
		tally = Mock()
		with patch(
			"express_tally.bridge.profiles.read_ledger",
			return_value={"_name": "Cash", "openingbalance": "-25", "parent": "Cash-in-Hand"},
		):
			with self.assertRaisesRegex(RuntimeError, "differs"):
				LedgerMasterProfile().deliver(
					{"ledger_name": "Cash", "opening_balance": "30", "parent": "Cash-in-Hand"}, config, tally
				)
		tally.import_xml.assert_not_called()

	def test_full_accounting_rejects_overlapping_exporters(self):
		flows = [
			{"key": "all", "accounting_scope": "all"},
			{"key": "payment", "accounting_scope": "payments"},
		]
		with self.assertRaises(ValueError):
			validate_exclusive_flows(flows)

	def test_resolution_uses_requested_mapping_not_outbound_selection(self):
		ctx = FlowContext(
			"Company",
			"target",
			"Tally",
			options={"ledgers": [{"account": "WRONG"}], "ledger_mappings": {"Cash": "RIGHT"}},
		)
		with patch(
			"express_tally.integrations.ledger_flow.ledger_specs",
			side_effect=lambda c: [{"account": c.options["account_names"][0]}],
		):
			self.assertEqual(FullLedgerFromTallyFlow().resolve(ctx, "Cash", {})["account"], "RIGHT")

	def test_reconciliation_difference_fails(self):
		flow = FullLedgerFromTallyFlow()
		ctx = FlowContext("Company", "target", "Tally", options={"fiscal_year_start": "2026-04-01"})
		with (
			patch.object(flow, "resolve", return_value={"account": "Cash"}),
			patch("express_tally.integrations.ledger_flow.balance", return_value=Decimal("9.98")),
		):
			result = flow.apply_record(
				ctx,
				{
					"kind": "ledger_reconciliation",
					"ledger": "Cash",
					"balance": 10,
					"as_of_date": "2026-04-30",
				},
			)
		self.assertEqual(result["status"], "Failed")


class TestLedgerCollection(TestCase):
	def test_full_ledger_profile_requires_explicit_scope(self):
		with self.assertRaisesRegex(ValueError, "ledger_names"):
			TallyLedgerMirrorProfile().collect(BridgeConfig(), Mock(), 10, {"require_ledger_scope": True})

	def test_scoped_request_retains_balancing_rows_and_isolates_checkpoint(self):
		profile = TallyLedgerMirrorProfile()
		profile._ledger_scope = ["Cash", "Bank"]
		profile._scope_key = "selected"
		tally = Mock()
		tally.export_collection.return_value = "<ENVELOPE/>"
		with tempfile.TemporaryDirectory() as directory:
			config = BridgeConfig(tally_company="Company", target_id="target", runtime_directory=directory)
			profile._vouchers(config, tally, "2026-04-01", "2026-04-30", {}, {})
		root = ET.fromstring(tally.export_collection.call_args.args[0])
		self.assertIn(
			"($$FilterCount:AllLedgerEntries:ETSelectedLedger > 0)",
			[r.text for r in root.findall(".//SYSTEM")],
		)
		self.assertIn("AllLedgerEntries.Amount", [r.text for r in root.findall(".//NATIVEMETHOD")])
		self.assertTrue(profile._target_key(config).endswith(":ledgers:selected"))

	def test_new_closing_snapshot_reconciles_without_master_alter(self):
		profile = TallyLedgerMirrorProfile()
		opening = [{"_name": "Cash", "guid": "cash", "alterid": "7", "closingbalance": "0"}]
		closing = [{"_name": "Cash", "guid": "cash", "alterid": "7", "closingbalance": "-10"}]
		with tempfile.TemporaryDirectory() as directory:
			config = BridgeConfig(
				target_id="target",
				tally_company="Company",
				from_date="2026-04-01",
				to_date="2026-04-30",
				runtime_directory=directory,
			)
			with (
				patch.object(profile, "_groups", return_value={}),
				patch.object(profile, "_vouchers", return_value=[]),
				patch.object(profile, "_ledgers", side_effect=[opening, closing]),
			):
				first = profile.collect(config, Mock(), 10)
			profile.acknowledge_collected(config, first, [{"status": "Success"}])
			with (
				patch.object(profile, "_groups", return_value={}),
				patch.object(profile, "_vouchers", return_value=[]),
				patch.object(profile, "_ledgers", side_effect=[opening, closing]),
			):
				self.assertEqual(profile.collect(config, Mock(), 10), [])
			closing[0]["closingbalance"] = "-11"
			with (
				patch.object(profile, "_groups", return_value={}),
				patch.object(profile, "_vouchers", return_value=[]),
				patch.object(profile, "_ledgers", side_effect=[opening, closing]),
			):
				changed = profile.collect(config, Mock(), 10)
		self.assertEqual(changed[0]["balance"], 11)
		self.assertNotEqual(changed[0]["_tally_type"], first[0]["_tally_type"])
