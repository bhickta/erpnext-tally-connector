"""Built-in TDL-free extraction profiles for Tally-to-ERPNext flows."""

import hashlib
import json
import os
import threading
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from .collection_gateway import (
	build_collection_export,
	nested_records,
	parse_collection_export,
	scalar,
)
from .profiles import AgentProfile


MASTER_COLLECTIONS = (
	(
		"unit",
		"ETControlCentreUnits",
		"Unit",
		("Name", "OriginalName", "DecimalPlaces", "GUID", "AlterID"),
		("UNIT",),
	),
	(
		"group",
		"ETControlCentreGroups",
		"Group",
		("Name", "Parent", "ReservedName", "IsRevenue", "GUID", "AlterID"),
		("GROUP",),
	),
	(
		"stock_group",
		"ETControlCentreStockGroups",
		"Stock Group",
		("Name", "Parent", "GUID", "AlterID"),
		("STOCKGROUP", "STOCKGROUPS"),
	),
	(
		"godown",
		"ETControlCentreGodowns",
		"Godown",
		("Name", "Parent", "GUID", "AlterID"),
		("GODOWN",),
	),
	(
		"ledger",
		"ETControlCentreLedgers",
		"Ledger",
		(
			"Name",
			"Parent",
			"LedgerCode",
			"GSTRegistrationType",
			"PartyGSTIN",
			"IncomeTaxNumber",
			"Address",
			"StateName",
			"CountryName",
			"PinCode",
			"Email",
			"LedgerPhone",
			"LedgerMobile",
			"CreditLimit",
			"GUID",
			"AlterID",
		),
		("LEDGER",),
	),
	(
		"stock_item",
		"ETControlCentreStockItems",
		"Stock Item",
		(
			"Name",
			"Parent",
			"BaseUnits",
			"AdditionalUnits",
			"Conversion",
			"PartNo",
			"Description",
			"GSTApplicable",
			"GSTTypeOfSupply",
			"GSTDetails.*",
			"OpeningBalance",
			"OpeningValue",
			"GUID",
			"AlterID",
		),
		("STOCKITEM",),
	),
)

VOUCHER_FIELDS = (
	"MasterID",
	"AlterID",
	"GUID",
	"VoucherNumber",
	"VoucherTypeName",
	"Date",
	"EffectiveDate",
	"Reference",
	"ReferenceDate",
	"Narration",
	"PartyLedgerName",
	"IsInvoice",
	"IsOrder",
	"IsCancelled",
	"IsDeleted",
	"AllLedgerEntries.*",
	"LedgerEntries.*",
	"AllInventoryEntries.*",
	"InventoryEntries.*",
)

LEDGER_BALANCE_FIELDS = (
	"Name",
	"Parent",
	"OpeningBalance",
	"ClosingBalance",
	"GUID",
	"AlterID",
)


class CheckpointStore:
	def __init__(self, directory):
		directory = Path(directory or ".").resolve()
		self.path = directory / "tally-inbound-checkpoints.json"
		self._lock = threading.RLock()

	def get(self, target, collection_type):
		with self._lock:
			return int(self._read().get(target, {}).get(collection_type, 0) or 0)

	def advance(self, target, collection_type, alter_id):
		with self._lock:
			values = self._read()
			values.setdefault(target, {})[collection_type] = max(
				int(values.get(target, {}).get(collection_type, 0) or 0),
				int(alter_id or 0),
			)
			self.path.parent.mkdir(parents=True, exist_ok=True)
			temporary = self.path.with_suffix(".tmp")
			with temporary.open("w", encoding="utf-8") as handle:
				json.dump(values, handle, ensure_ascii=False, indent=2)
				handle.write("\n")
			os.replace(temporary, self.path)

	def reset(self, target):
		with self._lock:
			values = self._read()
			removed = values.pop(target, None) is not None
			if removed:
				self.path.parent.mkdir(parents=True, exist_ok=True)
				temporary = self.path.with_suffix(".tmp")
				with temporary.open("w", encoding="utf-8") as handle:
					json.dump(values, handle, ensure_ascii=False, indent=2)
					handle.write("\n")
				os.replace(temporary, self.path)
			return removed

	def _read(self):
		try:
			with self.path.open(encoding="utf-8") as handle:
				value = json.load(handle)
			return value if isinstance(value, dict) else {}
		except (OSError, ValueError):
			return {}


class IncrementalTallyProfile(AgentProfile):
	def _target_key(self, config):
		return f"{config.target_id}:{config.tally_company}"

	def _checkpoint_store(self, config):
		return CheckpointStore(config.runtime_directory)

	def _pending(self, config, records, limit):
		store = self._checkpoint_store(config)
		target = self._target_key(config)
		pending = []
		for record in records:
			collection_type = record["_tally_type"]
			alter_id = int(record.get("_tally_alter_id") or 0)
			if alter_id > store.get(target, collection_type):
				pending.append(record)
		pending.sort(
			key=lambda row: (
				row.get("_tally_priority", 0),
				row.get("_tally_depth", 0),
				row.get("_tally_alter_id", 0),
				row.get("_tally_type", ""),
			)
		)
		return pending[:limit]

	def acknowledge_collected(self, config, records, results):
		by_type = defaultdict(list)
		for record, result in zip(records, results, strict=False):
			by_type[record["_tally_type"]].append((record, result))
		store = self._checkpoint_store(config)
		target = self._target_key(config)
		for collection_type, entries in by_type.items():
			checkpoint = store.get(target, collection_type)
			for record, result in sorted(entries, key=lambda entry: entry[0].get("_tally_alter_id", 0)):
				status = str(result.get("status", result.get("message", ""))).lower()
				if status not in {"success", "already exists", "skipped"}:
					break
				checkpoint = max(checkpoint, int(record.get("_tally_alter_id") or 0))
			store.advance(target, collection_type, checkpoint)


class TallyMastersProfile(IncrementalTallyProfile):
	key = "tally_masters_v1"

	def collect(self, config, tally_client, limit, options=None):
		raw_by_type = {}
		store = self._checkpoint_store(config)
		target = self._target_key(config)
		for collection_type, name, object_type, fields, tags in MASTER_COLLECTIONS:
			checkpoint = store.get(target, collection_type)
			# The complete Group hierarchy is also used to classify ledgers under
			# Sundry Debtors/Creditors; only its returned records are checkpoint-filtered.
			filters = (f"$AlterID > {checkpoint}",) if checkpoint and collection_type != "group" else ()
			request = build_collection_export(config.tally_company, name, object_type, fields, filters)
			response = tally_client.export_collection(request)
			raw_by_type[collection_type] = parse_collection_export(response, tags)

		groups = {
			master_name(record).casefold(): {
				"name": master_name(record),
				"parent": scalar(record, "parent"),
			}
			for record in raw_by_type["group"]
			if master_name(record)
		}
		records = []
		for priority, (collection_type, *_) in enumerate(MASTER_COLLECTIONS):
			for raw in raw_by_type[collection_type]:
				record = normalize_master(collection_type, raw, groups)
				if record:
					record["_tally_priority"] = priority
					records.append(record)
		parents_by_type = defaultdict(set)
		by_type_and_name = {}
		for record in records:
			by_type_and_name[(record["_tally_type"], record["name"].casefold())] = record
			if record.get("parent"):
				parents_by_type[record["_tally_type"]].add(record["parent"].casefold())
		for record in records:
			record["is_group"] = record["name"].casefold() in parents_by_type[record["_tally_type"]]
			record["_tally_depth"] = master_depth(record, by_type_and_name)
		return self._pending(config, records, limit)


class TallyVouchersProfile(IncrementalTallyProfile):
	key = "tally_vouchers_v1"

	def collect(self, config, tally_client, limit, options=None):
		checkpoint = self._checkpoint_store(config).get(self._target_key(config), "voucher")
		filters = [f"$AlterID > {checkpoint}"] if checkpoint else []
		if config.from_date:
			filters.append(f"$Date >= $$Date:{config.from_date.replace('-', '')}")
		if config.to_date:
			filters.append(f"$Date <= $$Date:{config.to_date.replace('-', '')}")
		request = build_collection_export(
			config.tally_company,
			"ETControlCentreVouchers",
			"Voucher",
			VOUCHER_FIELDS,
			filters,
		)
		response = tally_client.export_collection(request)
		raw_records = parse_collection_export(response, ("VOUCHER",))
		records = [normalize_voucher(record) for record in raw_records]
		return self._pending(config, [record for record in records if record], limit)


class TallyLedgerMirrorProfile(IncrementalTallyProfile):
	"""Stage opening balances, accounting vouchers, then closing-balance checks."""

	key = "tally_ledger_mirror_v1"

	def collect(self, config, tally_client, limit, options=None):
		options = options or {}
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
		for raw in reconciliation_ledgers:
			name = master_name(raw)
			if not name:
				continue
			reconciliation_records.append(
				{
					**_identity(reconciliation_type, raw, name),
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
		request = build_collection_export(
			config.tally_company,
			"ETLedgerMirrorLedgers",
			"Ledger",
			LEDGER_BALANCE_FIELDS,
			static_variables={
				"SVFROMDATE": from_date.replace("-", ""),
				"SVTODATE": to_date.replace("-", ""),
			},
		)
		return parse_collection_export(tally_client.export_collection(request), ("LEDGER",))

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
			f"$Date >= $$Date:{from_date.replace('-', '')}",
			f"$Date <= $$Date:{to_date.replace('-', '')}",
		]
		checkpoint = self._checkpoint_store(config).get(
			self._target_key(config), "ledger_mirror_voucher"
		)
		if checkpoint:
			filters.append(f"$AlterID > {checkpoint}")
		if not options.get("include_optional_vouchers"):
			filters.extend(("$IsOrder = No", "$IsOptional = No"))
		request = build_collection_export(
			config.tally_company,
			"ETLedgerMirrorVouchers",
			"Voucher",
			VOUCHER_FIELDS,
			filters,
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
		return records


def master_name(record):
	return scalar(record, "_name", "name", "name_list.name", "language_name_list.name")


def master_depth(record, by_type_and_name):
	depth = 0
	parent = str(record.get("parent") or "").casefold()
	visited = set()
	while parent and parent not in visited:
		visited.add(parent)
		parent_record = by_type_and_name.get((record["_tally_type"], parent))
		if not parent_record:
			break
		depth += 1
		parent = str(parent_record.get("parent") or "").casefold()
	return depth


def integer(value):
	try:
		return int(float(str(value or "0").replace(",", "")))
	except ValueError:
		return 0


def number(value):
	text = str(value or "0").strip().replace(",", "")
	match = __import__("re").search(r"[-+]?\d+(?:\.\d+)?", text)
	return float(match.group(0)) if match else 0.0


def tally_balance(value):
	"""Normalize Tally balances to ERPNext's debit-minus-credit convention."""
	text = str(value or "0").strip()
	amount = number(text)
	if "dr" in text.casefold():
		return abs(amount)
	if "cr" in text.casefold():
		return -abs(amount)
	# Native Tally amounts are negative for debit and positive for credit.
	return -amount


def tally_date(value):
	value = str(value or "").strip()
	if len(value) == 8 and value.isdigit():
		return f"{value[:4]}-{value[4:6]}-{value[6:]}"
	return value


def _primary_group(parent, groups):
	current = str(parent or "").strip()
	visited = set()
	last = current
	while current and current.casefold() not in visited:
		visited.add(current.casefold())
		last = current
		group = groups.get(current.casefold())
		if not group or not group.get("parent") or group["parent"].casefold() == "primary":
			break
		current = group["parent"]
	return last


def _identity(collection_type, raw, name):
	alter_id = integer(scalar(raw, "alter_id", "alterid"))
	guid = scalar(raw, "guid")
	if not guid:
		guid = hashlib.sha256(f"{collection_type}:{name}".encode()).hexdigest()
	return {
		"_tally_type": collection_type,
		"_tally_alter_id": alter_id,
		"_tally_guid": guid,
		"_tally_key": f"{collection_type}:{guid}",
	}


def normalize_master(collection_type, raw, groups):
	name = master_name(raw)
	if not name:
		return None
	parent = scalar(raw, "parent")
	base = {**_identity(collection_type, raw, name), "name": name, "parent": parent}
	if collection_type == "unit":
		return {**base, "kind": "uom", "must_be_whole_number": integer(scalar(raw, "decimal_places")) == 0}
	if collection_type == "stock_group":
		return {**base, "kind": "item_group"}
	if collection_type == "godown":
		return {**base, "kind": "warehouse"}
	if collection_type == "stock_item":
		return {
			**base,
			"kind": "item",
			"item_code": scalar(raw, "part_no", "partno") or name,
			"stock_uom": scalar(raw, "base_units", "baseunits") or "Nos",
			"description": scalar(raw, "description") or name,
			"opening_stock": number(scalar(raw, "opening_balance", "openingbalance")),
			"opening_value": number(scalar(raw, "opening_value", "openingvalue")),
			"hsn_code": scalar(raw, "gst_details_list.hsn_code", "hsn_code"),
		}
	if collection_type == "group":
		primary = _primary_group(parent, groups)
		if primary.casefold() == "sundry debtors":
			return {**base, "kind": "customer_group", "primary_group": primary}
		if primary.casefold() == "sundry creditors":
			return {**base, "kind": "supplier_group", "primary_group": primary}
		return {**base, "kind": "account_group", "primary_group": primary}
	if collection_type == "ledger":
		primary = _primary_group(parent, groups)
		kind = "account"
		if primary.casefold() == "sundry debtors":
			kind = "customer"
		elif primary.casefold() == "sundry creditors":
			kind = "supplier"
		addresses = raw.get("address_list") or raw.get("address") or []
		if isinstance(addresses, dict):
			addresses = list(addresses.values())
		if not isinstance(addresses, list):
			addresses = [addresses]
		return {
			**base,
			"kind": kind,
			"primary_group": primary,
			"code": scalar(raw, "ledger_code", "ledgercode") or name,
			"gst_category": scalar(raw, "gst_registration_type", "gstregistrationtype"),
			"gstin": scalar(raw, "party_gstin", "partygstin"),
			"pan": scalar(raw, "income_tax_number", "incometaxnumber"),
			"state": scalar(raw, "state_name", "statename"),
			"country": scalar(raw, "country_name", "countryname") or "India",
			"pincode": scalar(raw, "pin_code", "pincode"),
			"email": scalar(raw, "email"),
			"phone": scalar(raw, "ledger_phone", "ledgerphone"),
			"mobile": scalar(raw, "ledger_mobile", "ledgermobile"),
			"credit_limit": number(scalar(raw, "credit_limit", "creditlimit")),
			"address_lines": [str(value).strip() for value in addresses if str(value).strip()],
		}
	return None


def normalize_voucher(raw):
	voucher_number = scalar(raw, "voucher_number", "vouchernumber", "_name", "name")
	master_id = scalar(raw, "master_id", "masterid")
	name = voucher_number or master_id
	if not name:
		return None
	ledger_entries = []
	raw_ledger_entries = nested_records(raw, "all_ledger_entries_list") or nested_records(
		raw, "ledger_entries_list"
	)
	for entry in raw_ledger_entries:
		ledger_entries.append(
			{
				"ledger": scalar(entry, "ledger_name", "ledgername"),
				"amount": number(scalar(entry, "amount")),
				"is_deemed_positive": scalar(entry, "is_deemed_positive", "isdeemedpositive"),
				"bill_allocations": nested_records(entry, "bill_allocations_list"),
			}
		)
	inventory_entries = []
	raw_inventory_entries = nested_records(raw, "all_inventory_entries_list") or nested_records(
		raw, "inventory_entries_list"
	)
	for entry in raw_inventory_entries:
		inventory_entries.append(
			{
				"item": scalar(entry, "stock_item_name", "stockitemname"),
				"actual_qty": scalar(entry, "actual_qty", "actualqty"),
				"billed_qty": scalar(entry, "billed_qty", "billedqty"),
				"rate": scalar(entry, "rate"),
				"amount": number(scalar(entry, "amount")),
				"godown": scalar(entry, "godown_name", "godownname", "batch_allocations_list.godown_name"),
				"account": scalar(entry, "accounting_allocations_list.ledger_name", "ledger_name"),
			}
		)
	return {
		**_identity("voucher", raw, name),
		"kind": "voucher",
		"voucher_number": voucher_number,
		"voucher_type": scalar(raw, "voucher_type_name", "vouchertypename"),
		"master_id": master_id,
		"date": tally_date(scalar(raw, "date")),
		"reference": scalar(raw, "reference"),
		"reference_date": tally_date(scalar(raw, "reference_date", "referencedate")),
		"narration": scalar(raw, "narration"),
		"party_ledger": scalar(raw, "party_ledger_name", "partyledgername"),
		"cancelled": scalar(raw, "is_cancelled", "iscancelled").casefold() == "yes",
		"deleted": scalar(raw, "is_deleted", "isdeleted").casefold() == "yes",
		"ledger_entries": [entry for entry in ledger_entries if entry["ledger"]],
		"inventory_entries": [entry for entry in inventory_entries if entry["item"]],
	}
