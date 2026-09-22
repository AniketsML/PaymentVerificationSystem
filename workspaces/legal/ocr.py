"""
OCR / VLM extraction engine for the Legal Pipeline.

Handles:
  - Page number stamping on PDFs before sending to the VLM
  - Rendering PDF pages as images
  - Calling Medha VLM API (primary) with Gemini fallback
  - Language detection for routing Indic scripts to Gemini
  - Extraction caching via legal_ocr_cache table
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

# The model does not always spell its bookkeeping with the leading underscore it was asked for;
# canonical_meta moves "field_scripts" to "_field_scripts" etc. at parse time. See meta.py.
from workspaces.legal.meta import META_KEYS, canonical_meta as _canonical_meta  # noqa: F401


# ── Helpers ────────────────────────────────────────────────────────────────

def _image_to_bytes(image: Image.Image, fmt: str = "JPEG") -> bytes:
    """Convert a PIL Image to bytes."""
    buf = io.BytesIO()
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")
    image.save(buf, format=fmt, quality=90)
    return buf.getvalue()


def _parse_json(raw: str) -> Dict[str, Any]:
    """Robustly parse JSON from LLM output (handles code fences, embedded text)."""
    raw = raw.strip()
    try:
        return _canonical_meta(json.loads(raw))
    except json.JSONDecodeError:
        pass
    # Try markdown code fences
    m = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return _canonical_meta(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    # Try finding raw JSON object
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return _canonical_meta(json.loads(raw[start:end + 1]))
        except json.JSONDecodeError:
            pass
    return {"_parse_error": True, "_raw_text": raw[:500]}


# ── PDF Utilities ──────────────────────────────────────────────────────────

def print_page_numbers_on_pdf(file_path: str) -> str:
    """
    Stamp page numbers (PAGE 1, PAGE 2, ...) on each page of a PDF.

    Creates a temporary copy with page numbers stamped in red.
    Returns the path to the stamped temp file.
    """
    try:
        import fitz
    except ImportError:
        sys.stderr.write("[ocr] PyMuPDF (fitz) not installed, skipping page stamping\n")
        return file_path

    try:
        doc = fitz.open(file_path)
        for page_idx in range(doc.page_count):
            page = doc[page_idx]
            try:
                page.insert_text(
                    fitz.Point(10, 50),
                    f"PAGE {page_idx + 1}",
                    fontsize=48,
                    color=(1, 0, 0),
                )
            except Exception:
                pass

        # Save to temp file
        temp_fd, temp_path = tempfile.mkstemp(suffix=".pdf", prefix="stamped_")
        os.close(temp_fd)
        doc.save(temp_path)
        doc.close()
        return temp_path
    except Exception as e:
        sys.stderr.write(f"[ocr] page stamp error: {e}\n")
        return file_path


def render_pdf_page(file_path: str, page_idx: int, dpi: int = 200) -> Optional[Image.Image]:
    """Render a single PDF page as a PIL Image at the given DPI."""
    try:
        import fitz
        doc = fitz.open(file_path)
        if page_idx < 0 or page_idx >= doc.page_count:
            doc.close()
            return None
        page = doc[page_idx]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()
        return img
    except ImportError:
        sys.stderr.write("[ocr] PyMuPDF (fitz) not installed\n")
        return None
    except Exception as e:
        sys.stderr.write(f"[ocr] PDF page render error: {e}\n")
        return None


# ── Language Detection ─────────────────────────────────────────────────────

_INDIC_RANGES = [
    (0x0900, 0x097F),  # Devanagari (Hindi, Marathi, Sanskrit)
    (0x0980, 0x09FF),  # Bengali
    (0x0A00, 0x0A7F),  # Gurmukhi (Punjabi)
    (0x0A80, 0x0AFF),  # Gujarati
    (0x0B00, 0x0B7F),  # Oriya
    (0x0B80, 0x0BFF),  # Tamil
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
    (0x0D00, 0x0D7F),  # Malayalam
]


def detect_language(text: str) -> str:
    """
    Simple heuristic: check if text contains significant Indic script characters.

    Returns 'indic' if more than 5% of characters are Indic, else 'english'.
    """
    if not text:
        return "english"
    indic_count = 0
    total = 0
    for ch in text:
        cp = ord(ch)
        if cp > 127:
            total += 1
            if any(lo <= cp <= hi for lo, hi in _INDIC_RANGES):
                indic_count += 1
    if indic_count > 0 and (indic_count / max(len(text), 1)) > 0.05:
        return "indic"
    return "english"


# ── Cache Layer ────────────────────────────────────────────────────────────

# Cache layer removed per user request


# ── The extraction prompt ──────────────────────────────────────────────────
# What changed, and why, measured on real runs:
#   - the old rule "preserve the exact text as written" invited whole party clauses into name
#     fields ("Ganesh Singh s/o Nainsingh, caste - Rajpoot, age - 32 years, resident - …")
#   - with no field list the model named its own keys: 108 distinct fields in one run
#   - vernacular text came back in its own script unless the user remembered to ask otherwise
#   - three separate bookkeeping maps (pages, sources, scripts) are now one evidence block per
#     field, which also carries whether the value was legible — the review queue's main signal
_PROMPT_RULES = """RULES
1. Return one JSON object and nothing else — no markdown, no commentary.
2. PARTIES
   - borrower_*: the primary borrower only — exactly one person or one firm.
   - co_borrower_N_*: every co-applicant or joint borrower, numbered from 1. Use the same number
     for a person's name and address.
   - Never the lender — no bank, NBFC or housing finance company is ever a borrower.
   - A person is the borrower or a co-borrower, never both.
   - Sellers, previous owners, witnesses, advocates, officials and neighbours named in deeds are
     NOT parties to the loan. Never put them in borrower or co-borrower keys.
3. NAMES: the name only. Leave out honorifics (Mr, Mrs, Shri, Smt, Sh, M/s), the relation clause
   (S/O, W/O, D/O, son of, wife of …), age, caste, occupation, residence and ID numbers.
   Report a relation clause in the evidence block instead.
4. ENGLISH: write every value in English. Transliterate names and addresses into English letters —
   do not translate their meaning (रामप्रसाद → Ramprasad, not "God Prasad"). Translate
   descriptive text into English. When the source was not in English, put the original text in
   the evidence block.
5. Amounts and numbers exactly as written. Dates exactly as written.
6. HEADING: if the instructions or focus areas name a heading (e.g. "Schedule"), extract only from
   pages under that heading — not from powers of attorney, general terms or boilerplate.
7. If nothing requested is on these pages, return {}.
8. EVIDENCE (mandatory): for every field you return, add an entry to "_field_evidence":
   "_field_evidence": {"borrower_name": {"page": 33, "script": "printed", "legibility": "clear"},
                       "co_borrower_1_name": {"page": 34, "script": "handwritten", "legibility": "partial",
                                              "issue": "faint ink", "relation": "W/O Mohan Lal",
                                              "original": "सीता देवी"}}
   - page: the number in the red PAGE badge on the image you read it from — never a page number
     printed on the document itself.
   - script: "printed" (machine-printed or typed) or "handwritten".
   - legibility: "clear" when you are certain of every character; "partial" when part of it was
     hard to read (blur, faint or low-contrast ink, a stamp or signature over it, a fold, cut off
     at the edge, glare, a dark or skewed photo); "illegible" when you are guessing.
   - issue: when legibility is not "clear", the reason in a few words.
   - relation: names only — the relation clause you left out of the name.
   - original: only when the source was not in English — the text in its own script.
   Judge every field on its own. Be honest about legibility: a value marked "clear" is trusted
   without a human checking it."""


def build_extraction_prompt(document_type: str, page_info: str, kw_clause: str = "",
                            extraction_prompt: str = "", schema_block: str = "") -> str:
    fields = (f"FIELDS\n{schema_block}\n\n" if schema_block else
              "FIELDS\nReturn what the instructions ask for, as flat snake_case keys "
              "(borrower_name, co_borrower_1_name, …). No nested objects or arrays.\n\n")
    return (
        "You extract data from the pages of an Indian loan dossier (SARFAESI).\n\n"
        f"Document: {document_type} ({page_info}).{kw_clause}\n"
        "Each image carries a red PAGE badge with its page number.\n\n"
        f"WHAT THE USER ASKED FOR\n\"{extraction_prompt}\"\n\n"
        f"{fields}"
        f"{_PROMPT_RULES}\n\nReturn ONLY the JSON object."
    )


# ── VLM Client ─────────────────────────────────────────────────────────────

class LegalVLMClient:
    """
    VLM extraction client for legal documents.

    Builds the extraction prompt; which model reads the pages — Medha, Gemini, or the English /
    other-language pair chosen in Model settings — is decided per batch in llm.py.
    """

    def __init__(self):
        from config import runtime
        self._cfg = runtime.model_config()

    def extract_from_images(
        self,
        images: List[Image.Image],
        document_type: str,
        fields_to_extract: List[str] = None,
        page_numbers: List[int] = None,
        extraction_prompt: str = "",
        heading_hint: str = "",
        keywords: List[str] = None,
        headings: List[str] = None,
        total_pages: int = 0,
        schema_block: str = "",
        language_hint: str = "unknown",
    ) -> Dict[str, Any]:
        """
        Extract structured data from multiple document page images in a single call.

        `schema_block` is the run's closed field list (field_schema.RunSchema.prompt_block()). With
        it the model may only return those keys; without it (no prompt) it names its own.
        """
        if not images:
            return {}
            
        imgs_bytes = [_image_to_bytes(img) for img in images]
        cfg = self._cfg
        
        pages_str = ", ".join(str(p) for p in (page_numbers or [1]))
        page_info = f"Pages {pages_str}" if not total_pages else f"Pages {pages_str} of {total_pages}"
        
        kw_parts = []
        if heading_hint:
            kw_parts.append(f"Target Heading: {heading_hint}")
        if headings:
            kw_parts.append(f"Headings: {', '.join(headings)}")
        if keywords:
            kw_parts.append(f"Key Terms/Keywords: {', '.join(keywords)}")
        kw_clause = f"\nFocus Areas: {'; '.join(kw_parts)}\n" if kw_parts else ""

        prompt = build_extraction_prompt(
            document_type=document_type, page_info=page_info, kw_clause=kw_clause,
            extraction_prompt=extraction_prompt, schema_block=schema_block)

        # which model reads this batch — one model, or the English / other-language pair — and the
        # calls themselves live in llm.py; the reply keeps the contract the pipeline relies on
        # (_meta, _ocr_route, _raw_response, _error) and adds _calls and _routing
        from workspaces.legal import llm
        return llm.extract(prompt, imgs_bytes, language=language_hint or "unknown")


# ── Breaker status stub (for metrics.py compatibility) ─────────────────────

def get_breaker_status() -> dict:
    """Return circuit breaker status (simplified for new pipeline)."""
    return {"state": "closed", "failures": 0, "cooldown_remaining": 0}
