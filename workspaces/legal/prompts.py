"""
SARFAESI Section 13(2) VLM Extraction Prompts.

Document-type-specific structured extraction prompts for Indian banking
loan dossier processing. Covers Sanction Letters, Foreclosure Notices,
MODT deeds, Legal Reports, Collateral Documents, and Loan Agreements.

All prompts enforce:
- Pure JSON output (no markdown fences)
- Anti-hallucination: null for absent fields
- Multilingual support (Hindi Devanagari, regional scripts)
- Safe regex patterns (zero catastrophic backtracking)
"""
from __future__ import annotations

# ── prompt version tag (used in ocr_cache key) ──────────────────────────────
PROMPT_VERSION = "sarfaesi_v1"

# ── document classification ──────────────────────────────────────────────────
SARFAESI_CLASSIFY_PROMPT = """You are an expert Indian banking document classifier for SARFAESI Section 13(2) recovery proceedings.

Examine this document image and classify it into ONE of these types:
- sanction_letter: Loan sanction/approval letter from a bank/NBFC
- foreclosure_notice: Foreclosure (FCL) notice with financial breakdown of outstanding amounts
- modt: Memorandum of Deposit of Title Deeds (mortgage deed)
- legal_report: Legal / Title Search Report / Advocate Search Report
- collateral_deed: Collateral Document / Title Deed / Sale Deed / Patta / Conveyance deed
- loan_agreement: Full loan agreement document (typically 30-80 pages)
- identity_document: PAN card, Aadhaar, Passport, Voter ID
- valuation_report: Property valuation report
- bank_statement: Account statement / passbook
- other: Any other document type

Return ONLY a valid JSON object (no markdown, no prose):
{
  "document_type": "<type from list above>",
  "confidence": 0.95,
  "page_description": "<brief 5-10 word description of what this page shows>",
  "has_schedule": false,
  "estimated_pages": 1,
  "language": "<english|hindi|bilingual|other>"
}

Never invent facts. If uncertain, set confidence below 0.5."""


# ── sanction letter extraction ───────────────────────────────────────────────
SANCTION_LETTER_EXTRACT_PROMPT = """You are an expert Indian banking document analyst extracting data from a Loan Sanction Letter / Approval Letter.

Extract ALL available fields from this document. Return ONLY valid JSON (no markdown fences, no prose):
{
  "account_no_lan": "<Loan Account Number / LAN exactly as printed, e.g. 'HFXYZ12345' or null>",
  "applicant_name": "<Primary borrower / applicant full name, or null>",
  "applicant_address": "<Full residential address of the primary applicant, or null>",
  "co_applicant_1": "<First co-applicant / co-borrower name, or null>",
  "co_applicant_address_1": "<Address of first co-applicant, or null>",
  "co_applicant_2": "<Second co-applicant name, or null>",
  "co_applicant_address_2": "<Address of second co-applicant, or null>",
  "co_applicant_3": "<Third co-applicant name, or null>",
  "co_applicant_address_3": "<Address of third co-applicant, or null>",
  "guarantor_1": "<First guarantor name, or null>",
  "guarantor_1_add": "<Address of first guarantor, or null>",
  "guarantor_2": "<Second guarantor name, or null>",
  "guarantor_2_add": "<Address of second guarantor, or null>",
  "sanction_amount": "<Sanctioned loan amount as a number without commas, e.g. 1500000.00, or null>",
  "sanction_amount_in_words": "<Amount in words exactly as printed, e.g. 'Rupees Fifteen Lakhs Only', or null>",
  "roi_in_number": "<Rate of Interest as printed, e.g. '12.50% p.a.' or '12.5', or null>",
  "sanction_date": "<Date of sanction exactly as printed, e.g. '15/03/2023' or '15th March 2023', or null>",
  "disbursal_date": "<Date of disbursal if mentioned, or null>",
  "branch_name": "<Lending branch name if mentioned, or null>",
  "lender_name": "<Bank / NBFC / HFC name, or null>",
  "loan_type": "<Type of loan: Home Loan, LAP, Business Loan, etc., or null>",
  "property_address": "<Property address if mentioned in sanction letter, or null>",
  "verbatim_text": "<Full verbatim text transcription of the document>"
}

Rules:
- Never invent facts, dates, names, or numbers. If a field is not visible or readable, set it to null.
- For amounts, extract the pure numeric value without currency symbols or commas.
- Preserve names exactly as printed (do not add honorifics or titles).
- If the document is in Hindi/regional script, still extract field values but transliterate names to English if clearly identifiable."""


# ── foreclosure notice extraction ────────────────────────────────────────────
FCL_EXTRACT_PROMPT = """You are an expert Indian banking document analyst extracting data from a Foreclosure Notice (FCL) / Demand Notice under SARFAESI Act.

This document contains the financial breakdown of outstanding amounts. Extract ALL fields precisely.
Return ONLY valid JSON (no markdown fences, no prose):
{
  "account_no_lan": "<Loan Account Number / LAN, or null>",
  "applicant_name": "<Borrower name as mentioned in the notice, or null>",
  "applicant_address": "<Borrower address, or null>",
  "npa_date": "<NPA (Non-Performing Asset) classification date, or null>",
  "demand_date": "<Date of the demand/notice, or null>",
  "future_principal": "<Future Principal amount as number, or null>",
  "principal_overdue": "<Principal Overdue amount as number, or null>",
  "interest_overdue": "<Interest Overdue amount as number, or null>",
  "interest_on_termination": "<Interest on Termination amount as number, or null>",
  "late_payment_penal": "<Late Payment Penal / Penal Interest amount as number, or null>",
  "cheque_bounce_inc_gst": "<Cheque Bounce charges including GST as number, or null>",
  "other_charges_inc_gst": "<Other Charges including GST as number, or null>",
  "foreclosure_charges": "<Foreclosure Charges as number, or null>",
  "litigation_charges": "<Litigation Charges as number, or null>",
  "excess_amount": "<Excess Amount (credit) as number, or null>",
  "net_receivable": "<Net Receivable / Total Due as number, or null>",
  "co_applicant_1": "<Co-borrower 1 name if listed, or null>",
  "co_applicant_2": "<Co-borrower 2 name if listed, or null>",
  "guarantor_1": "<Guarantor 1 name if listed, or null>",
  "guarantor_2": "<Guarantor 2 name if listed, or null>",
  "lender_name": "<Bank / NBFC name, or null>",
  "verbatim_text": "<Full verbatim text transcription>"
}

CRITICAL CALCULATION: TOS (Total Outstanding Sum) = Net Receivable - Foreclosure Charges.
If Net Receivable is absent, TOS = Sanction Amount (from other documents).

Rules:
- All amounts must be clean numbers without currency symbols or commas (e.g. 1234567.89)
- Never invent or estimate amounts. If a line item is not present, set to null.
- Look for tabular financial breakdowns — these often appear as labeled rows."""


# ── mortgaged property extraction (MODT / Legal Report / Collateral) ─────────
PROPERTY_EXTRACT_PROMPT = """You are an expert Indian legal property document analyst extracting mortgaged property details from a {document_type}.

For MODT documents: property schedules are typically in Schedule III / Schedule B, found in the LAST 3 pages or FIRST 2 pages of the document.
For Legal Reports: property details are typically in pages 0-3.
For Collateral Deeds: property descriptions are in the body of the deed.

Extract ALL property details. Return ONLY valid JSON (no markdown fences, no prose):
{{
  "property_owner_mortgagor": "<Name of the property owner / mortgagor exactly as stated, or null>",
  "mortgaged_property_detail_1": "<COMPLETE description of the first/primary mortgaged property including survey number, plot number, area, building details, or null>",
  "directions": "<4-way boundaries: North: [boundary] | South: [boundary] | East: [boundary] | West: [boundary], or null>",
  "mortgaged_property_detail_2": "<Description of second mortgaged property if exists, or null>",
  "directions_2": "<4-way boundaries for second property: North: ... | South: ... | East: ... | West: ..., or null>",
  "property_area": "<Total area with unit, e.g. '1200 sq.ft.' or '5 Guntha', or null>",
  "property_type": "<Residential/Commercial/Agricultural/Industrial/Plot, or null>",
  "survey_number": "<Survey/Khasra/Plot number, or null>",
  "village_locality": "<Village or locality name, or null>",
  "taluka_tehsil": "<Taluka/Tehsil name, or null>",
  "district": "<District name, or null>",
  "state": "<State name, or null>",
  "pin_code": "<PIN code if mentioned, or null>",
  "registration_details": "<Document registration number and sub-registrar office, or null>",
  "verbatim_text": "<Full verbatim text of the property schedule/description section>"
}}

Rules:
- Extract the COMPLETE property description verbatim — do not summarize or shorten.
- 4-way boundaries (North/South/East/West) must be extracted exactly as written.
- If the document mentions multiple properties, extract both with separate detail/direction fields.
- Never invent property details. If boundaries or details are not present, set to null."""


# ── loan agreement schedule extraction ───────────────────────────────────────
LOAN_AGREEMENT_SCHEDULE_PROMPT = """You are an expert Indian banking document analyst extracting data from the SCHEDULE section of a Loan Agreement.

The schedule section typically appears on pages 26-40 of the agreement. Look for sections headed:
- "Schedule of Charges"
- "Date of Agreement"
- "Location of the Lender's Branch"
- "Borrower(s) Details" / "Details of Borrower"
- "Co-Borrower Details"
- "Guarantor Details"

Extract ALL available fields. Return ONLY valid JSON (no markdown fences, no prose):
{
  "account_no_lan": "<Loan Account Number / Agreement Number, or null>",
  "agreement_date": "<Date of the loan agreement, or null>",
  "branch_name": "<Branch name / location, or null>",
  "applicant_name": "<Primary borrower name from schedule table, or null>",
  "applicant_address": "<Borrower address from schedule, or null>",
  "co_applicant_1": "<Co-borrower 1 name, or null>",
  "co_applicant_address_1": "<Co-borrower 1 address, or null>",
  "co_applicant_2": "<Co-borrower 2 name, or null>",
  "co_applicant_address_2": "<Co-borrower 2 address, or null>",
  "co_applicant_3": "<Co-borrower 3 name, or null>",
  "co_applicant_address_3": "<Co-borrower 3 address, or null>",
  "guarantor_1": "<Guarantor 1 name, or null>",
  "guarantor_1_add": "<Guarantor 1 address, or null>",
  "guarantor_2": "<Guarantor 2 name, or null>",
  "guarantor_2_add": "<Guarantor 2 address, or null>",
  "sanction_amount": "<Loan amount as number, or null>",
  "roi_in_number": "<Rate of interest, or null>",
  "disbursal_date": "<Date of disbursal, or null>",
  "property_address": "<Mortgaged property address if in schedule, or null>",
  "verbatim_text": "<Verbatim text of the schedule pages>"
}

Rules:
- Borrower/Co-borrower details are often in a TABLE format — extract names and addresses from table rows.
- Never invent facts. Set null for absent fields.
- If there's an S.No. / Name / Address table, extract each row as a separate co-applicant."""


# ── generic document extraction (fallback) ───────────────────────────────────
DOCUMENT_EXTRACT_PROMPT = """You are an expert document analyst. Extract all identifiable legal, financial, and personal entity information from this document image.

Return ONLY valid JSON (no markdown fences, no prose):
{{
  "document_type": "{document_type}",
  "title_heading": "<main title or heading of the document, or null>",
  "date": "<any date found on the document, or null>",
  "reference_number": "<any reference/case/file number, or null>",
  "account_no_lan": "<loan account number if found, or null>",
  "borrower_name": "<borrower/applicant name if found, or null>",
  "borrower_address": "<borrower address if found, or null>",
  "lender_name": "<bank/NBFC/lender name if found, or null>",
  "amount": "<any monetary amount as number, or null>",
  "property_address": "<any property address if found, or null>",
  "summary": "<2-3 sentence summary of the document content>",
  "confidence": 0.7,
  "verbatim_text": "<full verbatim text transcription>",
  "language": "<english|hindi|bilingual|other>"
}}

Rules:
- Never invent facts. If a field is absent, set to null.
- Extract text in original script (Hindi Devanagari, regional scripts).
- If the document contains tables, extract key data from table cells."""
