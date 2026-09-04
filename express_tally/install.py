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


def after_install():
	ensure_tally_sync_role()


def before_migrate():
	ensure_tally_sync_role()


def after_migrate():
	ensure_tally_sync_role()
