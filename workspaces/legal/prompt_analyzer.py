"""
Stage 0 — Prompt Analyzer for the Legal Pipeline.

Parses the user's free-text extraction prompt into a structured ExtractionPlan
using the Medha VLM API (text-only), with Gemini fallback.

The plan has TWO categories of extraction instructions:
  1. BOUND instructions  — "extract field X from doc Y (optionally page Z)"
     These are STRICT: field X is ONLY extracted from doc Y, never from any other doc.
  2. UNBOUND instructions — "extract field X" (no doc specified)
     These are searched across ALL documents.

This distinction prevents cross-contamination (e.g. pulling foreclosure amounts
from a sanction letter, or pulling a borrower name from a mortgage deed).
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ── ExtractionPlan dataclass ───────────────────────────────────────────────

@dataclass
class ExtractionInstruction:
    """A single extraction instruction parsed from the user prompt.

    Represents one directive: "extract these fields [from this doc] [on these pages]".
    If doc_type is empty, the fields are UNBOUND (search everywhere).
    If doc_type is set, the fields are BOUND (search ONLY in that doc).
    """
    fields: List[str] = field(default_factory=list)
    doc_type: str = ""          # empty = search all docs
    pages: List[int] = field(default_factory=list)  # 0-indexed, empty = all pages
    heading_hint: str = ""      # e.g. "under Schedule of Charges"


@dataclass
class ExtractionPlan:
    """Structured output from prompt analysis.

    The plan splits the user's intent into bound and unbound instructions
    to prevent cross-contamination of extracted data.
    """
    instructions: List[ExtractionInstruction] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    raw_prompt: str = ""

    # ── Convenience properties for backward-compat ─────────────────────
    @property
    def target_docs(self) -> List[str]:
        """All unique doc types mentioned across bound instructions."""
        return list(set(
            inst.doc_type for inst in self.instructions if inst.doc_type
        ))

    @property
    def target_pages(self) -> List[int]:
        """All unique page numbers mentioned across all instructions."""
        pages = set()
        for inst in self.instructions:
            pages.update(inst.pages)
        return sorted(pages)

    @property
    def doc_field_map(self) -> Dict[str, List[str]]:
        """Map of doc_type -> bound fields (ONLY for bound instructions)."""
        m: Dict[str, List[str]] = {}
        for inst in self.instructions:
            if inst.doc_type:
                m.setdefault(inst.doc_type, []).extend(inst.fields)
        # Deduplicate
        return {k: list(dict.fromkeys(v)) for k, v in m.items()}

    @property
    def unbound_fields(self) -> List[str]:
        """Fields NOT tied to any specific document (search everywhere)."""
        fields = []
        for inst in self.instructions:
            if not inst.doc_type:
                fields.extend(inst.fields)
        return list(dict.fromkeys(fields))

    @property
    def doc_page_map(self) -> Dict[str, List[int]]:
        """Map of doc_type -> specific pages (for bound instructions with page limits)."""
        m: Dict[str, List[int]] = {}
        for inst in self.instructions:
            if inst.doc_type and inst.pages:
                m.setdefault(inst.doc_type, []).extend(inst.pages)
        return {k: sorted(set(v)) for k, v in m.items()}

    def get_fields_for_doc(self, doc_type: str) -> List[str]:
        """Get all fields that should be extracted from a given doc type.

        Returns bound fields for this doc + all unbound fields.
        """
        fields = []
        for inst in self.instructions:
            if inst.doc_type == doc_type or not inst.doc_type:
                fields.extend(inst.fields)
        return list(dict.fromkeys(fields))

    def get_pages_for_doc(self, doc_type: str) -> List[int]:
        """Get specific pages for a given doc type."""
        for inst in self.instructions:
            if inst.doc_type == doc_type and inst.pages:
                return sorted(set(inst.pages))
        return []

    def get_heading_hint_for_doc(self, doc_type: str) -> str:
        """Get the specific heading constraint for a given doc type."""
        for inst in self.instructions:
            if inst.doc_type == doc_type and inst.heading_hint:
                return inst.heading_hint
        return ""

    def has_bound_instructions(self) -> bool:
        """True if the user specified at least one doc-specific instruction."""
        return any(inst.doc_type for inst in self.instructions)

    def __repr__(self):
        bound = len([i for i in self.instructions if i.doc_type])
        unbound = len([i for i in self.instructions if not i.doc_type])
        return (f"ExtractionPlan(bound={bound}, unbound={unbound}, "
                f"keywords={len(self.keywords)})")


# ── Module-level cache ─────────────────────────────────────────────────────
_prompt_plan_cache: Dict[str, ExtractionPlan] = {}

# ── The system prompt ──────────────────────────────────────────────────────
# This is the most critical prompt in the entire pipeline. It must produce
# an unambiguous, structured plan that never cross-links data.

_PLANNER_SYSTEM_PROMPT = r"""You are a pipeline planner for a SARFAESI loan document extraction system.
Your job is to parse the user's extraction prompt into precise, unambiguous extraction instructions.

CRITICAL RULES — READ CAREFULLY:

RULE 1 — BOUND vs UNBOUND:
The user may say things in THREE different ways. You must distinguish them precisely:

  (A) BOUND: "Extract borrower name FROM the sanction letter"
      → The field "applicant_name" is BOUND to "sanction_letter".
      → It must ONLY be extracted from the sanction letter, never from any other document.

  (B) UNBOUND WITH DOC HINTS: "Extract borrower name. Look in sanction letter and loan agreement."
      → The field "applicant_name" is UNBOUND (not tied to one doc).
      → But the user suggests which docs to check.
      → Create separate BOUND instructions for each suggested doc with the same fields.

  (C) FULLY UNBOUND: "Extract borrower name"
      → No document specified. The field is UNBOUND.
      → It should be searched across ALL documents.

RULE 2 — PAGE SPECIFICITY:
If the user says "page 1-3 of the sanction letter", convert to 0-indexed: [0, 1, 2].
If the user says "page 2", convert to 0-indexed: [1].
If the user says "first page", that's [0].
If the user says "last page", leave pages EMPTY (we handle it dynamically).
If NO page is mentioned for a doc, leave pages EMPTY (meaning all pages of that doc).
NEVER attach page numbers from one document to a different document.

RULE 3 — HEADING HINTS:
If the user says "under the schedule of charges" or "in the property section",
capture that as a heading_hint for that instruction.

RULE 4 — FIELD NAME MAPPING:
Map the user's natural language to EXACTLY these field names:
  - "borrower name", "applicant", "primary borrower" → applicant_name
  - "borrower address" → applicant_address
  - "LAN", "loan account number", "account number" → account_no_lan
  - "co-borrower", "co-applicant 1" → co_applicant_1
  - "co-borrower address" → co_applicant_address_1
  - "guarantor" → guarantor_1
  - "property owner", "mortgagor" → property_owner_mortgagor
  - "sanction amount", "loan amount", "sanctioned amount" → sanction_amount
  - "sanction amount in words" → sanction_amount_in_words
  - "rate of interest", "ROI" → roi_in_number
  - "sanction date" → sanction_date
  - "disbursal date", "disbursement date" → disbursal_date
  - "NPA date", "classification date", "default date" → npa_date
  - "future principal" → future_principal
  - "principal overdue" → principal_overdue
  - "interest overdue" → interest_overdue
  - "interest on termination" → interest_on_termination
  - "late payment", "penal charges" → late_payment_penal
  - "cheque bounce" → cheque_bounce_inc_gst
  - "other charges" → other_charges_inc_gst
  - "foreclosure charges" → foreclosure_charges
  - "litigation charges" → litigation_charges
  - "excess amount" → excess_amount
  - "TOS", "total outstanding", "total dues" → tos
  - "property details", "property description" → mortgaged_property_detail_1
  - "directions", "boundaries", "compass" → directions
  - "second property" → mortgaged_property_detail_2
  - If the user asks for something not in this list, use the exact words as the field name.

RULE 5 — DOCUMENT TYPE MAPPING:
  - "sanction letter", "SL", "credit sanction", "approval letter" → sanction_letter
  - "FCL", "foreclosure notice", "demand notice", "13(2) notice", "SARFAESI notice" → foreclosure_notice
  - "MODT", "mortgage deed", "memorandum of deposit" → modt
  - "legal report", "title search", "advocate report" → legal_report
  - "sale deed", "collateral deed", "patta", "conveyance" → collateral_deed
  - "loan agreement", "agreement" → loan_agreement

OUTPUT FORMAT — Return ONLY this JSON structure:
{
  "instructions": [
    {
      "fields": ["applicant_name", "sanction_amount"],
      "doc_type": "sanction_letter",
      "pages": [0, 1, 2],
      "heading_hint": ""
    },
    {
      "fields": ["npa_date", "tos"],
      "doc_type": "foreclosure_notice",
      "pages": [],
      "heading_hint": ""
    },
    {
      "fields": ["roi_in_number"],
      "doc_type": "",
      "pages": [],
      "heading_hint": "schedule of charges"
    }
  ],
  "keywords": ["borrower", "applicant", "sanction", "loan account", "npa", "non-performing",
               "outstanding", "total dues", "foreclosure", "demand", "notice", "rate of interest",
               "roi", "schedule", "charges", "principal", "overdue"]
}

EXAMPLES:

User: "Extract borrower name and LAN from sanction letter page 1-3. Also get NPA date from foreclosure notice."
→ Two BOUND instructions: one for sanction_letter(pages 0,1,2) with [applicant_name, account_no_lan],
  one for foreclosure_notice(all pages) with [npa_date].

User: "Get me all the financial details and borrower information"
→ One UNBOUND instruction (doc_type="") with all relevant fields. No page limits.

User: "Extract property details from MODT and legal report"
→ Two BOUND instructions: modt with [mortgaged_property_detail_1, directions, ...],
  legal_report with [mortgaged_property_detail_1, directions, ...].

User: "Find the total outstanding amount. Check foreclosure notice."
→ One BOUND instruction: foreclosure_notice with [tos].
  The user named the doc, so it's BOUND, not unbound.

Return ONLY valid JSON. No explanations or text outside the JSON."""


def _parse_json_robust(raw: str) -> Dict[str, Any]:
    """Parse JSON from LLM output, handling code fences and embedded text."""
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
    return {}


def _call_medha_text(prompt: str) -> Optional[str]:
    """Call Medha API with text-only prompt (no image)."""
    try:
        import httpx
        from config import runtime
        cfg = runtime.model_config()

        url = cfg["url"]
        if url.endswith("/v1") or url.endswith("/v1/"):
            url = url.rstrip("/") + "/chat/completions"
        elif not url.endswith("/chat/completions"):
            url = url.rstrip("/") + "/chat/completions"

        body = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
            "temperature": 0.05,  # Very low for deterministic parsing
        }
        headers = {"Content-Type": "application/json"}
        api_key = cfg.get("key", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        resp = httpx.post(url, json=body, headers=headers, timeout=60.0)
        resp.raise_for_status()
        return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        sys.stderr.write(f"[prompt_analyzer] Medha call failed: {e}\n")
        return None


def _call_gemini_text(prompt: str) -> Optional[str]:
    """Fallback: call Gemini API with text-only prompt."""
    try:
        import google.generativeai as genai
        from config import settings
        if not settings.GEMINI_API_KEY:
            return None
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel(settings.GEMINI_MODEL)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        sys.stderr.write(f"[prompt_analyzer] Gemini fallback failed: {e}\n")
        return None


# ── Validation ─────────────────────────────────────────────────────────────

_VALID_DOC_TYPES = {
    "sanction_letter", "foreclosure_notice", "modt",
    "legal_report", "collateral_deed", "loan_agreement",
}

_VALID_FIELDS = {
    "account_no_lan", "property_owner_mortgagor", "applicant_name",
    "applicant_address", "co_applicant_1", "co_applicant_address_1",
    "co_applicant_2", "co_applicant_address_2", "co_applicant_3",
    "co_applicant_address_3", "guarantor_1", "guarantor_1_add",
    "guarantor_2", "guarantor_2_add", "sanction_amount",
    "sanction_amount_in_words", "roi_in_number", "sanction_date",
    "disbursal_date", "npa_date", "future_principal", "principal_overdue",
    "interest_overdue", "interest_on_termination", "late_payment_penal",
    "cheque_bounce_inc_gst", "other_charges_inc_gst", "foreclosure_charges",
    "litigation_charges", "excess_amount", "tos",
    "mortgaged_property_detail_1", "directions",
    "mortgaged_property_detail_2", "directions_2",
}


def _validate_instruction(inst_data: dict) -> Optional[ExtractionInstruction]:
    """Validate and clean a single instruction from the LLM output."""
    fields = inst_data.get("fields", [])
    if not fields or not isinstance(fields, list):
        return None

    # Clean fields — keep both known and custom field names
    clean_fields = [str(f).strip().lower() for f in fields if str(f).strip()]
    if not clean_fields:
        return None

    # Validate doc_type
    doc_type = str(inst_data.get("doc_type", "")).strip().lower()
    if doc_type and doc_type not in _VALID_DOC_TYPES:
        doc_type = ""  # Unknown doc type → treat as unbound

    # Validate pages (must be non-negative integers)
    raw_pages = inst_data.get("pages", [])
    pages = []
    if isinstance(raw_pages, list):
        for p in raw_pages:
            try:
                pi = int(p)
                if pi >= 0:
                    pages.append(pi)
            except (ValueError, TypeError):
                pass

    heading_hint = str(inst_data.get("heading_hint", "")).strip()

    return ExtractionInstruction(
        fields=clean_fields,
        doc_type=doc_type,
        pages=sorted(set(pages)),
        heading_hint=heading_hint,
    )


def analyze_extraction_prompt(
    prompt: str,
    document_filenames: List[str] = None,
) -> ExtractionPlan:
    """
    Stage 0: Parse the user's extraction prompt into an ExtractionPlan.

    The plan precisely distinguishes between:
    - BOUND instructions (field X from doc Y) — strict, no cross-doc extraction
    - UNBOUND instructions (field X, no doc specified) — search all docs

    Args:
        prompt: The user's free-text extraction prompt.
        document_filenames: List of filenames in the lead (for LLM context).

    Returns:
        ExtractionPlan with validated, structured instructions and keywords.
    """
    # Return default plan for empty/short prompts
    if not prompt or len(prompt.strip()) < 5:
        return ExtractionPlan(raw_prompt=prompt or "")

    # Check cache
    if prompt in _prompt_plan_cache:
        return _prompt_plan_cache[prompt]

    # Build the full prompt with document context
    file_context = ""
    if document_filenames:
        file_context = "\n\nDocuments available in this lead:\n" + "\n".join(
            f"  - {fn}" for fn in document_filenames if fn
        )

    full_prompt = (
        f"{_PLANNER_SYSTEM_PROMPT}{file_context}\n\n"
        f"User Prompt: {prompt}\n\n"
        f"Return ONLY valid JSON."
    )

    # Try Medha first, then Gemini
    raw_response = _call_medha_text(full_prompt)
    if not raw_response:
        raw_response = _call_gemini_text(full_prompt)

    # Parse and build the plan
    plan = ExtractionPlan(raw_prompt=prompt)

    if raw_response:
        parsed = _parse_json_robust(raw_response)
        if parsed:
            # Parse instructions
            raw_instructions = parsed.get("instructions", [])
            if isinstance(raw_instructions, list):
                for inst_data in raw_instructions:
                    if isinstance(inst_data, dict):
                        validated = _validate_instruction(inst_data)
                        if validated:
                            plan.instructions.append(validated)

            # Parse keywords
            raw_keywords = parsed.get("keywords", [])
            if isinstance(raw_keywords, list):
                plan.keywords = [
                    str(k).lower().strip()
                    for k in raw_keywords
                    if str(k).strip()
                ]

    # Cache the result
    _prompt_plan_cache[prompt] = plan
    return plan
