"""Read-only comparison of source ledger balances and ERP posted GL."""

from collections import defaultdict
from decimal import Decimal

from express_tally.bridge.collection_gateway import scalar
from express_tally.bridge.inbound_profiles import tally_balance


def compare_ledger_balances(mapping, source_rows, erp_rows):
	expected = defaultdict(Decimal)
	actual = defaultdict(Decimal)
	names = defaultdict(list)
	unmapped = []
	for row in source_rows:
		name = scalar(row, "name", "_name")
		if name not in mapping:
			unmapped.append(name)
			continue
		value = mapping[name]
		key = (value["account"], value.get("party_type") or "", value.get("party") or "")
		expected[key] += Decimal(str(tally_balance(scalar(row, "closing_balance"))))
		names[key].append(name)
	for row in erp_rows:
		key = (row["account"], row.get("party_type") or "", row.get("party") or "")
		actual[key] += Decimal(str(row["balance"]))
	comparisons = []
	for key in sorted(expected):
		difference = (actual[key] - expected[key]).quantize(Decimal("0.01"))
		comparisons.append(
			{
				"tally_ledgers": sorted(names[key]),
				"account": key[0],
				"party_type": key[1],
				"party": key[2],
				"source_balance": str(expected[key]),
				"erp_balance": str(actual[key]),
				"difference": str(difference),
				"matched": difference == 0,
			}
		)
	unmapped_erp = [
		{"account": key[0], "party_type": key[1], "party": key[2], "balance": str(value)}
		for key, value in sorted(actual.items())
		if key not in expected and value.quantize(Decimal("0.01"))
	]
	return {
		"comparisons": comparisons,
		"unmapped_tally_ledgers": sorted(unmapped),
		"erp_balances_without_source_mapping": unmapped_erp,
		"mapped_targets_matched": not unmapped and all(row["matched"] for row in comparisons),
	}


def compare_financial_posting_balances(mapping, opening_rows, closing_rows, records, erp_rows):
	"""Compare posted financial balances, keeping stock-derived reports explicit."""
	balances = {
		scalar(row, "name", "_name"): Decimal(str(tally_balance(scalar(row, "closing_balance"))))
		for row in opening_rows
	}
	for record in records:
		if (
			record.get("optional")
			or record.get("cancelled")
			or record.get("deleted")
			or record["voucher_type"] in {"Delivery Note", "Receipt Note"}
		):
			continue
		for entry in record.get("ledger_entries") or []:
			balances[entry["ledger"]] = balances.get(entry["ledger"], Decimal(0)) - Decimal(
				str(entry["amount"])
			)
	closing = {
		scalar(row, "name", "_name"): Decimal(str(tally_balance(scalar(row, "closing_balance"))))
		for row in closing_rows
	}
	financial = {name: value for name, value in mapping.items() if value.get("account_type") != "Stock"}
	rows = [
		{"name": name, "closingbalance": str(-balance)}
		for name, balance in balances.items()
		if name in financial
	]
	report = compare_ledger_balances(financial, rows, erp_rows)
	report["scope"] = "Posted accounting ledgers; ERPNext stock remains unchanged"
	report["excluded_stock_ledgers"] = sorted(set(mapping) - set(financial))
	report["stock_derived_report_balances"] = []
	report["source_statement_mismatches"] = []
	report["missing_opening_ledgers"] = sorted(
		set(financial) - {scalar(row, "name", "_name") for row in opening_rows}
	)
	report["missing_closing_ledgers"] = sorted(set(financial) - set(closing))
	report["unmapped_closing_ledgers"] = sorted(set(closing) - set(mapping))
	for name in financial:
		posted = balances.get(name, Decimal(0)).quantize(Decimal("0.01"))
		reported = closing.get(name, Decimal(0)).quantize(Decimal("0.01"))
		if posted != reported:
			item = {
				"ledger": name,
				"posted_balance": str(posted),
				"tally_reported_balance": str(reported),
				"difference": str(reported - posted),
			}
			if name == "Profit & Loss A/c":
				item["reason"] = (
					"Tally adds its stock-derived current profit to the reported balance; no source vouchers post that amount to this ledger."
				)
				report["stock_derived_report_balances"].append(item)
			else:
				report["source_statement_mismatches"].append(item)
	report["source_posted_financial_ledgers"] = len(financial)
	report["matched_financial_targets"] = sum(row["matched"] for row in report["comparisons"])
	report["financial_ledger_balances_match"] = (
		report["mapped_targets_matched"]
		and not report["source_statement_mismatches"]
		and not report["missing_opening_ledgers"]
		and not report["missing_closing_ledgers"]
		and not report["unmapped_closing_ledgers"]
	)
	return report
