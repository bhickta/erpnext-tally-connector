"""Destination profiles used by the standalone Tally agent."""

import importlib
import inspect
import json
from copy import deepcopy

from .clients import BridgeRequestError
from .collection_gateway import build_collection_export, scalar
from .json_gateway import build_master_imports
from .ledger_gateway import build_ledger_import, debit_balance, read_ledger
from .payment_gateway import (
	build_accounting_import,
	build_payment_import,
	build_payment_masters,
	read_accounting_voucher,
	verify_accounting_voucher,
	validate_accounting_voucher,
	validate_payment,
)
from .voucher_readback import (
	IDENTITY_FIELDS,
	VoucherReadbackError,
	is_cancelled,
	read_collection,
	read_voucher,
	verify_cancelled_voucher,
	verify_voucher,
)
from .xml_gateway import (
	build_unit_import,
	build_voucher_import,
	function_request,
	unit_name_error,
	voucher_remote_id,
)


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

		operation = document["operation"]
		guid = f"guid:{voucher_remote_id(document, config.target_id)}"
		reference = str(document.get("tally_voucher_id") or guid)

		def read_saved(saved_reference=None):
			saved_reference = saved_reference or reference
			if operation == "Cancel":
				return read_voucher(
					tally_client, config.tally_company, saved_reference, fields=IDENTITY_FIELDS
				)
			return read_voucher(tally_client, config.tally_company, saved_reference)

		if operation != "Cancel":
			version_marker = f"[ExpressTallyVersion:{document['source_hash']}]"
			narration = document.get("narration") or f"ERPNext {document.get('source_doctype', 'Sales Order')} {document['name']}"
			document["narration"] = f"{narration}\n{version_marker}"
		try:
			existing = read_saved()
			if operation == "Create" and existing:
				return verify_voucher(existing, document, version_marker)
			if operation != "Create":
				if not existing or scalar(existing, "vouchertypename").casefold() != (document.get("voucher_type") or "Sales").casefold():
					raise BridgeRequestError(f"Tally voucher {reference} could not be confirmed for {operation.lower()}")
				if is_cancelled(existing):
					if operation == "Cancel":
						return verify_cancelled_voucher(existing, document)
					raise BridgeRequestError(f"Tally voucher {reference} is already cancelled")
		except BridgeRequestError as exc:
			raise VoucherReadbackError(f"Cannot verify Tally vouchers before import: {exc}") from exc

		if operation != "Cancel":
			self.prepare_masters(document, config.tally_company, tally_client)

		voucher_result = tally_client.import_xml(
			build_voucher_import(document, config.tally_company, config.target_id),
			require_change=True,
			allow_ignored=True,
		)
		if not voucher_result.success:
			raise BridgeRequestError(voucher_result.message or "Tally voucher import failed")
		try:
			# Tally may replace an imported GUID with its own company-prefixed
			# identity. Prefer the master ID Tally just returned, so a successful
			# write is verified instead of being treated as missing.
			row = read_saved(voucher_result.last_voucher_id)
			if operation == "Cancel":
				return verify_cancelled_voucher(row, document)
			return verify_voucher(row, document, version_marker)
		except (BridgeRequestError, ValueError) as exc:
			raise VoucherReadbackError(
				f"Tally accepted the import for {document['name']}, but voucher verification failed: {exc}"
			) from exc

	@staticmethod
	def prepare_masters(document, company, tally):
		def sales_ledger():
			name = document["sales_ledger"]
			request = build_collection_export(
				company, "ETOutboundSalesLedger", "Ledger", ("Name", "AffectsStock"),
				[f"$Name = {json.dumps(name)}"],
			)
			rows = read_collection(tally, request, "LEDGER")
			if len(rows) > 1 or (rows and scalar(rows[0], "name", "_name").casefold() != name.casefold()):
				raise BridgeRequestError(f"Tally returned an ambiguous sales ledger for {name}")
			if rows and scalar(rows[0], "affectsstock").casefold() not in {"yes", "true", "1"}:
				raise BridgeRequestError(
					f"Enable Inventory values are affected on the Tally sales ledger '{name}' "
					"before syncing stock vouchers"
				)
			return bool(rows)

		has_stock = bool(document["items"])
		has_sales_ledger = has_stock and sales_ledger()
		units, *other_masters = build_master_imports(
			document, company, sales_ledger_exists=has_sales_ledger
		)
		unit_masters = units["tallymessage"]
		if unit_masters:
			names = [row["name"] for row in unit_masters]
			request = build_collection_export(
				company, "ETOutboundUnits", "Unit", ("Name",),
				[" OR ".join(f"($Name = {json.dumps(name)})" for name in names)],
			)
			existing = {
				scalar(row, "name", "_name").casefold()
				for row in read_collection(tally, request, "UNIT")
			}
			missing = [row for row in unit_masters if row["name"].casefold() not in existing]
			if missing:
				for row in missing:
					if error := unit_name_error(row["name"]):
						raise BridgeRequestError(f"Cannot create Tally unit '{row['name']}': {error}")
				result = tally.import_xml(build_unit_import(company, missing))
				if not result.success:
					raise BridgeRequestError(result.message or "Tally unit import failed")

		for index, payload in enumerate(other_masters):
			if not payload["tallymessage"]:
				continue
			result = tally.import_json(payload, "All Masters", require_change=False)
			if not result.success:
				raise BridgeRequestError(result.message or "Tally master import failed")
			if index == 0 and has_stock and not has_sales_ledger and not sales_ledger():
				raise BridgeRequestError(f"Tally did not create the sales ledger '{document['sales_ledger']}'")


class PaymentVoucherProfile(AgentProfile):
	key = "payment_voucher_v1"
	validate_document = staticmethod(validate_payment)
	build_document = staticmethod(build_payment_import)
	prepare_masters = True

	def deliver(self, document, config, tally_client):
		document = deepcopy(document)
		self.validate_document(document)
		if config.voucher_date_override:
			original_date = document["transaction_date"]
			document["transaction_date"] = config.voucher_date_override
			document["narration"] = (
				f"{document.get('narration') or ''}\nOriginal ERPNext date {original_date}"
			)
		operation = document["operation"]

		def read_saved():
			return read_accounting_voucher(tally_client, config.tally_company, document, config.target_id)

		try:
			existing = read_saved()
			if operation == "Create" and existing:
				return verify_accounting_voucher(existing, document)
			if operation != "Create":
				if not existing or scalar(existing, "vouchertypename").casefold() != document["voucher_type"].casefold():
					raise ValueError("The accounting voucher to change could not be confirmed")
				if is_cancelled(existing):
					return verify_accounting_voucher(existing, document)
		except (BridgeRequestError, ValueError) as exc:
			raise VoucherReadbackError(f"Cannot confirm accounting voucher {document['name']}: {exc}") from exc

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
		try:
			return verify_accounting_voucher(read_saved(), document)
		except (BridgeRequestError, ValueError) as exc:
			raise VoucherReadbackError(
				f"Tally accepted {document['name']}, but accounting verification failed: {exc}"
			) from exc


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
		def matches(row):
			return (
				scalar(row, "name", "_name").casefold() == document["ledger_name"].casefold()
				and scalar(row, "parent").casefold() == document["parent"].casefold()
				and (scalar(row, "isbillwiseon").casefold() in {"yes", "true", "1"})
				== bool(document.get("billwise"))
				and debit_balance(scalar(row, "openingbalance")).quantize(Decimal("0.01")) == expected
			)

		if existing:
			opening = debit_balance(scalar(existing, "openingbalance")).quantize(Decimal("0.01"))
			if opening != expected and not document.get("allow_opening_update"):
				raise BridgeRequestError(
					f"Existing Tally ledger opening {opening} differs from ERPNext {expected}; explicit opening-update permission is required"
				)
			if matches(existing):
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
		try:
			actual = read_ledger(tally_client, config.tally_company, document["ledger_name"], **period)
			if not actual or not matches(actual):
				raise ValueError("Tally did not retain the ledger name, group, bill-wise setting or opening balance")
		except (BridgeRequestError, ValueError) as exc:
			raise VoucherReadbackError(f"Cannot confirm the saved ledger '{document['ledger_name']}': {exc}") from exc
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
