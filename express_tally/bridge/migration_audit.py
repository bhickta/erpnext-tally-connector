"""Read-only completeness checks for a resumable Tally migration snapshot."""

import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from express_tally.bridge.collection_gateway import scalar


def snapshot_digest(records):
	"""Bind verification evidence to the exact cached source records."""
	return sha256(
		json.dumps(
			sorted(records, key=lambda row: str(row["master_id"])),
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		).encode()
	).hexdigest()


def active_business_vouchers(inventory):
	return [
		row
		for row in inventory
		if scalar(row, "iscancelled") != "Yes"
		and "[ExpressTallyOrigin:" not in scalar(row, "narration")
		and "ETGL-" not in scalar(row, "narration")
	]


def audit_snapshot(root):
	"""Report source coverage and mapping risks without database or network access."""
	inventory = active_business_vouchers(json.loads((root / "tally-voucher-inventory.json").read_text()))
	ids = [scalar(row, "masterid") for row in inventory]
	expected = set(ids)
	issues = []
	duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
	if duplicates or "" in expected:
		issues.append({"kind": "invalid_source_ids", "duplicates": duplicates, "blank_id": "" in expected})
	mapping_path = root / "ledger-mappings.json"
	mapping = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
	if not mapping:
		issues.append({"kind": "missing_ledger_mapping"})
	item_path = root / "item-mappings.json"
	items = json.loads(item_path.read_text()) if item_path.exists() else {}
	if not items:
		issues.append({"kind": "missing_item_mapping"})
	mapping_targets = defaultdict(list)
	for ledger, value in mapping.items():
		mapping_targets[(value.get("account"), value.get("party_type", ""), value.get("party", ""))].append(
			ledger
		)
	shared = [names for names in mapping_targets.values() if len(names) > 1]
	cached = set()
	counts = Counter()
	bill_counts = Counter()
	bill_types = Counter()
	unmapped_ledgers = set()
	unmapped_items = set()
	for path in sorted((root / "voucher-cache").glob("*.json")):
		try:
			record = json.loads(path.read_text())
			identity = str(record.get("master_id") or "")
			if identity != path.stem or identity not in expected or identity in cached:
				issues.append({"kind": "unexpected_cache_identity", "file": path.name, "id": identity})
				continue
			cached.add(identity)
			kind = record.get("voucher_type", "")
			counts[kind] += 1
			entries = record.get("ledger_entries") or []
			balance = sum((Decimal(str(entry["amount"])) for entry in entries), Decimal(0))
			if not record.get("optional") and (
				not entries or not balance.is_finite() or abs(balance) > Decimal("0.005")
			):
				issues.append({"kind": "unbalanced_source", "id": identity, "difference": str(balance)})
			if any(entry.get("bill_allocations") for entry in entries):
				bill_counts[kind] += 1
			for entry in entries:
				bill_types.update(scalar(bill, "bill_type") for bill in entry.get("bill_allocations") or [])
			unmapped_ledgers.update(entry["ledger"] for entry in entries if not mapping.get(entry["ledger"]))
			unmapped_items.update(
				entry["item"] for entry in record.get("inventory_entries", []) if entry["item"] not in items
			)
		except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
			issues.append({"kind": "invalid_cached_voucher", "file": path.name, "error": str(exc)})
	missing = sorted(
		expected - cached, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value)
	)
	if missing:
		issues.append({"kind": "incomplete_export", "count": len(missing), "ids": missing})
	if unmapped_ledgers:
		issues.append({"kind": "unmapped_ledgers", "names": sorted(unmapped_ledgers)})
	if unmapped_items:
		issues.append({"kind": "unmapped_items", "names": sorted(unmapped_items)})
	return {
		"expected_active_vouchers": len(expected),
		"cached_active_vouchers": len(cached),
		"voucher_types": dict(counts),
		"vouchers_with_bill_allocations": dict(bill_counts),
		"bill_allocation_types": dict(bill_types),
		"shared_ledger_targets": shared,
		"ledger_mappings": len(mapping),
		"item_mappings": len(items),
		"source_and_mapping_checks_passed": not issues,
		"issues": issues,
	}
