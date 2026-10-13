"""Installation helpers owned by the integration framework."""

import frappe


def ensure_tally_sync_role():
	"""Create the narrow role intended for unattended Tally agents."""
	if not frappe.db.exists("Role", "Tally Sync User"):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": "Tally Sync User",
				"desk_access": 0,
			}
		).insert(ignore_permissions=True)
	# ERPNext payment validation reads party and bank balances even when the
	# authorized integration inserts the document with ignore_permissions.
	from frappe.permissions import add_permission, update_permission_property

	for doctype in ("Customer", "Supplier", "Account", "Payment Entry", "Sales Invoice", "Purchase Invoice"):
		if not frappe.db.exists("DocType", doctype):
			continue
		filters = {"parent": doctype, "role": "Tally Sync User", "permlevel": 0, "if_owner": 0}
		permission = frappe.db.get_value("Custom DocPerm", filters, ["name", "read"], as_dict=True)
		if not permission:
			add_permission(doctype, "Tally Sync User", ptype="read")
		elif not permission.read:
			update_permission_property(doctype, "Tally Sync User", 0, "read", 1)
		frappe.clear_cache(doctype=doctype)


def after_install():
	ensure_tally_sync_role()


def before_migrate():
	ensure_tally_sync_role()


def after_migrate():
	ensure_tally_sync_role()
