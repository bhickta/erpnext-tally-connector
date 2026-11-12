"""One-way ERPNext -> TallyPrime master synchronization."""

import json
import logging
import time
from datetime import date

from express_tally import __version__ as bridge_version

from .clients import BridgeRequestError
from .collection_gateway import build_collection_export, parse_collection_export
from .master_trace import MasterTrace
from .normalization import master_name
from .xml_gateway import unit_name_error

LOGGER = logging.getLogger("erpnext_tally_bridge.master_sync")

MASTER_TYPES = (
	{
		"key": "Group",
		"label": "Accounting groups",
		"tally_type": "Group",
		"description": "Chart-of-accounts groups",
	},
	{
		"key": "Ledger",
		"label": "Ledgers",
		"tally_type": "Ledger",
		"description": "Chart-of-accounts ledgers (includes groups)",
	},
	{
		"key": "Unit",
		"label": "Units of measure",
		"tally_type": "Unit",
		"description": "UOM masters; existing Tally units are skipped",
	},
	{
		"key": "Item Group",
		"label": "Item groups",
		"tally_type": "Stock Group",
		"description": "Stock group hierarchy",
	},
	{
		"key": "Item",
		"label": "Items",
		"tally_type": "Stock Item",
		"description": "Stock Items only; sync Units and Item Groups first",
	},
	{
		"key": "Warehouse",
		"label": "Warehouses",
		"tally_type": "Godown",
		"description": "Warehouse hierarchy as Tally godowns",
	},
	{
		"key": "Customer",
		"label": "Customers",
		"tally_type": "Ledger",
		"description": "Customers as Sundry Debtors ledgers",
	},
	{
		"key": "Supplier",
		"label": "Suppliers",
		"tally_type": "Ledger",
		"description": "Suppliers as Sundry Creditors ledgers",
	},
)

MASTER_KEYS = tuple(master["key"] for master in MASTER_TYPES)

SUMMARY_PATH = "/api/method/express_tally.integrations.master_sync_export.get_master_sync_summary"
PAYLOAD_PATH = "/api/method/express_tally.integrations.master_sync_export.get_master_sync_payload"

# Keep individual JSONEx requests modest so Tally remains responsive on large
# item/customer sets and a single failed request does not lose the whole run.
MASTER_BATCH_SIZE = 10
MAX_MASTER_BATCH_SIZE = 25
MAX_MASTER_REQUEST_BYTES = 64 * 1024

DUPLICATE_POLICIES = frozenset({"skip", "suffix"})
DEFAULT_CUSTOMER_PARENT = "Sundry Debtors"
DEFAULT_SUPPLIER_PARENT = "Sundry Creditors"

# Batch key -> (Tally object type, record tags). Existing masters are altered
# instead of created, preventing Tally's duplicate-master modal error.
EXISTING_MASTER_COLLECTIONS = {
	"units": ("Unit", ("UNIT",)),
	"item_groups": ("Stock Group", ("STOCKGROUP", "STOCKGROUPS")),
	"account_groups": ("Group", ("GROUP",)),
	"account_ledgers": ("Ledger", ("LEDGER",)),
	"warehouses": ("Godown", ("GODOWN",)),
	"customers": ("Ledger", ("LEDGER",)),
	"suppliers": ("Ledger", ("LEDGER",)),
	"items": ("Stock Item", ("STOCKITEM",)),
}


def normalize_masters(masters):
	if masters in (None, ""):
		return list(MASTER_KEYS)
	if isinstance(masters, str):
		masters = json.loads(masters)
	if not isinstance(masters, (list, tuple)) or not masters:
		raise ValueError("Select at least one master type")
	normalized = []
	for value in masters:
		value = str(value or "").strip()
		if value not in MASTER_KEYS:
			raise ValueError(f"Unsupported master type: {value}")
		if value not in normalized:
			normalized.append(value)
	return normalized


def normalize_duplicate_policy(value):
	policy = str(value or "skip").strip().lower()
	if policy not in DUPLICATE_POLICIES:
		raise ValueError("duplicate_names must be skip or suffix")
	return policy


def configured_batch_size(config):
	value = int(getattr(config, "master_batch_size", MASTER_BATCH_SIZE) or MASTER_BATCH_SIZE)
	return min(max(value, 1), MAX_MASTER_BATCH_SIZE)


def batch_result(
	batch,
	status,
	message="",
	*,
	created=0,
	altered=0,
	ignored=0,
	errors=0,
	requests=0,
	total=0,
):
	return {
		"batch": batch["key"],
		"label": batch["label"],
		"status": status,
		"message": message,
		"created": created,
		"altered": altered,
		"ignored": ignored,
		"errors": errors,
		"requests": requests,
		"total": total,
	}


def batch_parts(batch):
	key = str(batch.get("key") or "")
	label = str(batch.get("label") or key)
	return {"key": key, "label": label}, batch.get("tallymessage") or []


def message_name(message):
	return str(
		message.get("name") or message.get("metadata", {}).get("name") or ""
	)


def is_cancelled(cancel_event):
	return bool(cancel_event and cancel_event.is_set())


class MasterSyncService:
	def __init__(self, config, frappe_client, tally_client):
		self.config = config
		self.frappe = frappe_client
		self.tally = tally_client
		self._existing_names_by_type = {}
		self._trace = None

	def metadata(self):
		return {"masters": [dict(master) for master in MASTER_TYPES]}

	def summary(self):
		return self.frappe.request(
			"GET",
			SUMMARY_PATH,
			{"company": self.config.erpnext_company},
		)

	def sync(
		self,
		masters=None,
		include_disabled=False,
		include_opening_balances=False,
		limit=None,
		duplicate_names="skip",
		cancel_event=None,
	):
		self._existing_names_by_type.clear()
		selected = normalize_masters(masters)
		duplicate_policy = normalize_duplicate_policy(duplicate_names)
		batch_size = configured_batch_size(self.config)
		trace = MasterTrace(
			self.config,
			selected,
			{
				"batch_size": batch_size,
				"include_disabled": bool(include_disabled),
				"include_opening_balances": bool(include_opening_balances),
				"limit": limit,
				"duplicate_names": duplicate_policy,
				"erpnext_company": self.config.erpnext_company,
				"tally_company": self.config.tally_company,
			},
			bridge_version=bridge_version,
		)
		self._trace = trace
		try:
			payload = self._fetch_payload(
				selected,
				include_disabled=include_disabled,
				include_opening_balances=include_opening_balances,
				limit=limit,
				duplicate_names=duplicate_policy,
			)
			batches = payload.get("batches") or []
			trace.write(
				"payload",
				connector_version=payload.get("connector_version"),
				tally_company=payload.get("tally_company"),
				counts=payload.get("counts"),
				skipped=payload.get("skipped") or [],
				batches=[
					{
						"key": batch.get("key"),
						"label": batch.get("label"),
						"count": len(batch.get("tallymessage") or []),
					}
					for batch in batches
				],
			)
			if not batches:
				raise BridgeRequestError("ERPNext returned no master batches")

			LOGGER.info(
				"Master sync started: masters=%s batch_size=%s batches=%s trace=%s",
				selected,
				batch_size,
				len(batches),
				trace.trace_id or "unavailable",
			)
			results, failed, was_cancelled = self._run_batches(
				batches, batch_size, cancel_event
			)
			summary, summary_error = self._safe_summary()

			result = {
				"ok": not failed,
				"masters": selected,
				"include_disabled": bool(include_disabled),
				"include_opening_balances": bool(include_opening_balances),
				"duplicate_names": duplicate_policy,
				"cancelled": was_cancelled,
				"skipped": payload.get("skipped") or [],
				"results": results,
				"summary": summary,
				"summary_error": summary_error,
				"trace_id": trace.trace_id,
			}
			trace.write(
				"sync_end",
				ok=result["ok"],
				cancelled=result["cancelled"],
				summary_error=summary_error,
				results=results,
			)
			LOGGER.info(
				"Master sync finished: ok=%s cancelled=%s failed_batches=%s",
				result["ok"],
				result["cancelled"],
				sum(row["status"] == "Failed" for row in results),
			)
			return result
		except Exception as exc:
			trace.write("sync_error", error=str(exc)[:4000], error_type=type(exc).__name__)
			exc.trace_id = trace.trace_id
			raise
		finally:
			self._trace = None

	def _trace_write(self, event, **fields):
		if self._trace:
			self._trace.write(event, **fields)

	@staticmethod
	def _trace_names(messages, limit=100):
		names = [message_name(message) for message in messages]
		return {
			"names": names[:limit],
			"truncated": len(names) > limit,
		}

	def _fetch_payload(
		self,
		selected,
		*,
		include_disabled,
		include_opening_balances,
		limit,
		duplicate_names,
	):
		params = {
			"company": self.config.erpnext_company,
			"masters": json.dumps(selected),
			"include_disabled": int(bool(include_disabled)),
			"include_opening_balances": int(bool(include_opening_balances)),
			"posting_date": getattr(self.config, "from_date", None) or date.today().isoformat(),
			"duplicate_names": duplicate_names,
			"customer_parent": getattr(self.config, "customer_parent", DEFAULT_CUSTOMER_PARENT),
			"supplier_parent": getattr(self.config, "supplier_parent", DEFAULT_SUPPLIER_PARENT),
		}
		if limit:
			params["limit"] = int(limit)
		return self.frappe.request("GET", PAYLOAD_PATH, params)

	def _existing_names(self, batch_key):
		collection = EXISTING_MASTER_COLLECTIONS.get(batch_key)
		if not collection:
			return set()
		object_type, tags = collection
		if object_type in self._existing_names_by_type:
			names = self._existing_names_by_type[object_type]
			self._trace_write(
				"lookup_cached",
				batch_key=batch_key,
				object_type=object_type,
				count=len(names),
			)
			return names
		collection_name = f"ETExisting{object_type.replace(' ', '')}"
		request = build_collection_export(
			self.config.tally_company,
			collection_name,
			object_type,
			("Name",),
		)
		self._trace_write(
			"lookup_start",
			batch_key=batch_key,
			object_type=object_type,
			collection=collection_name,
			tally_company=self.config.tally_company,
		)
		try:
			response = self.tally.export_collection(request)
			records = parse_collection_export(response, tags, raise_on_error=True)
		except Exception as exc:
			self._trace_write(
				"lookup_error",
				batch_key=batch_key,
				object_type=object_type,
				error=str(exc)[:4000],
				error_type=type(exc).__name__,
			)
			raise
		names = {master_name(row) for row in records if master_name(row)}
		self._existing_names_by_type[object_type] = names
		self._trace_write(
			"lookup_result",
			batch_key=batch_key,
			object_type=object_type,
			count=len(names),
			**self._trace_names([{"name": name} for name in sorted(names)], limit=200),
		)
		LOGGER.info("Existing Tally %s masters: %s", object_type, len(names))
		return names

	@staticmethod
	def _apply_actions(messages, existing_names):
		existing_folded = {name.casefold() for name in existing_names}
		for message in messages:
			metadata = message.setdefault("metadata", {})
			name = message.get("name") or metadata.get("name") or ""
			metadata["action"] = "alter" if str(name).casefold() in existing_folded else "create"
			existing_folded.add(str(name).casefold())

	def _run_batches(self, batches, batch_size, cancel_event):
		results = []
		failed = False
		was_cancelled = False
		for batch in batches:
			parts, messages = batch_parts(batch)
			if is_cancelled(cancel_event):
				was_cancelled = True
				result = batch_result(
					parts,
					"Cancelled",
					"Cancelled by operator",
					total=len(messages),
				)
			elif failed:
				result = batch_result(
					parts,
					"Skipped",
					"Skipped after an earlier batch failed",
					total=len(messages),
				)
			elif not messages:
				result = batch_result(parts, "Success", "Nothing to sync")
			else:
				try:
					existing_names = self._existing_names(parts["key"])
				except Exception as exc:
					result = batch_result(
						parts,
						"Failed",
						f"{parts['label']} lookup failed: {exc}"[:4000],
						errors=len(messages),
						total=len(messages),
					)
					LOGGER.warning("Master lookup %s failed: %s", parts["key"], exc)
				else:
					if parts["key"] == "units":
						# Units do not support aliases and altering a unit that is
						# already used by stock/vouchers can crash TallyPrime.
						# Keep existing units untouched; only create new ones.
						existing_folded = {name.casefold() for name in existing_names}
						skipped = [
							message
							for message in messages
							if message_name(message).casefold() in existing_folded
						]
						messages[:] = [
							message
							for message in messages
							if message_name(message).casefold() not in existing_folded
						]
						if skipped:
							self._trace_write(
								"skip_existing",
								batch_key=parts["key"],
								reason="existing Tally units are never altered",
								count=len(skipped),
								**self._trace_names(skipped, limit=200),
							)
						if not messages:
							result = batch_result(
								parts,
								"Success",
								"All Tally units already exist; existing units were not altered",
							)
						else:
							result = self._run_batch(
								parts, messages, batch_size, cancel_event, existing_names
							)
					else:
						result = self._run_batch(
							parts, messages, batch_size, cancel_event, existing_names
						)
			if result["status"] == "Cancelled":
				was_cancelled = True
			if result["status"] in {"Failed", "Cancelled"}:
				failed = True
			self._trace_write("batch_result", **result)
			results.append(result)
		return results, failed, was_cancelled

	def _run_batch(self, parts, messages, batch_size, cancel_event, existing_names=None):
		# TallyPrime answers "BAD UNIT NAME" for a unit it cannot name, and a
		# rejected name can leave the import request unusable. Screen them out
		# here so one unsupported ERPNext UOM cannot fail the whole batch.
		rejected_names = []
		if parts["key"] == "units" and messages:
			accepted = []
			for row in messages:
				reason = unit_name_error(message_name(row))
				if reason:
					rejected_names.append((message_name(row), reason))
				else:
					accepted.append(row)
			if rejected_names:
				self._trace_write(
					"unit_name_rejected",
					batch_key=parts["key"],
					count=len(rejected_names),
					reason=rejected_names[0][1],
					names=[name for name, _ in rejected_names][:200],
				)
			messages = accepted
		if not messages:
			if rejected_names:
				sample = ", ".join(name for name, _ in rejected_names[:5])
				return batch_result(
					parts,
					"Failed",
					(
						f"{len(rejected_names)} unit(s) cannot be created in TallyPrime"
						f" because {rejected_names[0][1]}. Rename them in ERPNext, "
						f"for example: {sample}"
					)[:4000],
					errors=len(rejected_names),
				)
			return batch_result(parts, "Success", "Nothing to sync")

		created = altered = ignored = errors = requests = 0
		status = "Success"
		message = ""
		offset = 0
		while offset < len(messages):
			if is_cancelled(cancel_event):
				status = "Cancelled"
				message = "Cancelled by operator"
				break
			chunk = messages[offset : offset + batch_size]
			try:
				if existing_names is not None:
					self._apply_actions(chunk, existing_names)
				while self._request_size(chunk) > MAX_MASTER_REQUEST_BYTES and len(chunk) > 1:
					chunk = chunk[:max(1, len(chunk) // 2)]
				request_bytes = self._request_size(chunk)
				if request_bytes > MAX_MASTER_REQUEST_BYTES:
					raise BridgeRequestError("A single master exceeds the safe 64 KiB request limit")
				actions = {}
				for row in chunk:
					action = row.get("metadata", {}).get("action") or "create"
					actions[action] = actions.get(action, 0) + 1
				self._trace_write(
					"chunk_start",
					batch_key=parts["key"],
					offset=offset,
					size=len(chunk),
					bytes=request_bytes,
					actions=actions,
					batch_size=batch_size,
					**self._trace_names(chunk, limit=25),
				)
				requests += 1
				started = time.monotonic()
				imported = self._import_chunk(chunk, parts["key"])
				elapsed = time.monotonic() - started
				offset += len(chunk)
				# Reduce future work before a slow response becomes a timeout.
				target = min(2.0, float(getattr(self.config, "request_timeout_seconds", 30)) / 5)
				if elapsed > target:
					batch_size = max(1, min(batch_size, len(chunk)) // 2)
				self._trace_write(
					"chunk_result",
					batch_key=parts["key"],
					size=len(chunk),
					seconds=round(elapsed, 3),
					success=bool(imported.success),
					created=imported.created,
					altered=imported.altered,
					ignored=imported.ignored,
					errors=imported.errors,
					message=str(imported.message or "")[:4000],
					next_batch_size=batch_size,
				)
				LOGGER.info("Master chunk %s: records=%s seconds=%.2f next_size=%s",
					parts["key"], len(chunk), elapsed, batch_size)
			except Exception as exc:
				status = "Failed"
				chunk_names = ", ".join(message_name(row) for row in chunk[:5])
				self._trace_write(
					"chunk_error",
					batch_key=parts["key"],
					offset=offset,
					size=len(chunk),
					error=str(exc)[:4000],
					error_type=type(exc).__name__,
					**self._trace_names(chunk, limit=25),
				)
				message = (
					f"{parts['label']} batch failed: {exc} "
					f"[chunk: {chunk_names}]"
				)[:4000]
				errors += len(chunk)
				LOGGER.warning("Master batch %s failed: %s", parts["key"], exc)
				break
			created += imported.created
			altered += imported.altered
			ignored += imported.ignored
			errors += imported.errors
			if not imported.success:
				status = "Failed"
				message = imported.message or f"{parts['label']} was rejected by Tally"
				LOGGER.warning("Master batch %s rejected: %s", parts["key"], message)
				break
			if existing_names is not None:
				existing_names.update(message_name(row) for row in chunk)

		if rejected_names and status == "Success":
			message = (
				f"Skipped {len(rejected_names)} unit(s) whose names TallyPrime cannot "
				f"accept ({rejected_names[0][1]}). First: "
				+ ", ".join(name for name, _ in rejected_names[:5])
			)[:4000]
		if status == "Success":
			LOGGER.info(
				"Master batch %s complete: created=%s altered=%s ignored=%s errors=%s requests=%s",
				parts["key"],
				created,
				altered,
				ignored,
				errors,
				requests,
			)
		return batch_result(
			parts,
			status,
			message,
			created=created,
			altered=altered,
			ignored=ignored,
			errors=errors,
			requests=requests,
			total=len(messages),
		)

	def _request(self, chunk):
		return {
			"static_variables": [
				{"name": "svMstImportFormat", "value": "jsonex"},
				{"name": "svCurrentCompany", "value": self.config.tally_company},
			],
			"tallymessage": chunk,
		}

	def _request_size(self, chunk):
		return len(json.dumps(self._request(chunk), ensure_ascii=False).encode("utf-8"))

	def _import_chunk(self, chunk, batch_key=""):
		if batch_key == "units":
			# TallyPrime's JSON importer raises an exception for Unit masters
			# and can open a modal that blocks its HTTP server. The XML master
			# import is reliable for units, so keep them on that path.
			from .xml_gateway import build_unit_import

			return self.tally.import_xml(
				build_unit_import(self.config.tally_company, chunk),
				require_change=False,
				allow_ignored=True,
			)
		return self.tally.import_json(
			self._request(chunk),
			"All Masters",
			require_change=False,
			allow_ignored=True,
		)

	def _safe_summary(self):
		try:
			return self.summary(), ""
		except Exception as exc:
			LOGGER.warning("Could not read master summary: %s", exc)
			return {}, str(exc)
