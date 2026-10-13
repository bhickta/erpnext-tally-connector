"""Destination profiles used by the standalone Tally agent."""

import importlib
import inspect
from copy import deepcopy

from .clients import BridgeRequestError
from .collection_gateway import build_collection_export, parse_collection_export, scalar
from .json_gateway import build_master_imports
from .ledger_gateway import build_ledger_import, debit_balance, read_ledger
from .payment_gateway import (
	build_accounting_import,
	build_payment_import,
	build_payment_masters,
	validate_accounting_voucher,
	validate_payment,
)
from .xml_gateway import build_voucher_import, function_request, voucher_remote_id


class AgentProfile:
	"""Translate records between a registered ERPNext flow and local Tally."""

	key = ""

	def validate_environment(self, tally_client):
		"""Raise before writes when the loaded Tally company lacks a requirement."""

	def deliver(self, document, config, tally_client):
		raise NotImplementedError

	def collect(self, config, tally_client, limit, options=None):
		"""Extract records for a Tally-to-ERPNext flow.

		Inbound profiles override this method because the extraction contract and
		checkpoint are specific to the registered ERPNext flow.
		"""
		raise NotImplementedError

	def acknowledge_collected(self, config, records, results):
		"""Advance any local extraction checkpoint after ERPNext accepts records."""

	def supports_direction(self, direction):
		if direction == "erpnext_to_tally":
			return type(self).deliver is not AgentProfile.deliver
		if direction == "tally_to_erpnext":
			return type(self).collect is not AgentProfile.collect
		return False


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
			document["narration"] = "\n".join(part for part in (document.get("narration"), date_note) if part)

		master_payloads = (
			()
			if document.get("operation") == "Cancel"
			else build_master_imports(document, config.tally_company)
		)
		for master_payload in master_payloads:
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
		return voucher_result.last_voucher_id or f"guid:{voucher_remote_id(document, config.target_id)}"


class PaymentVoucherProfile(AgentProfile):
	key = "payment_voucher_v1"
	validate_document = staticmethod(validate_payment)
	build_document = staticmethod(build_payment_import)
	prepare_masters = True

	def deliver(self, document, config, tally_client):
		document = deepcopy(document)
		self.validate_document(document)
		if document["operation"] == "Create":
			marker = f"[ExpressTallySource:{voucher_remote_id(document, config.target_id)}]"
			request = build_collection_export(
				config.tally_company,
				"ETPaymentSourceIdentity",
				"Voucher",
				("MasterID", "GUID", "IsCancelled"),
				[f'$Narration Contains "{marker}"'],
			)
			existing = [
				row
				for row in parse_collection_export(tally_client.export_collection(request), ("VOUCHER",))
				if scalar(row, "masterid", "master_id")
			]
			if len(existing) > 1:
				raise BridgeRequestError("Multiple Tally vouchers have this ERPNext payment identity")
			if existing:
				if scalar(existing[0], "iscancelled", "is_cancelled").casefold() == "yes":
					raise BridgeRequestError(
						"The ERPNext payment already exists as a cancelled Tally voucher"
					)
				return scalar(existing[0], "masterid", "master_id")
		if config.voucher_date_override:
			original_date = document["transaction_date"]
			document["transaction_date"] = config.voucher_date_override
			document["narration"] = (
				f"{document.get('narration') or ''}\nOriginal ERPNext date {original_date}"
			)
		if self.prepare_masters and document["operation"] != "Cancel" and document.get("masters"):
			result = tally_client.import_json(
				build_payment_masters(document, config.tally_company), "All Masters", require_change=False
			)
			if not result.success:
				raise BridgeRequestError(result.message or "Tally payment ledger import failed")
		result = tally_client.import_xml(
			self.build_document(document, config.tally_company, config.target_id),
			require_change=True,
			allow_ignored=True,
		)
		if not result.success:
			raise BridgeRequestError(result.message or "Tally payment import failed")
		return result.last_voucher_id or f"guid:{voucher_remote_id(document, config.target_id)}"


class GeneralLedgerVoucherProfile(PaymentVoucherProfile):
	key = "general_ledger_voucher_v1"
	validate_document = staticmethod(validate_accounting_voucher)
	build_document = staticmethod(build_accounting_import)
	prepare_masters = False


class LedgerMasterProfile(AgentProfile):
	key = "ledger_master_v1"

	def deliver(self, document, config, tally_client):
		from decimal import Decimal

		expected = Decimal(str(document["opening_balance"])).quantize(Decimal("0.01"))
		if not expected.is_finite():
			raise BridgeRequestError("Opening balance must be finite")
		period = {
			"from_date": document.get("fiscal_year_start"),
			"to_date": document.get("fiscal_year_start"),
		}
		existing = read_ledger(
			tally_client,
			config.tally_company,
			document.get("target_ledger") or document["ledger_name"],
			**period,
		)
		if existing:
			opening = debit_balance(scalar(existing, "openingbalance", "opening_balance")).quantize(
				Decimal("0.01")
			)
			if opening != expected and not document.get("allow_opening_update"):
				raise BridgeRequestError(
					f"Existing Tally ledger opening {opening} differs from ERPNext {expected}; explicit opening-update permission is required"
				)
			if (
				opening == expected
				and scalar(existing, "name", "_name") == document["ledger_name"]
				and scalar(existing, "parent") == document["parent"]
			):
				return f"ledger:{document['ledger_name']}"
		result = tally_client.import_xml(
			build_ledger_import(
				document,
				config.tally_company,
				existing=bool(existing),
				update_opening=not existing or opening != expected,
			),
			require_change=True,
			allow_ignored=True,
		)
		if not result.success:
			raise BridgeRequestError(result.message or "Tally ledger master import failed")
		actual = read_ledger(tally_client, config.tally_company, document["ledger_name"], **period)
		if (
			not actual
			or debit_balance(scalar(actual, "openingbalance", "opening_balance")).quantize(Decimal("0.01"))
			!= expected
		):
			raise BridgeRequestError("Tally did not retain the requested ledger opening balance")
		return f"ledger:{document['ledger_name']}"


class AgentProfileRegistry:
	def __init__(self, entries=()):
		from .inbound_profiles import (
			TallyLedgerMirrorProfile,
			TallyMastersProfile,
			TallyNativeTransactionsProfile,
			TallyPaymentVouchersProfile,
			TallyVouchersProfile,
		)

		self._profiles = {}
		for entry in (
			InventorySalesVoucherProfile,
			PaymentVoucherProfile,
			GeneralLedgerVoucherProfile,
			LedgerMasterProfile,
			TallyMastersProfile,
			TallyNativeTransactionsProfile,
			TallyPaymentVouchersProfile,
			TallyVouchersProfile,
			TallyLedgerMirrorProfile,
			*entries,
		):
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

	def metadata(self):
		return [
			{
				"key": key,
				"directions": [
					direction
					for direction in ("erpnext_to_tally", "tally_to_erpnext")
					if profile.supports_direction(direction)
				],
			}
			for key, profile in sorted(self._profiles.items())
		]

	def _instantiate(self, entry):
		if isinstance(entry, str):
			module_name, separator, attribute = entry.rpartition(".")
			if not separator:
				raise ValueError(f"Agent profile path must be a dotted import path: {entry}")
			entry = getattr(importlib.import_module(module_name), attribute)
		if inspect.isclass(entry):
			return entry()
		return entry
