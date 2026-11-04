"""Atomic JSON checkpoint handling for incremental inbound profiles."""

import json
import os
import threading
from pathlib import Path


class CheckpointStore:
	def __init__(self, directory):
		directory = Path(directory or ".").resolve()
		self.path = directory / "tally-inbound-checkpoints.json"
		self._lock = threading.RLock()

	def get(self, target, collection_type):
		with self._lock:
			return int(self._read().get(target, {}).get(collection_type, 0) or 0)

	def voucher_ids(self, target):
		with self._lock:
			return self._read().get(target, {}).get("_ledger_voucher_ids", [])

	def remember_vouchers(self, target, ids):
		with self._lock:
			values = self._read()
			entry = values.setdefault(target, {})
			entry["_ledger_voucher_ids"] = sorted(set(entry.get("_ledger_voucher_ids", [])) | set(ids))
			self.path.parent.mkdir(parents=True, exist_ok=True)
			temporary = self.path.with_suffix(".tmp")
			with temporary.open("w", encoding="utf-8") as handle:
				json.dump(values, handle, ensure_ascii=False, indent=2)
			os.replace(temporary, self.path)

	def advance(self, target, collection_type, alter_id):
		with self._lock:
			values = self._read()
			values.setdefault(target, {})[collection_type] = max(
				int(values.get(target, {}).get(collection_type, 0) or 0),
				int(alter_id or 0),
			)
			self.path.parent.mkdir(parents=True, exist_ok=True)
			temporary = self.path.with_suffix(".tmp")
			with temporary.open("w", encoding="utf-8") as handle:
				json.dump(values, handle, ensure_ascii=False, indent=2)
				handle.write("\n")
			os.replace(temporary, self.path)

	def reset(self, target):
		with self._lock:
			values = self._read()
			removed = values.pop(target, None) is not None
			if removed:
				self.path.parent.mkdir(parents=True, exist_ok=True)
				temporary = self.path.with_suffix(".tmp")
				with temporary.open("w", encoding="utf-8") as handle:
					json.dump(values, handle, ensure_ascii=False, indent=2)
					handle.write("\n")
				os.replace(temporary, self.path)
			return removed

	def _read(self):
		try:
			with self.path.open(encoding="utf-8") as handle:
				value = json.load(handle)
			return value if isinstance(value, dict) else {}
		except (OSError, ValueError):
			return {}
