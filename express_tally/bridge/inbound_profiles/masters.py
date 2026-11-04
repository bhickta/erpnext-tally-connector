"""Inbound Tally master collection profile."""

from collections import defaultdict

from ..collection_gateway import build_collection_export, parse_collection_export, scalar
from ..normalization import master_depth, master_name, normalize_master
from .base import IncrementalTallyProfile
from .constants import MASTER_COLLECTIONS


class TallyMastersProfile(IncrementalTallyProfile):
	key = "tally_masters_v1"

	def collect(self, config, tally_client, limit, options=None):
		raw_by_type = {}
		store = self._checkpoint_store(config)
		target = self._target_key(config)
		for collection_type, name, object_type, fields, tags in MASTER_COLLECTIONS:
			checkpoint = store.get(target, collection_type)
			# The complete Group hierarchy is also used to classify ledgers under
			# Sundry Debtors/Creditors; only its returned records are checkpoint-filtered.
			filters = (f"$AlterID > {checkpoint}",) if checkpoint and collection_type != "group" else ()
			request = build_collection_export(config.tally_company, name, object_type, fields, filters)
			response = tally_client.export_collection(request)
			raw_by_type[collection_type] = parse_collection_export(response, tags)

		groups = {
			master_name(record).casefold(): {
				"name": master_name(record),
				"parent": scalar(record, "parent"),
			}
			for record in raw_by_type["group"]
			if master_name(record)
		}
		records = []
		for priority, (collection_type, *_) in enumerate(MASTER_COLLECTIONS):
			for raw in raw_by_type[collection_type]:
				record = normalize_master(collection_type, raw, groups)
				if record:
					record["_tally_priority"] = priority
					records.append(record)
		parents_by_type = defaultdict(set)
		by_type_and_name = {}
		for record in records:
			by_type_and_name[(record["_tally_type"], record["name"].casefold())] = record
			if record.get("parent"):
				parents_by_type[record["_tally_type"]].add(record["parent"].casefold())
		for record in records:
			record["is_group"] = record["name"].casefold() in parents_by_type[record["_tally_type"]]
			record["_tally_depth"] = master_depth(record, by_type_and_name)
		return self._pending(config, records, limit)
