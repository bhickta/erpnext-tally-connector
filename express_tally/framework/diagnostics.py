"""Shared, read-only diagnostics for Tally integration flows."""

from typing import Any

from .contracts import FlowContext


def sync_activity(flow_key: str, context: FlowContext, limit: int = 50) -> list[dict[str, Any]]:
	import frappe

	limit = min(max(int(limit), 1), 200)
	return frappe.get_all(
		"Tally Sync Log",
		filters={
			"flow_key": flow_key,
			"target_id": context.target_id,
			"company": context.company,
		},
		fields=[
			"creation",
			"direction",
			"status",
			"operation",
			"source_type",
			"source_reference",
			"source_doctype",
			"source_name",
			"target_reference",
			"error",
		],
		order_by="creation desc",
		limit_page_length=limit,
	)
