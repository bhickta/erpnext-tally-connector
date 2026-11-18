"""Atomic JSON checkpoint handling for incremental inbound profiles."""

import json
import os
import re
import threading
from pathlib import Path


class CheckpointStore:
	_lock = threading.RLock()

	def __init__(self, directory):
		directory = Path(directory or ".").resolve()
		self.path = directory / "tally-inbound-checkpoints.json"

	def state(self, target):
		with self._lock:
			return self._read().get(target, {})

	def get(self, target, collection_type):
		return int(self.state(target).get(collection_type, 0) or 0)

	def acknowledge(self, target, updates):
		"""Persist complete versions above gaps and advance past contiguous successes."""
		with self._lock:
			values = self._read()
			entry = values.setdefault(target, {})
			ahead = entry.setdefault("_accepted_ahead", {})
			for collection_type, (accepted, unresolved) in updates.items():
				if not accepted:
					continue
				current = int(entry.get(collection_type, 0) or 0)
				completed = ahead.get(collection_type, {})
				for key, version in accepted.items():
					completed[key] = max(version, completed.get(key, 0))
				highest = max(completed.values())
				if unresolved is not None:
					highest = min(highest, unresolved - 1)
				checkpoint = max(current, highest)
				entry[collection_type] = checkpoint
				ahead[collection_type] = {
					key: version for key, version in completed.items() if version > checkpoint
				}
			self._write(values)

	def voucher_ids(self, target):
		with self._lock:
			return self._read().get(target, {}).get("_ledger_voucher_ids", [])

	def remember_vouchers(self, target, ids):
		with self._lock:
			values = self._read()
			entry = values.setdefault(target, {})
			entry["_ledger_voucher_ids"] = sorted(set(entry.get("_ledger_voucher_ids", [])) | set(ids))
			self._write(values)

	def advance(self, target, collection_type, alter_id):
		with self._lock:
			values = self._read()
			values.setdefault(target, {})[collection_type] = max(
				int(values.get(target, {}).get(collection_type, 0) or 0),
				int(alter_id or 0),
			)
			self._write(values)

	def reset(self, target, *, include_scopes=False):
		with self._lock:
			values = self._read()
			keys = [
				key for key in values
				if key == target or (
					include_scopes
					and key.startswith(f"{target}:")
					and re.fullmatch(r":(?:payments|native)(?::[a-f0-9]{16})?|:(?:ledgers:)?[a-f0-9]{16}", key[len(target):])
				)
			]
			for key in keys:
				del values[key]
			if keys:
				self._write(values)
			return bool(keys)

	def _write(self, values):
		self.path.parent.mkdir(parents=True, exist_ok=True)
		temporary = self.path.with_suffix(".tmp")
		with temporary.open("w", encoding="utf-8") as handle:
			json.dump(values, handle, ensure_ascii=False, indent=2)
			handle.write("\n")
		os.replace(temporary, self.path)

	def _read(self):
		try:
			with self.path.open(encoding="utf-8") as handle:
				value = json.load(handle)
		except FileNotFoundError:
			return {}
		if not isinstance(value, dict):
			raise ValueError("The Tally inbound checkpoint file must contain an object")
		return value
