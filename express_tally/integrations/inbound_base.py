"""Shared logging and lifecycle base for Tally-to-ERPNext flow handlers."""

import logging
import uuid
from collections.abc import Mapping

import frappe
from frappe.utils import now_datetime

from express_tally.bridge.common import is_success_status, stable_digest
from express_tally.framework import InboundFlow
from express_tally.framework.sync_log import sync_idempotency_key

LOGGER = logging.getLogger("express_tally.inbound")
ALLOWED_ROLES = frozenset({"Tally Sync User", "Accounts Manager", "System Manager"})


def _source_hash(record):
	stable_record = {key: value for key, value in record.items() if not key.startswith("_previous_")}
	return stable_digest(stable_record)


def _success_result(record, doctype="", name="", status="Success", message=""):
	return {
		"source": record.get("_tally_key"),
		"source_type": record.get("_tally_type"),
		"source_alter_id": record.get("_tally_alter_id"),
		"status": status,
		"doctype": doctype,
		"name": name,
		"message": message,
	}


class InboundLog:
	def __init__(self, flow_key):
		self.flow_key = flow_key

	def voucher_flow_scope(self):
		keys = ["express_tally.standard_vouchers_from_tally", "express_tally.payments_from_tally"]
		return ["in", keys] if self.flow_key in keys else self.flow_key

	def previous_success(self, context, record):
		return frappe.db.get_value(
			"Tally Sync Log",
			{
				"flow_key": self.voucher_flow_scope(),
				"direction": "Tally to ERPNext",
				"target_id": context.target_id,
				"source_reference": record.get("_tally_key"),
				"source_hash": _source_hash(record),
				"status": "Success",
			},
			["target_reference", "source_doctype"],
			as_dict=True,
		)

	def previous_target(self, context, record):
		return frappe.db.get_value(
			"Tally Sync Log",
			{
				"flow_key": self.voucher_flow_scope(),
				"direction": "Tally to ERPNext",
				"target_id": context.target_id,
				"source_reference": record.get("_tally_key"),
				"status": "Success",
			},
			["target_reference", "source_doctype"],
			order_by="creation desc",
			as_dict=True,
		)

	def record(self, context, record, result, operation="Create"):
		status = "Success" if is_success_status(result.get("status")) else "Failed"
		idempotency_key = None
		if status == "Success":
			idempotency_key = sync_idempotency_key(
				"inbound",
				self.flow_key,
				context.target_id,
				record.get("_tally_key"),
				_source_hash(record),
			)
		frappe.get_doc(
			{
				"doctype": "Tally Sync Log",
				"flow_key": self.flow_key,
				"direction": "Tally to ERPNext",
				"company": context.company,
				"request_id": str(uuid.uuid4()),
				"idempotency_key": idempotency_key,
				"status": status,
				"operation": operation,
				"source_system": "Tally",
				"source_type": record.get("_tally_type"),
				"source_reference": record.get("_tally_key"),
				"source_doctype": result.get("doctype"),
				"source_name": result.get("name"),
				"source_hash": _source_hash(record),
				"target_system": "ERPNext",
				"target_id": context.target_id,
				"tally_company": context.tally_company,
				"target_reference": result.get("name"),
				"synced_on": now_datetime(),
				"error": "" if status == "Success" else str(result.get("message") or "")[:4000],
			}
		).insert(ignore_permissions=True)

	def status(self, context):
		counts = dict(
			frappe.db.sql(
				"""
				SELECT status, COUNT(*)
				FROM `tabTally Sync Log`
				WHERE flow_key = %(flow)s
				  AND direction = 'Tally to ERPNext'
				  AND target_id = %(target)s
				GROUP BY status
				""",
				{"flow": self.flow_key, "target": context.target_id},
			)
		)
		return {"successful": counts.get("Success", 0), "failed": counts.get("Failed", 0)}


class LoggedInboundFlow(InboundFlow):
	allowed_roles = ALLOWED_ROLES

	def __init__(self):
		self.sync_log = InboundLog(self.key)

	def receive(self, context, records):
		results = []
		succeeded = failed = 0
		for index, record in enumerate(records):
			if not isinstance(record, Mapping) or not record.get("_tally_key"):
				LOGGER.warning("Inbound flow %s rejected a record without a Tally identity", self.key)
				results.append({"status": "Failed", "message": "Invalid Tally source identity"})
				failed += 1
				continue
			previous = self.sync_log.previous_success(context, record)
			if previous:
				results.append(
					_success_result(
						record,
						previous.get("source_doctype") or "",
						previous.get("target_reference") or "",
						status="Skipped",
						message="This Tally version was already imported",
					)
				)
				continue
			previous_target = self.sync_log.previous_target(context, record)
			if previous_target:
				record = dict(record)
				record["_previous_doctype"] = previous_target.get("source_doctype")
				record["_previous_name"] = previous_target.get("target_reference")
			savepoint = f"tally_inbound_{index}"
			frappe.db.savepoint(savepoint)
			try:
				if record.get("cancelled") or record.get("deleted"):
					result = self._cancel_previous(record)
					operation = "Cancel"
				else:
					result = self.apply_record(context, record)
					operation = "Alter" if previous_target else "Create"
			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				result = _success_result(record, status="Failed", message=str(exc) or type(exc).__name__)
				operation = "Alter" if previous_target else "Create"
			self.sync_log.record(context, record, result, operation=operation)
			results.append(result)
			if is_success_status(result.get("status")):
				succeeded += 1
			else:
				failed += 1
				LOGGER.warning(
					"Inbound flow %s failed for %s: %s",
					self.key,
					record.get("_tally_key"),
					result.get("message"),
				)
		LOGGER.info(
			"Inbound flow %s processed %s record(s): %s succeeded, %s failed",
			self.key,
			len(records),
			succeeded,
			failed,
		)
		return results

	def _cancel_previous(self, record):
		doctype = record.get("_previous_doctype")
		name = record.get("_previous_name")
		if not doctype or not name or not frappe.db.exists(doctype, name):
			return _success_result(
				record, status="Skipped", message="Cancelled Tally record was not imported"
			)
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus == 1:
			doc.cancel()
		elif doc.docstatus == 0:
			doc.delete(ignore_permissions=True)
		return _success_result(record, doctype, name, message=f"{doctype} cancelled")

	def status(self, context):
		return self.sync_log.status(context)

	def apply_record(self, context, record):
		raise NotImplementedError
