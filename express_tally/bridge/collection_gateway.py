"""TDL-file-free exports from Tally's standard HTTP/XML gateway."""

import re
import xml.etree.ElementTree as ET
from collections import defaultdict


NON_ALPHANUMERIC = re.compile(r"[^a-zA-Z0-9]+")


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
		ET.SubElement(static, str(name).upper()).text = str(value)
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
	for index, formula in enumerate(filters, 1):
		filter_name = f"ETFilter{index}"
		ET.SubElement(collection, "FILTERS").text = filter_name
		ET.SubElement(message, "SYSTEM", {"TYPE": "Formulae", "NAME": filter_name}).text = formula
	return ET.tostring(envelope, encoding="unicode")


def parse_collection_export(payload, record_tags):
	"""Parse Tally's collection XML while retaining repeated nested entries."""
	try:
		root = ET.fromstring(payload.replace("\x04", ""))
	except ET.ParseError as exc:
		raise ValueError(f"Tally returned invalid collection XML: {str(exc)}") from exc
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
	result = {
		key: entries[0] if len(entries) == 1 else entries
		for key, entries in values.items()
	}
	for key, value in element.attrib.items():
		result[f"_{normalized_key(key)}"] = value
	return result


def scalar(record, *paths, default=""):
	"""Read the first scalar value from alternative normalized paths."""
	for path in paths:
		value = record
		for part in path.split("."):
			if not isinstance(value, dict):
				value = None
				break
			value = value.get(part) if part in value else value.get(normalized_key(part))
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
