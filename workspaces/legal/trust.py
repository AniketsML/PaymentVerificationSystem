"""
Can this value be relied on without a person checking it?

The rule (agreed 2026-09-22): a value is trusted when

    it was found in the document's own text layer                          -> Verified
    or it is PRINTED, on a page that passes the quality checks, and the
       model says it read every character clearly                           -> Clear

Everything else is parsed and shown, but marked for review with the reasons, so a person can
find it, compare it with the page and correct it. A dossier counts as Extracted only when none of
its values needs review.

Reasons come from four independent sources, none of which alone is enough:
  - the page image (page_quality.py): blurry, faint, blank, dark, low resolution, skewed
  - the model's own verdict per field: handwritten, partly legible, illegible
  - consolidation: pages that disagree about the value, or about who the borrower is
  - the value itself: a name the cleaner couldn't separate, a pincode that isn't 6 digits,
    text still not in English

A run extracted before the model was asked for legibility has no verdict to go on. Its values
are "not verified" rather than "needs review": nothing is known to be wrong, but nothing is known
to be right either, and a re-run settles it. Keeping the two apart keeps the review queue to
values with evidence of a problem.

A person's correction or confirmation outranks all of this (see corrections.py).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

TRUST_VERSION = "1"

# reason -> label, in the order a reviewer should care about them
REASONS: Dict[str, str] = {
    "illegible": "Illegible",
    "blank": "Blank page",
    "conflict": "Conflicting reads",
    "handwritten": "Handwritten",
    "partial": "Partly legible",
    "blurry": "Blurry",
    "faint": "Faint print",
    "dark": "Dark",
    "low_resolution": "Low resolution",
    "rotated": "Rotated or skewed",
    "name": "Name needs a look",
    "format": "Format looks wrong",
    "not_english": "Not in English",
    "unchecked": "Not verified (older run)",
}
PROBLEMS = tuple(r for r in REASONS if r != "unchecked")

# field states, and dossier states
VERIFIED, CLEAR, REVIEW, UNCHECKED, CORRECTED, CONFIRMED = (
    "verified", "clear", "review", "unchecked", "corrected", "confirmed")
LEAD_EXTRACTED, LEAD_REVIEW, LEAD_UNVERIFIED, LEAD_REVIEWED, LEAD_EMPTY = (
    "extracted", "needs_review", "unverified", "reviewed", "no_values")

_VALUE_KEY = re.compile(r"^(borrower|co_borrower_\d+|guarantor_\d+)_(name|address)$")
_NON_LATIN = re.compile(r"[^\x00-\x7F]")
_NAME_FLAGS = ("contains digits", "looks like an email", "may name more than one person",
               "could not separate", "unusually long")
_SKIP = frozenset(("account_no_lan",))          # identity, from the upload itself


def _value_keys(ed: Dict[str, Any]) -> List[str]:
    schema = ed.get("_schema") or None
    keys = []
    for k, v in ed.items():
        if k.startswith("_") or k in _SKIP or k in ("telemetry", "page_extractions"):
            continue
        if isinstance(v, (dict, list)) or v in (None, "", "—"):
            continue
        keys.append(k)
    if schema:
        from workspaces.legal.field_schema import RunSchema
        rs = RunSchema.from_json(schema)
        if rs is not None and not rs.is_empty():
            keys = [k for k in keys if rs.allows(k)]
    return keys


def _source_record(ed: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    """The page record the chosen value was read from."""
    value = ed.get(key)
    best = None
    for px in ed.get("_page_extractions") or []:
        if not isinstance(px, dict):
            continue
        fv = (px.get("fields") or {}).get(key)
        if fv is None:
            continue
        if fv == value:
            return px
        best = best or px
    return best


def field_trust(ed: Dict[str, Any], key: str, quality: Dict[str, Any]) -> Tuple[str, List[str]]:
    """(state, reasons) for one value."""
    from workspaces.legal.page_quality import page_flags
    value = ed.get(key)
    notes = (ed.get("_field_notes") or {}).get(key) or {}
    rec = _source_record(ed, key)
    reasons: List[str] = []

    if notes.get("conflict"):
        reasons.append("conflict")
    flags = [str(f) for f in notes.get("flags") or []]
    if any(f.startswith(_NAME_FLAGS) for f in flags):
        reasons.append("name")
    if any(f.startswith(("not a 6-digit", "too long to be", "has no number", "has no figure", "has no date"))
           for f in flags):
        reasons.append("format")
    if isinstance(value, str) and _NON_LATIN.search(value):
        reasons.append("not_english")

    verified = bool(rec) and (rec.get("field_sources") or {}).get(key) == "text_layer"
    if not verified:
        ev = (rec.get("field_evidence") or {}).get(key) if rec else None
        ev = ev or {}
        script = str(ev.get("script") or notes.get("script") or
                     ((rec.get("field_scripts") or {}).get(key) if rec else "") or "").lower()
        legibility = str(ev.get("legibility") or notes.get("legibility") or "").lower()
        if "hand" in script:
            reasons.append("handwritten")
        if rec is not None:
            reasons.extend(f for f in page_flags(quality, rec.get("document_id", ""), rec.get("page_number"))
                           if f in REASONS)
        if legibility.startswith("illeg"):
            reasons.append("illegible")
        elif legibility.startswith("part"):
            reasons.append("partial")
        elif not legibility:
            reasons.append("unchecked")      # no verdict: an older run, or the model left it out

    reasons = [r for r in REASONS if r in reasons]            # canonical order, de-duplicated
    if any(r in PROBLEMS for r in reasons):
        return REVIEW, reasons
    if reasons:                                                # only "unchecked"
        return UNCHECKED, reasons
    return (VERIFIED if verified else CLEAR), []


def evaluate(ed: Dict[str, Any], quality: Optional[Dict[str, Any]] = None,
             corrections: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The `_trust` block for a stored extraction: per field, and for the dossier.

    `corrections` is {field: {"action": "corrected" | "confirmed", ...}} — a person's word, which
    settles the field whatever the evidence said."""
    quality = quality if quality is not None else (ed.get("_page_quality") or {})
    corrections = corrections or {}
    fields: Dict[str, Dict[str, Any]] = {}
    counts: Dict[str, int] = {}
    for key in _value_keys(ed):
        state, reasons = field_trust(ed, key, quality)
        c = corrections.get(key)
        if c and c.get("action") in (CORRECTED, CONFIRMED):
            fields[key] = {"state": c["action"], "reasons": reasons, "was": state}
            continue
        fields[key] = {"state": state, "reasons": reasons}
        for r in reasons:
            counts[r] = counts.get(r, 0) + 1
    for key, c in corrections.items():                         # a value a person added
        if key not in fields and c.get("action") == CORRECTED and ed.get(key) not in (None, ""):
            fields[key] = {"state": CORRECTED, "reasons": [], "was": "missing"}

    states = [f["state"] for f in fields.values()]
    if not states:
        lead = LEAD_EMPTY
    elif REVIEW in states:
        lead = LEAD_REVIEW
    elif UNCHECKED in states:
        lead = LEAD_UNVERIFIED
    elif any(s in (CORRECTED, CONFIRMED) for s in states):
        lead = LEAD_REVIEWED
    else:
        lead = LEAD_EXTRACTED
    return {"version": TRUST_VERSION, "state": lead, "fields": fields,
            "reasons": {r: counts[r] for r in REASONS if r in counts}}


def cited_pages(ed: Dict[str, Any]) -> Dict[str, List[int]]:
    """{document_id: [pages]} that values were read from — what page quality needs to judge."""
    out: Dict[str, List[int]] = {}
    for px in ed.get("_page_extractions") or []:
        if isinstance(px, dict) and px.get("fields") and str(px.get("page_number", "")).isdigit():
            out.setdefault(px.get("document_id") or "", []).append(int(px["page_number"]))
    return out
