"""Bidirectional financial-ledger mirrors with explicit, balanced ledger scopes."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import ClassVar

import frappe

from express_tally.framework import OutboundFlow, OutboundSyncLog, SourceSpec
from express_tally.integrations.payment_entry_flow import PaymentEntryMapper
from express_tally.integrations.tally_export import DEFAULT_ACCOUNT_GROUPS
from express_tally.integrations.tally_inbound_flows import LoggedInboundFlow, _success_result

MASTER_FLOW = "express_tally.full_ledger_masters_to_tally"
OUTBOUND_FLOW = "express_tally.full_ledger_to_tally"
INBOUND_FLOW = "express_tally.full_ledger_from_tally"
ROLES = frozenset({"Tally Sync User", "Accounts Manager", "System Manager"})
GL_DOCTYPES = (
	"Journal Entry",
	"Payment Entry",
	"Sales Invoice",
	"Purchase Invoice",
	"POS Invoice",
	"Delivery Note",
	"Purchase Receipt",
	"Stock Entry",
	"Stock Reconciliation",
	"Expense Claim",
	"Period Closing Voucher",
)


def digest(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
	).hexdigest()


def fiscal_start(context):
	value = context.options.get("fiscal_year_start") or context.from_date
	if not value:
		raise ValueError("Configure fiscal_year_start for full-ledger sync")
	return date.fromisoformat(str(value)).isoformat()


def balance(company, ledger, *, before=None, through=None, opening_day=None, from_date=None):
	conditions = ["company=%(company)s", "account=%(account)s", "is_cancelled=0"]
	values = {"company": company, "account": ledger["account"]}
	if ledger.get("party"):
		conditions += ["party_type=%(party_type)s", "party=%(party)s"]
		values.update(party_type=ledger["party_type"], party=ledger["party"])
	if opening_day:
		conditions.append(
			"(posting_date < %(opening_day)s OR (posting_date = %(opening_day)s AND is_opening='Yes'))"
		)
		values["opening_day"] = opening_day
	if from_date and ledger["root_type"] in {"Income", "Expense"}:
		conditions.append("posting_date >= %(from_date)s")
		values["from_date"] = from_date
	if before:
		conditions.append("posting_date < %(before)s")
		values["before"] = before
	if through:
		conditions.append("posting_date <= %(through)s")
		values["through"] = through
	result = frappe.db.sql(
		"SELECT COALESCE(SUM(debit-credit),0) FROM `tabGL Entry` WHERE " + " AND ".join(conditions), values
	)[0][0]
	return Decimal(str(result or 0)).quantize(Decimal("0.01"))


def ledger_specs(context):
	requested = context.options.get("ledgers")
	if requested is None:
		requested = [{"account": name} for name in context.options.get("account_names") or []]
	if not isinstance(requested, list) or not 1 <= len(requested) <= 1000:
		raise ValueError("Select between 1 and 1000 financial ledgers")
	currency = frappe.get_cached_value("Company", context.company, "default_currency")
	result = []
	seen = set()
	for selection in requested:
		if not isinstance(selection, dict):
			raise ValueError("Each ledger selection must be an object with an account")
		account = frappe.db.get_value(
			"Account",
			{"name": selection.get("account"), "company": context.company, "is_group": 0, "disabled": 0},
			["name", "account_name", "account_type", "root_type", "account_currency", "modified"],
			as_dict=True,
		)
		if not account or (account.account_currency and account.account_currency != currency):
			raise ValueError(f"Invalid or foreign-currency financial ledger: {selection.get('account')}")
		if account.account_type == "Stock":
			raise ValueError(
				"Stock accounts require an inventory migration policy; choose financial mirror accounts"
			)
		party_type, party = selection.get("party_type") or "", selection.get("party") or ""
		if bool(party_type) != bool(party) or (
			party_type
			and (party_type not in {"Customer", "Supplier"} or not frappe.db.exists(party_type, party))
		):
			raise ValueError("Party ledgers require a valid Customer or Supplier")
		if account.account_type in {"Receivable", "Payable"} and not party:
			raise ValueError("Receivable/payable ledgers must select a specific party")
		if party and account.account_type != ("Receivable" if party_type == "Customer" else "Payable"):
			raise ValueError("Party ledger account type does not match its party type")
		name = selection.get("ledger_name") or account.account_name
		if party:
			name = selection.get("ledger_name") or frappe.db.get_value(
				party_type, party, "customer_name" if party_type == "Customer" else "supplier_name"
			)
		if not isinstance(name, str) or not name.strip() or name.casefold() in seen:
			raise ValueError("Selected Tally ledger names must be nonempty and unique")
		seen.add(name.casefold())
		parent = (
			DEFAULT_ACCOUNT_GROUPS.get(account.account_type)
			or {
				"Asset": "Current Assets",
				"Liability": "Current Liabilities",
				"Equity": "Capital Account",
				"Income": "Indirect Incomes",
				"Expense": "Indirect Expenses",
			}[account.root_type]
		)
		result.append(
			{
				"account": account.name,
				"ledger_name": name,
				"party_type": party_type,
				"party": party,
				"parent": parent,
				"root_type": account.root_type,
				"modified": str(account.modified),
				"currency": currency,
			}
		)
	return result


class LedgerMastersToTallyFlow(OutboundFlow):
	key = MASTER_FLOW
	title = "Full-ledger masters and opening balances to Tally"
	agent_profile = "ledger_master_v1"
	allowed_roles = ROLES

	def __init__(self):
		self.sync_log = OutboundSyncLog(self.key, (SourceSpec("Account", "creation", submitted_only=False),))

	def pull(self, context, limit):
		start = fiscal_start(context)
		pending = []
		for spec in ledger_specs(context):
			opening = (
				balance(context.company, spec, opening_day=start)
				if spec["root_type"] not in {"Income", "Expense"}
				else Decimal(0)
			)
			reference = "ledger:" + digest([spec["account"], spec["party_type"], spec["party"]])
			previous = self.sync_log.previous_target_reference(
				"Account", spec["account"], context.target_id, reference
			)
			payload = {
				"source_doctype": "Account",
				"name": spec["account"],
				"source_reference": reference,
				"modified": spec["modified"],
				"identity_namespace": self.key,
				"operation": "Alter" if previous else "Create",
				"ledger_name": spec["ledger_name"],
				"parent": spec["parent"],
				"currency": spec["currency"],
				"billwise": bool(spec["party"]),
				"opening_balance": str(opening),
				"fiscal_year_start": start,
				"target_ledger": previous.removeprefix("ledger:") if previous else spec["ledger_name"],
				"allow_opening_update": bool(context.options.get("allow_opening_update")),
			}
			# Destination identity and Create/Alter are transport state, not source content.
			payload["source_hash"] = digest(
				{key: value for key, value in payload.items() if key not in {"operation", "target_ledger"}}
			)
			if frappe.db.exists(
				"Tally Sync Log",
				{
					"flow_key": self.key,
					"target_id": context.target_id,
					"source_reference": reference,
					"source_hash": payload["source_hash"],
					"status": "Success",
				},
			):
				continue
			pending.append(payload)
			if len(pending) >= limit:
				break
		return pending

	def acknowledge(self, context, results):
		return self.sync_log.acknowledge(context, results)


class FullLedgerToTallyFlow(OutboundFlow):
	key = OUTBOUND_FLOW
	title = "Full posted GL accounting to Tally"
	agent_profile = "general_ledger_voucher_v1"
	accounting_scope = "all"
	allowed_roles = ROLES

	def __init__(self):
		sources = []
		for doctype in GL_DOCTYPES:
			if not frappe.db.exists("DocType", doctype):
				continue
			meta = frappe.get_meta(doctype)
			if meta.has_field("company") and meta.has_field("posting_date"):
				sources.append(
					SourceSpec(
						doctype,
						"posting_date",
						include_cancelled_if_synced=True,
						sync_once=True,
						exclude_tally_imports=True,
						exclude_other_exports=True,
						exclude_opening=meta.has_field("is_opening"),
					)
				)
		self.sync_log = OutboundSyncLog(self.key, sources)
		self.mapper = PaymentEntryMapper(self)

	def pull(self, context, limit):
		specs = ledger_specs(context)
		start = fiscal_start(context)
		accounts = tuple(sorted({spec["account"] for spec in specs}))
		where = ["company=%(company)s", "account IN %(accounts)s", "posting_date >= %(start)s"]
		values = {"company": context.company, "accounts": accounts, "start": start}
		if context.to_date:
			where.append("posting_date <= %(through)s")
			values["through"] = context.to_date
		candidates = frappe.db.sql(
			"SELECT DISTINCT voucher_type, voucher_no FROM `tabGL Entry` WHERE " + " AND ".join(where), values
		)
		by_type = {}
		selected_names = context.options.get("document_names")
		if selected_names is not None and (
			not isinstance(selected_names, list) or any(not isinstance(name, str) for name in selected_names)
		):
			raise ValueError("document_names must be an array")
		for doctype, name in candidates:
			if doctype in self.sync_log.sources and (selected_names is None or name in selected_names):
				by_type.setdefault(doctype, set()).add(name)
		refs = []
		for doctype, names in by_type.items():
			refs.extend(
				self.sync_log.pending_references(
					context.company,
					context.target_id,
					limit,
					from_date=start,
					to_date=context.to_date,
					source_doctypes=[doctype],
					source_names=sorted(names),
				)
			)
		return [
			self.map_document(frappe.get_doc(doctype, name), context, specs)
			for _, doctype, name in sorted(refs)[:limit]
		]

	def map_document(self, doc, context, specs):
		previous = self.sync_log.previous_target_reference(doc.doctype, doc.name, context.target_id)
		operation = "Cancel" if doc.docstatus == 2 else "Create"
		payload = {
			"source_doctype": doc.doctype,
			"name": doc.name,
			"modified": str(doc.modified),
			"operation": operation,
			"identity_namespace": self.key,
			"origin_marker": self.key,
			"tally_voucher_id": previous,
			"voucher_type": "Journal",
			"transaction_date": str(doc.posting_date),
			"reference": doc.name,
			"narration": f"ERPNext {doc.doctype} {doc.name}",
			"ledger_entries": [],
			"masters": [],
		}
		if operation == "Cancel":
			if not previous:
				raise ValueError("Cannot cancel accounting that was not delivered to Tally")
		else:
			rows = frappe.get_all(
				"GL Entry",
				filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
				fields=[
					"account",
					"debit",
					"credit",
					"party_type",
					"party",
					"against_voucher_type",
					"against_voucher",
				],
				order_by="creation, name",
			)
			selection = {(spec["account"], spec["party_type"], spec["party"]): spec for spec in specs}
			for row in rows:
				key = (row.account, row.party_type or "", row.party or "")
				if key not in selection:
					raise ValueError(
						f"{doc.doctype} {doc.name} includes balancing ledger {key} outside the selected scope"
					)
				row["tally_ledger"] = selection[key]["ledger_name"]
			payload.update(self.mapper.map_ledger_entries(rows, context.company, preserve_lines=True))
			payload["party_ledger"] = ""
			for entry in payload["ledger_entries"]:
				for bill in entry.get("bill_allocations") or []:
					if bill["name"] == doc.name and doc.doctype in {"Sales Invoice", "Purchase Invoice"}:
						bill["bill_type"] = "New Ref"
		payload["source_hash"] = digest(payload)
		return payload

	def acknowledge(self, context, results):
		return self.sync_log.acknowledge(context, results)

	def status(self, context):
		return self.sync_log.status(context)


class FullLedgerFromTallyFlow(LoggedInboundFlow):
	key = INBOUND_FLOW
	title = "Full financial ledgers from Tally with balance reconciliation"
	agent_profile = "tally_ledger_mirror_v1"
	exclusive_group = "tally_accounting_inbound"
	allowed_roles = ROLES
	default_options: ClassVar[dict] = {
		"excluded_origins": [
			OUTBOUND_FLOW,
			"express_tally.payment_entries_to_tally",
			"raplbaddi.sales_invoices_to_tally",
			"srv_erp.sales_documents_to_tally",
		],
		"reconciliation_tolerance": 0.01,
		"require_ledger_scope": True,
	}

	def _cancel_previous(self, record):
		if record.get("_previous_doctype") == "Journal Entry" and record.get("_previous_name"):
			name = record["_previous_name"]
			if frappe.db.exists("Journal Entry", name):
				doc = frappe.get_doc("Journal Entry", name)
				doc.flags.ignore_permissions = True
				if doc.docstatus == 1:
					doc._cancel()
				elif doc.docstatus == 0:
					doc.delete(ignore_permissions=True)
				return _success_result(record, "Journal Entry", name, message="Journal Entry cancelled")
		return super()._cancel_previous(record)

	def resolve(self, context, name, source):
		if context.options.get("ledger_names") and name not in context.options["ledger_names"]:
			raise ValueError(f"Balancing ledger {name} is outside the selected ledger scope")
		mapping = (context.options.get("ledger_mappings") or {}).get(name)
		if isinstance(mapping, str):
			mapping = {"account": mapping}
		if not mapping:
			account = frappe.db.get_value(
				"Account", {"company": context.company, "account_name": name, "is_group": 0}, "name"
			)
			mapping = {"account": account}
		selection = (
			{"account_names": [mapping["account"]]}
			if not mapping.get("party")
			else {"ledgers": [{**mapping, "ledger_name": name}]}
		)
		from dataclasses import replace

		resolved = ledger_specs(
			replace(
				context,
				options={
					**{k: v for k, v in context.options.items() if k not in {"ledgers", "account_names"}},
					**selection,
				},
			)
		)[0]
		return resolved

	def apply_record(self, context, record):
		start = fiscal_start(context)
		if str(record.get("fiscal_year_start") or start) != start or (
			record.get("date") and record["date"] < start
		):
			raise ValueError("Tally record is outside the configured fiscal year")
		kind = record.get("kind")
		if kind == "ledger_reconciliation":
			ledger = self.resolve(context, record.get("ledger"), record)
			actual = balance(context.company, ledger, through=record["as_of_date"], from_date=start)
			expected = Decimal(str(record["balance"])).quantize(Decimal("0.01"))
			tolerance = Decimal(str(context.options.get("reconciliation_tolerance", 0.01)))
			return _success_result(
				record,
				"Account",
				ledger["account"],
				status="Success" if abs(actual - expected) <= tolerance else "Failed",
				message=f"ERPNext {actual}; Tally {expected}; difference {actual - expected}",
			)
		if kind == "opening_balance":
			ledger = self.resolve(context, record.get("ledger"), record)
			amount = Decimal(str(record["balance"]))
			owned = frappe.db.exists(
				"Tally Sync Log",
				{
					"flow_key": MASTER_FLOW,
					"target_id": context.target_id,
					"target_reference": "ledger:" + record["ledger"],
					"status": "Success",
				},
			)
			if owned:
				if balance(context.company, ledger, opening_day=start) != amount.quantize(Decimal("0.01")):
					raise ValueError(
						"The Tally opening conflicts with the ERPNext opening previously exported"
					)
				return _success_result(record, status="Skipped", message="Opening originated in ERPNext")
			temporary = frappe.db.get_value(
				"Account", {"company": context.company, "account_type": "Temporary", "is_group": 0}, "name"
			)
			if not temporary:
				raise ValueError("A Temporary Opening account is required")
			lines = [
				self.journal_line(ledger, amount),
				{
					"account": temporary,
					"debit_in_account_currency": float(max(-amount, 0)),
					"credit_in_account_currency": float(max(amount, 0)),
				},
			]
			return self.save_journal(context, record, lines, start, opening=True)
		if kind != "ledger_voucher":
			return _success_result(record, status="Skipped", message="Unsupported ledger record")
		if any(
			f"[ExpressTallyOrigin:{origin}]" in str(record.get("narration") or "")
			for origin in context.options.get("excluded_origins") or []
		):
			return _success_result(record, status="Skipped", message="Voucher originated in ERPNext")
		lines = []
		for entry in record.get("ledger_entries") or []:
			amount = -Decimal(str(entry.get("amount") or 0))
			if amount:
				ledger = self.resolve(context, entry["ledger"], entry)
				line = self.journal_line(ledger, amount)
				if ledger["root_type"] in {"Income", "Expense"}:
					line["cost_center"] = frappe.get_cached_value("Company", context.company, "cost_center")
				lines.append(line)
		if (
			len(lines) < 2
			or sum(
				Decimal(str(line["debit_in_account_currency"]))
				- Decimal(str(line["credit_in_account_currency"]))
				for line in lines
			)
			!= 0
		):
			raise ValueError("Tally voucher extraction must contain all balanced accounting lines")
		return self.save_journal(context, record, lines, record["date"])

	@staticmethod
	def journal_line(ledger, amount):
		row = {
			"account": ledger["account"],
			"debit_in_account_currency": float(max(amount, 0)),
			"credit_in_account_currency": float(max(-amount, 0)),
		}
		if ledger["party"]:
			row.update(party_type=ledger["party_type"], party=ledger["party"])
		return row

	@staticmethod
	def save_journal(context, record, lines, posting_date, opening=False):
		name = record.get("_previous_name")
		if (
			name
			and record.get("_previous_doctype") == "Journal Entry"
			and frappe.db.exists("Journal Entry", name)
		):
			previous = frappe.get_doc("Journal Entry", name)
			previous.flags.ignore_permissions = True
			if previous.docstatus == 1:
				previous._cancel()
			elif previous.docstatus == 0:
				previous.delete(ignore_permissions=True)
		doc = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": context.company,
				"posting_date": posting_date,
				"voucher_type": "Opening Entry" if opening else "Journal Entry",
				"is_opening": "Yes" if opening else "No",
				"user_remark": f"Tally ledger sync: {record.get('ledger') or record.get('voucher_number') or record.get('_tally_key')}",
				"accounts": lines,
			}
		)
		if frappe.get_meta("Journal Entry").has_field("branch"):
			doc.branch = context.options.get("branch")
		doc.insert(ignore_permissions=True)
		doc._submit()
		if doc.docstatus != 1:
			raise ValueError("Ledger acknowledgement requires a fully posted Journal Entry")
		return _success_result(record, "Journal Entry", doc.name, message="Posted full-ledger Journal Entry")
