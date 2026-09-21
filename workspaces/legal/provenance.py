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

BLUR is measured separately and sits ALONGSIDE that verdict rather than replacing it, so a
value can be "handwritten, on a blurry page". A page with no text layer is rendered and scored
by the average strength of its sharpest 1% of edges — which, unlike a plain Laplacian variance,
does not collapse just because a page is mostly white space. Every page is normalised to the
same pixel height first, so the score means the same thing for a digital PDF, a 200-DPI scan
and a phone photo. Only pages carrying enough ink are judged, and only the first few per
dossier, because rendering is by far the slowest thing here (~100 ms a page).

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

# display order: the strongest claim about a lead wins. Blur is NOT in here — it never
# replaces a dossier's script verdict, it rides alongside it as counts["blurred"], so a soft
# scan of a handwritten form still reads "Handwritten" and is still findable under "Scanned".
TAG_RANK = {"handwritten": 4, "scanned": 3, "printed": 2, "typed": 1, "unknown": 0}
LEAD_TAGS = ("handwritten", "scanned", "typed", "unknown", "none")
_VERSION = "3"                # bump when the tagging rules change, so cached verdicts recompute

# a page whose sharpest edges are softer than this reads as blurry. Calibrated on 42 real
# image-only pages: crisp scans score ~240, ordinary ones ~157, the softest phone scans ~97.
BLUR_EDGE_MAX = float(os.environ.get("LEGAL_BLUR_EDGE_MAX", "120"))
_MIN_INK = 0.005              # below this the page is too empty to judge sharpness at all
_BLUR_TARGET_PX = 1200        # every page is rendered to this height, so scores compare
_MAX_BLUR_PAGES = 4           # enough to tell whether a dossier is soft; caps the render cost

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif")
_MIN_PAGE_CHARS = 40          # below this a page carries no usable text layer (it is an image)
_TOKEN_HIT_RATIO = 0.75       # share of a value's words that must appear to count as "typed"
_WS = re.compile(r"[^a-z0-9]+")


def _norm(s: Any) -> str:
    return _WS.sub(" ", str(s or "").lower()).strip()


def _fingerprint(page_extractions: List[dict], updated_at) -> str:
    """Keyed on the extraction, not on the document bytes: swapping a source file for a
    different scan of the same pages without re-extracting keeps the cached verdict."""
    seed = json.dumps(
        [[p.get("document_id"), p.get("page_number"), sorted((p.get("fields") or {}).keys()),
          sorted((p.get("field_scripts") or {}).items())] for p in page_extractions],
        sort_keys=True, default=str) + str(updated_at) + _VERSION
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _sharpness(page) -> Optional[Tuple[float, float]]:
    """(edge strength, ink fraction) for one open page, or None when it can't be measured.

    The page is first rendered to a fixed pixel height. Without that the score would depend on
    the file's own resolution — the same page scores 143 as a PDF but 95 as a 200-DPI JPEG if
    each is measured at its native size."""
    try:
        import fitz
        import numpy as np
        zoom = _BLUR_TARGET_PX / max(1.0, page.rect.height)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.float32)
        if a.size < 16:
            return None
        ink = float((a < (np.percentile(a, 90) - 40)).mean())      # marks, against the paper white
        g = np.maximum(np.abs(np.diff(a, axis=1))[:-1, :], np.abs(np.diff(a, axis=0))[:, :-1])
        if g.size == 0:
            return None
        strong = g > np.percentile(g, 99)                          # the page's own sharpest edges
        return (float(g[strong].mean()) if strong.any() else 0.0), ink
    except Exception as e:  # noqa: BLE001 — a page we cannot render simply goes unjudged
        sys.stderr.write(f"[provenance] sharpness failed: {e}\n")
        return None


def _read_pages(doc_paths: Dict[str, str], wanted: Dict[str, List[int]]) -> Dict[Tuple[str, int], dict]:
    """Text layer — and, for pages that have none, sharpness — for every cited page.

    Each document is opened once. Only image-only pages are rendered (a page with a text layer
    was born digital), and only the first `_MAX_BLUR_PAGES` of a dossier, so a big scanned
    dossier costs a few hundred milliseconds rather than tens of seconds."""
    facts: Dict[Tuple[str, int], dict] = {}
    budget = _MAX_BLUR_PAGES
    for doc_id, page_numbers in wanted.items():
        path = doc_paths.get(doc_id, "")
        ext = os.path.splitext(path)[1].lower() if path else ""
        if not path or not os.path.exists(path) or (ext != ".pdf" and ext not in _IMAGE_EXT):
            for pn in page_numbers:
                facts[(doc_id, pn)] = {"text": None}
            continue
        try:
            import fitz
            with fitz.open(path) as doc:
                for pn in page_numbers:
                    idx = pn - 1
                    if idx < 0 or idx >= doc.page_count:
                        facts[(doc_id, pn)] = {"text": None}
                        continue
                    page = doc[idx]
                    text = page.get_text("text") or ""
                    fact: Dict[str, Any] = {"text": text}
                    if len(text.strip()) < _MIN_PAGE_CHARS and budget > 0:
                        budget -= 1
                        m = _sharpness(page)
                        if m and m[1] >= _MIN_INK:      # too little ink => no verdict, not "blurry"
                            fact["edge"] = round(m[0], 1)
                            fact["blurred"] = m[0] < BLUR_EDGE_MAX
                    facts[(doc_id, pn)] = fact
        except Exception as e:  # noqa: BLE001 — a damaged file must not break the dossier view
            sys.stderr.write(f"[provenance] page read failed for {path}: {e}\n")
            for pn in page_numbers:
                facts.setdefault((doc_id, pn), {"text": None})
    return facts


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

    wanted: Dict[str, List[int]] = {}
    for px in pxs:
        doc_id = px.get("document_id") or ""
        page_no = px.get("page_number")
        pn = int(page_no) if str(page_no).isdigit() else -1
        if pn not in wanted.setdefault(doc_id, []):
            wanted[doc_id].append(pn)
    facts = _read_pages(doc_paths, wanted)

    for px in pxs:
        doc_id = px.get("document_id") or ""
        page_no = px.get("page_number")
        scripts = px.get("field_scripts") or {}
        key = (doc_id, int(page_no) if str(page_no).isdigit() else -1)
        fact = facts.get(key) or {"text": None}
        raw_text = fact.get("text")
        blurred = bool(fact.get("blurred"))
        page_norm = _norm(raw_text) if raw_text else ""
        has_layer = bool(raw_text) and len(raw_text.strip()) >= _MIN_PAGE_CHARS
        pages.append({"document_id": doc_id, "page_number": key[1],
                      "has_text_layer": has_layer,
                      "chars": len(raw_text.strip()) if raw_text else 0,
                      "readable": raw_text is not None,
                      "blurred": blurred, "sharpness": fact.get("edge")})

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
                "script": script, "source": source, "blurred": blurred,
            }

    # `blurred` cuts across the script counts rather than adding to them: a value can be both
    # handwritten and on a blurry page, so these must not be summed into a single total.
    counts: Dict[str, int] = {}
    for info in fields.values():
        counts[info["script"]] = counts.get(info["script"], 0) + 1
        if info.get("blurred"):
            counts["blurred"] = counts.get("blurred", 0) + 1
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
