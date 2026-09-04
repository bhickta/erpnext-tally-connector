import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date
from pathlib import Path


SECRET_MASK = "********"


@dataclass(frozen=True)
class BridgeConfig:
	frappe_url: str = ""
	api_key: str = ""
	api_secret: str = ""
	erpnext_company: str = ""
	target_id: str = ""
	tally_company: str = ""
	flow_name: str = ""
	tally_url: str = "http://127.0.0.1:9000"
	poll_interval_seconds: int = 60
	batch_size: int = 20
	listen_host: str = "127.0.0.1"
	listen_port: int = 8765
	request_timeout_seconds: int = 30
	from_date: str | None = None
	to_date: str | None = None
	voucher_date_override: str | None = None
	agent_profiles: tuple[str, ...] = ()
	enabled_flows: tuple[str, ...] = ()
	auto_sync_enabled: bool = False
	auto_sync_directions: tuple[str, ...] = ("erpnext_to_tally", "tally_to_erpnext")
	open_browser_on_start: bool = True
	start_with_windows: bool = True
	flow_options: dict | None = None
	runtime_directory: str = field(default="", repr=False, compare=False)

	@classmethod
	def load(cls, path, validate=True):
		path = Path(path)
		values = {}
		if path.is_file():
			with path.open(encoding="utf-8") as handle:
				values = json.load(handle)

		env_values = {
			"api_key": os.getenv("ERPNEXT_TALLY_API_KEY") or os.getenv("SRV_TALLY_API_KEY"),
			"api_secret": os.getenv("ERPNEXT_TALLY_API_SECRET") or os.getenv("SRV_TALLY_API_SECRET"),
		}
		values.update({key: value for key, value in env_values.items() if value})
		known_fields = {field.name for field in fields(cls)}
		unknown = sorted(set(values) - known_fields)
		if unknown:
			raise ValueError(f"Unknown bridge configuration: {', '.join(unknown)}")
		for key in ("agent_profiles", "enabled_flows", "auto_sync_directions"):
			if key in values:
				values[key] = tuple(values[key] or ())
		config = cls(**values)
		if validate:
			config.validate()
		return config

	@property
	def selected_flows(self):
		"""Return configured flows while preserving the original single-flow setting."""
		flows = list(self.enabled_flows)
		if self.flow_name and self.flow_name not in flows:
			flows.insert(0, self.flow_name)
		return tuple(flows)

	def public_dict(self):
		values = asdict(self)
		values.pop("runtime_directory", None)
		values["api_secret"] = SECRET_MASK if self.api_secret else ""
		return values

	def storage_dict(self):
		values = asdict(self)
		values.pop("runtime_directory", None)
		for key in ("agent_profiles", "enabled_flows", "auto_sync_directions"):
			values[key] = list(values[key])
		return values

	def validate(self, require_flows=True):
		missing = [
			name
			for name in (
				"frappe_url",
				"api_key",
				"api_secret",
				"erpnext_company",
				"target_id",
				"tally_company",
			)
			if not getattr(self, name)
		]
		if missing:
			raise ValueError(f"Missing bridge configuration: {', '.join(missing)}")
		if require_flows and not self.selected_flows:
			raise ValueError("At least one flow must be selected")
		if not self.frappe_url.lower().startswith(("http://", "https://")):
			raise ValueError("frappe_url must be an HTTP or HTTPS URL")
		if not self.tally_url.lower().startswith("http://"):
			raise ValueError("tally_url must be an HTTP URL")
		if not 1 <= int(self.batch_size) <= 100:
			raise ValueError("batch_size must be between 1 and 100")
		if not 10 <= int(self.poll_interval_seconds) <= 86400:
			raise ValueError("poll_interval_seconds must be between 10 and 86400")
		if not 1 <= int(self.listen_port) <= 65535:
			raise ValueError("listen_port must be between 1 and 65535")
		if not 1 <= int(self.request_timeout_seconds) <= 300:
			raise ValueError("request_timeout_seconds must be between 1 and 300")
		if self.voucher_date_override:
			try:
				date.fromisoformat(self.voucher_date_override)
			except ValueError as exc:
				raise ValueError("voucher_date_override must use YYYY-MM-DD format") from exc
		if not all(isinstance(flow, str) and flow.strip() for flow in self.selected_flows):
			raise ValueError("enabled_flows must contain valid flow names")
		valid_directions = {"erpnext_to_tally", "tally_to_erpnext"}
		if not set(self.auto_sync_directions).issubset(valid_directions):
			raise ValueError("auto_sync_directions contains an unsupported direction")
		if self.auto_sync_enabled and not self.auto_sync_directions:
			raise ValueError("Select at least one automatic sync direction")
		if not isinstance(self.agent_profiles, (list, tuple)) or any(
			not isinstance(path, str) or not path.strip() for path in self.agent_profiles
		):
			raise ValueError("agent_profiles must be an array of dotted import paths")
		if self.flow_options is not None and not isinstance(self.flow_options, dict):
			raise ValueError("flow_options must be an object keyed by flow name")


class ConfigStore:
	"""Thread-safe JSON settings storage used by the local Control Centre."""

	EDITABLE_FIELDS = frozenset(field.name for field in fields(BridgeConfig)) - {
		"listen_host",
		"listen_port",
		"runtime_directory",
	}

	def __init__(self, path):
		self.path = Path(path).resolve()
		self._lock = threading.RLock()

	def load(self, validate=False):
		with self._lock:
			return BridgeConfig.load(self.path, validate=validate)

	def update(self, changes):
		if not isinstance(changes, dict):
			raise ValueError("settings must be a JSON object")
		unexpected = sorted(set(changes) - self.EDITABLE_FIELDS)
		if unexpected:
			raise ValueError(f"Settings cannot be changed here: {', '.join(unexpected)}")
		with self._lock:
			current = self.load(validate=False)
			cleaned = {}
			for key, value in changes.items():
				if key == "api_secret" and value in (None, "", SECRET_MASK):
					continue
				if key in {"agent_profiles", "enabled_flows", "auto_sync_directions"}:
					value = tuple(value or ())
				cleaned[key] = value
			updated = replace(current, **cleaned)
			updated.validate(require_flows=updated.auto_sync_enabled)
			self._write(updated)
			return updated

	def set_auto_sync(self, enabled):
		with self._lock:
			config = self.load(validate=False)
			updated = replace(config, auto_sync_enabled=bool(enabled))
			if enabled:
				updated.validate()
			self._write(updated)
			return updated

	def _write(self, config):
		self.path.parent.mkdir(parents=True, exist_ok=True)
		file_descriptor, temporary_name = tempfile.mkstemp(
			prefix=f".{self.path.name}.",
			dir=self.path.parent,
			text=True,
		)
		try:
			with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
				json.dump(config.storage_dict(), handle, ensure_ascii=False, indent=2)
				handle.write("\n")
			os.replace(temporary_name, self.path)
		except Exception:
			try:
				os.unlink(temporary_name)
			except OSError:
				pass
			raise
