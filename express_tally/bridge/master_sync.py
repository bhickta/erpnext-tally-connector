"""One-way ERPNext -> TallyPrime master synchronization."""

import json
import logging
from datetime import date

from .clients import BridgeRequestError
from .collection_gateway import build_collection_export, parse_collection_export
from .normalization import master_name

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
		"key": "Item",
		"label": "Items",
		"tally_type": "Stock Item",
		"description": "Item, Item Group and UOM masters",
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
MASTER_BATCH_SIZE = 250

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
	return min(max(value, 1), 1000)


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


def is_cancelled(cancel_event):
	return bool(cancel_event and cancel_event.is_set())


class MasterSyncService:
	def __init__(self, config, frappe_client, tally_client):
		self.config = config
		self.frappe = frappe_client
		self.tally = tally_client
		self._existing_names_by_type = {}

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
		selected = normalize_masters(masters)
		duplicate_policy = normalize_duplicate_policy(duplicate_names)
		batch_size = configured_batch_size(self.config)
		payload = self._fetch_payload(
			selected,
			include_disabled=include_disabled,
			include_opening_balances=include_opening_balances,
			limit=limit,
			duplicate_names=duplicate_policy,
		)
		batches = payload.get("batches") or []
		if not batches:
			raise BridgeRequestError("ERPNext returned no master batches")

		LOGGER.info(
			"Master sync started: masters=%s batch_size=%s batches=%s",
			selected,
			batch_size,
			len(batches),
		)
		results, failed, was_cancelled = self._run_batches(batches, batch_size, cancel_event)
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
		}
		LOGGER.info(
			"Master sync finished: ok=%s cancelled=%s failed_batches=%s",
			result["ok"],
			result["cancelled"],
			sum(row["status"] == "Failed" for row in results),
		)
		return result

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
			return self._existing_names_by_type[object_type]
		collection_name = f"ETExisting{object_type.replace(' ', '')}"
		request = build_collection_export(
			self.config.tally_company,
			collection_name,
			object_type,
			("Name",),
		)
		response = self.tally.export_collection(request)
		records = parse_collection_export(response, tags)
		names = {master_name(row) for row in records if master_name(row)}
		self._existing_names_by_type[object_type] = names
		LOGGER.info("Existing Tally %s masters: %s", object_type, len(names))
		return names

	@staticmethod
	def _apply_actions(messages, existing_names):
		existing_folded = {name.casefold() for name in existing_names}
		for message in messages:
			metadata = message.setdefault("metadata", {})
			name = message.get("name") or metadata.get("name") or ""
			metadata["action"] = "alter" if str(name).casefold() in existing_folded else "create"

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
					self._apply_actions(messages, existing_names)
					result = self._run_batch(parts, messages, batch_size, cancel_event)
			if result["status"] == "Cancelled":
				was_cancelled = True
			if result["status"] in {"Failed", "Cancelled"}:
				failed = True
			results.append(result)
		return results, failed, was_cancelled

	def _run_batch(self, parts, messages, batch_size, cancel_event):
		if not messages:
			return batch_result(parts, "Success", "Nothing to sync")

		created = altered = ignored = errors = requests = 0
		status = "Success"
		message = ""
		for offset in range(0, len(messages), batch_size):
			if is_cancelled(cancel_event):
				status = "Cancelled"
				message = "Cancelled by operator"
				break
			chunk = messages[offset : offset + batch_size]
			requests += 1
			try:
				imported = self._import_chunk(chunk)
			except Exception as exc:
				status = "Failed"
				message = f"{parts['label']} batch failed: {exc}"[:4000]
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

	def _import_chunk(self, chunk):
		request = {
			"static_variables": [
				{"name": "svMstImportFormat", "value": "jsonex"},
				{"name": "svCurrentCompany", "value": self.config.tally_company},
			],
			"tallymessage": chunk,
		}
		return self.tally.import_json(
			request,
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
