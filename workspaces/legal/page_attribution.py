"""
Which page did a value actually come from?

The model is asked to cite the page it read each value on, by reading a "PAGE N" badge stamped
onto every image. It is not reliable at it: measured over a real 172-dossier run, the cited page
was right for 42% of the values that could be checked. When the model cites nothing, the pipeline
used to fall back to the first page of the 20-page batch, which is a guess presented as a fact.

The document itself is a better witness. A PDF's text layer says exactly which pages a value
appears on, so this module re-attributes every value it can confirm and is explicit about the
ones it cannot:

    page_source = "text_layer"   the value is on that page, confirmed in the document's own text
    page_source = "model"        no text layer to check it against — the model's claim, unverified

Where a value appears on several pages (a borrower's name can appear on twenty), the page chosen
is the one carrying the most of that document's OTHER values too — the page where the record
actually lives, rather than the first passing mention. The model's own claim breaks a tie when it
is among the candidates, so a correct citation is never overridden.

Re-attribution runs over a whole document at once, not one record at a time: four batches of one
document can each emit a record for the same value, and reconciling them independently would
leave four rows on the same page. Records are rebuilt one-per-page, merged and deduped.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_WS = re.compile(r"[^a-z0-9]+")
_MIN_MATCH_CHARS = 6          # shorter values ("2019", "NIL") match anywhere and prove nothing
_MAX_PAGES = 400              # a guard against pathological documents
# A value is rarely transcribed character for character — a comma moves, a title is dropped. The
# same ratio the Typed/Scanned chips use (provenance.py), so the two cannot contradict each other
# on the same row: a value shown as "Typed" is a value this module can confirm a page for.
_TOKEN_HIT_RATIO = 0.75
_MIN_TOKENS = 2


def _norm(s: Any) -> str:
    return _WS.sub(" ", str(s or "").lower()).strip()


def _page_texts(path: str) -> Dict[int, str]:
    """Normalised text of every page of a document, or {} when there is nothing to read."""
    if not path or not os.path.exists(path):
        return {}
    try:
        import fitz
        out: Dict[int, str] = {}
        with fitz.open(path) as doc:
            for i in range(min(doc.page_count, _MAX_PAGES)):
                out[i + 1] = _norm(doc[i].get_text("text") or "")
        return out
    except Exception as e:  # noqa: BLE001 — an unreadable file simply goes unverified
        sys.stderr.write(f"[page_attribution] could not read {path}: {e}\n")
        return {}


def _pages_containing(value: Any, texts: Dict[int, str]) -> List[int]:
    """Pages whose text holds this value. An exact match is believed on its own; only when the
    value appears nowhere verbatim is the looser word-overlap rule tried, so a page found by
    exact match is never displaced by a page found by approximation."""
    v = _norm(value)
    if len(v) < _MIN_MATCH_CHARS:
        return []
    exact = [p for p, t in texts.items() if t and v in t]
    if exact:
        return exact
    tokens = [t for t in v.split() if len(t) > 2]
    if len(tokens) < _MIN_TOKENS:
        return []
    need = _TOKEN_HIT_RATIO * len(tokens)
    return [p for p, t in texts.items()
            if t and sum(1 for tok in tokens if tok in t) >= need]


def _resolve(hits_by_key: Dict[Tuple[str, str], List[int]],
             claimed: Dict[Tuple[str, str], int]) -> Dict[Tuple[str, str], int]:
    """Pick one page per value: the one sharing the page with the most other values, with the
    model's own citation breaking ties, then the earliest page."""
    weight: Dict[int, int] = {}
    for pages in hits_by_key.values():
        for p in pages:
            weight[p] = weight.get(p, 0) + 1
    chosen: Dict[Tuple[str, str], int] = {}
    for key, pages in hits_by_key.items():
        if not pages:
            continue
        claim = claimed.get(key)
        chosen[key] = min(pages, key=lambda p: (-weight.get(p, 0), p != claim, p))
    return chosen


def reattribute(page_extractions: List[dict], doc_paths: Dict[str, str]) -> Tuple[List[dict], Dict[str, int]]:
    """Rebuild page extractions with verified page numbers. Returns (records, stats).

    Values are never changed, added or dropped — only the page each one is filed under, and only
    when the document's own text proves a different page."""
    stats = {"values": 0, "verified": 0, "moved": 0, "unverified": 0}
    if not page_extractions:
        return [], stats

    by_doc: Dict[str, List[dict]] = {}
    for px in page_extractions:
        if isinstance(px, dict):
            by_doc.setdefault(px.get("document_id") or "", []).append(px)

    rebuilt: List[dict] = []
    for doc_id, records in by_doc.items():
        texts = _page_texts(doc_paths.get(doc_id, ""))

        # every distinct value this document produced, and where the model said it was
        hits_by_key: Dict[Tuple[str, str], List[int]] = {}
        claimed: Dict[Tuple[str, str], int] = {}
        origin: Dict[Tuple[str, str], dict] = {}
        for px in records:
            page = px.get("page_number")
            page = int(page) if str(page).isdigit() else 0
            for field, value in (px.get("fields") or {}).items():
                if field.startswith("_") or value in (None, "", "—"):
                    continue
                key = (field, _norm(value))
                claimed.setdefault(key, page)
                origin.setdefault(key, px)
                if key not in hits_by_key:
                    hits_by_key[key] = _pages_containing(value, texts) if texts else []

        resolved = _resolve(hits_by_key, claimed)

        # one record per page, values merged onto the page they were proved to be on
        pages: Dict[int, dict] = {}
        for key, src in origin.items():
            field = key[0]
            value = (src.get("fields") or {}).get(field)
            verified = key in resolved
            page = resolved[key] if verified else claimed.get(key, 0)
            stats["values"] += 1
            if verified:
                stats["verified"] += 1
                if page != claimed.get(key):
                    stats["moved"] += 1
            else:
                stats["unverified"] += 1

            rec = pages.get(page)
            if rec is None:
                rec = pages[page] = {
                    "document_id": doc_id,
                    "filename": src.get("filename", ""),
                    "page_number": page,
                    "fields": {},
                    "field_scripts": {},
                    "field_sources": {},
                    "ocr_route": src.get("ocr_route", "vlm"),
                    "telemetry": src.get("telemetry", {}),
                    "mismatches": [],
                    "raw_response": src.get("raw_response", ""),
                    "raw_ocr_text": src.get("raw_ocr_text", ""),
                    "ocr_confidence": src.get("ocr_confidence", 1.0),
                }
            rec["fields"][field] = value
            rec["field_sources"][field] = "text_layer" if verified else "model"
            # the chunk's script flags follow the field to whichever page it lands on, instead of
            # every page of the chunk carrying the whole chunk's flags
            fs = src.get("field_scripts") or {}
            if field in fs:
                rec["field_scripts"][field] = fs[field]

        rebuilt.extend(pages[p] for p in sorted(pages))

    return rebuilt, stats
