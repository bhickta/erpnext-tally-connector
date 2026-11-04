import json
import tempfile
import threading
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
	def __init__(self, fail_on=None):
		self.requests = []
		self.fail_on = fail_on

	def import_json(self, payload, report_id, require_change=False, allow_ignored=False):
		self.requests.append((payload, report_id))
		if self.fail_on is not None and len(self.requests) == self.fail_on:
			return ImportResult(success=False, errors=1, message="Tally rejected the batch")
		return ImportResult(success=True, created=len(payload["tallymessage"]))


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
			finally:
				controller.shutdown()
				server.shutdown()
				server.server_close()
				thread.join(2)
