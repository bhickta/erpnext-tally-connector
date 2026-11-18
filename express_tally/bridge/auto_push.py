"""Pull Stock Entry changes from ERPNext and push them to Tally.

The ERPNext site is remote and this agent runs behind NAT, so the site cannot
call us. This module polls the site's cursor feed instead: ask for documents
changed since the last cursor, deliver each one, acknowledge, repeat.

Run it beside the bridge:

    python -m express_tally.bridge.auto_push --interval 15
"""

import argparse
import json
import logging
import time
from decimal import Decimal

from .clients import BridgeRequestError, FrappeClient, TallyClient
from .common import stable_digest
from .config import BridgeConfig
from .voucher_readback import read_voucher, verify_stock_journal
from .xml_gateway import build_stock_journal_import, current_company_request, voucher_remote_id

LOGGER = logging.getLogger("erpnext_tally_bridge.auto_push")

PUSH_DOCTYPE = "Stock Entry"
PENDING_PATH = "/api/method/express_tally.integrations.auto_push.get_changed_documents"
DOCUMENT_PATH = "/api/method/express_tally.integrations.auto_push.get_document"
ACK_PATH = "/api/method/express_tally.integrations.auto_push.acknowledge"


def stock_entry_document(document):
	"""Translate an ERPNext Stock Entry into a Tally Stock Journal."""
	rows = []
	for row in document.get("items") or []:
		source = row.get("source_warehouse") or ""
		target = row.get("target_warehouse") or ""
		rows.append(
			{
				"item": row.get("item_name") or row.get("item_code"),
				"quantity": row.get("qty") or 0,
				"rate": row.get("rate") or 0,
				"amount": row.get("amount") or 0,
				"uom": row.get("uom") or "",
				"source_godown": source,
				"target_godown": target,
			}
		)
	return {
		"name": document["name"],
		"source_doctype": document["doctype"],
		"source_name": document["name"],
		"voucher_type": "Stock Journal",
		"operation": "Create",
		"transaction_date": document["posting_date"],
		"narration": f"ERPNext {document['doctype']} {document['name']}",
		"items": rows,
	}


def deliver_stock_entry(config, tally, document):
	payload = stock_entry_document(document)
	movements = (
		("source_godown", "InventoryEntriesOut.LIST"),
		("target_godown", "InventoryEntriesIn.LIST"),
	)
	legs = {tag: [] for _, tag in movements}
	for row in payload["items"]:
		quantity = Decimal(str(row["quantity"]))
		if not quantity.is_finite() or quantity <= 0 or not row["item"] or not row["uom"]:
			raise BridgeRequestError(f"{document['name']} has a stock row without a valid item, quantity or unit")
		if not row["source_godown"] and not row["target_godown"]:
			raise BridgeRequestError(f"{document['name']} has a stock row without a warehouse")
		for field, tag in movements:
			if row[field]:
				legs[tag].append({
					"item_code": row["item"], "stock_qty": quantity, "stock_uom": row["uom"],
					"warehouse": row[field],
				})
	if not any(legs.values()):
		raise BridgeRequestError(f"{document['name']} has no stock movements")
	marker = f"[ExpressTallyVersion:{stable_digest(payload)}]"
	payload["narration"] += f"\n{marker}"
	reference = f"guid:{voucher_remote_id(payload, config.target_id)}"

	def read_saved():
		return read_voucher(tally, config.tally_company, reference)

	existing = read_saved()
	if existing:
		return verify_stock_journal(existing, payload, marker, legs)
	result = tally.import_xml(
		build_stock_journal_import(payload, config.tally_company, config.target_id),
		require_change=True,
		allow_ignored=True,
	)
	if not result.success:
		raise BridgeRequestError(result.message or "Tally rejected the Stock Journal")
	return verify_stock_journal(read_saved(), payload, marker, legs)


def run_once(config, frappe, tally, limit=20):
	"""Acknowledge only the contiguous group of verified Stock Entries."""
	if tally.get_current_company(current_company_request()) != config.tally_company:
		raise BridgeRequestError(f"Load the configured Tally company '{config.tally_company}' first")
	scope = {"doctype": PUSH_DOCTYPE, "company": config.erpnext_company, "target_id": config.target_id}
	pending = frappe.request("GET", PENDING_PATH, {**scope, "limit": limit}) or []
	delivered = 0
	failed = []
	cursor = ""
	for entry in pending:
		try:
			document = frappe.request("GET", DOCUMENT_PATH, {**scope, "name": entry["name"]})
			if document["modified"] != entry["modified"] or document["company"] != config.erpnext_company:
				raise BridgeRequestError("Stock Entry changed after it was selected for delivery")
			deliver_stock_entry(config, tally, document)
		except Exception as exc:
			failed.append(f"{entry['name']}: {exc}")
			LOGGER.warning("Push failed for Stock Entry %s: %s", entry["name"], exc)
			break
		delivered += 1
		cursor = json.dumps([str(entry["modified"]), entry["name"]])
	if cursor:
		frappe.request("POST", ACK_PATH, {**scope, "cursor": cursor})
	return {PUSH_DOCTYPE: {"delivered": delivered, "failed": failed}}


def main():
	parser = argparse.ArgumentParser(description="Push ERPNext documents to Tally")
	parser.add_argument("--interval", type=int, default=15)
	parser.add_argument("--limit", type=int, default=20)
	parser.add_argument("--config", default="")
	args = parser.parse_args()

	config = BridgeConfig.load(args.config or "tally-bridge.json", validate=False)
	config.validate(require_flows=False)
	frappe = FrappeClient(
		config.frappe_url,
		config.api_key,
		config.api_secret,
		timeout=config.request_timeout_seconds,
		ca_bundle=config.ca_bundle or None,
		verify_tls=config.verify_tls,
	)
	tally = TallyClient(url=config.tally_url, timeout=config.request_timeout_seconds)

	LOGGER.info("Auto push started: interval=%ss limit=%s", args.interval, args.limit)
	while True:
		try:
			summary = run_once(config, frappe, tally, limit=args.limit)
			LOGGER.info("Auto push tick: %s", summary)
		except Exception as exc:
			LOGGER.warning("Auto push tick failed: %s", exc)
		time.sleep(args.interval)


if __name__ == "__main__":
	main()
