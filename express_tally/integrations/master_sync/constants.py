"""Constants and Tally sentinels for master sync payloads."""

from ..tally_export import DEFAULT_ACCOUNT_GROUPS

MASTER_SYNC_TYPES = ("Group", "Ledger", "Item", "Warehouse", "Customer", "Supplier")

MASTER_SYNC_LABELS = {
	"Group": "Accounting groups",
	"Ledger": "Ledgers",
	"Item": "Items",
	"Warehouse": "Warehouses",
	"Customer": "Customers",
	"Supplier": "Suppliers",
}

TALLY_MASTER_TYPES = {
	"Group": "Group",
	"Ledger": "Ledger",
	"Item": "Stock Item",
	"Warehouse": "Godown",
	"Customer": "Ledger",
	"Supplier": "Ledger",
}

# TallyPrime's predefined accounting groups. These are not created as custom
# masters; ERPNext account groups that match them are used as parents.
TALLY_STANDARD_GROUPS = {
	*DEFAULT_ACCOUNT_GROUPS.values(),
	"Capital Account",
	"Loans (Liability)",
	"Current Liabilities",
	"Fixed Assets",
	"Current Assets",
	"Investments",
	"Loans & Advances (Asset)",
	"Misc. Expenses (ASSET)",
	"Sales Accounts",
	"Purchase Accounts",
	"Direct Incomes",
	"Indirect Incomes",
	"Direct Expenses",
	"Indirect Expenses",
	"Bank Accounts",
	"Cash-in-Hand",
	"Sundry Debtors",
	"Sundry Creditors",
	"Duties & Taxes",
	"Stock-in-Hand",
}

PARTY_PARENT = {
	"Customer": "Sundry Debtors",
	"Supplier": "Sundry Creditors",
}

GST_REGISTRATION_TYPE = {
	"Registered Regular": "Regular",
	"Registered Composition": "Composition",
	"Unregistered": "Unregistered/Consumer",
	"Unregistered/Consumer": "Unregistered/Consumer",
	"Consumer": "Consumer",
	"SEZ": "SEZ",
	"Overseas": "Overseas",
	"Deemed Export": "Deemed Export",
	"UIN Holders": "UIN Holders",
	"Tax Deductor": "Tax Deductor",
	"Tax Collector": "Tax Collector",
	"Input Service Distributor": "Input Service Distributor",
	"Input Service Distributor (ISD)": "Input Service Distributor",
	"Non-Resident": "Overseas",
}

TALLY_PRIMARY = "\x04 Primary"
TALLY_NOT_APPLICABLE = "\x04 Not Applicable"
