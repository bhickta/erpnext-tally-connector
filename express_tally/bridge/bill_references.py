"""Resolve Tally bill identities without guessing between invoices."""

import re
from collections import defaultdict

from express_tally.bridge.collection_gateway import scalar


def dated_bill_key(name, date):
	last = str(name).rsplit("/", 1)[-1]
	if not re.fullmatch(r"\d+", last):
		return ""
	return f"date:{str(date).replace('-', '')}:number:{int(last)}"


def index_bill_sources(records):
	candidates = defaultdict(lambda: defaultdict(dict))
	for record in records:
		if record["voucher_type"] not in {"Sales", "Purchase", "Credit Note", "Debit Note"}:
			continue
		party = record.get("party_ledger")
		if not party:
			continue
		keys = {record.get("voucher_number"), dated_bill_key(record.get("voucher_number"), record["date"])}
		if record["voucher_type"] in {"Sales", "Purchase"}:
			keys.add(record.get("reference"))
		for entry in record.get("ledger_entries") or []:
			if entry["ledger"] != party:
				continue
			for bill in entry.get("bill_allocations") or []:
				if scalar(bill, "bill_type").casefold() == "new ref":
					keys.add(scalar(bill, "name"))
					if scalar(bill, "bill_id"):
						keys.add("id:" + scalar(bill, "bill_id"))
		for key in keys - {None, ""}:
			candidates[party][key][record["_tally_key"]] = {"_tally_key": record["_tally_key"]}
	return {
		party: {key: next(iter(values.values())) for key, values in refs.items() if len(values) == 1}
		for party, refs in candidates.items()
	}
