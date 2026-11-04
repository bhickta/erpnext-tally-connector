"""Tally Customer, Supplier, address, contact, credit-limit and Godown builders."""

from datetime import date

import frappe
from frappe.utils import flt

from ...bridge.common import clean_company_suffix as _clean_name
from .constants import GST_REGISTRATION_TYPE, PARTY_PARENT, TALLY_PRIMARY
from .names import (
	_alias_list,
	_has_column,
	_identity_alias,
	_message,
	_string_list,
	_text,
)


def _gst_registration_type(value):
	return GST_REGISTRATION_TYPE.get(str(value or "").strip(), "Unregistered/Consumer")


def _party_fieldnames(doctype):
	fields = [
		"name",
		"disabled",
		"tax_id",
		"tax_category",
		"default_currency",
		"default_price_list",
		"payment_terms",
		"website",
		"mobile_no",
		"email_id",
		"primary_address",
	]
	if doctype == "Customer":
		fields += [
			"customer_name",
			"customer_type",
			"customer_group",
			"territory",
			"customer_primary_address",
			"customer_primary_contact",
		]
	else:
		fields += [
			"supplier_name",
			"supplier_type",
			"supplier_group",
			"country",
			"supplier_primary_address",
			"supplier_primary_contact",
		]
	if _has_column(doctype, "gst_category"):
		fields.append("gst_category")
	if _has_column(doctype, "gstin"):
		fields.append("gstin")
	if _has_column(doctype, "pan"):
		fields.append("pan")
	return fields


def _fetch_parties(doctype, include_disabled=False):
	filters = {} if include_disabled else {"disabled": 0}
	return frappe.get_all(
		doctype,
		filters=filters,
		fields=_party_fieldnames(doctype),
		order_by="name",
	)


def _fetch_addresses(parties):
	names = sorted(
		{
			row.get("customer_primary_address") or row.get("supplier_primary_address")
			for row in parties
			if row.get("customer_primary_address") or row.get("supplier_primary_address")
		}
	)
	if not names:
		return {}
	return {
		row["name"]: row
		for row in frappe.get_all(
			"Address",
			filters={"name": ["in", names]},
			fields=[
				"name",
				"address_line1",
				"address_line2",
				"city",
				"state",
				"country",
				"pincode",
				"email_id",
				"phone",
			],
		)
	}


def _fetch_contacts(parties):
	names = sorted(
		{
			row.get("customer_primary_contact") or row.get("supplier_primary_contact")
			for row in parties
			if row.get("customer_primary_contact") or row.get("supplier_primary_contact")
		}
	)
	if not names:
		return {}
	return {
		row["name"]: row
		for row in frappe.get_all(
			"Contact",
			filters={"name": ["in", names]},
			fields=["name", "first_name", "last_name", "full_name", "email_id", "phone", "mobile_no"],
		)
	}


def _fetch_customer_credit_limits(customers, company):
	names = [row["name"] for row in customers]
	if not names:
		return {}
	limits = frappe.get_all(
		"Customer Credit Limit",
		filters={"parent": ["in", names], "company": company},
		fields=["parent", "credit_limit"],
	)
	result = {}
	for row in limits:
		result[row["parent"]] = result.get(row["parent"], 0) + flt(row.get("credit_limit"))
	return result


def _address_values(address):
	if not address:
		return [], "", "", ""
	lines = [
		address.get("address_line1"),
		address.get("address_line2"),
		address.get("city"),
	]
	lines = [_text(line) for line in lines if _text(line)]
	return lines, address.get("state") or "", address.get("pincode") or "", address.get("country") or ""


def _ledger_message(
	party,
	doctype,
	company,
	address,
	contact,
	credit_limit,
	posting_date=None,
	tally_name=None,
	parent=None,
):
	name = (
		tally_name
		or (party.get("customer_name") if doctype == "Customer" else party.get("supplier_name"))
		or party["name"]
	)
	address_lines, state, pincode, address_country = _address_values(address)
	tax_id = _text(party.get("gstin") or party.get("tax_id"))
	pan = _text(party.get("pan"))
	if len(tax_id) == 10 and not pan:
		pan, tax_id = tax_id, ""
	elif len(tax_id) == 15 and not pan:
		# Indian GSTIN embeds the 10-character PAN at positions 3-12.
		pan = tax_id[2:12]
	country = _text(party.get("country") or address_country or frappe.get_cached_value("Company", company, "country"))
	email = _text(
		(contact or {}).get("email_id")
		or (address or {}).get("email_id")
		or party.get("email_id")
	)
	phone = _text((contact or {}).get("phone") or (address or {}).get("phone"))
	mobile = _text((contact or {}).get("mobile_no") or party.get("mobile_no"))
	contact_name = _text(
		(contact or {}).get("full_name")
		or " ".join(
			part for part in ((contact or {}).get("first_name"), (contact or {}).get("last_name")) if part
		)
	)
	fields = {
		"parent": parent or PARTY_PARENT[doctype],
		"isbillwiseon": True,
		"iscostcentreson": False,
		"affectsstock": False,
		"isgstapplicable": bool(tax_id or party.get("gst_category") not in (None, "", "Unregistered")),
		"gstregistrationtype": _gst_registration_type(party.get("gst_category")),
		"partygstin": tax_id,
		"incometaxnumber": pan,
		"countryofresidence": country,
		"ledstatename": state,
		"pincode": pincode,
		"address": _string_list(address_lines) if address_lines else None,
		"email": email,
		"ledgerphone": phone,
		"ledgermobile": mobile,
		"ledgercontact": contact_name,
		"creditlimit": f"{flt(credit_limit):.2f}" if credit_limit else None,
		"ledgercode": party["name"],
		"website": _text(party.get("website")),
		"mailingname": _string_list([name]),
		"language name": _alias_list(
			name, _identity_alias(name, party["name"], doctype)
		),
	}
	if address_lines:
		fields["ledmailingdetails"] = [
			{
				"applicablefrom": str(posting_date or date.today().isoformat()).replace("-", ""),
				"mailingname": name,
				"address": _string_list(address_lines),
				"pincode": pincode,
				"state": state,
				"country": country,
			}
		]
	return _message("Ledger", name, **fields)


def _godown_message(row, company_abbr, tally_name=None):
	name = tally_name or _clean_name(row.get("warehouse_name") or row["name"], company_abbr)
	parent = _clean_name(row.get("parent_warehouse"), company_abbr)
	if not parent or parent.casefold() == "all warehouses":
		parent = TALLY_PRIMARY
	return _message(
		"Godown",
		name,
		parent=parent,
		**{
			"language name": _alias_list(
				name, _identity_alias(name, row["name"], "Warehouse")
			)
		},
	)
