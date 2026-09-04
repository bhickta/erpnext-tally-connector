"""Destination profiles used by the standalone Tally agent."""

import importlib
import inspect
from copy import deepcopy

from .clients import BridgeRequestError
from .json_gateway import build_master_imports
from .xml_gateway import build_voucher_import, function_request


class AgentProfile:
	"""Translate one mapped flow record into calls to Tally's local gateway."""

	key = ""

	def validate_environment(self, tally_client):
		"""Raise before writes when the loaded Tally company lacks a requirement."""

	def deliver(self, document, config, tally_client):
		raise NotImplementedError


class InventorySalesVoucherProfile(AgentProfile):
	"""Create dependencies and an inventory-aware Sales voucher."""

	key = "inventory_sales_voucher_v1"

	def validate_environment(self, tally_client):
		if not tally_client.get_logical_function(function_request("$$IsInventoryOn")):
			raise BridgeRequestError("Tally Maintain Inventory is disabled for the loaded company")

	def deliver(self, document, config, tally_client):
		document = deepcopy(document)
		if config.voucher_date_override:
			original_date = document["transaction_date"]
			document["transaction_date"] = config.voucher_date_override
			source_type = document.get("source_doctype", "Sales Order")
			date_note = f"ERPNext {source_type} {document['name']}; original date {original_date}"
			document["narration"] = "\n".join(
				part for part in (document.get("narration"), date_note) if part
			)

		for master_payload in build_master_imports(document, config.tally_company):
			master_result = tally_client.import_json(
				master_payload,
				"All Masters",
				require_change=False,
			)
			if not master_result.success:
				raise BridgeRequestError(master_result.message or "Tally master import failed")

		voucher_result = tally_client.import_xml(
			build_voucher_import(document, config.tally_company, config.target_id),
			require_change=True,
			allow_ignored=True,
		)
		if not voucher_result.success:
			raise BridgeRequestError(voucher_result.message or "Tally voucher import failed")
		return voucher_result.last_voucher_id


class AgentProfileRegistry:
	def __init__(self, entries=()):
		self._profiles = {}
		for entry in (InventorySalesVoucherProfile, *entries):
			self.register(entry)

	def register(self, entry):
		profile = self._instantiate(entry)
		if not isinstance(profile, AgentProfile):
			raise TypeError(f"Agent profile {entry!r} must implement AgentProfile")
		if not profile.key:
			raise ValueError("Agent profiles require a stable key")
		if profile.key in self._profiles:
			raise ValueError(f"Duplicate agent profile: {profile.key}")
		self._profiles[profile.key] = profile
		return profile

	def get(self, key):
		try:
			return self._profiles[key]
		except KeyError as exc:
			raise ValueError(f"Unsupported Tally agent profile: {key}") from exc

	def _instantiate(self, entry):
		if isinstance(entry, str):
			module_name, separator, attribute = entry.rpartition(".")
			if not separator:
				raise ValueError(f"Agent profile path must be a dotted import path: {entry}")
			entry = getattr(importlib.import_module(module_name), attribute)
		if inspect.isclass(entry):
			return entry()
		return entry
