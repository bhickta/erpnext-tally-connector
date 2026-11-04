"""Inbound full-ledger mirror collection profile."""

import hashlib
import json
from datetime import date, timedelta

from ..collection_gateway import build_collection_export, parse_collection_export, scalar
from ..normalization import (
	_identity,
	_primary_group,
	master_name,
	normalize_voucher,
	tally_balance,
)
from .base import IncrementalTallyProfile
from .constants import LEDGER_BALANCE_FIELDS, PAYMENT_VOUCHER_FIELDS


class TallyLedgerMirrorProfile(IncrementalTallyProfile):
	"""Stage opening balances, accounting vouchers, then closing-balance checks."""

	key = "tally_ledger_mirror_v1"

	def collect(self, config, tally_client, limit, options=None):
		options = options or {}
		self._ledger_scope = options.get("ledger_names") or []
		self._validate_period = bool(options.get("require_ledger_scope"))
		if not isinstance(self._ledger_scope, list) or any(
			not isinstance(name, str) or not name.strip() for name in self._ledger_scope
		):
			raise ValueError("ledger_names must contain nonempty ledger names")
		if len(self._ledger_scope) > 1000 or len({name.casefold() for name in self._ledger_scope}) != len(
			self._ledger_scope
		):
			raise ValueError("Choose at most 1000 distinct ledger names")
		if options.get("require_ledger_scope") and not self._ledger_scope:
			raise ValueError("Select ledger_names for the full-ledger inbound flow")
		self._scope_key = (
			hashlib.sha256(
				json.dumps(
					[sorted(self._ledger_scope), options.get("fiscal_year_start") or config.from_date]
				).encode()
			).hexdigest()[:16]
			if self._ledger_scope
			else ""
		)

		fiscal_year_start = str(options.get("fiscal_year_start") or config.from_date or "").strip()
		if not fiscal_year_start:
			raise ValueError("The ledger mirror flow requires fiscal_year_start")
		try:
			date.fromisoformat(fiscal_year_start)
		except ValueError as exc:
			raise ValueError("fiscal_year_start must use YYYY-MM-DD format") from exc

		through_date = str(config.to_date or date.today().isoformat())
		if date.fromisoformat(through_date) < date.fromisoformat(fiscal_year_start):
			raise ValueError("The reconciliation date cannot be before the fiscal-year start")

		groups = self._groups(config, tally_client)
		opening_snapshot_date = (date.fromisoformat(fiscal_year_start) - timedelta(days=1)).isoformat()
		opening_ledgers = self._ledgers(
			config,
			tally_client,
			opening_snapshot_date,
			opening_snapshot_date,
		)
		opening_type = f"ledger_mirror_opening_{fiscal_year_start.replace('-', '_')}"
		opening_records = []
		for raw in opening_ledgers:
			name = master_name(raw)
			if self._validate_period and _primary_group(scalar(raw, "parent"), groups) in {
				"Sales Accounts",
				"Purchase Accounts",
				"Direct Incomes",
				"Indirect Incomes",
				"Direct Expenses",
				"Indirect Expenses",
			}:
				continue
			balance = tally_balance(scalar(raw, "closing_balance", "closingbalance"))
			if not name or not balance:
				continue
			opening_records.append(
				{
					**_identity(opening_type, raw, name),
					"kind": "opening_balance",
					"ledger": name,
					"parent": scalar(raw, "parent"),
					"primary_group": _primary_group(scalar(raw, "parent"), groups),
					"balance": balance,
					"posting_date": fiscal_year_start,
					"snapshot_date": opening_snapshot_date,
					"fiscal_year_start": fiscal_year_start,
				}
			)
		pending = self._pending(config, opening_records, limit)
		if pending:
			return pending

		ledger_directory = self._ledger_directory(opening_ledgers, groups)
		voucher_records = self._vouchers(
			config,
			tally_client,
			fiscal_year_start,
			through_date,
			ledger_directory,
			options,
		)
		pending = self._pending(config, voucher_records, limit)
		if pending:
			return pending

		reconciliation_ledgers = self._ledgers(
			config,
			tally_client,
			fiscal_year_start,
			through_date,
		)
		reconciliation_type = f"ledger_mirror_reconciliation_{through_date.replace('-', '_')}"
		reconciliation_records = []
		snapshot = hashlib.sha256(
			json.dumps(
				sorted(
					(master_name(raw), tally_balance(scalar(raw, "closing_balance", "closingbalance")))
					for raw in reconciliation_ledgers
				),
				default=str,
			).encode()
		).hexdigest()[:16]
		reconciliation_type += "_" + snapshot
		for index, raw in enumerate(sorted(reconciliation_ledgers, key=master_name), 1):
			name = master_name(raw)
			if not name:
				continue
			reconciliation_records.append(
				{
					**_identity(reconciliation_type, {**raw, "alter_id": str(index)}, name),
					"kind": "ledger_reconciliation",
					"ledger": name,
					"parent": scalar(raw, "parent"),
					"primary_group": _primary_group(scalar(raw, "parent"), groups),
					"balance": tally_balance(scalar(raw, "closing_balance", "closingbalance")),
					"fiscal_year_start": fiscal_year_start,
					"as_of_date": through_date,
				}
			)
		return self._pending(config, reconciliation_records, limit)

	def _target_key(self, config):
		key = super()._target_key(config)
		return f"{key}:ledgers:{self._scope_key}" if getattr(self, "_scope_key", "") else key

	def _scope_formula(self, field):
		return " OR ".join(f"({field} = {json.dumps(name)})" for name in getattr(self, "_ledger_scope", []))

	def _groups(self, config, tally_client):
		request = build_collection_export(
			config.tally_company,
			"ETLedgerMirrorGroups",
			"Group",
			("Name", "Parent", "GUID", "AlterID"),
		)
		raw_groups = parse_collection_export(tally_client.export_collection(request), ("GROUP",))
		return {
			master_name(record).casefold(): {
				"name": master_name(record),
				"parent": scalar(record, "parent"),
			}
			for record in raw_groups
			if master_name(record)
		}

	def _ledgers(self, config, tally_client, from_date, to_date):
		validate_period = getattr(self, "_validate_period", False)
		computed = (
			{"ETPeriodFrom": "$$String:##SVFromDate", "ETPeriodTo": "$$String:##SVToDate"}
			if validate_period
			else {}
		)
		request = build_collection_export(
			config.tally_company,
			"ETLedgerMirrorLedgers",
			"Ledger",
			(*LEDGER_BALANCE_FIELDS, *computed),
			computed_fields=computed,
			filters=[self._scope_formula("$Name")] if getattr(self, "_ledger_scope", []) else [],
			static_variables={
				"SVFROMDATE": from_date.replace("-", ""),
				"SVTODATE": to_date.replace("-", ""),
			},
		)
		rows = [
			row
			for row in parse_collection_export(tally_client.export_collection(request), ("LEDGER",))
			if master_name(row)
		]
		if validate_period:
			for row in rows:
				if not scalar(row, "etperiodfrom") or not scalar(row, "etperiodto"):
					raise ValueError(
						f"Tally did not accept the requested balance period {from_date} to {to_date}"
					)
			if {master_name(row).casefold() for row in rows} != {
				name.casefold() for name in self._ledger_scope
			}:
				raise ValueError("Some selected ledger masters are missing in Tally")
		return rows

	def _ledger_directory(self, raw_ledgers, groups):
		return {
			master_name(raw).casefold(): {
				"parent": scalar(raw, "parent"),
				"primary_group": _primary_group(scalar(raw, "parent"), groups),
				"guid": scalar(raw, "guid"),
			}
			for raw in raw_ledgers
			if master_name(raw)
		}

	def _vouchers(self, config, tally_client, from_date, to_date, ledger_directory, options):
		filters = [
			"$Date >= ##SVFromDate",
			"$Date <= ##SVToDate",
		]
		checkpoint = self._checkpoint_store(config).get(self._target_key(config), "ledger_mirror_voucher")
		if checkpoint:
			filters.append(f"$AlterID > {checkpoint}")
		if not options.get("include_optional_vouchers"):
			filters.extend(("NOT $$IsOrderVch:$VoucherTypeName", "$IsOptional = No"))
		formulas = {}
		if getattr(self, "_ledger_scope", []):
			formulas["ETSelectedLedger"] = self._scope_formula("$LedgerName")
			known_ids = self._checkpoint_store(config).voucher_ids(self._target_key(config))
			membership = ["($$FilterCount:AllLedgerEntries:ETSelectedLedger > 0)"]
			membership.extend(f"($MasterID = {int(value)})" for value in known_ids)
			filters.append(" OR ".join(membership))
		request = build_collection_export(
			config.tally_company,
			"ETLedgerMirrorVouchers",
			"Voucher",
			PAYMENT_VOUCHER_FIELDS,
			filters,
			formulas=formulas,
			static_variables={
				"SVFROMDATE": from_date.replace("-", ""),
				"SVTODATE": to_date.replace("-", ""),
			},
		)
		raw_records = parse_collection_export(tally_client.export_collection(request), ("VOUCHER",))
		records = []
		for raw in raw_records:
			record = normalize_voucher(raw)
			if not record:
				continue
			excluded_origins = set(options.get("excluded_origins") or ())
			if any(
				f"[ExpressTallyOrigin:{origin}]" in str(record.get("narration") or "")
				for origin in excluded_origins
			):
				continue
			name = record.get("voucher_number") or record.get("master_id")
			record.update(_identity("ledger_mirror_voucher", raw, name))
			record["kind"] = "ledger_voucher"
			record["fiscal_year_start"] = from_date
			for entry in record.get("ledger_entries") or []:
				metadata = ledger_directory.get(str(entry.get("ledger") or "").casefold(), {})
				entry.update(
					{
						"parent": metadata.get("parent", ""),
						"primary_group": metadata.get("primary_group", ""),
						"ledger_guid": metadata.get("guid", ""),
					}
				)
			records.append(record)
		if getattr(self, "_ledger_scope", []):
			self._checkpoint_store(config).remember_vouchers(
				self._target_key(config),
				[int(record["master_id"]) for record in records if str(record.get("master_id")).isdigit()],
			)
		return records
