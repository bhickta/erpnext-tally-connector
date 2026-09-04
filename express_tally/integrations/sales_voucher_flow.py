"""Reusable ERPNext sales-document flow for inventory Sales vouchers."""

import hashlib
import json
import re

import frappe
from frappe.utils import flt

from express_tally.framework import OutboundFlow, OutboundSyncLog, SourceSpec


DEFAULT_SOURCES = (
	SourceSpec("Sales Order", "transaction_date"),
	SourceSpec("Delivery Note", "posting_date"),
)


class SalesDocumentMapper:
	"""Map standard ERPNext selling documents to the built-in agent contract.

	Company apps may subclass individual policy methods or replace
	``mapper_class`` on their flow without changing transport or Tally code.
	"""

	def map_document(self, document, company_details, target_id):
		abbr = company_details.abbr
		item_codes = {row.item_code for row in document.items if row.item_code}
		item_details = {
			row.name: row
			for row in frappe.get_all(
				"Item",
				filters={"name": ["in", sorted(item_codes)]},
				fields=["name", "item_name", "description", "item_group", "stock_uom", "gst_hsn_code"],
			)
		}
		transaction_date = document.get("transaction_date") or document.get("posting_date")
		delivery_date = document.get("delivery_date") or transaction_date
		items = [
			self.map_item(row, item_details.get(row.item_code) or frappe._dict(), abbr, delivery_date)
			for row in document.items
		]
		previous_reference = self.flow.sync_log.previous_target_reference(
			document.doctype, document.name, target_id
		)
		payload = {
			"source_doctype": document.doctype,
			"name": document.name,
			"modified": str(document.modified),
			"operation": "Alter" if previous_reference else "Create",
			"tally_voucher_id": previous_reference,
			"transaction_date": str(transaction_date),
			"delivery_date": str(delivery_date),
			"customer": self.map_customer(document, company_details),
			"currency": document.currency,
			"reference": self.reference(document),
			"narration": self.narration(document),
			"net_total": flt(document.base_net_total),
			"grand_total": self.grand_total(document),
			"rounding_adjustment": flt(document.base_rounding_adjustment),
			"sales_ledger": self.clean_company_suffix(company_details.default_income_account, abbr)
			or "Sales",
			"round_off_ledger": self.clean_company_suffix(company_details.round_off_account, abbr)
			or "Round Off",
			"taxes": self.map_taxes(document, abbr),
			"items": items,
			"masters": {
				"item_groups": self.item_group_masters({row["item_group"] for row in items}),
				"units": sorted({row["stock_uom"] for row in items if row["stock_uom"]}),
				"warehouses": sorted({row["warehouse"] for row in items if row["warehouse"]}),
			},
		}
		payload["source_hash"] = self.source_hash(payload)
		return payload

	def __init__(self, flow):
		self.flow = flow

	def map_item(self, row, item, company_abbr, default_delivery_date):
		stock_qty = flt(row.stock_qty) or flt(row.qty)
		return {
			"item_code": row.item_code,
			"item_name": item.get("item_name") or row.item_name or row.item_code,
			"description": item.get("description") or row.description or "",
			"item_group": item.get("item_group") or row.item_group or "",
			"hsn_code": item.get("gst_hsn_code") or row.get("gst_hsn_code") or "",
			"stock_uom": item.get("stock_uom") or row.stock_uom or row.uom,
			"stock_qty": stock_qty,
			"rate": flt(row.base_net_amount) / stock_qty if stock_qty else 0,
			"amount": flt(row.base_net_amount),
			"warehouse": self.clean_company_suffix(row.warehouse, company_abbr),
			"delivery_date": str(row.get("delivery_date") or default_delivery_date),
		}

	def map_customer(self, document, company_details):
		return {
			"id": document.customer,
			"name": document.customer_name or document.customer,
			"gstin": document.get("billing_address_gstin") or document.get("tax_id") or "",
			"country": company_details.country or "",
		}

	def map_taxes(self, document, company_abbr):
		return [
			{
				"ledger": self.clean_company_suffix(row.account_head, company_abbr),
				"amount": flt(row.base_tax_amount_after_discount_amount),
			}
			for row in document.taxes
			if row.account_head and flt(row.base_tax_amount_after_discount_amount)
		]

	def reference(self, document):
		linked_orders = [
			row.get("against_sales_order") for row in document.items if row.get("against_sales_order")
		]
		return document.get("po_no") or next(iter(linked_orders), None) or document.name

	def narration(self, document):
		return document.get("custom_remarks") or document.get("terms") or ""

	def grand_total(self, document):
		rounding_adjustment = flt(document.base_rounding_adjustment)
		if rounding_adjustment and not document.get("disable_rounded_total"):
			return flt(document.base_rounded_total)
		return flt(document.base_grand_total)

	def item_group_masters(self, group_names):
		masters = {}
		remaining = list(filter(None, group_names))
		while remaining:
			name = remaining.pop()
			if name == "All Item Groups" or name in masters:
				continue
			parent = frappe.db.get_value("Item Group", name, "parent_item_group") or ""
			masters[name] = {"name": name, "parent": "" if parent == "All Item Groups" else parent}
			if parent and parent != "All Item Groups":
				remaining.append(parent)
		ordered = []
		visited = set()

		def add_with_parent(name):
			if name in visited or name not in masters:
				return
			add_with_parent(masters[name]["parent"])
			visited.add(name)
			ordered.append(masters[name])

		for name in sorted(masters):
			add_with_parent(name)
		return ordered

	@staticmethod
	def clean_company_suffix(value, company_abbr):
		value = (value or "").strip()
		if company_abbr:
			value = re.sub(rf"\s+-\s+{re.escape(company_abbr)}$", "", value).strip()
		return value

	@staticmethod
	def source_hash(payload):
		content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
		return hashlib.sha256(content.encode()).hexdigest()


class SalesDocumentsToTallyFlow(OutboundFlow):
	"""Configurable flow using standard submitted ERPNext selling documents."""

	agent_profile = "inventory_sales_voucher_v1"
	source_specs = DEFAULT_SOURCES
	mapper_class = SalesDocumentMapper
	include_unscoped_legacy = False

	def __init__(self):
		self.sync_log = OutboundSyncLog(
			self.key,
			self.source_specs,
			include_unscoped_legacy=self.include_unscoped_legacy,
		)
		self.mapper = self.mapper_class(self)

	def pull(self, context, limit):
		return self.pull_sources(context, limit)

	def pull_sources(self, context, limit, source_doctypes=None):
		if not frappe.db.exists("Company", context.company):
			raise ValueError(f"Company {context.company} does not exist")
		company_details = frappe.db.get_value(
			"Company",
			context.company,
			["abbr", "country", "default_income_account", "round_off_account"],
			as_dict=True,
		)
		refs = self.sync_log.pending_references(
			context.company,
			context.target_id,
			limit,
			from_date=context.from_date,
			to_date=context.to_date,
			source_doctypes=source_doctypes,
		)
		return [
			self.mapper.map_document(frappe.get_doc(doctype, name), company_details, context.target_id)
			for _, doctype, name in refs
		]

	def acknowledge(self, context, results):
		return self.sync_log.acknowledge(context, results)

	def status(self, context):
		return self.sync_log.status(context)
