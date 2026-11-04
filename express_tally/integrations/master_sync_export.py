"""Exhaustive ERPNext -> TallyPrime master sync payloads.

User-selectable master groups are Item, Warehouse, Customer and Supplier.
Item sync automatically includes its dependencies (UOM and Item Group).

Every pushed Tally master stores the ERPNext identity in the Tally alias list
(``LANGUAGENAME.LIST`` / ``NAME.LIST``):

* Item        - Tally name is the ERPNext item name; alias is the item code.
* Warehouse   - Tally name is the warehouse name; alias is the ERPNext ID.
* Customer    - Tally name is the customer name; alias is the ERPNext ID.
* Supplier    - Tally name is the supplier name; alias is the ERPNext ID.

Duplicate display names are handled explicitly. Items append the item code
when their display name is shared. Parties and warehouses either keep the
first record and report the rest, or append the ERPNext ID to every colliding
record, depending on ``duplicate_names``.

Customers and suppliers additionally carry the ERPNext ID in ``LEDGERCODE``.
The payload is returned in dependency order so the bridge can import it in
separate JSONEx requests: units, groups, warehouses, parties, then items.
"""

import frappe
from frappe.utils import cint

from .master_sync.accounts import (
	_account_messages,
	_account_parent,
	_stock_group_message,
	_unit_message,
)
from .master_sync.constants import (
	GST_REGISTRATION_TYPE,
	MASTER_SYNC_LABELS,
	MASTER_SYNC_TYPES,
	PARTY_PARENT,
	TALLY_MASTER_TYPES,
	TALLY_NOT_APPLICABLE,
	TALLY_PRIMARY,
	TALLY_STANDARD_GROUPS,
)
from .master_sync.items import (
	_fetch_child_rows,
	_fetch_items,
	_item_fieldnames,
	_item_messages,
)
from .master_sync.names import (
	_alias_list,
	_duplicate_name_report,
	_has_column,
	_identity_alias,
	_is_warehouse_root,
	_item_tally_names,
	_message,
	_party_tally_names,
	_string_list,
	_text,
	_warehouse_tally_names,
)
from .master_sync.parties import (
	_address_values,
	_fetch_addresses,
	_fetch_contacts,
	_fetch_customer_credit_limits,
	_fetch_parties,
	_godown_message,
	_gst_registration_type,
	_ledger_message,
	_party_fieldnames,
)
from .master_sync.payload import (
	_parse_master_types,
	build_master_sync_payload,
	build_master_sync_summary,
)
from .tally_export import DEFAULT_ACCOUNT_GROUPS, _clean_name

__all__ = [
	"DEFAULT_ACCOUNT_GROUPS",
	"GST_REGISTRATION_TYPE",
	"MASTER_SYNC_LABELS",
	"MASTER_SYNC_TYPES",
	"PARTY_PARENT",
	"TALLY_MASTER_TYPES",
	"TALLY_NOT_APPLICABLE",
	"TALLY_PRIMARY",
	"TALLY_STANDARD_GROUPS",
	"_account_messages",
	"_account_parent",
	"_clean_name",
	"_address_values",
	"_alias_list",
	"_duplicate_name_report",
	"_fetch_addresses",
	"_fetch_child_rows",
	"_fetch_contacts",
	"_fetch_customer_credit_limits",
	"_fetch_items",
	"_fetch_parties",
	"_godown_message",
	"_gst_registration_type",
	"_has_column",
	"_identity_alias",
	"_is_warehouse_root",
	"_item_fieldnames",
	"_item_messages",
	"_item_tally_names",
	"_ledger_message",
	"_message",
	"_parse_master_types",
	"_party_fieldnames",
	"_party_tally_names",
	"_stock_group_message",
	"_string_list",
	"_text",
	"_unit_message",
	"_warehouse_tally_names",
	"build_master_sync_payload",
	"build_master_sync_summary",
	"get_master_sync_payload",
	"get_master_sync_summary",
]


@frappe.whitelist()
def get_master_sync_summary(company):
	frappe.only_for(("Accounts Manager", "System Manager", "Tally Sync User"))
	return build_master_sync_summary(company)


@frappe.whitelist()
def get_master_sync_payload(
	company,
	masters=None,
	include_disabled=0,
	include_opening_balances=0,
	posting_date=None,
	limit=None,
	duplicate_names="skip",
	customer_parent="Sundry Debtors",
	supplier_parent="Sundry Creditors",
):
	frappe.only_for(("Accounts Manager", "System Manager", "Tally Sync User"))
	return build_master_sync_payload(
		company,
		masters,
		include_disabled=cint(include_disabled),
		include_opening_balances=cint(include_opening_balances),
		posting_date=posting_date,
		limit=cint(limit) if limit not in (None, "") else None,
		duplicate_names=duplicate_names,
		customer_parent=customer_parent,
		supplier_parent=supplier_parent,
	)
