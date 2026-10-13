import tempfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from express_tally.bridge.config import BridgeConfig
from express_tally.bridge.inbound_profiles import CheckpointStore, TallyPaymentVouchersProfile
from express_tally.bridge.payment_gateway import build_payment_import
from express_tally.bridge.profiles import AgentProfileRegistry, PaymentVoucherProfile
from express_tally.bridge.xml_gateway import ImportResult, voucher_remote_id

PAYMENT_FLOW = "express_tally.payment_entries_to_tally"


def payment():
	return {
		"source_doctype": "Payment Entry",
		"name": "PAY-TEST-1",
		"operation": "Create",
		"voucher_type": "Receipt",
		"transaction_date": "2025-04-02",
		"identity_namespace": PAYMENT_FLOW,
		"origin_marker": PAYMENT_FLOW,
		"party_ledger": "Customer One",
		"ledger_entries": [
			{
				"ledger": "Customer One",
				"amount": "100.00",
				"is_party": True,
				"bill_allocations": [{"name": "INV-1", "bill_type": "Agst Ref", "amount": "100.00"}],
			},
			{"ledger": "Cash", "amount": "-98.00"},
			{"ledger": "Fees", "amount": "-2.00"},
		],
		"masters": [{"name": "Cash", "parent": "Cash-in-Hand"}],
	}


class TestPaymentGateway(TestCase):
	def test_receipt_retains_bank_fee_and_bill_allocation(self):
		root = ET.fromstring(build_payment_import(payment(), "Company", "target"))
		rows = root.findall(".//ALLLEDGERENTRIES.LIST")
		self.assertEqual(sum(Decimal(r.findtext("AMOUNT")) for r in rows), 0)
		self.assertEqual([r.findtext("ISDEEMEDPOSITIVE") for r in rows], ["No", "Yes", "Yes"])
		self.assertEqual(rows[0].findtext("BILLALLOCATIONS.LIST/NAME"), "INV-1")
		self.assertIn(f"[ExpressTallyOrigin:{PAYMENT_FLOW}]", root.findtext(".//NARRATION"))

	def test_unbalanced_payment_is_rejected_before_any_tally_write(self):
		doc = payment()
		doc["ledger_entries"][1]["amount"] = "-90"
		client = Mock()
		with self.assertRaisesRegex(ValueError, "balance"):
			PaymentVoucherProfile().deliver(doc, BridgeConfig(), client)
		client.import_json.assert_not_called()
		client.import_xml.assert_not_called()

	def test_bill_allocations_must_balance(self):
		doc = payment()
		doc["ledger_entries"][0]["bill_allocations"][0]["amount"] = "99"
		with self.assertRaisesRegex(ValueError, "bill allocations"):
			build_payment_import(doc, "Company", "target")

	def test_cancel_uses_destination_identity_without_master_imports(self):
		doc = dict(payment(), operation="Cancel", tally_voucher_id="guid:existing-guid")
		client = Mock()
		client.import_xml.return_value = ImportResult(success=True, last_voucher_id="42")
		self.assertEqual(
			PaymentVoucherProfile().deliver(
				doc, BridgeConfig(tally_company="Company", target_id="target"), client
			),
			"42",
		)
		client.import_json.assert_not_called()
		voucher = ET.fromstring(client.import_xml.call_args.args[0]).find(".//VOUCHER")
		self.assertEqual(voucher.attrib["TAGVALUE"], "existing-guid")
		self.assertEqual(voucher.findtext("ISCANCELLED"), "Yes")

	def test_retry_identity_is_stable_and_profile_registered(self):
		doc = payment()
		self.assertEqual(voucher_remote_id(doc, "target"), voucher_remote_id(deepcopy(doc), "target"))
		self.assertNotEqual(voucher_remote_id(doc, "target"), voucher_remote_id(doc, "other"))
		self.assertTrue(
			AgentProfileRegistry().get("payment_voucher_v1").supports_direction("erpnext_to_tally")
		)

	def test_lost_acknowledgement_reuses_tally_master_id(self):
		client = Mock()
		client.export_collection.return_value = (
			"<ENVELOPE><VOUCHER><MASTERID>42</MASTERID><ISCANCELLED>No</ISCANCELLED></VOUCHER></ENVELOPE>"
		)
		config = BridgeConfig(tally_company="Company", target_id="target")
		self.assertEqual(PaymentVoucherProfile().deliver(payment(), config, client), "42")
		client.import_xml.assert_not_called()
		client.import_json.assert_not_called()

	def test_payment_checkpoint_is_separate_from_all_vouchers(self):
		with tempfile.TemporaryDirectory() as directory:
			config = BridgeConfig(tally_company="Company", target_id="target", runtime_directory=directory)
			CheckpointStore(directory).advance("target:Company", "voucher", 100)
			client = Mock()
			client.export_collection.return_value = "<ENVELOPE><VOUCHER><MASTERID>42</MASTERID><ALTERID>12</ALTERID><GUID>guid</GUID><VOUCHERNUMBER>R1</VOUCHERNUMBER><VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME><DATE>20250402</DATE></VOUCHER></ENVELOPE>"
			profile = TallyPaymentVouchersProfile()
			records = profile.collect(config, client, 1)
			self.assertEqual(len(records), 1)
			profile.acknowledge_collected(config, records, [{"status": "Success"}])
			self.assertEqual(CheckpointStore(directory).get("target:Company:payments", "voucher"), 12)
			self.assertEqual(CheckpointStore(directory).get("target:Company", "voucher"), 100)
			CheckpointStore(directory).advance("target:Company:payments", "voucher", 100)
			scoped = profile.collect(config, client, 1, {"voucher_ids": [42]})
			self.assertEqual(len(scoped), 1)
			profile.acknowledge_collected(config, scoped, [{"status": "Success"}])
			self.assertEqual(CheckpointStore(directory).get("target:Company:payments", "voucher"), 100)
