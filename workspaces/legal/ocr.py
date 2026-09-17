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
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try markdown code fences
    m = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding raw JSON object
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
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


# ── Extraction Prompt Builder ──────────────────────────────────────────────

def _build_extraction_prompt(
    document_type: str,
    fields_to_extract: List[str],
    page_number: int,
    user_prompt: str = "",
    heading_hint: str = "",
) -> str:
    """Build the prompt that instructs the VLM to extract specific fields."""
    fields_str = ", ".join(fields_to_extract)

    prompt = (
        f"You are a legal document extraction specialist for SARFAESI loan dossiers.\n"
        f"This is page {page_number} of a '{document_type}' document.\n\n"
        f"Extract the following fields from this document image:\n{fields_str}\n\n"
        f"IMPORTANT RULES:\n"
        f"1. Return a JSON object with keys EXACTLY matching the field names listed above.\n"
        f"2. For currency amounts, return the numeric value (e.g. 4500000, not '₹45,00,000').\n"
        f"3. For dates, return in DD/MM/YYYY format where possible.\n"
        f"4. For names and addresses, preserve the exact text as written in the document.\n"
        f"5. If a field is not found on this page, set its value to null.\n"
        f"6. You MUST extract 'account_no_lan' (Loan Account Number) if present.\n"
        f"7. You MUST extract 'applicant_name' (Primary Borrower name) if present.\n"
    )

    if heading_hint:
        prompt += f"8. CRITICAL CONSTRAINT: You must ONLY extract these fields from the section under the heading '{heading_hint}'. Ignore any matching information found elsewhere on the page.\n"

    if user_prompt:
        prompt += f"\nAdditional user instructions: {user_prompt}\n"

    prompt += "\nReturn ONLY the JSON object, no explanations."
    return prompt


# ── VLM Client ─────────────────────────────────────────────────────────────

class LegalVLMClient:
    """
    VLM extraction client for legal documents.

    Routes to Medha API (primary) or Gemini (fallback / Indic script).
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
    ) -> Dict[str, Any]:
        """
        Extract structured data from multiple document page images in a single call.
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

        prompt = (
            f"You are a professional legal document extraction AI.\n\n"
            f"This is the document being read: {document_type} ({page_info}).{kw_clause}\n\n"
            f"USER EXTRACTION INSTRUCTIONS:\n"
            f"\"{extraction_prompt}\"\n\n"
            f"TASK:\n"
            f"Visually inspect the provided document page image(s) in this batch and extract all information requested in the USER EXTRACTION INSTRUCTIONS.\n"
            f"Each image is clearly stamped with its exact page number [PAGE X].\n\n"
            f"RULES:\n"
            f"1. Return ONLY a valid JSON object. No preamble, no markdown fences, no conversational text.\n"
            f"2. BORROWER VS CO-BORROWER DISTINCTION (STRICT & MUTUALLY EXCLUSIVE):\n"
            f"   - 'borrower_name', 'borrower_address': The PRIMARY applicant / borrower only (the customer, main borrower company or individual). Exactly ONE primary borrower.\n"
            f"   - 'co_borrower_1_name', 'co_borrower_1_address', 'co_borrower_2_name', 'co_borrower_2_address', etc.: Co-applicants, secondary joint borrowers, directors, or guarantors.\n"
            f"   - LENDER / FINANCIER EXCLUSION: NEVER extract the Lender, financing company, Bank, or NBFC (e.g. 'UGRO Capital', 'HDFC', 'ICICI', 'Bank of Baroda', etc.) as the borrower or co-borrower! The borrower is the customer obtaining the facility, never the lender.\n"
            f"   - MUTUAL EXCLUSIVITY: NEVER mark the same person or entity as both primary borrower AND co-borrower! The main borrower goes to 'borrower_name'. All other secondary joint parties go to 'co_borrower_1_name', 'co_borrower_2_name', etc.\n"
            f"   - CRITICAL: Return flat key-value pairs. DO NOT return nested objects (e.g. no 'borrower': {{'name': ...}}) and DO NOT return arrays of objects (e.g. no 'borrower_details': [...] or 'co_borrower_details': [...]).\n"
            f"3. PROVENANCE & PAGE CITATION (MANDATORY):\n"
            f"   - In '_cited_pages' and '_field_page_sources', you MUST ONLY report the exact integer shown in the stamped red badge [PAGE X] in the top-left corner of the image (e.g. if the red badge says [PAGE 33], cite 33).\n"
            f"   - NEVER report physical page numbers printed on the footer, header, or body of the document page itself (e.g. do NOT cite bottom-center printed numbers). The stamped red badge [PAGE X] is the ONLY source of truth for page citations.\n"
            f"   - Provide '_cited_pages': a JSON list of integer page numbers from the red badges where the extracted data was found, e.g. [33]. If nothing found, return [].\n"
            f"   - Provide '_field_page_sources': a JSON object mapping each extracted field name to the red badge page number where it was found, e.g. {{\"borrower_name\": 33, \"borrower_address\": 33}}.\n"
            f"4. For names and addresses, preserve the exact text as written or printed in the document.\n"
            f"5. For numbers or monetary amounts, extract the exact figures.\n"
            f"6. STRICT HEADING CONSTRAINT: If USER EXTRACTION INSTRUCTIONS or Focus Areas specifies a target heading (e.g. 'Schedule'), you must ONLY extract fields from pages displaying or belonging to that heading. If a page does NOT belong to that heading (such as a Power of Attorney, General Terms, or boilerplate clauses), DO NOT extract borrower details from it.\n"
            f"7. If the requested information is not found in this batch of pages, return an empty JSON object {{}}.\n"
            f"\nReturn ONLY the JSON object."
        )

        parsed = None
        route = ""

        try:
            parsed, route = self._call_medha(prompt, imgs_bytes)
        except Exception as e:
            sys.stderr.write(f"[ocr] Medha VLM failed: {e}\n")

        if parsed is None or parsed.get("_parse_error"):
            try:
                parsed, route = self._call_gemini(prompt, imgs_bytes)
            except Exception as e:
                sys.stderr.write(f"[ocr] Gemini fallback also failed: {e}\n")
                parsed = {"_error": str(e)}
                route = "failed"

        parsed["_ocr_route"] = route
        return parsed

    def _call_medha(self, prompt: str, images_bytes: List[bytes]) -> Tuple[Dict[str, Any], str]:
        import httpx

        cfg = self._cfg
        content_parts = []
        for b in images_bytes:
            b64 = base64.b64encode(b).decode()
            content_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            
        content_parts.append({"type": "text", "text": prompt})

        url = cfg["url"]
        if url.endswith("/v1") or url.endswith("/v1/"):
            url = url.rstrip("/") + "/chat/completions"
        elif not url.endswith("/chat/completions"):
            url = url.rstrip("/") + "/chat/completions"

        body = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 4096,
            "temperature": 0.1,
        }
        headers = {"Content-Type": "application/json"}
        api_key = cfg.get("key", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        t0 = time.perf_counter()
        resp = httpx.post(url, json=body, headers=headers, timeout=120.0)
        resp.raise_for_status()
        ms = round((time.perf_counter() - t0) * 1000, 1)

        resp_data = resp.json()
        raw = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = resp_data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

        parsed = _parse_json(raw)
        parsed["_raw_response"] = raw
        parsed["_meta"] = {
            "ms": ms,
            "model": cfg.get("model", "medha-vlm"),
            "route": "medha_vlm",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
        return parsed, "medha_vlm"

    def _call_gemini(self, prompt: str, images_bytes: List[bytes]) -> Tuple[Dict[str, Any], str]:
        import google.generativeai as genai
        from config import settings

        if not settings.GEMINI_API_KEY:
            raise RuntimeError("No GEMINI_API_KEY configured")

        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(settings.GEMINI_MODEL)

        contents = [prompt]
        for b in images_bytes:
            contents.append({"mime_type": "image/jpeg", "data": b})

        t0 = time.perf_counter()
        response = model.generate_content(contents)
        ms = round((time.perf_counter() - t0) * 1000, 1)

        raw = response.text
        usage_meta = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0
        completion_tokens = getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0
        total_tokens = getattr(usage_meta, "total_token_count", 0) if usage_meta else (prompt_tokens + completion_tokens)

        parsed = _parse_json(raw)
        parsed["_raw_response"] = raw
        parsed["_meta"] = {
            "ms": ms,
            "model": settings.GEMINI_MODEL,
            "route": "gemini_fallback",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
        return parsed, "gemini_fallback"


# ── Breaker status stub (for metrics.py compatibility) ─────────────────────

def get_breaker_status() -> dict:
    """Return circuit breaker status (simplified for new pipeline)."""
    return {"state": "closed", "failures": 0, "cooldown_remaining": 0}
