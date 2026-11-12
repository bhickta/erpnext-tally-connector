import xml.etree.ElementTree as ET
from unittest import TestCase

from express_tally.bridge.master_sync import apply_unit_name_mapping
from express_tally.bridge.xml_gateway import (
	UNIT_NAME_PATTERN,
	build_unit_import,
	resolve_unit_names,
	tally_unit_name,
	unit_name_error,
)


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


class TestUnitNameMapping(TestCase):
	def test_names_are_made_tally_safe(self):
		cases = {
			"Nos": "Nos",
			"Test_Underscore": "Test_Underscore",
			"14 Pkg": "OneFour_Pkg",
			"20 Pkg": "TwoZero_Pkg",
			"Ampere-Hour": "Ampere_Hour",
			"Btu/Hour": "Btu_Hour",
			"Acre (US)": "Acre_US",
			"Barrel(Beer)": "Barrel_Beer",
		}
		for original, expected in cases.items():
			mapped = tally_unit_name(original)
			self.assertEqual(mapped, expected, original)
			self.assertIsNotNone(UNIT_NAME_PATTERN.fullmatch(mapped), original)

	def test_unmapped_name_has_no_error(self):
		for original in ("Nos", "14 Pkg", "Btu/Hour"):
			self.assertEqual(unit_name_error(tally_unit_name(original)), "", original)

	def test_collisions_get_distinct_names(self):
		mapping = resolve_unit_names(["A-B", "A B", "A/B"])

		self.assertEqual(len(set(mapping.values())), 3)
		for mapped in mapping.values():
			self.assertIsNotNone(UNIT_NAME_PATTERN.fullmatch(mapped))

	def test_mapping_is_stable(self):
		first = resolve_unit_names(["14 Pkg", "Btu/Hour"])
		second = resolve_unit_names(["Btu/Hour", "14 Pkg"])

		self.assertEqual(first, second)


class TestApplyingUnitNames(TestCase):
	def test_units_and_item_uoms_are_rewritten(self):
		batches = [
			{
				"key": "units",
				"tallymessage": [
					{"metadata": {"type": "Unit", "name": "14 Pkg"}, "name": "14 Pkg"},
					{"metadata": {"type": "Unit", "name": "Nos"}, "name": "Nos"},
				],
			},
			{
				"key": "items",
				"tallymessage": [
					{
						"name": "ITEM-1",
						"base units": "14 Pkg",
						"uoms": [{"uom": "Btu/Hour"}, {"uom": "Nos"}],
					}
				],
			},
		]

		rewritten, mapping = apply_unit_name_mapping(batches)

		self.assertEqual(mapping, {"14 Pkg": "OneFour_Pkg", "Btu/Hour": "Btu_Hour"})
		unit = rewritten[0]["tallymessage"][0]
		self.assertEqual(unit["name"], "OneFour_Pkg")
		self.assertEqual(unit["metadata"]["name"], "OneFour_Pkg")
		self.assertEqual(unit["metadata"]["unit_alias"], "14 Pkg")
		item = rewritten[1]["tallymessage"][0]
		self.assertEqual(item["base units"], "OneFour_Pkg")
		self.assertEqual([c["uom"] for c in item["uoms"]], ["Btu_Hour", "Nos"])

	def test_safe_names_are_left_alone(self):
		batches = [{"key": "units", "tallymessage": [{"name": "Nos"}]}]

		rewritten, mapping = apply_unit_name_mapping(batches)

		self.assertEqual(mapping, {})
		self.assertEqual(rewritten[0]["tallymessage"][0]["name"], "Nos")


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
