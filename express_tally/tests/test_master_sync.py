import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from express_tally.bridge.config import ConfigStore
from express_tally.bridge.controller import ControlCentre
from express_tally.bridge.http_server import BridgeHTTPServer
from express_tally.bridge.json_gateway import ImportResult
from express_tally.bridge.master_sync import MasterSyncService
from express_tally.integrations import master_sync_export as export


class TestMasterAliases(TestCase):
	def test_alias_keeps_namespaced_identity_when_name_matches(self):
		aliases = export._alias_list(
			"Bala Ji Strips",
			export._identity_alias("Bala Ji Strips", "Bala Ji Strips", "Customer"),
		)

		names = aliases[0]["name"]
		self.assertEqual(names[1], "Bala Ji Strips")
		self.assertIn("ERPNext:Customer:Bala Ji Strips", names)

	def test_alias_keeps_raw_id_and_namespaced_identity_when_name_differs(self):
		aliases = export._alias_list(
			"Acme",
			export._identity_alias("Acme", "CUST-0001", "Customer"),
		)

		names = aliases[0]["name"]
		self.assertEqual(names[1], "Acme")
		self.assertIn("CUST-0001", names)
		self.assertIn("ERPNext:Customer:CUST-0001", names)


class TestSeparateMasterTypes(TestCase):
	def test_unit_and_item_group_are_independently_selectable(self):
		from express_tally.bridge.master_sync import MASTER_KEYS, normalize_masters
		from express_tally.integrations.master_sync.constants import MASTER_SYNC_TYPES
		from express_tally.integrations.master_sync.payload import _parse_master_types

		self.assertIn("Unit", MASTER_SYNC_TYPES)
		self.assertIn("Item Group", MASTER_SYNC_TYPES)
		self.assertIn("Unit", MASTER_KEYS)
		self.assertIn("Item Group", MASTER_KEYS)
		self.assertEqual(
			_parse_master_types(["Unit", "Item Group"]), ["Unit", "Item Group"]
		)
		self.assertEqual(
			normalize_masters(["Unit", "Item Group"]), ["Unit", "Item Group"]
		)
		self.assertIn("Unit", normalize_masters(None))
		self.assertIn("Item Group", normalize_masters(None))


class TestNamePolicy(TestCase):
	def test_item_names_use_display_and_suffix_duplicates(self):
		items = [
			{"name": "I1", "item_code": "I1", "item_name": "Widget"},
			{"name": "I2", "item_code": "I2", "item_name": "Widget"},
			{"name": "I3", "item_code": "I3", "item_name": "Unique"},
		]

		names = export._item_tally_names(items)

		self.assertEqual(names["I1"], "Widget (I1)")
		self.assertEqual(names["I2"], "Widget (I2)")
		self.assertEqual(names["I3"], "Unique")

	def test_party_duplicates_skip_or_suffix(self):
		rows = [
			{"name": "C1", "customer_name": "Same"},
			{"name": "C2", "customer_name": "Same"},
			{"name": "C3", "customer_name": "Other"},
		]

		names, skipped = export._party_tally_names(rows, "Customer", "skip")
		self.assertEqual(names, {"C1": "Same", "C3": "Other"})
		self.assertEqual(skipped[0]["erp_name"], "C2")

		names, skipped = export._party_tally_names(rows, "Customer", "suffix")
		self.assertEqual(names["C1"], "Same (C1)")
		self.assertEqual(names["C2"], "Same (C2)")
		self.assertFalse(skipped)

	def test_warehouse_root_is_excluded_and_children_use_primary(self):
		self.assertTrue(
			export._is_warehouse_root(
				{
					"name": "All Warehouses - TC",
					"warehouse_name": "All Warehouses",
					"parent_warehouse": "",
				}
			)
		)
		self.assertFalse(
			export._is_warehouse_root(
				{
					"name": "Stores - TC",
					"warehouse_name": "Stores",
					"parent_warehouse": "All Warehouses - TC",
				}
			)
		)
		message = export._godown_message(
			{
				"name": "Stores - TC",
				"warehouse_name": "Stores",
				"parent_warehouse": "All Warehouses - TC",
			},
			"TC",
		)
		self.assertEqual(message["parent"], export.TALLY_PRIMARY)


class TestAccountMapping(TestCase):
	def test_groups_and_ledgers_map_with_aliases_and_parents(self):
		accounts = [
			{
				"name": "Application of Funds (Assets)",
				"account_name": "Application of Funds (Assets)",
				"parent_account": "",
				"is_group": 1,
				"root_type": "Asset",
				"account_type": "",
				"account_currency": "INR",
				"disabled": 0,
			},
			{
				"name": "Current Assets",
				"account_name": "Current Assets",
				"parent_account": "Application of Funds (Assets)",
				"is_group": 1,
				"root_type": "Asset",
				"account_type": "",
				"account_currency": "INR",
				"disabled": 0,
			},
			{
				"name": "Debtors",
				"account_name": "Debtors",
				"parent_account": "Current Assets",
				"is_group": 1,
				"root_type": "Asset",
				"account_type": "Receivable",
				"account_currency": "INR",
				"disabled": 0,
			},
			{
				"name": "Cash",
				"account_name": "Cash",
				"parent_account": "Application of Funds (Assets)",
				"is_group": 0,
				"root_type": "Asset",
				"account_type": "Cash",
				"account_currency": "INR",
				"disabled": 0,
			},
			{
				"name": "Bank USD",
				"account_name": "Bank USD",
				"parent_account": "Current Assets",
				"is_group": 0,
				"root_type": "Asset",
				"account_type": "Bank",
				"account_currency": "USD",
				"disabled": 0,
				"account_number": "123",
			},
		]

		groups, ledgers = export._account_messages(accounts, "TC", company_currency="INR")
		group_names = {row["name"]: row for row in groups}
		ledger_names = {row["name"]: row for row in ledgers}

		# Tally's standard Current Assets group is reused, not duplicated.
		self.assertNotIn("Current Assets", group_names)
		self.assertIn("Debtors", group_names)
		self.assertEqual(group_names["Debtors"]["parent"], "Current Assets")
		self.assertIn(
			"ERPNext:Account:Debtors", group_names["Debtors"]["language name"][0]["name"]
		)

		self.assertEqual(ledger_names["Cash"]["parent"], "Cash-in-Hand")
		self.assertEqual(ledger_names["Cash"]["ledgercode"], "Cash")
		self.assertFalse(ledger_names["Cash"]["isbillwiseon"])
		self.assertEqual(ledger_names["Bank USD"]["currency"], "USD")
		self.assertEqual(ledger_names["Bank USD"]["ledgercode"], "123")
		self.assertIn(
			"ERPNext:Account:Bank USD",
			ledger_names["Bank USD"]["language name"][0]["name"],
		)


class TestPartyMapping(TestCase):
	def test_customer_ledger_maps_identity_gst_address_and_contact(self):
		customer = {
			"name": "CUST-0001",
			"customer_name": "Acme",
			"tax_id": "24AAQCA8719H1ZC",
			"gst_category": "Registered Regular",
			"customer_type": "Company",
			"customer_group": "Commercial",
			"territory": "All Territories",
			"disabled": 0,
		}
		address = {
			"address_line1": "1 Main Street",
			"address_line2": "Industrial Area",
			"city": "Ahmedabad",
			"state": "Gujarat",
			"country": "India",
			"pincode": "380001",
			"email_id": "billing@example.com",
			"phone": "07912345678",
		}
		contact = {
			"full_name": "Asha Patel",
			"email_id": "asha@example.com",
			"phone": "07912345678",
			"mobile_no": "9999999999",
		}

		message = export._ledger_message(
			customer,
			"Customer",
			"Test Company",
			address,
			contact,
			100,
			posting_date="2026-04-01",
		)

		self.assertEqual(message["metadata"]["type"], "Ledger")
		self.assertEqual(message["name"], "Acme")
		self.assertEqual(message["parent"], "Sundry Debtors")
		self.assertEqual(message["ledgercode"], "CUST-0001")
		self.assertEqual(message["partygstin"], "24AAQCA8719H1ZC")
		self.assertEqual(message["incometaxnumber"], "AAQCA8719H")
		self.assertEqual(message["gstregistrationtype"], "Regular")
		self.assertEqual(message["ledstatename"], "Gujarat")
		self.assertEqual(message["pincode"], "380001")
		self.assertEqual(message["ledgercontact"], "Asha Patel")
		self.assertEqual(message["ledgermobile"], "9999999999")
		self.assertEqual(message["creditlimit"], "100.00")
		self.assertEqual(message["address"][1], "1 Main Street")
		self.assertEqual(message["ledmailingdetails"][0]["applicablefrom"], "20260401")
		self.assertIn("ERPNext:Customer:CUST-0001", message["language name"][0]["name"])

	def test_customer_parent_is_configurable(self):
		message = export._ledger_message(
			{"name": "C1", "customer_name": "Acme", "country": "India", "disabled": 0},
			"Customer",
			"Test Company",
			None,
			None,
			0,
			parent="Custom Debtors",
		)

		self.assertEqual(message["parent"], "Custom Debtors")

	def test_warehouse_godown_maps_parent_and_alias(self):
		message = export._godown_message(
			{
				"name": "Stores - TC",
				"warehouse_name": "Stores",
				"parent_warehouse": "All Warehouses - TC",
			},
			"TC",
		)

		self.assertEqual(message["metadata"]["type"], "Godown")
		self.assertEqual(message["name"], "Stores")
		self.assertEqual(message["parent"], export.TALLY_PRIMARY)
		self.assertIn("Stores - TC", message["language name"][0]["name"])
		self.assertIn("ERPNext:Warehouse:Stores - TC", message["language name"][0]["name"])


class TestItemMapping(TestCase):
	def test_item_mapping_includes_alias_dependencies_and_commercial_fields(self):
		items = [
			{
				"name": "ITEM-1",
				"item_code": "ITEM-1",
				"item_name": "Item One",
				"description": "<p>Description</p>",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_batch_no": 1,
				"has_expiry_date": 0,
				"gst_hsn_code": "1234",
				"valuation_method": "FIFO",
				"valuation_rate": 10,
				"standard_rate": 12,
				"safety_stock": 5,
				"min_order_qty": 2,
				"lead_time_days": 3,
				"weight_per_unit": 1.5,
				"weight_uom": "Kg",
				"brand": "Brand One",
				"disabled": 0,
			}
		]
		rows = {
			"Item Barcode": [{"parent": "ITEM-1", "barcode": "123456", "uom": "Nos"}],
			"UOM Conversion Detail": [
				{"parent": "ITEM-1", "uom": "Box", "conversion_factor": 10}
			],
			"Item Reorder": [
				{
					"parent": "ITEM-1",
					"warehouse": "Stores",
					"warehouse_reorder_level": 7,
					"warehouse_reorder_qty": 9,
				}
			],
		}

		with patch(
			"express_tally.integrations.master_sync.items._fetch_child_rows",
			side_effect=lambda doctype, names, fields: rows.get(doctype, []),
		):
			messages = export._item_messages(
				items,
				"TC",
				{"ITEM-1": "Item One"},
				posting_date="2026-04-01",
			)

		message = messages[0]
		self.assertEqual(message["metadata"]["type"], "Stock Item")
		self.assertEqual(message["name"], "Item One")
		self.assertEqual(message["partno"], "ITEM-1")
		self.assertEqual(message["parent"], "Products")
		self.assertEqual(message["base units"], "Nos")
		self.assertEqual(message["gstapplicable"], "Applicable")
		self.assertEqual(message["gsttypeofsupply"], "Goods")
		self.assertTrue(message["isbatchwiseon"])
		self.assertEqual(message["hsncode"], "1234")
		self.assertEqual(message["valuationmethod"], "FIFO")
		self.assertEqual(message["standardcostlist"][0]["date"], "20260401")
		self.assertEqual(message["standardcostlist"][0]["rate"], "10.00/Nos")
		self.assertEqual(message["standardpricelist"][0]["rate"], "12.00/Nos")
		self.assertEqual(message["reorderlevel"], "7.00")
		self.assertEqual(message["barcodes"][0]["barcode"], "123456")
		self.assertEqual(message["uoms"][0]["uom"], "Box")
		self.assertIn("ERPNext:Item:ITEM-1", message["language name"][0]["name"])
		self.assertIn("Item One", message["language name"][0]["name"])


class FakeFrappe:
	def __init__(self, batches):
		self.batches = batches
		self.calls = []

	def request(self, method, path, data=None):
		self.calls.append((method, path, data))
		if path.endswith("get_master_sync_summary"):
			return {"Item": 5}
		return {"batches": self.batches}


class FakeTally:
	def __init__(self, fail_on=None, existing_units=None):
		self.requests = []
		self.xml_requests = []
		self.exports = []
		self.fail_on = fail_on
		self.existing_units = existing_units or set()

	def export_collection(self, request):
		self.exports.append(request)
		if "<TYPE>Unit</TYPE>" in request:
			# Real Tally Unit exports can expose only the NAME attribute.
			rows = "".join(
				f'<UNIT NAME="{name}"></UNIT>' for name in sorted(self.existing_units)
			)
			return f"<ENVELOPE><DATA><COLLECTION>{rows}</COLLECTION></DATA></ENVELOPE>"
		return "<ENVELOPE><DATA><COLLECTION></COLLECTION></DATA></ENVELOPE>"

	def import_json(self, payload, report_id, require_change=False, allow_ignored=False):
		self.requests.append((payload, report_id))
		if self.fail_on is not None and len(self.requests) == self.fail_on:
			return ImportResult(success=False, errors=1, message="Tally rejected the batch")
		return ImportResult(success=True, created=len(payload["tallymessage"]))

	def import_xml(self, xml, require_change=False, allow_ignored=False):
		self.xml_requests.append(xml)
		if self.fail_on is not None and len(self.xml_requests) == self.fail_on:
			return ImportResult(success=False, errors=1, message="Tally rejected the batch")
		return ImportResult(success=True, created=1)


class TestMasterSyncService(TestCase):
	def config(self):
		return SimpleNamespace(erpnext_company="ERP Company", tally_company="Tally Company")

	def test_sync_chunks_requests_and_aggregates_results(self):
		batches = [
			{
				"key": "items",
				"label": "Items",
				"tallymessage": [{"name": f"ITEM-{index}"} for index in range(5)],
			}
		]
		frappe = FakeFrappe(batches)
		tally = FakeTally()
		service = MasterSyncService(self.config(), frappe, tally)

		with patch("express_tally.bridge.master_sync.MASTER_BATCH_SIZE", 2):
			result = service.sync(["Item"], limit=3)

		self.assertTrue(result["ok"])
		self.assertEqual(len(tally.requests), 3)
		self.assertEqual(result["results"][0]["created"], 5)
		self.assertEqual(result["results"][0]["requests"], 3)
		self.assertEqual(result["summary"], {"Item": 5})
		payload_call = next(call for call in frappe.calls if call[1].endswith("get_master_sync_payload"))
		self.assertEqual(payload_call[2]["limit"], 3)
		for payload, report_id in tally.requests:
			self.assertEqual(report_id, "All Masters")
			self.assertEqual(payload["static_variables"][1]["value"], "Tally Company")

	def test_existing_tally_unit_is_skipped_instead_of_altered(self):
		batches = [
			{
				"key": "units",
				"label": "Units",
				"tallymessage": [
					{
						"metadata": {"type": "Unit", "action": "create", "name": "14 Pkg"},
						"name": "14 Pkg",
					}
				],
			}
		]
		frappe = FakeFrappe(batches)
		tally = FakeTally(existing_units={"14 Pkg"})
		service = MasterSyncService(self.config(), frappe, tally)

		result = service.sync(["Item"])

		self.assertTrue(result["ok"])
		self.assertEqual(len(tally.exports), 1)
		self.assertEqual(tally.requests, [])
		self.assertIn("already exist", result["results"][0]["message"])

	def test_new_units_use_xml_master_import(self):
		batches = [
			{
				"key": "units",
				"label": "Units",
				"tallymessage": [
					{
						"metadata": {"type": "Unit", "action": "create", "name": "New Unit"},
						"name": "New Unit",
						"originalname": "New Unit",
						"issimpleunit": True,
						"decimalplaces": "3",
					}
				],
			}
		]
		tally = FakeTally()
		service = MasterSyncService(self.config(), FakeFrappe(batches), tally)

		result = service.sync(["Unit"])

		self.assertTrue(result["ok"])
		self.assertEqual(result["results"][0]["created"], 1)
		self.assertEqual(tally.requests, [])
		self.assertEqual(len(tally.xml_requests), 1)
		xml = tally.xml_requests[0]
		self.assertIn("<TALLYREQUEST>Import Data</TALLYREQUEST>", xml)
		self.assertIn("<IMPORTDATA>", xml)
		self.assertIn("<REPORTNAME>All Masters</REPORTNAME>", xml)
		self.assertIn("<REQUESTDATA>", xml)
		self.assertIn('<UNIT Action="Create">', xml)
		self.assertIn("<NAME>New Unit</NAME>", xml)
		self.assertIn("<ORIGINALNAME>New Unit</ORIGINALNAME>", xml)
		self.assertIn("<ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>", xml)
		self.assertIn("<DECIMALPLACES>3</DECIMALPLACES>", xml)
		self.assertIn("<SVCURRENTCOMPANY>Tally Company</SVCURRENTCOMPANY>", xml)

	def test_sync_can_be_cancelled_before_the_first_batch(self):
		batches = [
			{
				"key": "items",
				"label": "Items",
				"tallymessage": [{"name": "ITEM-1"}],
			}
		]
		frappe = FakeFrappe(batches)
		tally = FakeTally()
		service = MasterSyncService(self.config(), frappe, tally)
		cancel_event = threading.Event()
		cancel_event.set()

		result = service.sync(["Item"], cancel_event=cancel_event)

		self.assertTrue(result["cancelled"])
		self.assertFalse(result["ok"])
		self.assertEqual(result["results"][0]["status"], "Cancelled")
		self.assertEqual(len(tally.requests), 0)

	def test_sync_stops_after_a_failed_chunk(self):
		batches = [
			{
				"key": "items",
				"label": "Items",
				"tallymessage": [{"name": f"ITEM-{index}"} for index in range(5)],
			},
			{"key": "other", "label": "Other", "tallymessage": [{"name": "OTHER-1"}]},
		]
		frappe = FakeFrappe(batches)
		tally = FakeTally(fail_on=2)
		service = MasterSyncService(self.config(), frappe, tally)

		with patch("express_tally.bridge.master_sync.MASTER_BATCH_SIZE", 2):
			result = service.sync(["Item"])

		self.assertFalse(result["ok"])
		self.assertEqual(len(tally.requests), 2)
		self.assertEqual(result["results"][0]["status"], "Failed")
		self.assertEqual(result["results"][1]["status"], "Skipped")


class TestMasterTrace(TestCase):
	def config(self, directory):
		return SimpleNamespace(
			erpnext_company="ERP Company",
			tally_company="Tally Company",
			api_secret="super-secret-value",
			runtime_directory=directory,
		)

	def read_events(self, directory, result):
		self.assertTrue(result["trace_id"])
		self.assertTrue(result["trace_id"].startswith("masters-"))
		self.assertTrue(result["trace_id"].endswith(".jsonl"))
		path = Path(directory) / "traces" / result["trace_id"]
		self.assertTrue(path.is_file())
		content = path.read_text(encoding="utf-8")
		self.assertNotIn("super-secret-value", content)
		return [json.loads(line) for line in content.splitlines() if line.strip()]

	def test_sync_writes_a_debug_trace_and_returns_trace_id(self):
		batches = [
			{
				"key": "items",
				"label": "Items",
				"tallymessage": [{"name": "ITEM-1"}, {"name": "ITEM-2"}],
			}
		]
		with tempfile.TemporaryDirectory() as directory:
			frappe = FakeFrappe(batches)
			tally = FakeTally()
			service = MasterSyncService(self.config(directory), frappe, tally)

			with patch("express_tally.bridge.master_sync.MASTER_BATCH_SIZE", 1):
				result = service.sync(["Item"])

			events = self.read_events(directory, result)

		self.assertEqual(events[0]["event"], "sync_start")
		self.assertEqual(events[0]["selected"], ["Item"])
		self.assertEqual(events[-1]["event"], "sync_end")
		names = [event["event"] for event in events]
		for expected in (
			"payload",
			"lookup_start",
			"lookup_result",
			"chunk_start",
			"chunk_result",
			"batch_result",
			"sync_end",
		):
			self.assertIn(expected, names)
		chunks = [event for event in events if event["event"] == "chunk_start"]
		self.assertEqual([event["size"] for event in chunks], [1, 1])
		results = [event for event in events if event["event"] == "chunk_result"]
		self.assertTrue(all(event["success"] for event in results))
		batch = next(event for event in events if event["event"] == "batch_result")
		self.assertEqual(batch["status"], "Success")
		self.assertEqual(batch["created"], 2)
		self.assertTrue(events[-1]["ok"])

	def test_trace_records_skipped_existing_units(self):
		batches = [
			{
				"key": "units",
				"label": "Units",
				"tallymessage": [
					{"metadata": {"type": "Unit", "action": "create", "name": "14 Pkg"}, "name": "14 Pkg"},
					{"metadata": {"type": "Unit", "action": "create", "name": "New Unit"}, "name": "New Unit"},
				],
			}
		]
		with tempfile.TemporaryDirectory() as directory:
			service = MasterSyncService(
				self.config(directory),
				FakeFrappe(batches),
				FakeTally(existing_units={"14 Pkg"}),
			)
			result = service.sync(["Unit"])
			events = self.read_events(directory, result)

		skip = next(event for event in events if event["event"] == "skip_existing")
		self.assertEqual(skip["count"], 1)
		self.assertEqual(skip["names"], ["14 Pkg"])
		chunks = [event for event in events if event["event"] == "chunk_start"]
		self.assertEqual(chunks[0]["names"], ["New Unit"])

	def test_failed_sync_still_writes_trace_and_attaches_trace_id(self):
		class BrokenFrappe:
			def request(self, method, path, data=None):
				raise RuntimeError("ERPNext rejected the payload")

		with tempfile.TemporaryDirectory() as directory:
			service = MasterSyncService(
				self.config(directory), BrokenFrappe(), FakeTally()
			)
			with self.assertRaises(RuntimeError) as caught:
				service.sync(["Item"])
			trace_id = caught.exception.trace_id
			path = Path(directory) / "traces" / trace_id
			self.assertTrue(path.is_file())
			events = [
				json.loads(line)
				for line in path.read_text(encoding="utf-8").splitlines()
				if line.strip()
			]

		self.assertEqual(events[-1]["event"], "sync_error")
		self.assertIn("ERPNext rejected the payload", events[-1]["error"])

	def test_trace_files_are_pruned_to_the_latest_runs(self):
		from express_tally.bridge.master_trace import MasterTrace

		with tempfile.TemporaryDirectory() as directory:
			trace_dir = Path(directory) / "traces"
			trace_dir.mkdir(parents=True)
			for index in range(25):
				(trace_dir / f"masters-202601{index:02d}T000000Z-{index:08x}.jsonl").write_text(
					"{}\n", encoding="utf-8"
				)

			trace = MasterTrace(self.config(directory), ["Item"], {})
			self.assertTrue(trace.available)
			self.assertLessEqual(len(list(trace_dir.glob("masters-*.jsonl"))), 20)


class FakeMasterService:
	def metadata(self):
		return {
			"masters": [
				{
					"key": "Item",
					"label": "Items",
					"tally_type": "Stock Item",
					"description": "Item, Item Group and UOM masters",
				}
			]
		}

	def summary(self):
		return {"Item": 5, "Item Group": 2, "UOM": 3}

	def sync(
		self,
		masters=None,
		include_disabled=False,
		include_opening_balances=False,
		limit=None,
		duplicate_names="skip",
		cancel_event=None,
	):
		return {
			"ok": True,
			"masters": masters or ["Item"],
			"results": [
				{
					"batch": "items",
					"label": "Items",
					"status": "Success",
					"created": 5,
					"altered": 0,
					"ignored": 0,
					"errors": 0,
					"requests": 1,
					"total": 5,
				}
			],
			"summary": {"Item": 5},
		}


class TestMasterSyncHTTP(TestCase):
	def test_masters_endpoints_return_summary_and_run_sync(self):
		with tempfile.TemporaryDirectory() as directory:
			config_path = Path(directory) / "tally-bridge.json"
			ConfigStore(config_path).update(
				{
					"frappe_url": "https://erp.example.com",
					"api_key": "key",
					"api_secret": "secret",
					"erpnext_company": "ERP Company",
					"target_id": "target-1",
					"tally_company": "Tally Company",
					"enabled_flows": ["test.outbound"],
				}
			)
			controller = ControlCentre(config_path)
			controller._master_service = Mock(return_value=FakeMasterService())
			server = BridgeHTTPServer(("127.0.0.1", 0), controller)
			thread = threading.Thread(target=server.serve_forever, daemon=True)
			thread.start()
			base_url = f"http://127.0.0.1:{server.server_port}"
			try:
				with urllib.request.urlopen(f"{base_url}/api/v1/masters", timeout=2) as response:
					summary = json.loads(response.read())
				self.assertEqual(summary["masters"][0]["key"], "Item")
				self.assertEqual(summary["summary"]["Item"], 5)

				request = urllib.request.Request(
					f"{base_url}/api/v1/masters/sync",
					data=json.dumps({"masters": ["Item"]}).encode(),
					headers={"Content-Type": "application/json"},
					method="POST",
				)
				with urllib.request.urlopen(request, timeout=2) as response:
					result = json.loads(response.read())
				self.assertTrue(result["ok"])
				self.assertEqual(result["results"][0]["created"], 5)

				cancel_request = urllib.request.Request(
					f"{base_url}/api/v1/masters/cancel",
					data=b"{}",
					headers={"Content-Type": "application/json"},
					method="POST",
				)
				with urllib.request.urlopen(cancel_request, timeout=2) as response:
					cancel = json.loads(response.read())
				self.assertFalse(cancel["cancelled"])

				trace_dir = Path(directory) / "traces"
				trace_dir.mkdir()
				trace_id = "masters-20260101T000000Z-abcdef12.jsonl"
				trace_content = '{"event":"sync_start","selected":["Item"]}\n'
				(trace_dir / trace_id).write_text(trace_content, encoding="utf-8")
				with urllib.request.urlopen(
					f"{base_url}/api/v1/masters/trace?id={trace_id}", timeout=2
				) as response:
					self.assertEqual(response.status, 200)
					self.assertIn(
						"attachment", response.headers["Content-Disposition"]
					)
					self.assertEqual(response.read().decode(), trace_content)

				with self.assertRaises(urllib.error.HTTPError) as caught:
					urllib.request.urlopen(
						f"{base_url}/api/v1/masters/trace?id=../secret", timeout=2
					)
				self.assertEqual(caught.exception.code, 400)
				caught.exception.close()
			finally:
				controller.shutdown()
				server.shutdown()
				server.server_close()
				thread.join(2)
