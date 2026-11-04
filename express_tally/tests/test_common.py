from unittest import TestCase

from express_tally.bridge.common import (
	clean_company_suffix,
	is_success_status,
	parse_number,
	stable_digest,
	to_money,
)


class TestCommonHelpers(TestCase):
	def test_parse_number_ignores_tally_formatting(self):
		self.assertEqual(parse_number("1,234.50 Dr"), 1234.5)
		self.assertEqual(parse_number(""), 0.0)
		self.assertEqual(parse_number(None, default=7), 7.0)

	def test_to_money_quantizes_to_two_decimals(self):
		self.assertEqual(str(to_money("10.005")), "10.01")
		self.assertEqual(str(to_money(None)), "0.00")

	def test_clean_company_suffix_only_removes_the_abbreviation(self):
		self.assertEqual(clean_company_suffix("Cash - SRV", "SRV"), "Cash")
		self.assertEqual(clean_company_suffix("Cash SRV", "SRV"), "Cash SRV")

	def test_stable_digest_is_order_independent_and_preserves_legacy_encoding(self):
		first = stable_digest({"b": 2, "a": 1})
		self.assertEqual(first, stable_digest({"a": 1, "b": 2}))
		self.assertEqual(first, stable_digest({"a": 1, "b": 2}, ensure_ascii=True))
		self.assertNotEqual(
			stable_digest({"name": "Café"}, ensure_ascii=True),
			stable_digest({"name": "Café"}, ensure_ascii=False),
		)

	def test_success_status_is_case_insensitive(self):
		self.assertTrue(is_success_status("Already Exists"))
		self.assertTrue(is_success_status(" skipped "))
		self.assertFalse(is_success_status("Failed"))
