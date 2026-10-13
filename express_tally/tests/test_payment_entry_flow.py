from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from express_tally.framework import FlowContext
from express_tally.integrations.payment_entry_flow import PAYMENT_FLOW, PaymentEntryMapper
from express_tally.integrations.tally_inbound_flows import TallyVouchersToERPNextFlow


class TestPaymentMapping(TestCase):
	def test_posted_gl_uses_base_amounts_and_customer_refunds(self):
		def value(doctype, name, fields, **kwargs):
			if doctype == "Customer":
				return "Customer One"
			return frappe._dict(
				account_name="Bank", account_type="Bank", root_type="Asset", company="Company"
			)

		rows = [
			{"account": "Debtors", "party": "CUST-1", "party_type": "Customer", "debit": 100, "credit": 0},
			{"account": "Bank", "debit": 0, "credit": 100},
		]
		with patch("frappe.db", new=Mock()) as db:
			db.get_value.side_effect = value
			mapped = PaymentEntryMapper(SimpleNamespace(key=PAYMENT_FLOW)).map_ledger_entries(rows, "Company")
		self.assertEqual(mapped["ledger_entries"][0]["amount"], "-100.00")
		self.assertEqual(mapped["masters"][0]["parent"], "Sundry Debtors")

	def test_inbound_receipt_with_deduction_and_no_party_header(self):
		flow = TallyVouchersToERPNextFlow()
		record = {
			"date": "2025-04-02",
			"voucher_number": "R1",
			"ledger_entries": [
				{"ledger": "Customer One", "amount": 100},
				{"ledger": "Fees", "amount": -2},
				{"ledger": "Cash", "amount": -98},
			],
		}
		module = "express_tally.integrations.tally_inbound_flows"
		with (
			patch(
				module + "._account_name", side_effect=lambda c, n: {"Cash": "Cash", "Fees": "Fees"}.get(n)
			),
			patch(
				module + "._party_name",
				side_effect=lambda t, n: "CUST-1" if t == "Customer" and n == "Customer One" else None,
			),
			patch(module + "._party_account", return_value="Debtors"),
			patch("frappe.db", new=Mock()) as db,
			patch("frappe.get_meta", return_value=Mock(has_field=lambda field: False)),
			patch.object(flow, "_save_document", side_effect=lambda c, r, v: v),
		):
			db.get_value.side_effect = lambda dt, n, f: (
				"INR"
				if f in {"account_currency", "default_currency"}
				else "Company"
				if f == "company"
				else {"Cash": "Cash", "Fees": "Expense"}.get(n, "Main")
			)
			mapped = flow._payment(FlowContext("Company", "target", "Tally"), record, "receipt")
		self.assertEqual(mapped["paid_amount"], 100)
		self.assertEqual(mapped["received_amount"], 98)
		self.assertEqual(mapped["paid_to"], "Cash")
		self.assertEqual(mapped["deductions"][0]["amount"], 2)

	def test_outbound_origin_is_skipped_on_inbound(self):
		context = FlowContext("Company", "target", "Tally", options={"excluded_origins": [PAYMENT_FLOW]})
		result = TallyVouchersToERPNextFlow().apply_record(
			context, {"narration": f"[ExpressTallyOrigin:{PAYMENT_FLOW}]", "voucher_type": "Receipt"}
		)
		self.assertEqual(result["status"], "Skipped")
