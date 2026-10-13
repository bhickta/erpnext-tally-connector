#!/usr/bin/env python3
"""Check that the live Tally voucher inventory still matches the saved source."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from express_tally.bridge.clients import TallyClient
from express_tally.bridge.collection_gateway import build_collection_export, parse_collection_export, scalar
from express_tally.bridge.migration_audit import active_business_vouchers, snapshot_digest


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", required=True, type=Path)
	args = parser.parse_args()
	root = args.root.resolve()
	cfg = json.loads((root / "bridge-config-private.json").read_text())["config"]
	computed = {"ETPeriodFrom": "$$String:##SVFromDate", "ETPeriodTo": "$$String:##SVToDate"}
	request = build_collection_export(
		cfg["tally_company"],
		"ETMigrationFinalInventory",
		"Voucher",
		["MasterID", "AlterID", "GUID", "Date", "VoucherTypeName", "IsCancelled", "Narration", *computed],
		static_variables={"SVFROMDATE": "20250401", "SVTODATE": "20270331"},
		computed_fields=computed,
	)
	rows = parse_collection_export(
		TallyClient("http://172.17.0.1:19000", 120).export_collection(request), ["VOUCHER"]
	)
	rows = [row for row in rows if scalar(row, "masterid")]
	if not rows or any(
		scalar(row, "etperiodfrom") != "1-Apr-25" or scalar(row, "etperiodto") != "31-Mar-27" for row in rows
	):
		raise ValueError("Live Tally inventory returned the wrong period")
	live = {scalar(row, "masterid"): scalar(row, "alterid") for row in active_business_vouchers(rows)}
	records = [json.loads(path.read_text()) for path in (root / "voucher-cache").glob("*.json")]
	cached = {str(record["master_id"]): str(record["_tally_alter_id"]) for record in records}
	report = {
		"source_snapshot_sha256": snapshot_digest(records),
		"checked_at": datetime.now(timezone.utc).isoformat(),
		"live_active_vouchers": len(live),
		"cached_active_vouchers": len(cached),
		"new_source_ids": sorted(set(live) - set(cached)),
		"removed_source_ids": sorted(set(cached) - set(live)),
		"changed_source_ids": sorted(key for key in live.keys() & cached.keys() if live[key] != cached[key]),
	}
	report["source_snapshot_current"] = not any(
		report[key] for key in ("new_source_ids", "removed_source_ids", "changed_source_ids")
	)
	(root / "final-live-inventory.json").write_text(json.dumps(rows, indent=2))
	(root / "final-source-verification.json").write_text(json.dumps(report, indent=2))
	print(json.dumps(report, indent=2))
	if not report["source_snapshot_current"]:
		raise SystemExit(1)


if __name__ == "__main__":
	main()
