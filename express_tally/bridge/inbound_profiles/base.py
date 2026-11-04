"""Shared incremental collection behavior for inbound Tally profiles."""

from collections import defaultdict

from ..common import is_success_status
from ..profiles import AgentProfile
from .checkpoints import CheckpointStore


class IncrementalTallyProfile(AgentProfile):
	def _target_key(self, config):
		return f"{config.target_id}:{config.tally_company}"

	def _checkpoint_store(self, config):
		return CheckpointStore(config.runtime_directory)

	def _pending(self, config, records, limit):
		store = self._checkpoint_store(config)
		target = self._target_key(config)
		pending = []
		for record in records:
			collection_type = record["_tally_type"]
			alter_id = int(record.get("_tally_alter_id") or 0)
			if alter_id > store.get(target, collection_type):
				pending.append(record)
		pending.sort(
			key=lambda row: (
				row.get("_tally_priority", 0),
				row.get("_tally_depth", 0),
				row.get("_tally_alter_id", 0),
				row.get("_tally_type", ""),
			)
		)
		return pending[:limit]

	def acknowledge_collected(self, config, records, results):
		by_type = defaultdict(list)
		for record, result in zip(records, results, strict=False):
			by_type[record["_tally_type"]].append((record, result))
		store = self._checkpoint_store(config)
		target = self._target_key(config)
		for collection_type, entries in by_type.items():
			checkpoint = store.get(target, collection_type)
			for record, result in sorted(entries, key=lambda entry: entry[0].get("_tally_alter_id", 0)):
				status = result.get("status", result.get("message", ""))
				if not is_success_status(status):
					break
				checkpoint = max(checkpoint, int(record.get("_tally_alter_id") or 0))
			store.advance(target, collection_type, checkpoint)
