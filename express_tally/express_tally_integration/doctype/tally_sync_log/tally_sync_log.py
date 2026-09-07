from frappe.model.document import Document


class TallySyncLog(Document):
	pass


def on_doctype_update():
	import frappe

	frappe.db.add_index(
		"Tally Sync Log", ["flow_key", "direction", "target_id", "status"], "tally_flow_status"
	)
	frappe.db.add_index(
		"Tally Sync Log",
		["source_doctype", "source_name", "source_modified", "target_id", "status"],
		"tally_source_version",
	)
