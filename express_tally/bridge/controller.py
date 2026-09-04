import json
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .clients import FrappeClient, TallyClient
from .config import ConfigStore
from .service import SyncService


LOGGER = logging.getLogger("erpnext_tally_bridge")


def utc_now():
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ControlCentre:
	"""Long-running coordinator shared by the local UI and automatic scheduler."""

	def __init__(self, config_path, history_path=None):
		self.config_store = ConfigStore(config_path)
		config_path = Path(config_path).resolve()
		self.history_path = Path(history_path or config_path.with_name("tally-bridge-history.json"))
		self._state_lock = threading.RLock()
		self._history = deque(self._load_history(), maxlen=100)
		self._sync_thread = None
		self._current = None
		self._last_error = ""
		self._next_auto_sync = None
		self._wake_scheduler = threading.Event()
		self._shutdown = threading.Event()
		self._scheduler_thread = threading.Thread(
			target=self._scheduler_loop,
			name="tally-control-centre-scheduler",
			daemon=True,
		)
		self._scheduler_thread.start()

	def config(self):
		return replace(
			self.config_store.load(validate=False),
			runtime_directory=str(self.config_store.path.parent),
		)

	def update_config(self, changes):
		config = self.config_store.update(changes)
		self._configure_windows_startup(config.start_with_windows)
		with self._state_lock:
			self._next_auto_sync = None
		self._wake_scheduler.set()
		return config.public_dict()

	def _configure_windows_startup(self, enabled):
		if os.name != "nt" or not getattr(sys, "frozen", False):
			return
		app_data = os.getenv("APPDATA")
		if not app_data:
			return
		startup = Path(app_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
		launcher = startup / "Express Tally Control Centre.cmd"
		if not enabled:
			try:
				launcher.unlink()
			except FileNotFoundError:
				pass
			return
		startup.mkdir(parents=True, exist_ok=True)
		executable = str(Path(sys.executable).resolve())
		config_path = str(self.config_store.path)
		launcher.write_text(
			f'@echo off\r\nstart "" "{executable}" --config "{config_path}"\r\n',
			encoding="utf-8",
		)

	def set_auto_sync(self, enabled):
		config = self.config_store.set_auto_sync(enabled)
		with self._state_lock:
			self._next_auto_sync = None
		self._wake_scheduler.set()
		return {"enabled": config.auto_sync_enabled}

	def reset_inbound_checkpoints(self):
		from .inbound_profiles import CheckpointStore

		config = self.config()
		config.validate()
		target = f"{config.target_id}:{config.tally_company}"
		removed = CheckpointStore(config.runtime_directory).reset(target)
		return {"reset": removed, "target": target}

	def make_service(self, require_flows=True):
		config = self.config()
		config.validate(require_flows=require_flows)
		frappe_client = FrappeClient(
			config.frappe_url,
			config.api_key,
			config.api_secret,
			config.request_timeout_seconds,
		)
		tally_client = TallyClient(config.tally_url, config.request_timeout_seconds)
		return SyncService(config, frappe_client, tally_client)

	def flows(self):
		return self.make_service(require_flows=False).discover_flows()

	def health(self):
		result = {
			"ok": False,
			"erpnext": {"ok": False, "error": ""},
			"tally": {"ok": False, "error": ""},
		}
		try:
			service = self.make_service(require_flows=False)
		except Exception as exc:
			result["erpnext"]["error"] = str(exc)
			result["tally"]["error"] = str(exc)
			return result
		try:
			flows = service.discover_flows()
			result["erpnext"] = {"ok": True, "flow_count": len(flows), "error": ""}
		except Exception as exc:
			result["erpnext"]["error"] = str(exc)
		try:
			result["tally"] = {**service.health(), "error": ""}
		except Exception as exc:
			result["tally"]["error"] = str(exc)
		result["ok"] = result["erpnext"]["ok"] and result["tally"].get("ok", False)
		return result

	def state(self):
		config = self.config()
		try:
			config.validate()
			configuration_error = ""
		except Exception as exc:
			configuration_error = str(exc)
		with self._state_lock:
			running = bool(self._sync_thread and self._sync_thread.is_alive())
			return {
				"status": 1,
				"running": running,
				"current": self._current,
				"last_error": self._last_error,
				"last_sync": self._history[-1] if self._history else None,
				"history": list(reversed(list(self._history)[-20:])),
				"auto_sync_enabled": config.auto_sync_enabled,
				"auto_sync_directions": list(config.auto_sync_directions),
				"next_auto_sync": self._next_auto_sync,
				"configuration_error": configuration_error,
			}

	def trigger(self, direction="all", flow_keys=None, limit=None, source="manual"):
		if direction not in {"all", "erpnext_to_tally", "tally_to_erpnext"}:
			raise ValueError("direction must be all, erpnext_to_tally, or tally_to_erpnext")
		if limit is not None and not 1 <= int(limit) <= 100:
			raise ValueError("limit must be between 1 and 100")
		with self._state_lock:
			if self._sync_thread and self._sync_thread.is_alive():
				return False
			self._last_error = ""
			self._current = {
				"direction": direction,
				"flow_keys": list(flow_keys or ()),
				"source": source,
				"started_at": utc_now(),
			}
			self._sync_thread = threading.Thread(
				target=self._run,
				args=(direction, tuple(flow_keys or ()), limit, source),
				name="tally-control-centre-sync",
				daemon=True,
			)
			self._sync_thread.start()
			return True

	def _run(self, direction, flow_keys, limit, source):
		try:
			service = self.make_service()
			flows = service.discover_flows()
			selected = set(flow_keys or service.config.selected_flows)
			matching = [
				flow
				for flow in flows
				if flow.get("key") in selected
				and (direction == "all" or flow.get("direction") == direction)
			]
			if not matching:
				raise ValueError("No selected, available flow matches this sync direction")
			for flow in matching:
				if not flow.get("available"):
					summary = {
						"fetched": 0,
						"succeeded": 0,
						"failed": 0,
						"skipped": 1,
						"error": flow.get("unavailable_reason"),
						"flow": flow.get("key"),
						"direction": flow.get("direction"),
					}
				else:
					summary = service.sync_flow(
						flow.get("key"),
						flow.get("direction"),
						flow.get("agent_profile"),
						limit,
					).to_dict()
				self._record({"at": utc_now(), "source": source, **summary})
		except Exception as exc:
			LOGGER.exception("Control Centre sync failed")
			with self._state_lock:
				self._last_error = str(exc)
			self._record(
				{
					"at": utc_now(),
					"source": source,
					"flow": "",
					"direction": direction,
					"fetched": 0,
					"succeeded": 0,
					"failed": 0,
					"skipped": 1,
					"error": str(exc),
				}
			)
		finally:
			with self._state_lock:
				self._current = None

	def _record(self, entry):
		with self._state_lock:
			self._history.append(entry)
			if entry.get("error"):
				self._last_error = entry["error"]
			self._save_history()

	def _load_history(self):
		try:
			with self.history_path.open(encoding="utf-8") as handle:
				value = json.load(handle)
			return value[-100:] if isinstance(value, list) else []
		except (OSError, ValueError):
			return []

	def _save_history(self):
		self.history_path.parent.mkdir(parents=True, exist_ok=True)
		temporary = self.history_path.with_suffix(f"{self.history_path.suffix}.tmp")
		with temporary.open("w", encoding="utf-8") as handle:
			json.dump(list(self._history), handle, ensure_ascii=False, indent=2)
			handle.write("\n")
		os.replace(temporary, self.history_path)

	def _scheduler_loop(self):
		while not self._shutdown.is_set():
			try:
				config = self.config()
				if not config.auto_sync_enabled:
					with self._state_lock:
						self._next_auto_sync = None
					self._wake_scheduler.wait(5)
					self._wake_scheduler.clear()
					continue
				now = time.monotonic()
				with self._state_lock:
					if self._next_auto_sync is None:
						self._next_auto_sync = utc_now()
						due = now
					else:
						due = getattr(self, "_next_auto_monotonic", now)
				if now >= due:
					for direction in config.auto_sync_directions:
						self.trigger(direction=direction, source="automatic")
						thread = self._sync_thread
						if thread:
							thread.join()
					next_due = time.monotonic() + max(int(config.poll_interval_seconds), 10)
					with self._state_lock:
						self._next_auto_monotonic = next_due
						self._next_auto_sync = datetime.fromtimestamp(
							time.time() + max(int(config.poll_interval_seconds), 10), timezone.utc
						).isoformat(timespec="seconds")
				self._wake_scheduler.wait(1)
				self._wake_scheduler.clear()
			except Exception as exc:
				LOGGER.error("Automatic sync scheduler error: %s", exc)
				with self._state_lock:
					self._last_error = str(exc)
				self._wake_scheduler.wait(5)
				self._wake_scheduler.clear()

	def shutdown(self):
		self._shutdown.set()
		self._wake_scheduler.set()
