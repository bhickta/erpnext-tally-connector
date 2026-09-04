"""Discovery of Tally flows contributed through Frappe hooks."""

import importlib
import inspect
import re
from collections.abc import Iterable
from typing import Any, Callable

from .contracts import FlowDirection, IntegrationFlow


FLOW_HOOK = "tally_integration_flows"
FLOW_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{2,139}$")


class FlowRegistry:
	def __init__(self, entries: Iterable[Any] = (), loader: Callable[[str], Any] | None = None):
		self._loader = loader or _load_object
		self._flows: dict[str, IntegrationFlow] = {}
		for entry in entries:
			self.register(entry)

	def register(self, entry: Any) -> IntegrationFlow:
		flow = self._instantiate(entry)
		if not isinstance(flow, IntegrationFlow):
			raise TypeError(f"Tally flow {entry!r} must implement IntegrationFlow")
		if not FLOW_KEY_PATTERN.fullmatch(flow.key or ""):
			raise ValueError(f"Invalid Tally flow key: {flow.key!r}")
		if not isinstance(flow.direction, FlowDirection):
			raise TypeError(f"Tally flow {flow.key!r} has an invalid direction")
		if not isinstance(flow.schema_version, int) or flow.schema_version < 1:
			raise ValueError(f"Tally flow {flow.key!r} has an invalid schema version")
		if flow.key in self._flows:
			raise ValueError(f"Duplicate Tally flow key: {flow.key}")
		self._flows[flow.key] = flow
		return flow

	def get(self, key: str) -> IntegrationFlow:
		try:
			return self._flows[key]
		except KeyError as exc:
			raise KeyError(f"Unknown Tally flow: {key}") from exc

	def all(self) -> tuple[IntegrationFlow, ...]:
		return tuple(self._flows[key] for key in sorted(self._flows))

	def _instantiate(self, entry: Any) -> Any:
		resolved = self._loader(entry) if isinstance(entry, str) else entry
		if inspect.isclass(resolved):
			return resolved()
		if callable(resolved) and not isinstance(resolved, IntegrationFlow):
			return resolved()
		return resolved


def _load_object(path: str) -> Any:
	module_name, separator, attribute = path.rpartition(".")
	if not separator:
		raise ValueError(f"Tally flow path must be a dotted import path: {path}")
	return getattr(importlib.import_module(module_name), attribute)


def get_registry() -> FlowRegistry:
	"""Build a registry from all installed apps on every request.

	Avoiding a process-level cache keeps hooks predictable during bench migrate and
	development reloads. Flow construction should therefore remain inexpensive.
	"""
	import frappe

	entries = frappe.get_hooks(FLOW_HOOK) or []
	if isinstance(entries, dict):
		entries = entries.values()
	return FlowRegistry(entries)
