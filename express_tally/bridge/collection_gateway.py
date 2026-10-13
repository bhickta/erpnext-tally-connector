"""TDL-file-free exports from Tally's standard HTTP/XML gateway."""

import re
import xml.etree.ElementTree as ET
from collections import defaultdict

NON_ALPHANUMERIC = re.compile(r"[^a-zA-Z0-9]+")
NUMERIC_CHARACTER_REFERENCE = re.compile(r"&#(?:x([0-9a-fA-F]+)|([0-9]+));")
INVALID_XML_CHARACTER = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
UNDECLARED_UDF_TAG = re.compile(r"(<\s*/?\s*)UDF:")


def _valid_xml_character(codepoint):
	return (
		codepoint in (9, 10, 13)
		or 32 <= codepoint <= 0xD7FF
		or 0xE000 <= codepoint <= 0xFFFD
		or 0x10000 <= codepoint <= 0x10FFFF
	)


def _clean_tally_xml(payload):
	# Tally can emit its reserved-name marker as either a raw control character
	# or a numeric reference. Both are invalid in XML 1.0. Some exports also
	# contain UDF-prefixed tags without declaring an XML namespace.
	payload = INVALID_XML_CHARACTER.sub("", payload)
	payload = UNDECLARED_UDF_TAG.sub(r"\1UDF_", payload)

	def valid_reference(match):
		codepoint = int(match.group(1), 16) if match.group(1) else int(match.group(2))
		return match.group(0) if _valid_xml_character(codepoint) else ""

	return NUMERIC_CHARACTER_REFERENCE.sub(valid_reference, payload)


def xml_tag(value):
	return str(value).split("}")[-1]


def normalized_key(value):
	value = NON_ALPHANUMERIC.sub("_", xml_tag(value)).strip("_")
	return value.lower()


def build_collection_export(
	company,
	collection_name,
	object_type,
	native_fields,
	filters=(),
	static_variables=None,
	formulas=None,
	computed_fields=None,
):
	"""Build an inline collection export; no TDL/TCP needs to be installed in Tally."""
	envelope = ET.Element("ENVELOPE")
	header = ET.SubElement(envelope, "HEADER")
	ET.SubElement(header, "VERSION").text = "1"
	ET.SubElement(header, "TALLYREQUEST").text = "Export"
	ET.SubElement(header, "TYPE").text = "Collection"
	ET.SubElement(header, "ID").text = collection_name
	body = ET.SubElement(envelope, "BODY")
	description = ET.SubElement(body, "DESC")
	static = ET.SubElement(description, "STATICVARIABLES")
	ET.SubElement(static, "SVEXPORTFORMAT").text = "$$SysName:XML"
	ET.SubElement(static, "SVCURRENTCOMPANY").text = company
	for name, value in (static_variables or {}).items():
		if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", str(name)):
			raise ValueError(f"Invalid Tally static variable: {name}")
		attributes = {"TYPE": "Date"} if str(name).upper() in {"SVFROMDATE", "SVTODATE"} else {}
		ET.SubElement(static, str(name).upper(), attributes).text = str(value)
	tdl = ET.SubElement(description, "TDL")
	message = ET.SubElement(tdl, "TDLMESSAGE")
	collection = ET.SubElement(
		message,
		"COLLECTION",
		{"NAME": collection_name, "ISMODIFY": "No", "ISFIXED": "No", "ISINITIALIZE": "Yes"},
	)
	ET.SubElement(collection, "TYPE").text = object_type
	for field in native_fields:
		ET.SubElement(collection, "NATIVEMETHOD").text = field
	for name, expression in (computed_fields or {}).items():
		if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", str(name)):
			raise ValueError(f"Invalid Tally computed field: {name}")
		ET.SubElement(collection, "COMPUTE").text = f"{name} : {expression}"
	for index, formula in enumerate(filters, 1):
		filter_name = f"ETFilter{index}"
		ET.SubElement(collection, "FILTERS").text = filter_name
		ET.SubElement(message, "SYSTEM", {"TYPE": "Formulae", "NAME": filter_name}).text = formula
	for name, formula in (formulas or {}).items():
		if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", str(name)):
			raise ValueError(f"Invalid Tally formula name: {name}")
		ET.SubElement(message, "SYSTEM", {"TYPE": "Formulae", "NAME": name}).text = formula
	return ET.tostring(envelope, encoding="unicode")


def parse_collection_export(payload, record_tags):
	"""Parse Tally's collection XML while retaining repeated nested entries."""
	try:
		root = ET.fromstring(_clean_tally_xml(payload))
	except ET.ParseError as exc:
		raise ValueError(f"Tally returned invalid collection XML: {exc!s}") from exc
	tags = {normalized_key(tag) for tag in record_tags}
	records = []
	for element in root.iter():
		if normalized_key(element.tag) not in tags:
			continue
		value = _element_value(element)
		if not isinstance(value, dict):
			value = {"value": value}
		value["_xml_tag"] = xml_tag(element.tag)
		records.append(value)
	return records


def _element_value(element):
	children = list(element)
	if not children:
		return (element.text or "").strip()
	values = defaultdict(list)
	for child in children:
		values[normalized_key(child.tag)].append(_element_value(child))
	result = {key: entries[0] if len(entries) == 1 else entries for key, entries in values.items()}
	for key, value in element.attrib.items():
		result[f"_{normalized_key(key)}"] = value
	return result


def scalar(record, *paths, default=""):
	"""Read the first scalar value from alternative normalized paths."""
	for path in paths:
		value = record
		for part in path.split("."):
			if isinstance(value, list):
				value = value[0] if value else None
			if not isinstance(value, dict):
				value = None
				break
			if part in value:
				value = value[part]
			else:
				key = normalized_key(part)
				matched = next((k for k in value if k.replace("_", "") == key.replace("_", "")), key)
				value = value.get(matched)
		if isinstance(value, list):
			value = value[0] if value else None
		if isinstance(value, dict):
			value = scalar(value, "name", "value", default="")
		if value not in (None, ""):
			return str(value).strip()
	return default


def nested_records(record, *names):
	"""Find nested list records regardless of Tally's LIST wrapper depth."""
	targets = {normalized_key(name).replace("_", "") for name in names}
	result = []

	def walk(value, parent_key=""):
		if isinstance(value, dict):
			for key, child in value.items():
				if normalized_key(key).replace("_", "") in targets:
					if isinstance(child, list):
						result.extend(item for item in child if isinstance(item, dict))
					elif isinstance(child, dict):
						result.append(child)
				else:
					walk(child, key)
		elif isinstance(value, list):
			for child in value:
				walk(child, parent_key)

	walk(record)
	return result
