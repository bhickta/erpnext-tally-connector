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
	flow: str = ""
	direction: str = ""

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

	def discover_flows(self):
		response = self.frappe.get_flows()
		flows = response.get("flows", response if isinstance(response, list) else [])
		selected = set(self.config.selected_flows)
		for flow in flows:
			flow["selected"] = flow.get("key") in selected
			profile_key = flow.get("agent_profile")
			try:
				profile = self.profiles.get(profile_key)
				flow["available"] = profile.supports_direction(flow.get("direction"))
			except ValueError:
				flow["available"] = False
			flow["unavailable_reason"] = (
				"" if flow["available"] else f"Agent profile '{profile_key or '(not set)'}' is not installed"
			)
		return flows

	def sync_once(self, limit=None):
		"""Backward-compatible single outbound flow entry point."""
		flow = self.config.flow_name or next(iter(self.config.selected_flows), "")
		return self.sync_flow(flow, "erpnext_to_tally", limit=limit)

	def sync_flow(self, flow, direction, agent_profile=None, limit=None):
		if not self._lock.acquire(blocking=False):
			return SyncSummary(
				skipped=1,
				error="A sync is already running",
				flow=flow,
				direction=direction,
			)
		try:
			if direction == "erpnext_to_tally":
				return self._sync_outbound(flow, limit)
			if direction == "tally_to_erpnext":
				return self._sync_inbound(flow, agent_profile, limit)
			return SyncSummary(error=f"Unsupported sync direction: {direction}", flow=flow, direction=direction)
		finally:
			self._lock.release()

	def _check_environment(self, summary):
		try:
			health = self.health()
		except Exception as exc:
			summary.error = str(exc)
			return False
		if not health["company_matches"]:
			summary.error = (
				f"Tally company mismatch: loaded '{health['loaded_tally_company']}', "
				f"configured '{health['configured_tally_company']}'"
			)
			return False
		return True

	def _sync_outbound(self, flow, limit=None):
		summary = SyncSummary(flow=flow, direction="erpnext_to_tally")
		if not self._check_environment(summary):
			return summary
		try:
			batch = self.frappe.get_unsynced_documents(self.config, limit=limit, flow=flow)
		except Exception as exc:
			summary.error = str(exc)
			return summary

		if batch.get("schema_version") != 1:
			summary.error = f"Unsupported Frappe sync schema: {batch.get('schema_version')}"
			return summary
		if batch.get("flow") != flow:
			summary.error = f"Unexpected Tally flow: {batch.get('flow')}"
			return summary
		try:
			profile = self.profiles.get(batch.get("agent_profile"))
			if not profile.supports_direction("erpnext_to_tally"):
				raise ValueError(f"Agent profile {profile.key} does not support ERPNext to Tally")
			profile.validate_environment(self.tally)
		except Exception as exc:
			summary.error = str(exc)
			return summary

		documents = batch.get("documents") or batch.get("orders") or []
		summary.fetched = len(documents)
		for document in documents:
			result = self._sync_document(document, profile)
			try:
				self.frappe.acknowledge(self.config, [result], flow=flow)
			except BridgeRequestError as exc:
				# A deterministic Tally GUID makes a retry identifiable after a lost acknowledgement.
				LOGGER.error("Could not acknowledge %s: %s", document["name"], exc)
				summary.failed += 1
				summary.error = str(exc)
				continue
			if result["status"] == "Success":
				summary.succeeded += 1
			else:
				summary.failed += 1
		return summary

	def _sync_inbound(self, flow, agent_profile, limit=None):
		summary = SyncSummary(flow=flow, direction="tally_to_erpnext")
		if not self._check_environment(summary):
			return summary
		try:
			profile = self.profiles.get(agent_profile)
			if not profile.supports_direction("tally_to_erpnext"):
				raise ValueError(f"Agent profile {profile.key} does not support Tally to ERPNext")
			profile.validate_environment(self.tally)
			options = (self.config.flow_options or {}).get(flow, {})
			records = list(profile.collect(self.config, self.tally, limit or self.config.batch_size, options))
			summary.fetched = len(records)
			response = self.frappe.receive(self.config, flow, records)
		except Exception as exc:
			summary.error = str(exc)
			return summary

		results = response.get("results", [])
		for result in results:
			status = str(result.get("status", result.get("message", ""))).lower()
			if status in {"success", "already exists", "skipped"}:
				summary.succeeded += 1
			else:
				summary.failed += 1
		if len(results) < len(records):
			summary.failed += len(records) - len(results)
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
