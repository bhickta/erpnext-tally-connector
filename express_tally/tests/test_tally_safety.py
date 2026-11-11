"""Regression coverage for bounded work and unknown Tally request outcomes."""

import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from express_tally.bridge.clients import BridgeRequestError, TallyClient
from express_tally.bridge.json_gateway import ImportResult
from express_tally.bridge.master_sync import MasterSyncService, configured_batch_size
from express_tally.bridge import tally_transport


class TestTallyTransportSafety(TestCase):
	def setUp(self):
		self.states = patch.object(tally_transport, "_STATES", {})
		self.states.start()
		self.addCleanup(self.states.stop)

	@patch("express_tally.bridge.tally_transport.urllib.request.urlopen")
	def test_timeout_blocks_new_clients_xml_and_json_without_retry(self, open_url):
		open_url.side_effect = TimeoutError("timed out")
		with self.assertRaisesRegex(BridgeRequestError, "outcome unknown"):
			TallyClient().post_json({}, "All Masters")
		with self.assertRaisesRegex(BridgeRequestError, "paused"):
			TallyClient().post_xml("<ENVELOPE/>")
		self.assertEqual(open_url.call_count, 1)

	@patch("express_tally.bridge.tally_transport.urllib.request.urlopen")
	def test_concurrent_client_does_not_queue_another_request(self, open_url):
		entered, release = threading.Event(), threading.Event()
		def respond(*args, **kwargs):
			entered.set()
			release.wait(2)
			response = Mock()
			response.__enter__ = Mock(return_value=response)
			response.__exit__ = Mock(return_value=False)
			response.read.return_value = b"OK"
			return response
		open_url.side_effect = respond
		outcomes = []
		thread = threading.Thread(target=lambda: outcomes.append(TallyClient().post_xml("x")))
		thread.start()
		try:
			self.assertTrue(entered.wait(1))
			with self.assertRaisesRegex(BridgeRequestError, "busy"):
				TallyClient().post_json({}, "All Masters")
		finally:
			release.set()
			thread.join(2)
		self.assertEqual(outcomes, ["OK"])
		self.assertEqual(open_url.call_count, 1)

	@patch("express_tally.bridge.tally_transport.time.sleep")
	@patch("express_tally.bridge.tally_transport.urllib.request.urlopen")
	def test_reset_endpoint_allows_retry_after_pause(self, open_url, sleep):
		open_url.side_effect = TimeoutError("timed out")
		with self.assertRaisesRegex(BridgeRequestError, "outcome unknown"):
			TallyClient().post_xml("x")
		with self.assertRaisesRegex(BridgeRequestError, "paused"):
			TallyClient().post_xml("x")
		tally_transport.reset_endpoint("http://127.0.0.1:9000/")
		response = Mock()
		response.__enter__ = Mock(return_value=response)
		response.__exit__ = Mock(return_value=False)
		response.read.return_value = b"OK"
		open_url.side_effect = None
		open_url.return_value = response
		self.assertEqual(TallyClient().post_xml("x"), "OK")

	@patch("express_tally.bridge.tally_transport.time.sleep")
	@patch("express_tally.bridge.tally_transport.urllib.request.urlopen")
	def test_success_paces_next_request(self, open_url, sleep):
		response = open_url.return_value.__enter__.return_value
		response.read.return_value = b"OK"
		TallyClient().post_xml("x")
		TallyClient().post_xml("x")
		self.assertGreater(sleep.call_args.args[0], 0)


class TestMasterWorkLimits(TestCase):
	def service(self):
		tally = Mock()
		tally.import_json.side_effect = lambda payload, *a, **kw: ImportResult(
			success=True, created=len(payload["tallymessage"]))
		return MasterSyncService(SimpleNamespace(tally_company="Test"), Mock(), tally)

	def test_old_settings_are_capped(self):
		self.assertEqual(configured_batch_size(SimpleNamespace(master_batch_size=1000)), 25)
		self.assertEqual(configured_batch_size(SimpleNamespace()), 10)

	def test_large_unicode_records_split_by_encoded_request_size(self):
		service = self.service()
		messages = [{"name": str(i), "description": "界" * 10000} for i in range(5)]
		result = service._run_batch({"key": "items", "label": "Items"}, messages, 25, None)
		self.assertEqual(result["created"], 5)
		for call in service.tally.import_json.call_args_list:
			self.assertLessEqual(service._request_size(call.args[0]["tallymessage"]), 65536)

	def test_oversized_single_record_is_rejected_without_contacting_tally(self):
		service = self.service()
		result = service._run_batch({"key": "items", "label": "Items"},
			[{"description": "x" * 65536}], 10, None)
		self.assertEqual(result["status"], "Failed")
		service.tally.import_json.assert_not_called()

	@patch("express_tally.bridge.master_sync.time.monotonic", side_effect=[0, 3, 4, 4.1, 5, 5.1])
	def test_slow_success_shrinks_following_chunks(self, monotonic):
		service = self.service()
		messages = [{"name": str(i)} for i in range(8)]
		result = service._run_batch({"key": "items", "label": "Items"}, messages, 4, None)
		self.assertEqual(result["created"], 8)
		self.assertEqual([len(c.args[0]["tallymessage"]) for c in
			service.tally.import_json.call_args_list], [4, 2, 2])

	def test_timeout_stops_remaining_chunks_and_dependent_batches(self):
		service = self.service()
		service._existing_names = Mock(return_value=set())
		service.tally.import_json.side_effect = BridgeRequestError("outcome unknown")
		batches = [{"key": key, "tallymessage": [{"name": str(i)} for i in range(3)]}
			for key in ("items", "customers")]
		results, failed, cancelled = service._run_batches(batches, 1, None)
		self.assertTrue(failed)
		self.assertFalse(cancelled)
		self.assertEqual([r["status"] for r in results], ["Failed", "Skipped"])
		self.assertEqual(service.tally.import_json.call_count, 1)
		self.assertEqual(service._existing_names.call_count, 1)

	def test_successful_names_are_reused_across_chunks_and_batches(self):
		service = self.service()
		known = set()
		service._existing_names = Mock(return_value=known)
		batches = [{"key": key, "tallymessage": [{"name": "Shared"}, {"name": "Shared"}]}
			for key in ("account_ledgers", "customers")]
		results, failed, _ = service._run_batches(batches, 1, None)
		self.assertFalse(failed)
		actions = [c.args[0]["tallymessage"][0]["metadata"]["action"]
			for c in service.tally.import_json.call_args_list]
		self.assertEqual(actions, ["create", "alter", "alter", "alter"])

	def test_duplicate_names_within_one_request_are_not_created_twice(self):
		service = self.service()
		messages = [{"name": "Shared"}, {"name": "SHARED"}]
		service._run_batch({"key": "items", "label": "Items"}, messages, 10, None, set())
		self.assertEqual([m["metadata"]["action"] for m in messages], ["create", "alter"])

	def test_lookup_error_cannot_be_mistaken_for_empty_tally_company(self):
		service = self.service()
		service.tally.export_collection.return_value = "<ENVELOPE><LINEERROR>Company unavailable</LINEERROR></ENVELOPE>"
		results, failed, _ = service._run_batches([
			{"key": "items", "tallymessage": [{"name": "Existing"}]}], 10, None)
		self.assertTrue(failed)
		self.assertIn("Company unavailable", results[0]["message"])
		service.tally.import_json.assert_not_called()
