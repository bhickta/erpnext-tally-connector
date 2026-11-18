import json
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from express_tally.bridge import auto_push
from express_tally.bridge.clients import BridgeRequestError
from express_tally.bridge.collection_gateway import parse_collection_export
from express_tally.bridge.config import BridgeConfig
from express_tally.bridge.inbound_profiles import CheckpointStore
from express_tally.bridge.inbound_profiles.base import IncrementalTallyProfile
from express_tally.bridge.json_gateway import parse_import_response as parse_json_import
from express_tally.bridge.xml_gateway import parse_import_response as parse_xml_import


class TestGatewayVerification(TestCase):
	def test_status_without_import_counters_is_not_success(self):
		self.assertFalse(parse_xml_import("<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER></ENVELOPE>").success)
		self.assertFalse(parse_json_import({"status": "1", "data": {"import_result": {}}}).success)
		self.assertEqual(parse_xml_import("<RESPONSE>Unknown Request</RESPONSE>").message, "Unknown Request")
		self.assertFalse(parse_xml_import("<RESPONSE><CREATED>invalid</CREATED><ALTERED>1</ALTERED></RESPONSE>").success)

	def test_export_errors_stop_consumers_that_did_not_opt_into_strict_parsing(self):
		with self.assertRaisesRegex(ValueError, "Unknown Request"):
			parse_collection_export("<RESPONSE>Unknown Request</RESPONSE>", ("VOUCHER",))
		self.assertEqual(
			parse_collection_export("<ENVELOPE><CMPINFO><LEDGER>25</LEDGER></CMPINFO></ENVELOPE>", ("LEDGER",)),
			[],
		)


class TestInboundCheckpointGaps(TestCase):
	def test_hierarchy_can_send_newer_parent_without_skipping_older_child(self):
		parent = {"_tally_type": "group", "_tally_key": "parent", "_tally_alter_id": 42, "_tally_depth": 0}
		child = {"_tally_type": "group", "_tally_key": "child", "_tally_alter_id": 10, "_tally_depth": 1}
		with tempfile.TemporaryDirectory() as directory:
			config = BridgeConfig(target_id="target", tally_company="Company", runtime_directory=directory)
			profile = IncrementalTallyProfile()
			first = profile._pending(config, [child, parent], 1)
			self.assertEqual(first, [parent])
			profile.acknowledge_collected(config, first, [{"source": "parent", "status": "Success"}])
			self.assertLess(profile._checkpoint_store(config).get("target:Company", "group"), 10)
			# A fresh process has the same pending child and does not resend the parent.
			profile = IncrementalTallyProfile()
			second = profile._pending(config, [child, parent], 1)
			self.assertEqual(second, [child])
			profile.acknowledge_collected(config, second, [{"source": "child", "status": "Success"}])
			self.assertEqual(profile._checkpoint_store(config).get("target:Company", "group"), 42)

	def test_reordered_acknowledgements_preserve_failed_older_record(self):
		older = {"_tally_type": "voucher", "_tally_key": "old", "_tally_alter_id": 10}
		newer = {"_tally_type": "voucher", "_tally_key": "new", "_tally_alter_id": 11}
		with tempfile.TemporaryDirectory() as directory:
			config = BridgeConfig(runtime_directory=directory)
			profile = IncrementalTallyProfile()
			batch = profile._pending(config, [older, newer], 2)
			profile.acknowledge_collected(config, batch, [
				{"source": "new", "source_alter_id": 11, "status": "Success"},
				{"source": "old", "source_alter_id": 10, "status": "Failed"},
			])
			self.assertEqual(profile._pending(config, [older, newer], 2), [older])
			with self.assertRaisesRegex(ValueError, "one acknowledgement per"):
				profile.acknowledge_collected(config, [older], [])


	def test_reset_includes_scoped_checkpoints_and_leaves_other_companies(self):
		with tempfile.TemporaryDirectory() as directory:
			store = CheckpointStore(directory)
			store.advance("target:Company:payments", "voucher", 20)
			store.advance("target:Company:native:1234567890abcdef", "voucher", 30)
			store.advance("target:Company:Other", "voucher", 40)
			self.assertTrue(store.reset("target:Company", include_scopes=True))
			self.assertEqual(store.get("target:Company:payments", "voucher"), 0)
			self.assertEqual(store.get("target:Company:native:1234567890abcdef", "voucher"), 0)
			self.assertEqual(store.get("target:Company:Other", "voucher"), 40)


class TestStockPushCursor(TestCase):
	def test_cursor_stops_at_failed_row_even_when_timestamps_match(self):
		config = BridgeConfig(erpnext_company="ERP", tally_company="Tally", target_id="target")
		tally = Mock()
		tally.get_current_company.return_value = "Tally"
		frappe = Mock()
		stamp = "2026-07-01 08:00:00"
		rows = [{"name": name, "modified": stamp} for name in ("STE-1", "STE-2", "STE-3")]
		frappe.request.side_effect = [
			rows, {**rows[0], "company": "ERP"}, {**rows[1], "company": "ERP"}, {},
		]
		with patch.object(auto_push, "deliver_stock_entry", side_effect=["42", BridgeRequestError("stock missing")]):
			result = auto_push.run_once(config, frappe, tally)
		self.assertEqual(result["Stock Entry"]["delivered"], 1)
		self.assertEqual(result["Stock Entry"]["failed"], ["STE-2: stock missing"])
		method, path, ack = frappe.request.call_args.args
		self.assertEqual((method, path), ("POST", auto_push.ACK_PATH))
		self.assertEqual(json.loads(ack["cursor"]), [stamp, "STE-1"])
		self.assertEqual((ack["company"], ack["target_id"]), ("ERP", "target"))

	def test_stock_journal_rejects_a_transfer_with_the_source_and_destination_swapped(self):
		from express_tally.bridge.common import stable_digest
		from express_tally.bridge.xml_gateway import ImportResult, voucher_remote_id

		config = BridgeConfig(tally_company="Tally", target_id="target")
		document = {
			"name": "STE-1", "doctype": "Stock Entry", "posting_date": "2026-07-01",
			"items": [{
				"item_code": "ITEM-1", "qty": 2, "uom": "Nos", "rate": 25, "amount": 50,
				"source_warehouse": "Stores", "target_warehouse": "Dispatch",
			}],
		}
		payload = auto_push.stock_entry_document(document)
		guid = voucher_remote_id(payload, config.target_id)

		def line(tag, godown):
			return f"""<{tag}.LIST><STOCKITEMNAME>ITEM-1</STOCKITEMNAME>
				<ACTUALQTY>2 Nos</ACTUALQTY><BILLEDQTY>2 Nos</BILLEDQTY>
				<GODOWNNAME>{godown}</GODOWNNAME></{tag}.LIST>"""

		tally = Mock()
		tally.import_xml.return_value = ImportResult(success=True, created=1)
		tally.export_collection.side_effect = [
			"<ENVELOPE><BODY><DATA><COLLECTION/></DATA></BODY></ENVELOPE>",
			f"""<ENVELOPE><VOUCHER><MASTERID>42</MASTERID><GUID>{guid}</GUID>
				<VOUCHERTYPENAME>Stock Journal</VOUCHERTYPENAME><DATE>20260701</DATE>
				<NARRATION>[ExpressTallyVersion:{stable_digest(payload)}]</NARRATION>
				{line('INVENTORYENTRIESOUT', 'Dispatch')}{line('INVENTORYENTRIESIN', 'Stores')}
				</VOUCHER></ENVELOPE>""",
		]
		with self.assertRaisesRegex(BridgeRequestError, "did not retain InventoryEntriesOut"):
			auto_push.deliver_stock_entry(config, tally, document)
