"""
Script provenance for extracted legal values — "was this value typed or handwritten?"

Two independent sources, strongest first:

  1. THE MODEL (runs extracted after `_field_scripts` was added to the extraction prompt).
     The VLM looks at the page and reports, per field, `handwritten` or `printed`. This is
     the only source that can actually recognise handwriting.

  2. THE PDF TEXT LAYER (works on every run, including ones extracted earlier). A value that
     appears in the page's embedded text was set as machine-readable text -> `typed`. A value
     that is not in the text layer was read from pixels -> `scanned`: a scan or a photo, which
     MAY be handwriting but cannot be proven so from the text layer alone.

So a field ends up as one of:
    handwritten  model saw handwriting
    printed      model saw machine print
    typed        value found in the PDF's own text layer
    scanned      value only in the image (scan/photo — possibly handwritten)
    unknown      no page/document to check (nothing is guessed)

A dossier with no extracted values at all is tagged `none` ("No values") — there is nothing
to judge, and saying "unchecked" would hide that the extraction came back empty.

Nothing here writes to the extraction pipeline: it reads `legal_leads.extracted_data`
(the page extractions the pipeline already stored) plus the source documents, and caches the
verdict per lead in `legal_field_provenance`, keyed by a fingerprint of the extraction it was
computed from, so re-extracting a lead invalidates it automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from psycopg.types.json import Jsonb

from db import pg

# display order: the strongest claim about a lead wins
TAG_RANK = {"handwritten": 4, "scanned": 3, "printed": 2, "typed": 1, "unknown": 0}
LEAD_TAGS = ("handwritten", "scanned", "typed", "unknown", "none")
_VERSION = "2"                # bump when the tagging rules change, so cached verdicts recompute

_MIN_PAGE_CHARS = 40          # below this a page carries no usable text layer (it is an image)
_TOKEN_HIT_RATIO = 0.75       # share of a value's words that must appear to count as "typed"
_WS = re.compile(r"[^a-z0-9]+")


def _norm(s: Any) -> str:
    return _WS.sub(" ", str(s or "").lower()).strip()


def _fingerprint(page_extractions: List[dict], updated_at) -> str:
    seed = json.dumps(
        [[p.get("document_id"), p.get("page_number"), sorted((p.get("fields") or {}).keys()),
          sorted((p.get("field_scripts") or {}).items())] for p in page_extractions],
        sort_keys=True, default=str) + str(updated_at) + _VERSION
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _page_text(path: str, page_number: int) -> Optional[str]:
    """The embedded text of one 1-indexed page, or None when it can't be read."""
    if not path or not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext != ".pdf":
        return "" if ext in (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif") else None
    try:
        import fitz
        with fitz.open(path) as doc:
            idx = int(page_number) - 1
            if idx < 0 or idx >= doc.page_count:
                return None
            return doc[idx].get_text("text") or ""
    except Exception as e:  # noqa: BLE001 — a damaged file must not break the dossier view
        sys.stderr.write(f"[provenance] text layer read failed for {path} p{page_number}: {e}\n")
        return None


def _value_in_text(value: Any, page_norm: str) -> bool:
    v = _norm(value)
    if not v or not page_norm:
        return False
    if v in page_norm:
        return True
    tokens = [t for t in v.split() if len(t) > 2]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in page_norm)
    return hits / len(tokens) >= _TOKEN_HIT_RATIO


def _model_script(field: str, scripts: Dict[str, Any]) -> str:
    """The model's own verdict for a field, matching normalised/renamed keys leniently."""
    if not scripts:
        return ""
    raw = scripts.get(field)
    if raw is None:
        f = _norm(field).replace(" ", "_")
        for k, v in scripts.items():
            nk = _norm(k).replace(" ", "_")
            if nk == f or nk in f or f in nk:
                raw = v
                break
    val = _norm(raw)
    if "hand" in val:
        return "handwritten"
    if "print" in val or "typed" in val or "machine" in val:
        return "printed"
    return ""


def _lead_tag(fields: Dict[str, dict]) -> str:
    tag = "unknown"
    for info in fields.values():
        s = info.get("script", "unknown")
        s = "typed" if s == "printed" else s
        if TAG_RANK.get(s, 0) > TAG_RANK.get(tag, 0):
            tag = s
    return tag


def _load_lead(lead_id: str) -> Tuple[Optional[dict], List[dict], Dict[str, str]]:
    with pg.pool().connection() as c:
        lead = c.execute(
            "SELECT lead_id, extracted_data, updated_at FROM legal_leads WHERE lead_id=%s",
            (lead_id,)).fetchone()
        docs = c.execute(
            "SELECT document_id, file_path FROM legal_lead_documents WHERE lead_id=%s",
            (lead_id,)).fetchall()
    if not lead:
        return None, [], {}
    ed = lead.get("extracted_data")
    if isinstance(ed, str):
        try:
            ed = json.loads(ed)
        except Exception:
            ed = {}
    ed = ed or {}
    pxs = ed.get("_page_extractions") or ed.get("page_extractions") or []
    return lead, [p for p in pxs if isinstance(p, dict)], {d["document_id"]: d["file_path"] for d in docs}


def analyze(lead_id: str, force: bool = False) -> Dict[str, Any]:
    """Provenance for one lead: {lead_id, tag, fields:{"<page>|<field>": {...}}, pages:[...]}.
    Cached per lead; recomputed when the lead's extraction changes."""
    lead, pxs, doc_paths = _load_lead(lead_id)
    if not lead:
        return {"lead_id": lead_id, "tag": "unknown", "fields": {}, "pages": [], "counts": {}}

    fp = _fingerprint(pxs, lead.get("updated_at"))
    if not force:
        with pg.pool().connection() as c:
            hit = c.execute(
                "SELECT tag, fields, counts FROM legal_field_provenance WHERE lead_id=%s AND fingerprint=%s",
                (lead_id, fp)).fetchone()
        if hit:
            return {"lead_id": lead_id, "tag": hit["tag"], "fields": hit["fields"] or {},
                    "counts": hit["counts"] or {}, "cached": True}

    fields: Dict[str, dict] = {}
    pages: List[dict] = []
    text_cache: Dict[Tuple[str, int], Optional[str]] = {}

    for px in pxs:
        doc_id = px.get("document_id") or ""
        page_no = px.get("page_number")
        scripts = px.get("field_scripts") or {}
        key = (doc_id, int(page_no) if str(page_no).isdigit() else -1)
        if key not in text_cache:
            text_cache[key] = _page_text(doc_paths.get(doc_id, ""), key[1])
        raw_text = text_cache[key]
        page_norm = _norm(raw_text) if raw_text else ""
        has_layer = bool(raw_text) and len(raw_text.strip()) >= _MIN_PAGE_CHARS
        pages.append({"document_id": doc_id, "page_number": key[1],
                      "has_text_layer": has_layer,
                      "chars": len(raw_text.strip()) if raw_text else 0,
                      "readable": raw_text is not None})

        for field, value in (px.get("fields") or {}).items():
            if field.startswith("_") or value in (None, "", "—"):
                continue
            model = _model_script(field, scripts)
            if model:
                script, source = model, "model"
            elif raw_text is None:
                script, source = "unknown", "unavailable"
            elif has_layer and _value_in_text(value, page_norm):
                script, source = "typed", "text_layer"
            else:
                script, source = "scanned", "text_layer"
            fields[f"{key[1]}|{field}"] = {
                "field": field, "page_number": key[1], "document_id": doc_id,
                "script": script, "source": source,
            }

    counts: Dict[str, int] = {}
    for info in fields.values():
        counts[info["script"]] = counts.get(info["script"], 0) + 1
    tag = _lead_tag(fields) if fields else "none"

    try:
        with pg.pool().connection() as c:
            c.execute(
                "INSERT INTO legal_field_provenance(lead_id, fingerprint, tag, fields, counts, computed_at) "
                "VALUES(%s,%s,%s,%s,%s,now()) ON CONFLICT (lead_id) DO UPDATE SET "
                "fingerprint=EXCLUDED.fingerprint, tag=EXCLUDED.tag, fields=EXCLUDED.fields, "
                "counts=EXCLUDED.counts, computed_at=now()",
                (lead_id, fp, tag, Jsonb(fields), Jsonb(counts)))
    except Exception as e:  # noqa: BLE001 — the cache is an optimisation, never a hard failure
        sys.stderr.write(f"[provenance] cache write failed for {lead_id}: {e}\n")

    return {"lead_id": lead_id, "tag": tag, "fields": fields, "counts": counts,
            "pages": pages, "cached": False}


def scan(lead_ids: List[str], limit: int = 40) -> Dict[str, str]:
    """Tags for a batch of leads (computing the missing ones). Used by the dashboard."""
    out: Dict[str, str] = {}
    for lead_id in list(lead_ids)[:limit]:
        try:
            out[lead_id] = analyze(lead_id)["tag"]
        except Exception as e:  # noqa: BLE001 — one bad dossier must not fail the table
            sys.stderr.write(f"[provenance] scan failed for {lead_id}: {e}\n")
            out[lead_id] = "unknown"
    return out


def tag_counts(scope: str = "real") -> List[dict]:
    """Lead-tag distribution for the Observability view (cached rows only)."""
    where = "l.is_test = false" if scope == "real" else ("l.is_test = true" if scope == "test" else "TRUE")
    with pg.pool().connection() as c:
        rows = c.execute(
            f"SELECT p.tag, count(*) AS n FROM legal_field_provenance p "
            f"JOIN legal_leads l ON l.lead_id = p.lead_id WHERE {where} GROUP BY 1 ORDER BY 2 DESC").fetchall()
    return [{"tag": r["tag"], "n": r["n"]} for r in rows]
