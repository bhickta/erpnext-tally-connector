from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from express_tally.integrations.sales_voucher_flow import SalesDocumentMapper


class Record(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


class TestSalesDocumentMapper(TestCase):
	def test_maps_standard_document_without_company_policy(self):
		flow = SimpleNamespace(
			sync_log=SimpleNamespace(previous_target_reference=Mock(return_value=""))
		)
		mapper = SalesDocumentMapper(flow)
		document = Record(
			doctype="Sales Order",
			name="SO-1",
			modified="2026-09-04 10:00:00",
			transaction_date="2026-09-04",
			delivery_date="2026-09-10",
			customer="CUST-1",
			customer_name="Customer One",
			currency="INR",
			po_no="PO-1",
			terms="Terms",
			base_net_total=250,
			base_grand_total=295,
			base_rounded_total=295,
			base_rounding_adjustment=0,
			taxes=[Record(account_head="Output IGST - TC", base_tax_amount_after_discount_amount=45)],
			items=[
				Record(
					item_code="ITEM-1",
					item_name="Item One",
					description="",
					item_group="Products",
					gst_hsn_code="",
					stock_uom="Nos",
					uom="Nos",
					stock_qty=2,
					qty=2,
					base_net_amount=250,
					warehouse="Stores - TC",
					delivery_date="2026-09-10",
					against_sales_order=None,
				)
			],
		)
		company = Record(
			abbr="TC",
			country="India",
			default_income_account="Sales - TC",
			round_off_account="Round Off - TC",
		)
		item = Record(
			name="ITEM-1",
			item_name="Item One",
			description="Item description",
			item_group="Products",
			stock_uom="Nos",
			gst_hsn_code="1234",
		)
		fake_frappe = SimpleNamespace(
			get_all=Mock(return_value=[item]),
			db=SimpleNamespace(get_value=Mock(return_value="All Item Groups")),
			_dict=lambda: Record(),
		)

		with patch("express_tally.integrations.sales_voucher_flow.frappe", fake_frappe):
			payload = mapper.map_document(document, company, "target-1")

		self.assertEqual(payload["operation"], "Create")
		self.assertEqual(payload["sales_ledger"], "Sales")
		self.assertEqual(payload["taxes"][0]["ledger"], "Output IGST")
		self.assertEqual(payload["items"][0]["rate"], 125)
		self.assertEqual(payload["items"][0]["warehouse"], "Stores")
		self.assertEqual(payload["masters"]["item_groups"], [{"name": "Products", "parent": ""}])
		self.assertEqual(len(payload["source_hash"]), 64)
