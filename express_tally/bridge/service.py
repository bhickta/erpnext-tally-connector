import logging
import threading
import uuid
from dataclasses import asdict, dataclass

from .clients import BridgeRequestError
from .profiles import AgentProfileRegistry
from .xml_gateway import current_company_request


LOGGER = logging.getLogger("erpnext_tally_bridge")


@dataclass
class SyncSummary:
	fetched: int = 0
	succeeded: int = 0
	failed: int = 0
	skipped: int = 0
	error: str = ""

	def to_dict(self):
		return asdict(self)


class SyncService:
	def __init__(self, config, frappe_client, tally_client, profile_registry=None):
		self.config = config
		self.frappe = frappe_client
		self.tally = tally_client
		self.profiles = profile_registry or AgentProfileRegistry(config.agent_profiles)
		self._lock = threading.Lock()

	def health(self):
		loaded_company = self.tally.get_current_company(current_company_request())
		company_matches = loaded_company == self.config.tally_company
		return {
			"ok": bool(loaded_company) and company_matches,
			"loaded_tally_company": loaded_company,
			"configured_tally_company": self.config.tally_company,
			"company_matches": company_matches,
		}

	def sync_once(self, limit=None):
		if not self._lock.acquire(blocking=False):
			return SyncSummary(skipped=1, error="A sync is already running")
		try:
			return self._sync_once(limit)
		finally:
			self._lock.release()

	def _sync_once(self, limit=None):
		summary = SyncSummary()
		try:
			health = self.health()
			if not health["company_matches"]:
				summary.error = (
					f"Tally company mismatch: loaded '{health['loaded_tally_company']}', "
					f"configured '{health['configured_tally_company']}'"
				)
				return summary
			batch = self.frappe.get_unsynced_documents(self.config, limit=limit)
		except Exception as exc:
			summary.error = str(exc)
			return summary

		if batch.get("schema_version") != 1:
			summary.error = f"Unsupported Frappe sync schema: {batch.get('schema_version')}"
			return summary
		if batch.get("flow") != self.config.flow_name:
			summary.error = f"Unexpected Tally flow: {batch.get('flow')}"
			return summary
		try:
			profile = self.profiles.get(batch.get("agent_profile"))
			profile.validate_environment(self.tally)
		except Exception as exc:
			summary.error = str(exc)
			return summary

		documents = batch.get("documents") or batch.get("orders") or []
		summary.fetched = len(documents)
		for document in documents:
			result = self._sync_document(document, profile)
			try:
				self.frappe.acknowledge(self.config, [result])
			except BridgeRequestError as exc:
				# The deterministic Tally GUID and voucher number make the next retry
				# identifiable even when the acknowledgement response was lost.
				LOGGER.error("Could not acknowledge %s: %s", document["name"], exc)
				summary.failed += 1
				summary.error = str(exc)
				continue
			if result["status"] == "Success":
				summary.succeeded += 1
			else:
				summary.failed += 1
		return summary

	def _sync_document(self, document, profile):
		request_id = str(uuid.uuid4())
		result = {
			"request_id": request_id,
			"source_name": document["name"],
			"source_doctype": document.get("source_doctype", ""),
			"source_modified": document["modified"],
			"source_version": document["modified"],
			"source_hash": document["source_hash"],
			"operation": document["operation"],
			"status": "Failed",
			"target_reference": "",
			"tally_voucher_id": "",
			"error": "",
		}
		try:
			target_reference = profile.deliver(document, self.config, self.tally)
			result["status"] = "Success"
			result["target_reference"] = target_reference
			result["tally_voucher_id"] = target_reference
		except Exception as exc:
			result["error"] = str(exc)[:4000]
			LOGGER.error("Sync failed for %s: %s", document.get("name"), exc)
		return result
