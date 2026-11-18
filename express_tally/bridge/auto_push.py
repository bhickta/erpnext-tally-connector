"""Pull Delivery Note / Stock Entry changes from ERPNext and push them to Tally.

The ERPNext site is remote and this agent runs behind NAT, so the site cannot
call us. This module polls the site's cursor feed instead: ask for documents
changed since the last cursor, deliver each one, acknowledge, repeat.

Run it beside the bridge:

    python -m express_tally.bridge.auto_push --interval 15
"""

import argparse
import logging
import time
from datetime import date

from .clients import BridgeRequestError, FrappeClient, TallyClient
from .config import BridgeConfig
from .xml_gateway import build_stock_journal_import

LOGGER = logging.getLogger("erpnext_tally_bridge.auto_push")

PUSH_DOCTYPES = ("Delivery Note", "Stock Entry")
PENDING_PATH = "/api/method/express_tally.integrations.auto_push.get_changed_documents"
DOCUMENT_PATH = "/api/method/express_tally.integrations.auto_push.get_document"
ACK_PATH = "/api/method/express_tally.integrations.auto_push.acknowledge"


def _voucher_date(posting_date):
	return str(posting_date or date.today().isoformat()).replace("-", "")


def stock_entry_document(document):
	"""Translate an ERPNext Stock Entry into a Tally Stock Journal."""
	rows = []
	for row in document.get("items") or []:
		source = row.get("source_warehouse") or document.get("from_warehouse") or ""
		target = row.get("target_warehouse") or document.get("to_warehouse") or ""
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
		"transaction_date": _voucher_date(document.get("posting_date")),
		"narration": f"ERPNext {document['doctype']} {document['name']}",
		"items": rows,
	}


def deliver_stock_entry(config, tally, document):
	payload = stock_entry_document(document)
	if not payload["items"]:
		raise BridgeRequestError(f"{document['name']} has no items")
	result = tally.import_xml(
		build_stock_journal_import(payload, config.tally_company, config.target_id),
		require_change=True,
		allow_ignored=True,
	)
	if not result.success:
		raise BridgeRequestError(result.message or "Tally rejected the Stock Journal")
	return result.last_voucher_id or "guid"


def deliver_delivery_note(config, frappe, document):
	"""Delivery Notes are delivered by the registered sales-document flow."""
	raise BridgeRequestError(
		"Delivery Note delivery is handled by srv.sales_documents_to_tally; "
		"use the flow while the dedicated profile is being validated"
	)


def run_once(config, frappe, tally, limit=20):
	"""Process one batch per doctype. Returns a per-doctype summary."""
	summary = {}
	for doctype in PUSH_DOCTYPES:
		pending = frappe.request(
			"GET", PENDING_PATH, {"doctype": doctype, "limit": limit}
		) or []
		delivered = 0
		failed = []
		cursor = ""
		for entry in pending:
			cursor = str(entry["modified"])
			try:
				document = frappe.request(
					"GET", DOCUMENT_PATH, {"doctype": doctype, "name": entry["name"]}
				)
				if doctype == "Stock Entry":
					deliver_stock_entry(config, tally, document)
				else:
					deliver_delivery_note(config, frappe, document)
				delivered += 1
			except Exception as exc:
				failed.append(f"{entry['name']}: {str(exc)[:160]}")
				LOGGER.warning("Push failed for %s %s: %s", doctype, entry["name"], exc)
		if cursor:
			frappe.request("GET", ACK_PATH, {"doctype": doctype, "cursor": cursor})
		summary[doctype] = {"delivered": delivered, "failed": failed}
	return summary


def main():
	parser = argparse.ArgumentParser(description="Push ERPNext documents to Tally")
	parser.add_argument("--interval", type=int, default=15)
	parser.add_argument("--limit", type=int, default=20)
	parser.add_argument("--config", default="")
	args = parser.parse_args()

	config = BridgeConfig.load(args.config) if args.config else BridgeConfig.load()
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
