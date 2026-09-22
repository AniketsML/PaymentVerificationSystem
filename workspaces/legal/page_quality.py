"""
Is this page image good enough to trust what was read from it?

Measured on the image-only pages values were actually read from (239 pages, 2026-09-22), and
every threshold checked by eye at the resolution the model receives:

    Blurry           sharpest-edge strength < 95   soft photocopies and phone shots; 122+ is crisp
    Faint print      darkest ink lighter than 110  light-grey print on white
    Blank page       essentially no ink            a value "read" here was mis-cited or invented
    Dark             paper darker than 160         (none in the corpus so far — objective floor)
    Low resolution   source image under 100 DPI    too few pixels per character
    Rotated/skewed   text lines >= 5 degrees off   (a page turned fully sideways is judged by the model)

What deliberately is NOT here: whole-page contrast. It is dominated by stamp-paper watermarks,
which are textured by design, not faint. Semantic problems — a stamp or signature over the text,
a fold, a noisy card background — are judged by the model per field (its legibility verdict),
which is the other half of the trust rule.

Pages with a text layer are not judged: a value confirmed in the document's own text cannot
have been misread from the image.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

TARGET_PX = 1200              # every page is judged at the same height, so thresholds compare
THRESHOLDS = {
    "edge_min": 95.0,
    "faint_ink": 110.0,
    "blank_ink": 240.0,
    "dark_paper": 160.0,
    "min_dpi": 100.0,
    "skew_max": 5,
}
LABELS = {
    "blurry": "Blurry",
    "faint": "Faint print",
    "blank": "Blank page",
    "dark": "Dark",
    "low_resolution": "Low resolution",
    "rotated": "Rotated or skewed",
}
_MIN_TEXT_CHARS = 40
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif")


def _effective_dpi(page) -> Optional[float]:
    """Pixels per inch of the image that makes up the page, when one image covers most of it."""
    try:
        infos = page.get_image_info()
    except Exception:  # noqa: BLE001
        return None
    page_area = max(1.0, page.rect.width * page.rect.height)
    best = None
    for info in infos or []:
        x0, y0, x1, y1 = info.get("bbox", (0, 0, 0, 0))
        area = max(0.0, (x1 - x0) * (y1 - y0))
        if area >= 0.5 * page_area and (best is None or area > best[0]):
            best = (area, info, x1 - x0, y1 - y0)
    if best is None:
        return None
    _, info, bw, bh = best
    if bw <= 0 or bh <= 0:
        return None
    return min(info.get("width", 0) / (bw / 72.0), info.get("height", 0) / (bh / 72.0))


def measure(page) -> Dict[str, Any]:
    """The raw signals for one open page."""
    import fitz
    import numpy as np
    from PIL import Image

    zoom = TARGET_PX / max(1.0, page.rect.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.float32)
    g = np.maximum(np.abs(np.diff(a, axis=1))[:-1, :], np.abs(np.diff(a, axis=0))[:, :-1])
    strong = g > np.percentile(g, 99)
    paper = float(np.percentile(a, 90))
    ink = float(np.percentile(a, 0.5))           # the darkest strokes, even on a sparse page

    # orientation: the rotation that lines text up in rows, on a small binarised copy
    small = Image.fromarray(a.astype(np.uint8)).resize((max(1, int(a.shape[1] * 500 / a.shape[0])), 500))
    binar = ((np.asarray(small) < paper - 40) * 255).astype(np.uint8)
    # a solid black band — the scanner lid's edge, a dark margin — is not text; left in, one such
    # band down the side makes an upright page look sideways
    binar[:, (binar > 0).mean(axis=0) > 0.5] = 0
    binar[(binar > 0).mean(axis=1) > 0.5, :] = 0
    b = Image.fromarray(binar)
    best_angle, best_var = 0, -1.0
    for angle in range(-8, 9):
        var = float(np.asarray(b.rotate(angle, fillcolor=0)).sum(axis=1).var())
        if var > best_var:
            best_angle, best_var = angle, var
    # No sideways test on purpose: projection profiles cannot tell orientation on pages made of card
    # photos and handwriting (tried three formulations on the corpus; each flagged upright KYC pages).
    # A page turned 90 degrees is left to the model's per-field legibility verdict instead.
    dpi = _effective_dpi(page)
    return {
        "edge": round(float(g[strong].mean()) if strong.any() else 0.0, 1),
        "paper": round(paper, 1), "ink": round(ink, 1),
        "ink_share": round(float((a < paper - 40).mean()), 4),
        "skew": best_angle,
        "dpi": round(dpi, 1) if dpi else None,
    }


def judge(m: Dict[str, Any]) -> List[str]:
    """Quality flags (keys of LABELS) for measured signals."""
    t = THRESHOLDS
    flags: List[str] = []
    if m.get("ink", 0) >= t["blank_ink"] or m.get("ink_share", 1) < 0.001:
        return ["blank"]                          # nothing else means anything on a blank page
    if m.get("edge", 999) < t["edge_min"]:
        flags.append("blurry")
    if m.get("ink", 0) > t["faint_ink"]:
        flags.append("faint")
    if m.get("paper", 255) < t["dark_paper"]:
        flags.append("dark")
    if m.get("dpi") is not None and m["dpi"] < t["min_dpi"]:
        flags.append("low_resolution")
    if abs(m.get("skew", 0)) >= t["skew_max"]:
        flags.append("rotated")
    return flags


def assess(doc_paths: Dict[str, str], wanted: Dict[str, List[int]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """{document_id: {"page": {"flags": [...], "metrics": {...}, "text_layer": bool}}} for the
    cited pages. Page numbers are stored as strings so the result survives a JSON round trip
    unchanged. Each document is opened once; a page that can't be read is simply left out."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for doc_id, pages in wanted.items():
        path = doc_paths.get(doc_id, "")
        ext = os.path.splitext(path)[1].lower() if path else ""
        if not path or not os.path.exists(path) or (ext != ".pdf" and ext not in _IMAGE_EXT):
            continue
        try:
            import fitz
            with fitz.open(path) as doc:
                for pn in sorted(set(pages)):
                    if not (0 <= pn - 1 < doc.page_count):
                        continue
                    page = doc[pn - 1]
                    if len((page.get_text("text") or "").strip()) >= _MIN_TEXT_CHARS:
                        out.setdefault(doc_id, {})[str(pn)] = {"flags": [], "text_layer": True}
                        continue
                    m = measure(page)
                    out.setdefault(doc_id, {})[str(pn)] = {"flags": judge(m), "metrics": m,
                                                          "text_layer": False}
        except Exception as e:  # noqa: BLE001 — an unreadable document goes unjudged, not failed
            sys.stderr.write(f"[page_quality] could not assess {path}: {e}\n")
    return out


def page_flags(quality: Dict[str, Any], doc_id: str, page: Any) -> List[str]:
    return list(((quality or {}).get(doc_id) or {}).get(str(page), {}).get("flags") or [])
