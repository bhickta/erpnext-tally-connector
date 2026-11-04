"""Tally Unit, Group and Ledger message builders."""

from frappe.utils import cint

from ...bridge.common import clean_company_suffix as _clean_name
from ..tally_export import DEFAULT_ACCOUNT_GROUPS
from .constants import TALLY_PRIMARY, TALLY_STANDARD_GROUPS
from .names import _alias_list, _identity_alias, _message


def _unit_message(row):
	return _message(
		"Unit",
		row["name"],
		issimpleunit=True,
		originalname=row.get("uom_name") or row["name"],
		decimalplaces="0" if cint(row.get("must_be_whole_number")) else "3",
	)


def _stock_group_message(row):
	name = row["name"]
	parent = row.get("parent_item_group") or ""
	return _message(
		"Stock Group",
		name,
		parent=TALLY_PRIMARY if parent in ("", "All Item Groups") else parent,
		shouldquantitiesbeadded=True,
		**{
			"language name": _alias_list(
				name,
				_identity_alias(name, row.get("item_group_name") or name, "Item Group"),
			)
		},
	)


def _account_parent(account, by_name, company_abbr):
	parent = by_name.get(account.get("parent_account"))
	if parent and parent.get("parent_account"):
		return _clean_name(parent.get("account_name") or parent.get("name"), company_abbr)
	return DEFAULT_ACCOUNT_GROUPS.get(account.get("account_type")) or {
		"Asset": "Current Assets",
		"Liability": "Current Liabilities",
		"Equity": "Capital Account",
		"Income": "Indirect Incomes",
		"Expense": "Indirect Expenses",
	}.get(account.get("root_type"), "Current Assets")


def _account_messages(accounts, company_abbr, include_disabled=False, company_currency=None):
	"""Return Tally Group and Ledger masters for the ERPNext chart of accounts."""
	by_name = {row["name"]: row for row in accounts}
	groups = []
	ledgers = []
	for account in accounts:
		if not account.get("parent_account"):
			continue
		if account.get("disabled") and not include_disabled:
			continue
		name = _clean_name(account.get("account_name") or account["name"], company_abbr)
		parent = _account_parent(account, by_name, company_abbr)
		aliases = _alias_list(
			name, _identity_alias(name, account["name"], "Account")
		)
		if cint(account.get("is_group")):
			if name in TALLY_STANDARD_GROUPS:
				# Use Tally's predefined group as the parent for its children.
				continue
			message = _message(
				"Group",
				name,
				parent=parent,
				isbillwiseon=False,
				iscostcentreson=False,
				isaddable=True,
				isrevenue=account.get("root_type") == "Income",
				affectsstock=False,
				**{"language name": aliases},
			)
			if account.get("disabled"):
				message["isdeleted"] = True
			groups.append(message)
			continue
		message = _message(
			"Ledger",
			name,
			parent=parent,
			isbillwiseon=account.get("account_type") in ("Receivable", "Payable"),
			iscostcentreson=False,
			affectsstock=False,
			isgstapplicable=False,
			ledgercode=account.get("account_number") or account["name"],
			**{"language name": aliases},
		)
		currency = account.get("account_currency")
		if currency and company_currency and currency != company_currency:
			message["currency"] = currency
		if account.get("disabled"):
			message["isdeleted"] = True
		ledgers.append(message)
	return groups, ledgers
