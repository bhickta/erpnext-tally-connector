"""Company-scoped Stock Entry feed for the standalone bridge."""

import json

import frappe

from express_tally.bridge.common import stable_digest


def _check_scope(doctype, company, target_id):
	frappe.only_for(["Tally Sync User", "Accounts Manager", "Stock Manager", "System Manager"])
	if doctype != "Stock Entry" or not company or not target_id:
		frappe.throw("Stock Entry, company, and target ID are required for this push feed")


def _cache_key(company, target_id):
	return f"express_tally_stock_push::{stable_digest([company, target_id])}"


def _cursor(value):
	if not value:
		return "", ""
	modified, name = json.loads(value)
	if not isinstance(modified, str) or not isinstance(name, str) or not modified or not name:
		raise ValueError("Invalid Stock Entry cursor")
	return modified, name


@frappe.whitelist()
def get_changed_documents(doctype, company, target_id, limit=20):
	"""Return the next rows ordered by modification time and document name."""
	_check_scope(doctype, company, target_id)
	limit = max(1, min(int(limit or 20), 100))
	modified, name = _cursor(frappe.cache().get_value(_cache_key(company, target_id)))
	filters = {"company": company, "docstatus": 1}

	def select(extra, count):
		return frappe.get_list(
			doctype,
			filters={**filters, **extra},
			fields=["name", "modified"],
			order_by="modified asc, name asc",
			limit_page_length=count,
		)

	if not modified:
		return select({}, limit)
	peers = select({"modified": modified, "name": [">", name]}, limit)
	return peers + select({"modified": [">", modified]}, limit - len(peers)) if len(peers) < limit else peers


@frappe.whitelist(methods=["POST"])
def acknowledge(doctype, company, target_id, cursor):
	"""Advance this target's cursor only for an accessible document in its company."""
	_check_scope(doctype, company, target_id)
	modified, name = _cursor(cursor)
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	if doc.company != company or doc.docstatus != 1 or str(doc.modified) != modified:
		frappe.throw("Stock Entry changed before the push could be acknowledged")
	cache = frappe.cache()
	key = _cache_key(company, target_id)
	if (modified, name) > _cursor(cache.get_value(key)):
		cache.set_value(key, cursor)
	return {"cursor": cache.get_value(key)}


@frappe.whitelist()
def get_document(doctype, company, target_id, name):
	"""Return stock units and warehouses for a submitted Stock Entry."""
	_check_scope(doctype, company, target_id)
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	if doc.company != company or doc.docstatus != 1:
		frappe.throw(f"{doctype} {name} is not submitted for the requested company")
	return {
		"name": doc.name,
		"doctype": doc.doctype,
		"company": doc.company,
		"posting_date": str(doc.posting_date),
		"modified": str(doc.modified),
		"items": [
			{
				"item_code": row.item_code,
				"item_name": row.item_name,
				"qty": row.transfer_qty,
				"uom": row.stock_uom,
				"rate": row.basic_rate,
				"amount": row.basic_amount,
				"source_warehouse": row.s_warehouse,
				"target_warehouse": row.t_warehouse,
			}
			for row in doc.items
		],
	}
