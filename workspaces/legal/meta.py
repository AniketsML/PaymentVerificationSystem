"""
Telling a value apart from a note about a value.

The model is asked to return its bookkeeping — which page it read a field on, whether the field
was handwritten, how legible it was — under keys that start with an underscore. It does not always
comply: "field_scripts" arrives as often as "_field_scripts". Downstream code decides what is a
value by the prefix alone, so an unprefixed note used to be flattened into value columns
("field_scripts_borrower_name") on the dashboard, in the drawer and in exports.

This module is the single definition of "bookkeeping", with no dependencies, so the parser, the
normaliser, the read path and the data migration all agree.
"""
from __future__ import annotations

from typing import Any, Dict

# Every per-field note is named field_<something>, so its flattened form can never collide with a
# real field: "evidence_of_title" is a value, "field_evidence_borrower_name" never is.
META_KEYS = frozenset((
    "cited_pages", "field_page_sources", "field_scripts", "field_sources", "field_evidence",
    "page_sources", "ocr_route", "raw_response", "medha_error",
    "telemetry", "phase_timings", "page_extractions", "status", "parse_error", "error",
))

# bookkeeping that runs have flattened (or could flatten) into value-looking names
_FLATTENED_PREFIXES = ("field_scripts", "field_sources", "field_page_sources", "field_evidence",
                       "cited_pages")


def _bare(key: Any) -> str:
    return str(key).strip().lower().replace(" ", "_").replace("-", "_").lstrip("_")


def is_meta_key(key: Any) -> bool:
    """True for bookkeeping keys, however they are spelled — including keys an older run already
    flattened into a value-looking name."""
    b = _bare(key)
    return b in META_KEYS or any(b.startswith(f"{p}_") for p in _FLATTENED_PREFIXES)


def canonical_meta(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Move unprefixed bookkeeping under its underscore name, in place, and return it."""
    if not isinstance(parsed, dict):
        return parsed
    for k in list(parsed.keys()):
        if k.startswith("_"):
            continue
        b = _bare(k)
        if b in META_KEYS:
            parsed.setdefault(f"_{b}", parsed.pop(k))
    return parsed


def is_flattened_leak(key: Any) -> bool:
    """A value-looking key that is really flattened bookkeeping ("field_scripts_borrower_name").
    Narrower than is_meta_key on purpose: the pipeline deliberately keeps unprefixed copies of
    `telemetry` and `page_extractions`, and a migration must not touch those."""
    b = _bare(key)
    return not str(key).startswith("_") and any(b.startswith(f"{p}_") for p in _FLATTENED_PREFIXES)


def strip_flattened(values: Dict[str, Any]) -> int:
    """Remove flattened bookkeeping from a dict of values, in place. Returns how many went."""
    if not isinstance(values, dict):
        return 0
    doomed = [k for k in values if is_flattened_leak(k)]
    for k in doomed:
        values.pop(k, None)
    return len(doomed)
