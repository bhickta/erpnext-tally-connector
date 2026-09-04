"""Stable contracts implemented by company-specific Tally flows."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class FlowDirection(str, Enum):
	ERP_NEXT_TO_TALLY = "erpnext_to_tally"
	TALLY_TO_ERP_NEXT = "tally_to_erpnext"


@dataclass(frozen=True)
class FlowContext:
	"""Connection-neutral context passed to every flow operation."""

	company: str
	target_id: str
	tally_company: str
	from_date: str | None = None
	to_date: str | None = None
	options: Mapping[str, Any] = field(default_factory=dict)


class IntegrationFlow(ABC):
	"""Base metadata and authorization contract shared by both directions."""

	key = ""
	title = ""
	direction: FlowDirection
	schema_version = 1
	agent_profile = ""
	allowed_roles = frozenset({"System Manager"})

	def authorize(self, operation: str) -> None:
		"""Authorize the current Frappe user before touching integration data."""
		import frappe
		from frappe import _

		if frappe.session.user == "Guest" or not self.allowed_roles.intersection(frappe.get_roles()):
			frappe.throw(
				_("You are not permitted to run Tally flow {0}").format(frappe.bold(self.key)),
				frappe.PermissionError,
			)

	def metadata(self) -> dict[str, Any]:
		metadata = {
			"key": self.key,
			"title": self.title or self.key,
			"direction": self.direction.value,
			"schema_version": self.schema_version,
		}
		if self.agent_profile:
			metadata["agent_profile"] = self.agent_profile
		return metadata

	def status(self, context: FlowContext) -> Mapping[str, Any]:
		return {}


class OutboundFlow(IntegrationFlow):
	"""A pull-and-acknowledge flow whose source of truth is ERPNext."""

	direction = FlowDirection.ERP_NEXT_TO_TALLY

	@abstractmethod
	def pull(self, context: FlowContext, limit: int) -> Sequence[Mapping[str, Any]]:
		"""Return source records mapped to a versioned Tally-facing contract."""

	@abstractmethod
	def acknowledge(
		self,
		context: FlowContext,
		results: Sequence[Mapping[str, Any]],
	) -> Mapping[str, Any]:
		"""Persist destination results idempotently after Tally responds."""


class InboundFlow(IntegrationFlow):
	"""A receive flow whose source records originate in Tally."""

	direction = FlowDirection.TALLY_TO_ERP_NEXT

	@abstractmethod
	def receive(
		self,
		context: FlowContext,
		records: Sequence[Mapping[str, Any]],
	) -> Sequence[Mapping[str, Any]]:
		"""Validate, map, and apply Tally records to ERPNext."""
