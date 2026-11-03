"""One-way ERPNext -> TallyPrime master synchronization."""

import json
from datetime import date

from .clients import BridgeRequestError

MASTER_TYPES = (
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


class MasterSyncService:
	def __init__(self, config, frappe_client, tally_client):
		self.config = config
		self.frappe = frappe_client
		self.tally = tally_client

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
	):
		selected = normalize_masters(masters)
		duplicate_names = str(duplicate_names or "skip").strip().lower()
		if duplicate_names not in {"skip", "suffix"}:
			raise ValueError("duplicate_names must be skip or suffix")
		params = {
			"company": self.config.erpnext_company,
			"masters": json.dumps(selected),
			"include_disabled": int(bool(include_disabled)),
			"include_opening_balances": int(bool(include_opening_balances)),
			"posting_date": getattr(self.config, "from_date", None) or date.today().isoformat(),
			"duplicate_names": duplicate_names,
		}
		if limit:
			params["limit"] = int(limit)
		payload = self.frappe.request("GET", PAYLOAD_PATH, params)
		batches = payload.get("batches") or []
		if not batches:
			raise BridgeRequestError("ERPNext returned no master batches")

		results = []
		failed = False
		for batch in batches:
			key = str(batch.get("key") or "")
			label = str(batch.get("label") or key)
			messages = batch.get("tallymessage") or []
			if failed:
				results.append(
					{
						"batch": key,
						"label": label,
						"status": "Skipped",
						"message": "Skipped after an earlier batch failed",
						"created": 0,
						"altered": 0,
						"ignored": 0,
						"errors": 0,
						"requests": 0,
						"total": len(messages),
					}
				)
				continue
			if not messages:
				results.append(
					{
						"batch": key,
						"label": label,
						"status": "Success",
						"message": "Nothing to sync",
						"created": 0,
						"altered": 0,
						"ignored": 0,
						"errors": 0,
						"requests": 0,
						"total": 0,
					}
				)
				continue

			created = altered = ignored = errors = requests = 0
			status = "Success"
			message = ""
			for offset in range(0, len(messages), MASTER_BATCH_SIZE):
				chunk = messages[offset : offset + MASTER_BATCH_SIZE]
				request = {
					"static_variables": [
						{"name": "svMstImportFormat", "value": "jsonex"},
						{"name": "svCurrentCompany", "value": self.config.tally_company},
					],
					"tallymessage": chunk,
				}
				requests += 1
				try:
					result = self.tally.import_json(
						request,
						"All Masters",
						require_change=False,
						allow_ignored=True,
					)
				except Exception as exc:
					status = "Failed"
					message = str(exc)[:4000]
					errors += len(chunk)
					break
				created += result.created
				altered += result.altered
				ignored += result.ignored
				errors += result.errors
				if not result.success:
					status = "Failed"
					message = result.message or "Tally rejected the master batch"
					break

			results.append(
				{
					"batch": key,
					"label": label,
					"status": status,
					"message": message,
					"created": created,
					"altered": altered,
					"ignored": ignored,
					"errors": errors,
					"requests": requests,
					"total": len(messages),
				}
			)
			if status == "Failed":
				failed = True

		try:
			summary = self.summary()
			summary_error = ""
		except Exception as exc:
			summary = {}
			summary_error = str(exc)

		return {
			"ok": not failed,
			"masters": selected,
			"include_disabled": bool(include_disabled),
			"include_opening_balances": bool(include_opening_balances),
			"duplicate_names": duplicate_names,
			"skipped": payload.get("skipped") or [],
			"results": results,
			"summary": summary,
			"summary_error": summary_error,
		}
