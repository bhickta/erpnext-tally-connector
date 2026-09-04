"""Reusable durable state for pull/acknowledge integration flows."""

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contracts import FlowContext
from .engine import parse_sequence


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
DOCTYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*$")
VALID_STATUSES = frozenset({"Success", "Failed"})
VALID_OPERATIONS = frozenset({"Create", "Alter", "Cancel"})


@dataclass(frozen=True)
class SourceSpec:
	"""Describe a submitted ERPNext source that can be pulled incrementally."""

	doctype: str
	date_field: str
	company_field: str = "company"
	submitted_only: bool = True

	def __post_init__(self):
		if not DOCTYPE_PATTERN.fullmatch(self.doctype):
			raise ValueError(f"Invalid source DocType: {self.doctype!r}")
		for value in (self.date_field, self.company_field):
			if not IDENTIFIER_PATTERN.fullmatch(value):
				raise ValueError(f"Invalid source field: {value!r}")


class OutboundSyncLog:
	"""Store acknowledgements and find source versions still needing delivery.

	The caller owns mapping and eligibility policy. This class owns the repeated
	version/target/flow matching, idempotent acknowledgement, and status counts.
	"""

	def __init__(
		self,
		flow_key: str,
		sources: Sequence[SourceSpec],
		*,
		include_unscoped_legacy: bool = False,
	):
		self.flow_key = flow_key
		self.sources = {source.doctype: source for source in sources}
		self.include_unscoped_legacy = include_unscoped_legacy
		if not flow_key or not self.sources:
			raise ValueError("OutboundSyncLog requires a flow key and at least one source")

	def previous_target_reference(self, source_doctype: str, source_name: str, target_id: str) -> str:
		import frappe

		self._source(source_doctype)
		rows = frappe.db.sql(
			"""
			SELECT COALESCE(NULLIF(target_reference, ''), tally_voucher_id)
			FROM `tabTally Sync Log`
			WHERE source_doctype = %(source_doctype)s
			  AND source_name = %(source_name)s
			  AND target_id = %(target_id)s
			  AND status = 'Success'
			  AND (flow_key = %(flow_key)s
			       OR (%(include_legacy)s = 1 AND COALESCE(flow_key, '') = ''))
			ORDER BY creation DESC
			LIMIT 1
			""",
			{
				"source_doctype": source_doctype,
				"source_name": source_name,
				"target_id": target_id,
				"flow_key": self.flow_key,
				"include_legacy": int(self.include_unscoped_legacy),
			},
		)
		return rows[0][0] if rows and rows[0][0] else ""

	def pending_references(
		self,
		company: str,
		target_id: str,
		limit: int,
		*,
		from_date: str | None = None,
		to_date: str | None = None,
		source_doctypes: Sequence[str] | None = None,
	) -> list[tuple[str, str, str]]:
		"""Return globally date-sorted ``(date, doctype, name)`` references."""
		selected = source_doctypes or tuple(self.sources)
		refs = []
		for source_doctype in selected:
			refs.extend(
				self._pending_for_source(
					self._source(source_doctype), company, target_id, limit, from_date, to_date
				)
			)
		return sorted(refs)[:limit]

	def acknowledge(self, context: FlowContext, results: Any) -> Mapping[str, int]:
		"""Persist result rows once per request ID and return the accepted count."""
		import frappe
		from frappe.utils import now_datetime

		created = 0
		for result in parse_sequence(results, "results"):
			request_id = str(result.get("request_id") or "").strip()
			if not request_id or len(request_id) > 140:
				raise ValueError("Every result requires a valid request_id")
			if frappe.db.exists("Tally Sync Log", {"request_id": request_id}):
				continue
			status = result.get("status")
			operation = result.get("operation")
			if status not in VALID_STATUSES or operation not in VALID_OPERATIONS:
				raise ValueError("Invalid sync status or operation")
			source_doctype = str(result.get("source_doctype") or "").strip()
			self._source(source_doctype)
			source_name = str(result.get("source_name") or "").strip()
			if not frappe.db.exists(source_doctype, source_name):
				raise ValueError(f"{source_doctype} {source_name} does not exist")
			target_reference = str(
				result.get("target_reference") or result.get("tally_voucher_id") or ""
			)
			frappe.get_doc(
				{
					"doctype": "Tally Sync Log",
					"flow_key": self.flow_key,
					"direction": "ERPNext to Tally",
					"company": context.company,
					"request_id": request_id,
					"status": status,
					"operation": operation,
					"source_system": "ERPNext",
					"source_type": source_doctype,
					"source_reference": source_name,
					"source_doctype": source_doctype,
					"source_name": source_name,
					"source_modified": result.get("source_modified") or result.get("source_version"),
					"source_hash": result.get("source_hash"),
					"target_system": "Tally",
					"target_id": context.target_id,
					"tally_company": context.tally_company,
					"target_reference": target_reference,
					"tally_voucher_id": target_reference,
					"synced_on": now_datetime(),
					"error": str(result.get("error") or "")[:4000],
				}
			).insert(ignore_permissions=True)
			created += 1
		return {"accepted": created}

	def status(self, context: FlowContext) -> Mapping[str, Any]:
		import frappe

		pending_by_doctype = {
			doctype: len(self.pending_references(context.company, context.target_id, 100000, source_doctypes=(doctype,)))
			for doctype in self.sources
		}
		counts = dict(
			frappe.db.sql(
				"""
				SELECT status, COUNT(*)
				FROM `tabTally Sync Log`
				WHERE target_id = %(target_id)s
				  AND (flow_key = %(flow_key)s
				       OR (%(include_legacy)s = 1 AND COALESCE(flow_key, '') = ''))
				GROUP BY status
				""",
				{
					"target_id": context.target_id,
					"flow_key": self.flow_key,
					"include_legacy": int(self.include_unscoped_legacy),
				},
			)
		)
		return {
			"target_id": context.target_id,
			"tally_company": context.tally_company,
			"pending": sum(pending_by_doctype.values()),
			"pending_by_doctype": pending_by_doctype,
			"successful": counts.get("Success", 0),
			"failed": counts.get("Failed", 0),
		}

	def _pending_for_source(self, source, company, target_id, limit, from_date, to_date):
		import frappe

		conditions = [
			f"source.`{source.company_field}` = %(company)s",
			"log.name IS NULL",
		]
		if source.submitted_only:
			conditions.append("source.docstatus = 1")
		values = {
			"company": company,
			"target_id": target_id,
			"flow_key": self.flow_key,
			"include_legacy": int(self.include_unscoped_legacy),
			"limit": limit,
		}
		if from_date:
			conditions.append(f"source.`{source.date_field}` >= %(from_date)s")
			values["from_date"] = from_date
		if to_date:
			conditions.append(f"source.`{source.date_field}` <= %(to_date)s")
			values["to_date"] = to_date
		rows = frappe.db.sql(
			f"""
			SELECT source.`{source.date_field}`, source.name
			FROM `tab{source.doctype}` source
			LEFT JOIN `tabTally Sync Log` log
			  ON log.source_doctype = %(source_doctype)s
			 AND log.source_name = source.name
			 AND log.source_modified = source.modified
			 AND log.target_id = %(target_id)s
			 AND log.status = 'Success'
			 AND (log.flow_key = %(flow_key)s
			      OR (%(include_legacy)s = 1 AND COALESCE(log.flow_key, '') = ''))
			WHERE {' AND '.join(conditions)}
			ORDER BY source.`{source.date_field}`, source.name
			LIMIT %(limit)s
			""",
			{**values, "source_doctype": source.doctype},
		)
		return [(str(row[0]), source.doctype, row[1]) for row in rows]

	def _source(self, source_doctype: str) -> SourceSpec:
		try:
			return self.sources[source_doctype]
		except KeyError as exc:
			raise ValueError(f"Unsupported source DocType: {source_doctype}") from exc
