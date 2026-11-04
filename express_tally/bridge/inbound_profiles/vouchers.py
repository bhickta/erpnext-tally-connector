"""Inbound Tally voucher collection profiles."""

import json

from ..collection_gateway import build_collection_export, parse_collection_export
from ..normalization import normalize_voucher
from .base import IncrementalTallyProfile
from .constants import NATIVE_TRANSACTION_FIELDS, PAYMENT_VOUCHER_FIELDS, VOUCHER_FIELDS


class TallyVouchersProfile(IncrementalTallyProfile):
	key = "tally_vouchers_v1"
	voucher_fields = VOUCHER_FIELDS

	def collect(self, config, tally_client, limit, options=None):
		options = options or {}
		self._scoped_collection = bool(options.get("voucher_ids") or options.get("voucher_numbers"))
		checkpoint = (
			0
			if self._scoped_collection
			else self._checkpoint_store(config).get(self._target_key(config), "voucher")
		)
		filters = [f"$AlterID > {checkpoint}"] if checkpoint else []
		voucher_ids = options.get("voucher_ids")
		if voucher_ids is not None:
			if (
				not isinstance(voucher_ids, list)
				or not voucher_ids
				or len(voucher_ids) > 100
				or any(not str(value).isdigit() or int(value) < 1 for value in voucher_ids)
			):
				raise ValueError("voucher_ids must contain between 1 and 100 positive Tally master IDs")
			filters.append(" OR ".join(f"($MasterID = {int(value)})" for value in voucher_ids))
		for option, method in (("voucher_types", "$VoucherTypeName"), ("voucher_numbers", "$VoucherNumber")):
			values = options.get(option)
			if values is not None:
				if (
					not isinstance(values, list)
					or not values
					or len(values) > 100
					or any(not isinstance(v, str) or not v for v in values)
				):
					raise ValueError(f"{option} must contain between 1 and 100 nonempty strings")
				filters.append(" OR ".join(f"({method} = {json.dumps(v)})" for v in values))
		if config.from_date:
			filters.append("$Date >= ##SVFromDate")
		if config.to_date:
			filters.append("$Date <= ##SVToDate")
		request = build_collection_export(
			config.tally_company,
			"ETControlCentreVouchers",
			"Voucher",
			self.voucher_fields,
			filters,
			static_variables={
				key: value.replace("-", "")
				for key, value in (("SVFROMDATE", config.from_date), ("SVTODATE", config.to_date))
				if value
			},
		)
		response = tally_client.export_collection(request)
		raw_records = parse_collection_export(response, ("VOUCHER",))
		records = [normalize_voucher(record) for record in raw_records]
		return self._pending(config, [record for record in records if record], limit)

	def _pending(self, config, records, limit):
		if getattr(self, "_scoped_collection", False):
			return sorted(records, key=lambda record: record.get("_tally_alter_id", 0))[:limit]
		return super()._pending(config, records, limit)

	def acknowledge_collected(self, config, records, results):
		# A targeted diagnostic must not skip older, unselected vouchers later.
		if not getattr(self, "_scoped_collection", False):
			return super().acknowledge_collected(config, records, results)


class TallyNativeTransactionsProfile(TallyVouchersProfile):
	key = "tally_native_transactions_v1"
	voucher_fields = NATIVE_TRANSACTION_FIELDS


class TallyPaymentVouchersProfile(TallyVouchersProfile):
	key = "tally_payment_vouchers_v1"
	voucher_fields = PAYMENT_VOUCHER_FIELDS

	def _target_key(self, config):
		return f"{super()._target_key(config)}:payments"

	def collect(self, config, tally_client, limit, options=None):
		options = {**(options or {}), "voucher_types": ["Payment", "Receipt"]}
		return super().collect(config, tally_client, limit, options)
