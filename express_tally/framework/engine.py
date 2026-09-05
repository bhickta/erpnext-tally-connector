"""Direction-neutral orchestration around registered integration flows."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from .contracts import FlowContext, FlowDirection, InboundFlow, OutboundFlow
from .registry import FlowRegistry


MAX_BATCH_SIZE = 100
TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,139}$")


def parse_sequence(value: Any, fieldname: str) -> list[Mapping[str, Any]]:
	if isinstance(value, str):
		value = json.loads(value)
	if not isinstance(value, list) or len(value) > MAX_BATCH_SIZE:
		raise ValueError(f"{fieldname} must be a JSON array containing at most {MAX_BATCH_SIZE} entries")
	if any(not isinstance(item, Mapping) for item in value):
		raise ValueError(f"Every {fieldname} entry must be an object")
	return value


def parse_options(value: Any) -> Mapping[str, Any]:
	if not value:
		return {}
	if isinstance(value, str):
		value = json.loads(value)
	if not isinstance(value, Mapping):
		raise ValueError("options must be a JSON object")
	return value


def make_context(
	company: str,
	target_id: str,
	tally_company: str,
	from_date: str | None = None,
	to_date: str | None = None,
	options: Any = None,
) -> FlowContext:
	company = str(company or "").strip()
	target_id = str(target_id or "").strip()
	tally_company = str(tally_company or "").strip()
	if not company:
		raise ValueError("company is required")
	if not TARGET_ID_PATTERN.fullmatch(target_id):
		raise ValueError("target_id may contain letters, numbers, dot, colon, underscore and hyphen")
	if not tally_company or len(tally_company) > 140:
		raise ValueError("a valid tally_company is required")
	return FlowContext(
		company=company,
		target_id=target_id,
		tally_company=tally_company,
		from_date=from_date or None,
		to_date=to_date or None,
		options=parse_options(options),
	)


class FlowEngine:
	def __init__(self, registry: FlowRegistry):
		self.registry = registry

	def list_flows(self) -> list[dict[str, Any]]:
		return [flow.metadata() for flow in self.registry.all()]

	@staticmethod
	def _with_default_options(flow, context):
		defaults = dict(getattr(flow, "default_options", {}) or {})
		if not defaults:
			return context
		return replace(context, options={**defaults, **dict(context.options)})

	def pull(self, flow_key: str, context: FlowContext, limit: int = 20) -> dict[str, Any]:
		flow = self.registry.get(flow_key)
		if not isinstance(flow, OutboundFlow):
			raise ValueError(f"Tally flow {flow_key} does not support pull")
		context = self._with_default_options(flow, context)
		limit = min(max(int(limit), 1), MAX_BATCH_SIZE)
		flow.authorize("pull")
		documents = list(flow.pull(context, limit))
		if len(documents) > limit:
			raise ValueError(f"Tally flow {flow_key} returned more than the requested limit")
		return {
			"schema_version": flow.schema_version,
			"flow": flow.key,
			"direction": FlowDirection.ERP_NEXT_TO_TALLY.value,
			"agent_profile": flow.agent_profile,
			"target_id": context.target_id,
			"tally_company": context.tally_company,
			"company": context.company,
			"documents": documents,
		}

	def acknowledge(self, flow_key: str, context: FlowContext, results: Any) -> dict[str, Any]:
		flow = self.registry.get(flow_key)
		if not isinstance(flow, OutboundFlow):
			raise ValueError(f"Tally flow {flow_key} does not support acknowledgement")
		context = self._with_default_options(flow, context)
		flow.authorize("acknowledge")
		response = dict(flow.acknowledge(context, parse_sequence(results, "results")))
		return {"flow": flow.key, **response}

	def receive(self, flow_key: str, context: FlowContext, records: Any) -> dict[str, Any]:
		flow = self.registry.get(flow_key)
		if not isinstance(flow, InboundFlow):
			raise ValueError(f"Tally flow {flow_key} does not support receive")
		context = self._with_default_options(flow, context)
		flow.authorize("receive")
		results = list(flow.receive(context, parse_sequence(records, "records")))
		return {
			"schema_version": flow.schema_version,
			"flow": flow.key,
			"direction": FlowDirection.TALLY_TO_ERP_NEXT.value,
			"results": results,
		}

	def status(self, flow_key: str, context: FlowContext) -> dict[str, Any]:
		flow = self.registry.get(flow_key)
		context = self._with_default_options(flow, context)
		flow.authorize("status")
		return {"flow": flow.key, **dict(flow.status(context))}
