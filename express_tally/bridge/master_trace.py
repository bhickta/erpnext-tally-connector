"""Append-only JSONL trace for master sync runs."""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger("erpnext_tally_bridge.master_trace")

MAX_TRACES = 20


class MasterTrace:
	"""Write a bounded, secret-free trail of one master sync run.

	The trace is append-only JSON Lines so a crash or timeout still leaves the
	events written before the failure. It intentionally excludes API keys and
	secrets.
	"""

	def __init__(self, config, selected, options, bridge_version=""):
		self.path = None
		self.trace_id = ""
		runtime = str(getattr(config, "runtime_directory", "") or "").strip()
		if not runtime:
			return
		trace_dir = Path(runtime) / "traces"
		try:
			trace_dir.mkdir(parents=True, exist_ok=True)
			self.trace_id = (
				f"masters-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
				f"{uuid.uuid4().hex[:8]}.jsonl"
			)
			self.path = trace_dir / self.trace_id
			self._prune(trace_dir)
		except OSError as exc:
			LOGGER.warning("Could not create master trace: %s", exc)
			return
		self.write(
			"sync_start",
			selected=list(selected),
			options=options,
			bridge_version=bridge_version,
		)

	@property
	def available(self):
		return self.path is not None

	def write(self, event, **fields):
		if not self.path:
			return
		record = {
			"event": event,
			"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
			**fields,
		}
		try:
			with self.path.open("a", encoding="utf-8") as handle:
				handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
		except OSError as exc:
			LOGGER.warning("Could not write master trace: %s", exc)

	def _prune(self, trace_dir):
		try:
			paths = sorted(
				trace_dir.glob("masters-*.jsonl"),
				key=lambda path: path.stat().st_mtime,
				reverse=True,
			)
			# Leave room for the trace about to be written.
			for old_path in paths[MAX_TRACES - 1 :]:
				old_path.unlink(missing_ok=True)
		except OSError:
			pass
