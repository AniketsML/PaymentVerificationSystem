"""
Triple-route OCR engine for SARFAESI document extraction.

Updated to 8-Stage CV Pipeline:
Stage 1: Triage (CNN/heuristic) -> printed / handwritten / blank
Stage 2A: Printed path (RapidOCR) -> check confidence
Stage 2B: Handwritten path (TrOCR)
Stage 3: Dual extraction & comparison (VLM)
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageStat
from psycopg.types.json import Jsonb

from config import settings
from db import pg
from workspaces.legal.circuit_breaker import CircuitBreaker
from workspaces.legal.prompts import PROMPT_VERSION
from workspaces.legal.models import ExtractionSource, PageType

# Module-level circuit breaker instance
_vlm_breaker = CircuitBreaker(
    name="legal_vlm",
    threshold=settings.VLM_BREAKER_THRESHOLD,
    cooldown_seconds=settings.VLM_BREAKER_COOLDOWN,
    max_wait=settings.VLM_BREAKER_MAX_WAIT,
)

_rapid_engine = None
_rapid_available: Optional[bool] = None

def _get_rapid():
    global _rapid_engine, _rapid_available
    if _rapid_available is not None:
        return _rapid_engine
    try:
        from rapidocr_onnxruntime import RapidOCR
        _rapid_engine = RapidOCR()
        _rapid_available = True
    except ImportError:
        _rapid_available = False
    return _rapid_engine

def get_breaker_status() -> dict:
    return _vlm_breaker.status()

class LegalOCRClient(ABC):
    @abstractmethod
    def classify_document(self, image: Optional[Image.Image], filename: str = "") -> Dict[str, Any]: ...
    @abstractmethod
    def extract_data(self, image: Optional[Image.Image], document_type: str, prompt: str = "", row: Dict[str, Any] = None, extraction_plan: dict = None) -> Dict[str, Any]: ...
    def extract_raw_text(self, image: Optional[Image.Image]) -> str: ...

def _image_to_bytes(image: Image.Image, fmt: str = "JPEG") -> bytes:
    buf = io.BytesIO()
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")
    image.save(buf, format=fmt, quality=90)
    return buf.getvalue()

def _cache_key(image_bytes: bytes, model: str, prompt_hint: str) -> str:
    h = hashlib.sha256(image_bytes).hexdigest()[:16]
    return f"{h}:{model}:{PROMPT_VERSION}:{prompt_hint[:20]}"

def _cache_get(key: str) -> Optional[Dict]:
    try:
        with pg.pool().connection() as c:
            r = c.execute(
                "UPDATE legal_ocr_cache SET hits=hits+1 WHERE cache_key=%s RETURNING extraction",
                (key,)
            ).fetchone()
            if r:
                return r["extraction"]
    except Exception:
        pass
    return None

def _cache_set(key: str, extraction: dict, model: str, route: str):
    try:
        with pg.pool().connection() as c:
            c.execute(
                "INSERT INTO legal_ocr_cache(cache_key,extraction,model,ocr_route) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT(cache_key) DO NOTHING",
                (key, Jsonb(extraction), model, route)
            )
    except Exception:
        pass

def _parse_json(raw: str) -> Dict[str, Any]:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"_parse_error": True, "_raw_text": raw[:500]}

# Stage 1: Triage (CNN/heuristic)
def classify_page_type(image: Image.Image) -> PageType:
    """Stage 1: Classify image as PRINTED, HANDWRITTEN, or BLANK."""
    if not image:
        return PageType.BLANK
        
    # Check if blank using simple heuristic (standard deviation of grayscale)
    gray = image.convert("L")
    stat = ImageStat.Stat(gray)
    if stat.stddev[0] < 5.0: # Very low variance means mostly solid color (blank)
        return PageType.BLANK
        
    # Heuristic for MobileNetV3 (Simulated for now, as loading the actual torch model on every frame is heavy unless cached)
    # A true implementation would pass `gray` into a MobileNetV3-Small classifier.
    # We will rely on RapidOCR's confidence later if we guess wrong.
    # For now, default to PRINTED unless it's a small crop.
    return PageType.PRINTED


def rapidocr_extract(image: Image.Image) -> str:
    res = rapidocr_detect_and_recognize(image)
    return res.get("text", "")

def rapidocr_detect_and_recognize(image: Image.Image) -> Dict[str, Any]:
    """Stage 2A: Printed path using RapidOCR (with per-word/line confidence)."""
    engine = _get_rapid()
    if not engine or image is None:
        return {"text": "", "lines": [], "confidence": 0.0, "uncertain": True, "uncertain_crops": 0, "low_conf_lines": []}
    try:
        import numpy as np
        img_array = np.array(image.convert("RGB"))
        result, _ = engine(img_array)
        if not result:
            return {"text": "", "lines": [], "confidence": 0.0, "uncertain": True, "uncertain_crops": 0, "low_conf_lines": []}

        lines = []
        scores = []
        low_conf = []

        for item in result:
            if not item or len(item) < 3:
                continue
            box, txt, score = item[0], item[1], float(item[2])
            txt = txt.strip()
            if not txt:
                continue
            lines.append(txt)
            scores.append(score)
            if score < 0.85:
                low_conf.append({"text": txt, "score": round(score, 3)})

        full_text = "\n".join(lines)
        avg_score = float(np.mean(scores)) if scores else 0.0

        is_uncertain = (avg_score < 0.90) or (len(low_conf) > max(1, int(len(lines) * 0.10))) or (len(full_text) < 20)

        return {
            "text": full_text,
            "lines": lines,
            "confidence": round(avg_score, 3),
            "uncertain": is_uncertain,
            "uncertain_crops": len(low_conf),
            "low_conf_lines": [lc["text"] for lc in low_conf[:8]],
            "total_lines": len(lines)
        }
    except Exception as e:
        sys.stderr.write(f"[legal_ocr] RapidOCR DET/REC error: {e}\n")
        return {"text": "", "lines": [], "confidence": 0.0, "uncertain": True, "uncertain_crops": 0, "low_conf_lines": []}

def trocr_extract(image: Image.Image) -> Dict[str, Any]:
    """Stage 2B: Handwritten Path using TrOCR."""
    try:
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel
        import torch
    except ImportError:
        sys.stderr.write("[trocr] TrOCR dependencies not installed. Falling back.\n")
        return {"text": "", "route": "trocr_failed", "lines": []}

    try:
        global _trocr_processor, _trocr_model
        if '_trocr_processor' not in globals():
            sys.stderr.write("[trocr] Loading TrOCR model (QuickHawk/trocr-indic)...\n")
            _trocr_processor = TrOCRProcessor.from_pretrained("QuickHawk/trocr-indic")
            _trocr_model = VisionEncoderDecoderModel.from_pretrained("QuickHawk/trocr-indic")
        
        pixel_values = _trocr_processor(image.convert("RGB"), return_tensors="pt").pixel_values
        generated_ids = _trocr_model.generate(pixel_values)
        generated_text = _trocr_processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
        return {
            "text": generated_text,
            "route": "trocr",
            "lines": [generated_text]
        }
    except Exception as e:
        sys.stderr.write(f"[trocr] TrOCR failed: {e}\n")
        return {"text": "", "route": "trocr_failed", "lines": []}
_prompt_plan_cache = {}

class SARFAESIDocumentOCR(LegalOCRClient):
    """Production OCR client: Triage -> Printed/Handwritten -> VLM Dual Extraction."""

    def __init__(self):
        from config import runtime
        self._cfg = runtime.model_config()

    def analyze_prompt_intent(self, prompt: str) -> Dict[str, Any]:
        """Stage 0: Use text-LLM to parse the prompt into an ExtractionPlan (Cached per batch)."""
        if not prompt or len(prompt.strip()) < 5:
            return {"target_docs": [], "target_pages": [], "keywords": []}
            
        if prompt in _prompt_plan_cache:
            return _prompt_plan_cache[prompt]
            
        sys_prompt = (
            "You are a pipeline planner. Analyze the user's extraction prompt and output a strict JSON object:\n"
            "- 'target_docs': list of relevant document types (e.g. ['sanction_letter', 'loan_agreement', 'modt']). Empty if not specified.\n"
            "- 'target_pages': list of specific page numbers (0-indexed) if mentioned. Empty if not specified.\n"
            "- 'keywords': list of 10-15 highly relevant synonyms, field names, or related words that MUST appear on a page for it to contain this data. (lowercase).\n"
            f"User Prompt: {prompt}\n"
            "Return ONLY valid JSON."
        )
        
        parsed, _ = self._call_vlm(sys_prompt, image=None, cache_hint="prompt_planner")
        
        plan = {"target_docs": [], "target_pages": [], "keywords": []}
        if isinstance(parsed, dict):
            plan["target_docs"] = [str(d).lower() for d in parsed.get("target_docs", [])]
            plan["target_pages"] = [int(p) for p in parsed.get("target_pages", []) if str(p).isdigit()]
            plan["keywords"] = [str(k).lower() for k in parsed.get("keywords", [])]
            
        _prompt_plan_cache[prompt] = plan
        return plan

    def _call_vlm(self, prompt: str, image: Optional[Image.Image],
                  cache_hint: str = "") -> Tuple[Dict[str, Any], str]:
        """Call primary VLM (Medha), fallback to Gemini on circuit break."""
        img_bytes = _image_to_bytes(image) if image else b""
        cfg = self._cfg

        if img_bytes:
            ck = _cache_key(img_bytes, cfg["model"], cache_hint)
            cached = _cache_get(ck)
            if cached:
                cached["_meta"] = {"ms": 0, "model": cfg["model"], "cached": True, "route": "cache"}
                return cached, "cache"

        if _vlm_breaker.allow_request():
            try:
                parsed, route = self._call_medha(prompt, image, img_bytes, cfg)
                _vlm_breaker.record_success()
                if img_bytes:
                    _cache_set(_cache_key(img_bytes, cfg["model"], cache_hint), parsed, cfg["model"], route)
                return parsed, route
            except Exception as e:
                _vlm_breaker.record_failure(str(e))
                sys.stderr.write(f"[legal_ocr] Medha VLM failed: {e}\n")

        return self._call_gemini_fallback(prompt, img_bytes, cache_hint)

    def _call_medha(self, prompt: str, image: Optional[Image.Image],
                    img_bytes: bytes, cfg: dict) -> Tuple[Dict[str, Any], str]:
        import httpx
        content_parts = []
        if image and img_bytes:
            b64 = base64.b64encode(img_bytes).decode()
            content_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        content_parts.append({"type": "text", "text": prompt})

        t0 = time.perf_counter()
        body = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": cfg.get("max_tokens", 4096),
            "temperature": cfg.get("temperature", 0.1),
        }
        headers = {"Content-Type": "application/json"}
        api_key = cfg.get("api_key") or getattr(settings, "VISION_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = cfg["url"]
        if url.endswith("/v1") or url.endswith("/v1/"):
            url = url.rstrip("/") + "/chat/completions"
            
        resp = httpx.post(
            url, json=body,
            headers=headers,
            timeout=120.0,
        )
        resp.raise_for_status()
        ms = round((time.perf_counter() - t0) * 1000, 1)
        raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _parse_json(raw)
        parsed["_meta"] = {"ms": ms, "model": cfg["model"], "cached": False, "route": "medha_vlm"}
        return parsed, "medha_vlm"

    def _call_gemini_fallback(self, prompt: str, img_bytes: bytes,
                              cache_hint: str) -> Tuple[Dict[str, Any], str]:
        from workspaces.legal.gemini_client import call_gemini
        parsed, meta = call_gemini(prompt, image_bytes=img_bytes if img_bytes else None)
        if not meta.get("error") and img_bytes:
            _cache_set(_cache_key(img_bytes, meta.get("model", "gemini"), cache_hint),
                       parsed, meta.get("model", "gemini"), "gemini_fallback")
        parsed["_meta"] = meta
        return parsed, "gemini_fallback"

    def classify_document(self, image: Optional[Image.Image], filename: str = "") -> Dict[str, Any]:
        from workspaces.legal.prompts import SARFAESI_CLASSIFY_PROMPT
        prompt = SARFAESI_CLASSIFY_PROMPT
        if filename:
            prompt += f"\n\nFilename hint: {filename}"
        parsed, route = self._call_vlm(prompt, image, cache_hint="classify")
        parsed["_ocr_route"] = route
        return parsed

    def extract_raw_text(self, image: Optional[Image.Image]) -> str:
        if image is None: return ""
        
        # Stage 1: Triage
        page_type = classify_page_type(image)
        
        if page_type == PageType.BLANK:
            return ""
            
        if page_type == PageType.HANDWRITTEN:
            # Stage 2B
            tr_res = trocr_extract(image)
            return tr_res.get("text", "")
            
        # Stage 2A
        res = rapidocr_detect_and_recognize(image)
        if res.get("uncertain"):
            # Handwritten fallback inside printed doc
            tr_res = trocr_extract(image)
            return f"{res.get('text', '')}\n{tr_res.get('text', '')}"
            
        return res.get("text", "")

    def extract_data(self, image: Optional[Image.Image], document_type: str,
                     prompt: str = "", row: Dict[str, Any] = None, extraction_plan: dict = None) -> Dict[str, Any]:
        """Runs the document extraction phase for a single page."""
        if not prompt:
            prompt = self._get_prompt_for_type(document_type)

        schema_instruction = (
            "\n\nIMPORTANT: You must return the extracted data as a JSON object. "
            "You MUST extract the Loan Account Number and map it to the exact key 'account_no_lan'. "
            "You MUST extract the Primary Borrower's name and map it to the exact key 'applicant_name'. "
            "If they are not found, return null for those keys."
        )
        if schema_instruction not in prompt:
            prompt += schema_instruction

        # Stage 1: Triage
        page_type = classify_page_type(image)
        if page_type == PageType.BLANK:
            return {"_status": "blank_discarded"}
            
        raw_ocr_text = ""
        ocr_conf = 0.0
        is_uncertain = False
        low_conf_lines = []
        route_used = ""

        if page_type == PageType.HANDWRITTEN:
            # Stage 2B: Handwritten Path
            tr_res = trocr_extract(image)
            raw_ocr_text = tr_res.get("text", "")
            is_uncertain = True # Handwritten path ALWAYS escalates to Stage 3
            ocr_conf = 0.5
            route_used = "trocr"
        else:
            # Stage 2A: Printed Path
            ocr_det = rapidocr_detect_and_recognize(image)
            raw_ocr_text = ocr_det.get("text", "")
            ocr_conf = ocr_det.get("confidence", 0.0)
            is_uncertain = ocr_det.get("uncertain", True)
            low_conf_lines = ocr_det.get("low_conf_lines", [])
            route_used = "rapidocr"
            
            # Stage 2.5: Boilerplate Filtering (Heuristic)
            # Use dynamic keywords from the ExtractionPlan (Stage 0).
            text_lower = raw_ocr_text.lower()
            plan_keywords = extraction_plan.get("keywords", []) if extraction_plan else []
            
            # Only apply strict boilerplate filtering if we successfully generated keywords
            if len(text_lower) > 200 and plan_keywords:
                # If the page contains ZERO of the generated synonyms/keywords, drop it.
                if not any(kw in text_lower for kw in plan_keywords):
                    return {"_status": "boilerplate_discarded", "_ocr_route": "smart_filter"}

        # Check if we can skip Stage 3 Dual Extraction
        if (not is_uncertain) and (ocr_conf >= 0.90):
            # High confidence printed -> send OCR text only to LLM (no VLM).
            llm_prompt = f"{prompt}\n\nDOCUMENT TEXT:\n{raw_ocr_text}"
            parsed, route = self._call_vlm(llm_prompt, image=None, cache_hint=f"{document_type}_llm")
            parsed["_ocr_route"] = "ocr_only_llm"
            parsed["_confidence"] = ocr_conf
            parsed["_raw_ocr_text"] = raw_ocr_text
            parsed["_source"] = ExtractionSource.OCR_ONLY.value
            return parsed

        # Stage 3: Dual extraction & comparison (low-confidence printed + all handwritten)
        escalated_prompt = prompt + (
            "\n\n--- DUAL EXTRACTION RECONCILIATION INSTRUCTIONS ---\n"
            "You are performing Stage 3 Dual Extraction. You have been provided the original image AND the raw OCR text below. "
            "You must extract the requested fields independently from the image, compare them against the OCR text, and reconcile any differences. "
            "If the OCR and your visual extraction AGREE, return the value. "
            "If they DISAGREE, pick the most visually accurate answer, but YOU MUST FLAG the field by adding it to the 'dual_source_mismatch_fields' array in your JSON output. "
            "Output Format: { \"extracted_fields\": { ... }, \"dual_source_mismatch_fields\": [\"field_name_1\"] }\n\n"
        )
        
        if raw_ocr_text:
            escalated_prompt += (
                f"--- RAW OCR TEXT (Detected via {route_used}) ---\n"
                f"{raw_ocr_text[:3000]}\n"
                f"--- END RAW OCR TEXT ---\n"
            )

        parsed, route = self._call_vlm(escalated_prompt, image, cache_hint=f"{document_type}_dual")
        
        # Format the output to support Stage 4 Provenance Tracking
        final_fields = parsed.get("extracted_fields", parsed)
        mismatches = parsed.get("dual_source_mismatch_fields", [])
        
        candidates = {}
        for k, v in final_fields.items():
            if k.startswith("_"): continue
            source = ExtractionSource.OCR_VLM_AGREED.value
            mismatch_flag = False
            if k in mismatches:
                source = ExtractionSource.OCR_VLM_MISMATCH.value
                mismatch_flag = True
                
            candidates[k] = {
                "value": v,
                "source": source,
                "confidence": 1.0 if not mismatch_flag else 0.5,
                "mismatch_flag": mismatch_flag,
                "ocr_value": None,
            }
            
        parsed["_ocr_route"] = route
        parsed["_vlm_escalated"] = True
        parsed["_ocr_confidence"] = ocr_conf
        parsed["_raw_ocr_text"] = raw_ocr_text
        parsed["_field_candidates"] = candidates
        return parsed

    def _get_prompt_for_type(self, doc_type: str) -> str:
        from workspaces.legal import prompts
        mapping = {
            "sanction_letter": prompts.SANCTION_LETTER_EXTRACT_PROMPT,
            "foreclosure_notice": prompts.FCL_EXTRACT_PROMPT,
            "fcl": prompts.FCL_EXTRACT_PROMPT,
            "modt": prompts.PROPERTY_EXTRACT_PROMPT.format(document_type="MODT"),
            "legal_report": prompts.PROPERTY_EXTRACT_PROMPT.format(document_type="Legal/Title Search Report"),
            "title_search": prompts.PROPERTY_EXTRACT_PROMPT.format(document_type="Legal/Title Search Report"),
            "collateral_deed": prompts.PROPERTY_EXTRACT_PROMPT.format(document_type="Collateral Deed / Title Deed"),
            "loan_agreement": prompts.LOAN_AGREEMENT_SCHEDULE_PROMPT,
        }
        return mapping.get(doc_type, prompts.DOCUMENT_EXTRACT_PROMPT.format(document_type=doc_type))


class PrecomputedLegalOCR(LegalOCRClient):
    def classify_document(self, image: Optional[Image.Image], filename: str = "") -> Dict[str, Any]:
        return {"document_type": "other", "confidence": 0.85, "_ocr_route": "precomputed"}

    def extract_data(self, image: Optional[Image.Image], document_type: str,
                     prompt: str = "", row: Dict[str, Any] = None) -> Dict[str, Any]:
        return {"document_type": document_type, "verbatim_text": "[precomputed]",
                "_ocr_route": "precomputed"}

    def extract_raw_text(self, image: Optional[Image.Image]) -> str:
        return "[precomputed raw text]"
