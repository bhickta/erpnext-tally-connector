import tempfile
import xml.etree.ElementTree as ET
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock

from express_tally.bridge.config import BridgeConfig
from express_tally.bridge.inbound_profiles import CheckpointStore, TallyLedgerMirrorProfile
from express_tally.bridge.ledger_gateway import build_ledger_import, debit_balance


class TestLedgerBridgeProtocol(TestCase):
	def test_balance_validation_ignores_company_object_counts(self):
		profile = TallyLedgerMirrorProfile()
		profile._ledger_scope = ["Cash"]
		profile._validate_period = True
		tally = Mock()
		tally.export_collection.return_value = '<ENVELOPE><CMPINFO><LEDGER>90</LEDGER></CMPINFO><COLLECTION><LEDGER NAME="Cash"><CLOSINGBALANCE>-12</CLOSINGBALANCE><ETPERIODFROM>31-Mar-26</ETPERIODFROM><ETPERIODTO>31-Mar-26</ETPERIODTO></LEDGER></COLLECTION></ENVELOPE>'
		rows = profile._ledgers(BridgeConfig(tally_company="Company"), tally, "2026-03-31", "2026-03-31")
		self.assertEqual(len(rows), 1)

	def test_invalid_tally_balance_period_is_not_a_zero_balance(self):
		profile = TallyLedgerMirrorProfile()
		profile._ledger_scope = ["Cash"]
		profile._validate_period = True
		tally = Mock()
		tally.export_collection.return_value = '<ENVELOPE><LEDGER NAME="Cash"><CLOSINGBALANCE>0</CLOSINGBALANCE><ETPERIODFROM>1-Apr-26</ETPERIODFROM></LEDGER></ENVELOPE>'
		with self.assertRaisesRegex(ValueError, "did not accept"):
			profile._ledgers(BridgeConfig(tally_company="Company"), tally, "2026-04-01", "2026-04-30")

	def test_zero_opening_update_uses_replace_not_ignore_or_combine(self):
		document = {"ledger_name": "Cash", "opening_balance": "0.00", "parent": "Cash-in-Hand"}
		alter = ET.fromstring(build_ledger_import(document, "Company", existing=True))
		self.assertEqual(alter.findtext(".//IMPORTDUPS"), "@@DUPMODIFY")
		self.assertEqual(alter.findtext(".//OPENINGBALANCE"), "0.00")
		create = ET.fromstring(build_ledger_import(document, "Company"))
		self.assertEqual(create.findtext(".//IMPORTDUPS"), "@@DUPIGNORE")

	def test_balance_direction_accepts_signed_and_dr_cr_formats(self):
		self.assertEqual(debit_balance("-1,200.50"), Decimal("1200.50"))
		self.assertEqual(debit_balance("1200.50 Dr"), Decimal("1200.50"))
		self.assertEqual(debit_balance("1200.50 Cr"), Decimal("-1200.50"))

	def test_cancelled_voucher_remains_in_scope_after_ledger_lines_disappear(self):
		with tempfile.TemporaryDirectory() as directory:
			config = BridgeConfig(target_id="target", tally_company="Company", runtime_directory=directory)
			profile = TallyLedgerMirrorProfile()
			profile._ledger_scope = ["Cash", "Bank"]
			profile._scope_key = "selected"
			CheckpointStore(directory).remember_vouchers(profile._target_key(config), [42])
			tally = Mock()
			tally.export_collection.return_value = "<ENVELOPE><VOUCHER><MASTERID>42</MASTERID><ALTERID>11</ALTERID><GUID>g42</GUID><VOUCHERNUMBER>J1</VOUCHERNUMBER><VOUCHERTYPENAME>Journal</VOUCHERTYPENAME><DATE>20260402</DATE><ISCANCELLED>Yes</ISCANCELLED></VOUCHER></ENVELOPE>"
			rows = profile._vouchers(config, tally, "2026-04-01", "2026-04-30", {}, {})
			self.assertTrue(rows[0]["cancelled"])
			self.assertEqual(rows[0]["ledger_entries"], [])
			request = ET.fromstring(tally.export_collection.call_args.args[0])
			self.assertTrue(any("$MasterID = 42" in row.text for row in request.findall(".//SYSTEM")))
			self.assertIn("$Date >= ##SVFromDate", [row.text for row in request.findall(".//SYSTEM")])
			self.assertIn(
				"NOT $$IsOrderVch:$VoucherTypeName", [row.text for row in request.findall(".//SYSTEM")]
			)
			self.assertEqual(request.findtext(".//SVFROMDATE"), "20260401")
			self.assertEqual(request.find(".//SVFROMDATE").get("TYPE"), "Date")
			self.assertEqual(request.find(".//SVTODATE").get("TYPE"), "Date")
