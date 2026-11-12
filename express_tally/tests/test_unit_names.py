import xml.etree.ElementTree as ET
from unittest import TestCase

from express_tally.bridge.xml_gateway import build_unit_import, unit_name_error


class TestUnitNameRules(TestCase):
	"""Rules verified against TallyPrime 1.1.7.1 through the HTTP gateway."""

	def test_letters_and_underscores_are_accepted(self):
		for name in ("NOS", "Pkg", "SqFt", "Ampere", "Test_Underscore", "ZZNEW"):
			self.assertEqual(unit_name_error(name), "", name)

	def test_digits_are_rejected(self):
		for name in ("14 Pkg", "20 Pkg", "ZZ1", "10 Kg"):
			self.assertNotEqual(unit_name_error(name), "", name)

	def test_spaces_and_punctuation_are_rejected(self):
		for name in ("ZZ AB", "Ampere-Hour", "Btu/Hour", "Acre (US)", "Barrel(Beer)"):
			self.assertNotEqual(unit_name_error(name), "", name)

	def test_empty_name_is_rejected(self):
		self.assertNotEqual(unit_name_error(""), "")
		self.assertNotEqual(unit_name_error(None), "")


class TestUnitImportPayload(TestCase):
	def unit(self, name, **extra):
		row = {
			"metadata": {"type": "Unit", "action": "create", "name": name},
			"name": name,
			"issimpleunit": True,
			"decimalplaces": "3",
		}
		row.update(extra)
		return row

	def parse(self, xml):
		root = ET.fromstring(xml)
		return root.findall(".//UNIT")

	def test_simple_unit_has_no_original_name(self):
		units = self.parse(build_unit_import("Company", [self.unit("Pkg")]))

		self.assertEqual(len(units), 1)
		self.assertIsNone(units[0].find("ORIGINALNAME"))
		self.assertEqual(units[0].findtext("NAME"), "Pkg")
		self.assertEqual(units[0].findtext("ISSIMPLEUNIT"), "Yes")
		self.assertEqual(units[0].findtext("DECIMALPLACES"), "3")

	def test_compound_unit_keeps_original_name(self):
		row = self.unit("Box", issimpleunit=False, originalname="NOS")
		units = self.parse(build_unit_import("Company", [row]))

		self.assertEqual(units[0].findtext("ORIGINALNAME"), "NOS")
		self.assertEqual(units[0].findtext("ISSIMPLEUNIT"), "No")

	def test_row_without_name_is_skipped(self):
		units = self.parse(build_unit_import("Company", [self.unit("")]))

		self.assertEqual(units, [])
