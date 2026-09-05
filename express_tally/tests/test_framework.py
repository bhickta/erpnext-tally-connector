import json
import sys
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from express_tally.framework import FlowContext, FlowEngine, FlowRegistry, InboundFlow, OutboundFlow
from express_tally.framework.engine import make_context, parse_sequence


class ExampleOutboundFlow(OutboundFlow):
	key = "test.outbound"
	title = "Test outbound"
	agent_profile = "test_profile_v1"

	def __init__(self):
		self.operations = []

	def authorize(self, operation):
		self.operations.append(operation)

	def pull(self, context, limit):
		return [{"name": "SO-1", "company": context.company}][:limit]

	def acknowledge(self, context, results):
		return {"accepted": len(results)}

	def status(self, context):
		return {"pending": 1}


class ExampleInboundFlow(InboundFlow):
	key = "test.inbound"
	default_options = {"mode": "safe", "submit": False}

	def authorize(self, operation):
		pass

	def receive(self, context, records):
		return [
			{"source": row["name"], "status": "Success", "options": dict(context.options)}
			for row in records
		]


class TestFlowRegistry(TestCase):
	def test_registers_classes_and_sorts_metadata(self):
		registry = FlowRegistry([ExampleOutboundFlow, ExampleInboundFlow])

		self.assertEqual([flow.key for flow in registry.all()], ["test.inbound", "test.outbound"])
		self.assertIsInstance(registry.get("test.outbound"), ExampleOutboundFlow)

	def test_rejects_duplicate_keys(self):
		with self.assertRaisesRegex(ValueError, "Duplicate"):
			FlowRegistry([ExampleOutboundFlow, ExampleOutboundFlow])

	def test_discovers_dotted_paths_from_frappe_hooks(self):
		fake_frappe = SimpleNamespace(
			get_hooks=lambda hook: [
				f"{ExampleOutboundFlow.__module__}.ExampleOutboundFlow",
			]
		)
		with patch.dict(sys.modules, {"frappe": fake_frappe}):
			from express_tally.framework.registry import get_registry

			registry = get_registry()

		self.assertIsInstance(registry.get("test.outbound"), ExampleOutboundFlow)


class TestFlowEngine(TestCase):
	def setUp(self):
		self.outbound = ExampleOutboundFlow()
		self.engine = FlowEngine(FlowRegistry([self.outbound, ExampleInboundFlow]))
		self.context = FlowContext("ERP Company", "target-1", "Tally Company")

	def test_pull_returns_versioned_envelope(self):
		response = self.engine.pull("test.outbound", self.context, 20)

		self.assertEqual(response["schema_version"], 1)
		self.assertEqual(response["direction"], "erpnext_to_tally")
		self.assertEqual(response["agent_profile"], "test_profile_v1")
		self.assertEqual(response["documents"][0]["name"], "SO-1")
		self.assertEqual(self.outbound.operations, ["pull"])

	def test_acknowledgement_parses_json(self):
		response = self.engine.acknowledge(
			"test.outbound",
			self.context,
			json.dumps([{"request_id": "one"}]),
		)

		self.assertEqual(response, {"flow": "test.outbound", "accepted": 1})

	def test_receive_dispatches_to_inbound_flow(self):
		response = self.engine.receive("test.inbound", self.context, [{"name": "TALLY-1"}])

		self.assertEqual(response["direction"], "tally_to_erpnext")
		self.assertEqual(response["results"][0]["source"], "TALLY-1")
		self.assertEqual(response["results"][0]["options"], {"mode": "safe", "submit": False})

	def test_context_options_override_flow_defaults(self):
		context = FlowContext("ERP Company", "target-1", "Tally Company", options={"submit": True})

		response = self.engine.receive("test.inbound", context, [{"name": "TALLY-1"}])

		self.assertEqual(response["results"][0]["options"], {"mode": "safe", "submit": True})

	def test_direction_mismatch_is_rejected(self):
		with self.assertRaisesRegex(ValueError, "does not support pull"):
			self.engine.pull("test.inbound", self.context)

	def test_context_and_batch_validation(self):
		context = make_context("ERP", "target:one", "Tally", options='{"mode": "create"}')
		self.assertEqual(context.options["mode"], "create")

		with self.assertRaisesRegex(ValueError, "target_id"):
			make_context("ERP", "bad target", "Tally")
		with self.assertRaisesRegex(ValueError, "Every records entry"):
			parse_sequence(["not-an-object"], "records")
