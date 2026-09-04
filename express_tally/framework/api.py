"""Versioned Frappe API shared by all registered Tally integration flows."""

import frappe

from .engine import FlowEngine, make_context
from .registry import get_registry


def _engine() -> FlowEngine:
	return FlowEngine(get_registry())


def _context(company, target_id, tally_company, from_date=None, to_date=None, options=None):
	return make_context(company, target_id, tally_company, from_date, to_date, options)


@frappe.whitelist()
def get_flows():
	if frappe.session.user == "Guest":
		frappe.throw("Authentication is required", frappe.PermissionError)
	return {"schema_version": 1, "flows": _engine().list_flows()}


@frappe.whitelist()
def pull(flow, company, target_id, tally_company, limit=20, from_date=None, to_date=None, options=None):
	return _engine().pull(
		flow,
		_context(company, target_id, tally_company, from_date, to_date, options),
		limit,
	)


@frappe.whitelist(methods=["POST"])
def acknowledge(flow, company, target_id, tally_company, results, options=None):
	return _engine().acknowledge(
		flow,
		_context(company, target_id, tally_company, options=options),
		results,
	)


@frappe.whitelist(methods=["POST"])
def receive(flow, company, target_id, tally_company, records, options=None):
	return _engine().receive(
		flow,
		_context(company, target_id, tally_company, options=options),
		records,
	)


@frappe.whitelist()
def get_status(flow, company, target_id, tally_company, options=None):
	return _engine().status(
		flow,
		_context(company, target_id, tally_company, options=options),
	)
