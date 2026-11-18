"""Cursor-based push feed for the local Tally agent.

The ERPNext site is remote and the agent runs on the Tally PC behind NAT, so
the site cannot call the agent. The agent polls this feed instead: it asks for
documents changed since its last cursor, delivers them to Tally, then
acknowledges.

Only submitted documents are offered. The cursor lives in the site cache and
is advanced per doctype, so a document is delivered once per modification.
"""

import json

import frappe

DOCTYPE_LABEL = {
	"Delivery Note": "Delivery Note",
	"Stock Entry": "Stock Entry",
}


def _cache_key(doctype):
	return f"express_tally_push_cursor::{doctype}"


def _normalise_doctype(doctype):
	if doctype not in DOCTYPE_LABEL:
		frappe.throw(f"Unsupported doctype for push: {doctype}")
	return doctype


@frappe.whitelist()
def get_cursor(doctype):
	"""Return the stored cursor for a doctype ('' when nothing was pushed)."""
	doctype = _normalise_doctype(doctype)
	return frappe.cache().get_value(_cache_key(doctype)) or ""


@frappe.whitelist()
def get_changed_documents(doctype, since="", limit=20):
	"""Return submitted documents changed after ``since``, oldest first."""
	doctype = _normalise_doctype(doctype)
	limit = max(1, min(int(limit or 20), 100))
	filters = {"docstatus": 1}
	if since:
		filters["modified"] = [">", since]
	docs = frappe.get_all(
		doctype,
		filters=filters,
		fields=["name", "modified"],
		order_by="modified asc",
		limit_page_length=limit,
	)
	return docs


@frappe.whitelist()
def acknowledge(doctype, cursor):
	"""Advance the cursor to ``cursor`` after successful delivery."""
	doctype = _normalise_doctype(doctype)
	if not cursor:
		return {"cursor": get_cursor(doctype)}
	current = frappe.cache().get_value(_cache_key(doctype)) or ""
	if str(cursor) > str(current):
		frappe.cache().set_value(_cache_key(doctype), str(cursor))
	return {"cursor": get_cursor(doctype)}


@frappe.whitelist()
def get_document(doctype, name):
	"""Return the fields the agent needs to build a Tally voucher."""
	doctype = _normalise_doctype(doctype)
	doc = frappe.get_doc(doctype, name)
	if doc.docstatus != 1:
		frappe.throw(f"{doctype} {name} is not submitted")
	fields = {
		"name": doc.name,
		"doctype": doc.doctype,
		"company": doc.get("company"),
		"posting_date": str(doc.get("posting_date") or doc.get("posting_date") or ""),
		"posting_time": str(doc.get("posting_time") or ""),
		"remarks": doc.get("remarks") or "",
		"customer": doc.get("customer"),
		"supplier": doc.get("supplier"),
		"stock_entry_type": doc.get("stock_entry_type"),
		"from_warehouse": doc.get("from_warehouse"),
		"to_warehouse": doc.get("to_warehouse"),
		"modified": str(doc.modified),
	}
	rows = []
	for row in doc.get("items") or []:
		rows.append(
			{
				"item_code": row.get("item_code"),
				"item_name": row.get("item_name"),
				"qty": row.get("qty"),
				"uom": row.get("uom") or row.get("stock_uom"),
				"rate": row.get("rate"),
				"amount": row.get("amount"),
				"warehouse": row.get("warehouse"),
				"source_warehouse": row.get("s_warehouse"),
				"target_warehouse": row.get("t_warehouse"),
				"description": row.get("description"),
			}
		)
	fields["items"] = rows
	return fields
