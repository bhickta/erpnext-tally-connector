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
		state = self._checkpoint_store(config).state(self._target_key(config))
		ahead = state.get("_accepted_ahead", {})
		pending = []
		for record in records:
			collection_type = record["_tally_type"]
			alter_id = int(record.get("_tally_alter_id") or 0)
			if alter_id > max(
				int(state.get(collection_type, 0) or 0),
				ahead.get(collection_type, {}).get(record["_tally_key"], 0),
			):
				pending.append(record)
		pending.sort(
			key=lambda row: (
				row.get("_tally_priority", 0),
				row.get("_tally_depth", 0),
				row.get("_tally_alter_id", 0),
				row.get("_tally_type", ""),
			)
		)
		selected = pending[:limit]
		self._omitted_floor = {}
		for record in pending[limit:]:
			kind = record["_tally_type"]
			version = int(record["_tally_alter_id"])
			self._omitted_floor[kind] = min(self._omitted_floor.get(kind, version), version)
		return selected

	def acknowledge_collected(self, config, records, results):
		if len(records) != len(results):
			raise ValueError("ERPNext did not return one acknowledgement per Tally record")
		if any("source" in result for result in results):
			by_source = {result.get("source"): result for result in results}
			if len(by_source) != len(records) or set(by_source) != {row["_tally_key"] for row in records}:
				raise ValueError("ERPNext returned unexpected or duplicate Tally record identities")
			results = [by_source[record["_tally_key"]] for record in records]
		accepted = defaultdict(dict)
		unresolved = dict(self._omitted_floor)
		for record, result in zip(records, results, strict=True):
			kind = record["_tally_type"]
			version = int(record["_tally_alter_id"])
			if result.get("source_alter_id", version) != version:
				raise ValueError("ERPNext acknowledged a different Tally record version")
			status = result.get("status", result.get("message", ""))
			if is_success_status(status):
				accepted[kind][record["_tally_key"]] = version
			else:
				unresolved[kind] = min(unresolved.get(kind, version), version)
		if accepted:
			self._checkpoint_store(config).acknowledge(
				self._target_key(config),
				{kind: (versions, unresolved.get(kind)) for kind, versions in accepted.items()},
			)
