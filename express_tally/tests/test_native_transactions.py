import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from express_tally.bridge.balance_reconciliation import (
	compare_financial_posting_balances,
	compare_ledger_balances,
)
from express_tally.bridge.bill_references import dated_bill_key, index_bill_sources
from express_tally.bridge.collection_gateway import parse_collection_export
from express_tally.bridge.inbound_profiles import _primary_group, normalize_voucher
from express_tally.bridge.migration_audit import audit_snapshot, snapshot_digest
from express_tally.framework import FlowContext
from express_tally.integrations.native_transaction_flow import NativeTransactionsFromTallyFlow


class TestNativeTransactions(TestCase):
	def test_verification_digest_ignores_file_order_but_detects_changed_source(self):
		records = [{"master_id": "1", "amount": 10}, {"master_id": "2", "amount": 20}]
		self.assertEqual(snapshot_digest(records), snapshot_digest(list(reversed(records))))
		original = snapshot_digest(records)
		records[0]["amount"] = 11
		self.assertNotEqual(original, snapshot_digest(records))

	def test_missing_source_snapshot_cannot_pass_zero_balance_reconciliation(self):
		mapping = {"Cash": {"account": "Cash"}, "Missing": {"account": "Missing"}}
		report = compare_financial_posting_balances(
			mapping,
			[{"name": "Cash", "closingbalance": "0"}],
			[{"name": "Cash", "closingbalance": "0"}],
			[],
			[],
		)
		self.assertFalse(report["financial_ledger_balances_match"])
		self.assertEqual(report["missing_opening_ledgers"], ["Missing"])
		self.assertEqual(report["missing_closing_ledgers"], ["Missing"])

	def test_zero_value_source_is_logged_without_fabricating_a_transaction(self):
		flow = NativeTransactionsFromTallyFlow()
		result = flow.apply_record(
			FlowContext("Company", "target", "Tally"),
			{
				"voucher_type": "Purchase",
				"ledger_entries": [{"ledger": "Supplier", "amount": 0}],
			},
		)
		self.assertEqual(result["status"], "Skipped")
		self.assertEqual(result["doctype"], "")
		self.assertEqual(result["name"], "")

	def test_financial_scope_keeps_stock_derived_report_difference_visible(self):
		mapping = {
			"Cash": {"account": "Cash"},
			"Income": {"account": "Income"},
			"Profit & Loss A/c": {"account": "Profit"},
			"Stock": {"account": "Stock", "account_type": "Stock"},
		}
		opening = [
			{"name": "Cash", "closingbalance": "-10"},
			{"name": "Income", "closingbalance": "0"},
			{"name": "Profit & Loss A/c", "closingbalance": "0"},
			{"name": "Stock", "closingbalance": "-50"},
		]
		closing = [
			{"name": "Cash", "closingbalance": "-20"},
			{"name": "Income", "closingbalance": "10"},
			{"name": "Profit & Loss A/c", "closingbalance": "60"},
			{"name": "Stock", "closingbalance": "-100"},
		]
		records = [
			{
				"voucher_type": "Sales",
				"ledger_entries": [{"ledger": "Cash", "amount": -10}, {"ledger": "Income", "amount": 10}],
			}
		]
		actual = [
			{"account": "Cash", "balance": 20},
			{"account": "Income", "balance": -10},
			{"account": "Profit", "balance": 0},
			{"account": "Stock", "balance": 999},
		]
		report = compare_financial_posting_balances(mapping, opening, closing, records, actual)
		self.assertTrue(report["financial_ledger_balances_match"])
		self.assertEqual(report["excluded_stock_ledgers"], ["Stock"])
		self.assertEqual(report["stock_derived_report_balances"][0]["tally_reported_balance"], "-60.00")
		self.assertEqual(report["source_posted_financial_ledgers"], 3)

	def test_common_party_setting_is_restored_when_historical_invoice_fails(self):
		flow = NativeTransactionsFromTallyFlow()
		db = Mock()
		db.get_single_value.return_value = 1
		context = FlowContext("Company", "target", "Tally", options={"historical_migration": True})
		with (
			patch("frappe.db", db),
			patch.object(flow, "_save_with_purchase_policy", side_effect=ValueError("failed")),
		):
			with self.assertRaises(ValueError):
				flow._save_document(context, {}, {"doctype": "Sales Invoice"})
		self.assertEqual(
			db.set_single_value.call_args_list[-1].args,
			("Accounts Settings", "enable_common_party_accounting", 1),
		)

	def test_financial_amount_line_retains_exact_amount_and_source_quantity_description(self):
		context = FlowContext(
			"Company",
			"target",
			"Tally",
			options={
				"financial_only": True,
				"accounting_item": "Accounting",
				"item_mappings": {"Metal": "Metal"},
				"ledger_mappings": {
					"Supplier": {"account": "Payable", "party_type": "Supplier", "party": "S1"},
					"Purchases": {"account": "Purchases", "root_type": "Expense"},
				},
			},
		)
		record = {
			"party_ledger": "Supplier",
			"voucher_type": "Purchase",
			"date": "2025-04-01",
			"voucher_number": "1",
			"master_id": "1",
			"ledger_entries": [
				{"ledger": "Supplier", "amount": 1234.32},
				{"ledger": "Purchases", "amount": -1234.32},
			],
			"inventory_entries": [
				{
					"item": "Metal",
					"billed_qty": "1000 KGS",
					"amount": -1234.32,
					"account": "Purchases",
					"rate": "1.23432/KGS",
				}
			],
		}
		flow = NativeTransactionsFromTallyFlow()
		with (
			patch.object(flow, "_save_document", side_effect=lambda c, r, v: v),
			patch("frappe.get_cached_value", return_value="Main"),
		):
			values = flow.invoice(context, record, sales=False, is_return=False)
		self.assertEqual(values["items"][0]["rate"], 1234.32)
		self.assertEqual(values["items"][0]["qty"], 1)
		self.assertIn("1000 KGS", values["items"][0]["description"])
		self.assertEqual(values["set_posting_time"], 1)
		record["inventory_entries"][0]["billed_qty"] = ""
		with (
			patch.object(flow, "_save_document", side_effect=lambda c, r, v: v),
			patch("frappe.get_cached_value", return_value="Main"),
		):
			values = flow.invoice(context, record, sales=False, is_return=False)
		self.assertEqual(values["items"][0]["rate"], 1234.32)
		self.assertEqual(values["items"][0]["qty"], 1)

	def test_reconciliation_groups_source_aliases_without_double_counting_erp(self):
		mapping = {"First": {"account": "Bank"}, "Alias": {"account": "Bank"}}
		report = compare_ledger_balances(
			mapping,
			[{"name": "First", "closingbalance": "-10"}, {"name": "Alias", "closingbalance": "-20"}],
			[{"account": "Bank", "balance": 30}],
		)
		self.assertTrue(report["mapped_targets_matched"])
		self.assertEqual(len(report["comparisons"]), 1)
		self.assertEqual(report["comparisons"][0]["source_balance"], "30.0")

	def test_bill_index_does_not_guess_when_same_invoice_number_is_reused(self):
		first = {
			"voucher_type": "Sales",
			"voucher_number": "1",
			"party_ledger": "Customer",
			"date": "2025-04-01",
			"_tally_key": "first",
			"ledger_entries": [],
		}
		second = {**first, "date": "2026-04-01", "_tally_key": "second"}
		index = index_bill_sources([first, second])["Customer"]
		self.assertNotIn("1", index)
		self.assertEqual(index[dated_bill_key("0001", "2026-04-01")], {"_tally_key": "second"})

	def test_historical_purchase_exception_is_restored_after_failed_submission(self):
		flow = NativeTransactionsFromTallyFlow()
		db = Mock()
		db.get_value.return_value = 0
		context = FlowContext("Company", "target", "Tally", options={"historical_migration": True})
		with (
			patch("frappe.db", db),
			patch.object(flow, "_save_native_document", side_effect=ValueError("posting failed")),
		):
			with self.assertRaisesRegex(ValueError, "posting failed"):
				flow._save_document(context, {}, {"doctype": "Purchase Invoice", "supplier": "S1"})
		self.assertEqual(
			db.set_value.call_args_list[-1].args,
			("Supplier", "S1", "allow_purchase_invoice_creation_without_purchase_order", 0),
		)

	def test_incomplete_snapshot_is_not_ready_despite_balanced_cached_voucher(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "voucher-cache").mkdir()
			(root / "tally-voucher-inventory.json").write_text(
				json.dumps([{"masterid": "1"}, {"masterid": "2"}])
			)
			(root / "ledger-mappings.json").write_text(
				json.dumps({"Cash": {"account": "Cash"}, "Sales": {"account": "Sales"}})
			)
			(root / "item-mappings.json").write_text(json.dumps({"Fan": "Fan"}))
			(root / "voucher-cache/1.json").write_text(
				json.dumps(
					{
						"master_id": "1",
						"voucher_type": "Sales",
						"ledger_entries": [
							{"ledger": "Cash", "amount": -10},
							{"ledger": "Sales", "amount": 10},
						],
					}
				)
			)
			report = audit_snapshot(root)
		self.assertFalse(report["source_and_mapping_checks_passed"])
		self.assertEqual(report["issues"], [{"kind": "incomplete_export", "count": 1, "ids": ["2"]}])

	def test_against_reference_resolves_imported_invoice_and_amount(self):
		context = FlowContext(
			"Company",
			"target",
			"Tally",
			options={"bill_sources": {"Customer": {"INV-1": {"_tally_key": "voucher:1"}}}},
		)
		entry = {
			"ledger": "Customer",
			"bill_allocations": [{"billtype": "Agst Ref", "name": "INV-1", "amount": "100"}],
		}
		flow = NativeTransactionsFromTallyFlow()
		flow.sync_log.previous_target = Mock(
			return_value={"source_doctype": "Sales Invoice", "target_reference": "SI-1"}
		)
		with patch(
			"frappe.get_doc",
			return_value=frappe._dict(
				name="SI-1", docstatus=1, company="Company", customer="C1", outstanding_amount=200
			),
		):
			result = flow.bill_references(context, entry, {"party_type": "Customer", "party": "C1"})
		self.assertEqual(
			result,
			[{"reference_doctype": "Sales Invoice", "reference_name": "SI-1", "allocated_amount": 100}],
		)

	def test_missing_against_reference_fails_instead_of_creating_unallocated_payment(self):
		context = FlowContext("Company", "target", "Tally")
		with self.assertRaisesRegex(ValueError, "No unique source invoice"):
			NativeTransactionsFromTallyFlow().bill_references(
				context,
				{
					"ledger": "Customer",
					"bill_allocations": [{"billtype": "Agst Ref", "name": "Missing", "amount": 10}],
				},
				{"party_type": "Customer", "party": "C1"},
			)

	def test_financial_overpayment_retains_credit_without_overallocating_invoice(self):
		context = FlowContext("Company", "target", "Tally", options={"financial_only": True})
		entry = {
			"ledger": "Customer",
			"bill_allocations": [{"billtype": "Agst Ref", "name": "INV-1", "amount": "100"}],
		}
		flow = NativeTransactionsFromTallyFlow()
		doc = frappe._dict(name="SI-1", outstanding_amount=50)
		with patch.object(flow, "bill_target", return_value=("Sales Invoice", doc)):
			refs = flow.bill_references(context, entry, {}, source_id="123")
			self.assertEqual(refs[0]["allocated_amount"], 50)
			self.assertEqual(
				context.options["bill_reference_review"][0]["source_requested_allocation"], "100.00"
			)
			doc.outstanding_amount = 0
			self.assertEqual(flow.bill_references(context, entry, {}, source_id="124"), [])
			context.options["financial_only"] = False
			with self.assertRaisesRegex(ValueError, "outstanding"):
				flow.bill_references(context, entry, {})

	def test_bill_redistribution_journal_preserves_net_money_and_supported_references(self):
		context = FlowContext(
			"Company",
			"target",
			"Tally",
			options={
				"financial_only": True,
				"ledger_mappings": {
					"Customer": {"account": "AR", "party_type": "Customer", "party": "C1"},
					"Bank": {"account": "Bank", "account_type": "Bank"},
				},
			},
		)
		bills = [
			{"billtype": "Agst Ref", "name": name, "amount": amount}
			for name, amount in [("Old", -324450), ("New1", 331200), ("New2", 204000), ("New3", 120450)]
		]
		record = {
			"master_id": "58138",
			"date": "2026-06-16",
			"voucher_number": "316",
			"voucher_type": "Receipt",
			"ledger_entries": [
				{"ledger": "Customer", "amount": 331200, "bill_allocations": bills},
				{"ledger": "Bank", "amount": -331200},
			],
		}
		outstanding = {"Old": 0, "New1": 331200, "New2": 204000, "New3": 120450}
		flow = NativeTransactionsFromTallyFlow()
		with (
			patch.object(
				flow,
				"bill_target",
				side_effect=lambda c, e, m, b: (
					"Sales Invoice",
					frappe._dict(name=b["name"], outstanding_amount=outstanding[b["name"]]),
				),
			),
			patch.object(flow, "_save_document", side_effect=lambda c, r, v: v),
			patch("frappe.get_cached_value", return_value="Main"),
		):
			values = flow.journal(context, record, "Bank Entry")
		party_lines = [row for row in values["accounts"] if row.get("party") == "C1"]
		self.assertEqual(
			sum(row["debit_in_account_currency"] - row["credit_in_account_currency"] for row in party_lines),
			-331200,
		)
		self.assertEqual(sum(bool(row.get("reference_name")) for row in party_lines), 3)
		self.assertEqual(
			[row["debit_in_account_currency"] for row in party_lines if not row.get("reference_name")],
			[324450],
		)

	def test_stock_document_cannot_be_cancelled_by_financial_import(self):
		with patch("frappe.db", Mock(exists=Mock(return_value=True))):
			with self.assertRaisesRegex(ValueError, "cannot be cancelled"):
				NativeTransactionsFromTallyFlow()._cancel_previous(
					{"_previous_doctype": "Delivery Note", "_previous_name": "DN-1"}
				)

	def test_party_and_bank_classification_stops_before_current_assets(self):
		groups = {
			"punjab": {"parent": "Sundry Debtors"},
			"sundry debtors": {"parent": "Current Assets"},
			"bank accounts": {"parent": "Current Assets"},
			"current assets": {"parent": "Primary"},
		}
		self.assertEqual(_primary_group("Punjab", groups), "Sundry Debtors")
		self.assertEqual(_primary_group("Bank Accounts", groups), "Bank Accounts")

	def test_real_nested_accounting_and_batch_allocations_are_preserved(self):
		raw = "<ENVELOPE><VOUCHER><MASTERID>12</MASTERID><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME><ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>Fan</STOCKITEMNAME><AMOUNT>500</AMOUNT><ACCOUNTINGALLOCATIONS.LIST><LEDGERNAME>Sales 18%</LEDGERNAME><AMOUNT>500</AMOUNT></ACCOUNTINGALLOCATIONS.LIST><BATCHALLOCATIONS.LIST><GODOWNNAME>Main</GODOWNNAME><TRACKINGNUMBER>DN-1</TRACKINGNUMBER></BATCHALLOCATIONS.LIST></ALLINVENTORYENTRIES.LIST></VOUCHER></ENVELOPE>"
		record = normalize_voucher(parse_collection_export(raw, ["VOUCHER"])[0])
		self.assertEqual(record["inventory_entries"][0]["account"], "Sales 18%")
		self.assertEqual(record["inventory_entries"][0]["godown"], "Main")
		self.assertEqual(record["inventory_entries"][0]["tracking_numbers"], ["DN-1"])

	def test_accounting_purchase_becomes_purchase_invoice_with_signed_round_off(self):
		context = FlowContext(
			"Company",
			"target",
			"Tally",
			options={
				"accounting_item": "Accounting Line",
				"ledger_mappings": {
					"Supplier": {"account": "Creditors", "party_type": "Supplier", "party": "S1"},
					"Purchases": {"account": "Purchases", "root_type": "Expense"},
					"Tax": {"account": "Tax", "account_type": "Tax"},
					"Round": {"account": "Round", "account_type": "Round Off"},
				},
			},
		)
		record = {
			"voucher_type": "Purchase",
			"master_id": "12",
			"voucher_number": "P1",
			"party_ledger": "Supplier",
			"date": "2025-04-01",
			"ledger_entries": [
				{"ledger": "Supplier", "amount": 118},
				{"ledger": "Purchases", "amount": -100.4},
				{"ledger": "Tax", "amount": -18},
				{"ledger": "Round", "amount": 0.4},
			],
		}
		flow = NativeTransactionsFromTallyFlow()
		with (
			patch.object(flow, "_save_document", side_effect=lambda c, r, v: v),
			patch("frappe.get_cached_value", return_value="Main"),
		):
			values = flow.invoice(context, record, sales=False, is_return=False)
		self.assertEqual(values["doctype"], "Purchase Invoice")
		self.assertEqual(values["items"][0]["rate"], 100.4)
		self.assertEqual(values["taxes"][1]["tax_amount"], -0.4)

	def test_wrong_account_fails_even_when_posted_totals_balance(self):
		context = FlowContext(
			"Company",
			"target",
			"Tally",
			options={"ledger_mappings": {"Cash": {"account": "Cash"}, "Income": {"account": "Income"}}},
		)
		record = {"ledger_entries": [{"ledger": "Cash", "amount": -100}, {"ledger": "Income", "amount": 100}]}
		with patch(
			"frappe.get_all",
			return_value=[
				frappe._dict(account="Wrong Cash", debit=100, credit=0),
				frappe._dict(account="Income", debit=0, credit=100),
			],
		):
			with self.assertRaisesRegex(ValueError, "differs"):
				NativeTransactionsFromTallyFlow().verify_gl(context, record, "Sales Invoice", "S1")

	def test_expense_payment_has_bank_entry_subtype(self):
		context = FlowContext(
			"Company",
			"target",
			"Tally",
			options={
				"ledger_mappings": {
					"Bank": {"account": "Bank", "account_type": "Bank"},
					"Fees": {"account": "Fees", "root_type": "Expense"},
				}
			},
		)
		flow = NativeTransactionsFromTallyFlow()
		with patch.object(flow, "journal", return_value="journal") as journal:
			self.assertEqual(
				flow.payment(
					context,
					{"ledger_entries": [{"ledger": "Bank", "amount": 10}, {"ledger": "Fees", "amount": -10}]},
					"payment",
				),
				"journal",
			)
		journal.assert_called_once_with(
			context,
			{"ledger_entries": [{"ledger": "Bank", "amount": 10}, {"ledger": "Fees", "amount": -10}]},
			"Bank Entry",
		)
		record = {
			"ledger_entries": [{"ledger": "Bank", "amount": 10}, {"ledger": "Fees", "amount": -10}],
			"date": "2025-04-02",
			"voucher_number": "3",
			"voucher_type": "Payment",
			"master_id": "43114",
		}
		with (
			patch.object(flow, "_save_document", side_effect=lambda c, r, v: v),
			patch("frappe.get_cached_value", return_value="Main"),
		):
			values = flow.journal(context, record, "Bank Entry")
		self.assertEqual(values["cheque_no"], "3")
		self.assertEqual(values["cheque_date"], "2025-04-02")
