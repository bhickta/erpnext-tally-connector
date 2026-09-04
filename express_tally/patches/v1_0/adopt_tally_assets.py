"""Adopt shared metadata that was originally shipped by company apps."""

import frappe


def execute():
	module = "Express Tally Integration"
	if frappe.db.exists("DocType", "Tally Sync Log"):
		frappe.db.set_value("DocType", "Tally Sync Log", "module", module, update_modified=False)
	if frappe.db.exists("Page", "tally-export"):
		frappe.db.set_value("Page", "tally-export", "module", module, update_modified=False)
	if frappe.db.exists("Workspace", "Tally Integration"):
		frappe.db.set_value(
			"Workspace",
			"Tally Integration",
			{"module": module, "app": "express_tally"},
			update_modified=False,
		)
