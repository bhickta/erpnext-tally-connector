"""Safe summaries for records previewed in the local Control Centre."""


def summarize_records(records):
	return [summarize_record(record) for record in records]


def summarize_record(record):
	ledgers = [
		entry.get("ledger")
		for entry in record.get("ledger_entries", ())
		if entry.get("ledger")
	]
	customer = record.get("customer") or {}
	return {
		"source": record.get("name") or record.get("_tally_key") or "",
		"kind": record.get("kind") or record.get("source_doctype") or "",
		"operation": record.get("operation") or "Import",
		"date": record.get("transaction_date") or record.get("date") or record.get("as_of_date") or "",
		"party": customer.get("name") or record.get("party_ledger") or record.get("ledger") or "",
		"amount": record.get("grand_total") if "grand_total" in record else record.get("balance", ""),
		"ledgers": list(dict.fromkeys(ledgers)),
	}
