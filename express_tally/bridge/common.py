"""Small dependency-free helpers shared by the bridge and server integrations.

The portable Windows build only ships ``express_tally.bridge``. Keep this module
stdlib-only so both the standalone bridge and Frappe can import it.
"""

import hashlib
import json
import re
from decimal import Decimal, ROUND_HALF_UP

NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")
SUCCESS_STATUSES = frozenset({"success", "already exists", "skipped"})
ZERO_MONEY = Decimal("0.00")
MONEY_QUANTUM = Decimal("0.01")


def parse_number(value, default=0.0):
	"""Return the first number in a Tally/Frappe value, ignoring thousands separators."""
	match = NUMBER_PATTERN.search(str(value if value is not None else default).replace(",", ""))
	return float(match.group(0)) if match else float(default)


def to_money(value):
	"""Return a two-decimal Decimal suitable for accounting comparisons."""
	return Decimal(str(value or 0)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def compact_json(value, *, ensure_ascii=True):
	"""Serialise values deterministically for hashes and idempotency keys."""
	return json.dumps(
		value,
		sort_keys=True,
		separators=(",", ":"),
		ensure_ascii=ensure_ascii,
		default=str,
	)


def stable_digest(value, *, ensure_ascii=True):
	"""Return a SHA-256 digest of the canonical JSON representation of ``value``."""
	return hashlib.sha256(compact_json(value, ensure_ascii=ensure_ascii).encode()).hexdigest()


def clean_company_suffix(value, company_abbr=None):
	"""Remove only Tally's trailing `` - ABBR`` company suffix."""
	value = (value or "").strip()
	if company_abbr:
		value = re.sub(rf"\s+-\s+{re.escape(company_abbr)}$", "", value).strip()
	return value


def is_success_status(value):
	"""Return whether a sync result status counts as a successful import."""
	return str(value or "").strip().casefold() in SUCCESS_STATUSES
