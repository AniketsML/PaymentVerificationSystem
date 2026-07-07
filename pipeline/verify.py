"""
STAGE 4 - Deterministic verification.

Matches the document against the system record on the mandatory fields for the
lender:
    - every lender : date, amount, receiver-name (against that lender's allowlist)
    - SMFG lenders : ALSO loan_account_number (LAN)

100% deterministic, no model calls. A field is only "matched" on positive
evidence; anything unconfirmed keeps the lead OUT of `verified`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from config import settings
from pipeline.models import VERIFIED, UNVERIFIED

# ── load config once ──────────────────────────────────────────────────────────
with open(settings.LENDER_RECEIVERS_PATH, encoding="utf-8") as f:
    LENDER_RECEIVERS = json.load(f)
with open(settings.LENDER_RULES_PATH, encoding="utf-8") as f:
    LENDER_RULES = json.load(f)


# ── normalisers ───────────────────────────────────────────────────────────────
def _norm_name(s) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _num(s):
    if s is None:
        return None
    m = re.search(r"[0-9]+(?:\.[0-9]{1,2})?", str(s).replace(",", ""))
    return float(m.group(0)) if m else None


def _parse_date(s):
    if not s:
        return None
    from dateutil import parser as dp
    try:
        return dp.parse(str(s), dayfirst=True, fuzzy=True).date()
    except Exception:
        return None


def rule_for(lender: str) -> dict:
    return LENDER_RULES.get(lender, LENDER_RULES["__default__"])


def accepted_receivers(lender: str) -> list:
    return LENDER_RECEIVERS.get(lender, [])


# ── individual field checks ───────────────────────────────────────────────────
def check_amount(doc_amount, sys_amount, tol) -> tuple[bool, str]:
    d, s = _num(doc_amount), _num(sys_amount)
    if d is None:
        return False, "amount not readable on document"
    if s is None:
        return False, "system amount missing"
    if abs(d - s) <= tol:
        return True, f"amount matches ({d:.2f})"
    return False, f"amount mismatch (doc {d:.2f} vs system {s:.2f})"


def check_date(doc_date, sys_date, tol_days) -> tuple[bool, str]:
    d, s = _parse_date(doc_date), _parse_date(sys_date)
    if d is None:
        return False, "date not readable on document"
    if s is None:
        return False, "system date missing"
    gap = abs((d - s).days)
    if gap <= tol_days:
        return True, f"date matches (gap {gap}d)"
    return False, f"date mismatch (gap {gap}d > {tol_days}d - possible old/recycled receipt)"


def _lender_self_names(lender: str) -> list:
    """Candidate names derived from the lender's own code, used ONLY as a fallback
    when no receiver allowlist is configured. 'MOBIKWIK_SOE' -> ['MOBIKWIK SOE',
    'MOBIKWIK']. Tokens shorter than 4 chars are dropped so a tiny/generic code
    cannot spuriously match unrelated text (keeps zero-false-positives intact)."""
    code = str(lender or "").strip()
    if not code:
        return []
    out, seen = [], set()
    for c in (code.replace("_", " "), code.split("_")[0]):
        n = _norm_name(c)
        if len(n) >= 4 and n not in seen:
            seen.add(n)
            out.append(c)
    return out


def _field_matches(n: str, cand: str) -> bool:
    """Does the extracted receiver FIELD match an accepted name? Field-level match is
    strong evidence (the model already isolated the payee), so equality or containment
    either direction is accepted."""
    return bool(cand) and (n == cand or n in cand or cand in n)


def _word_in_text(n: str, text: str) -> bool:
    """Whole-word (boundary) match of an accepted name inside the OCR body text. Stricter
    than a raw substring so a generic fragment can't leak a false match — used only when
    the payee wasn't captured as a field."""
    if not n or not text:
        return False
    return re.search(r"\b" + re.escape(n) + r"\b", text) is not None


def check_receiver(doc_receiver, doc_text, lender) -> tuple[bool, str]:
    """Zero-false-positive receiver check.

    Priority:
      1. The extracted receiver/payee FIELD matches an accepted name  → PASS (strong).
      2. The receiver field is POPULATED but matches NONE of them → FAIL closed. A
         readable payee that is a *different party* is positive evidence the money did
         NOT go to the lender; a stray mention of the lender elsewhere on the page must
         NOT rescue it. (This is what previously produced ~10% false-positive verifies.)
      3. The receiver field is EMPTY/unreadable → fall back to a whole-word match of an
         accepted name in the body OCR text. Word-boundary only, so a generic token
         can't manufacture a match.
    """
    allow = accepted_receivers(lender)
    accepted = allow if allow else _lender_self_names(lender)
    norm = [(_norm_name(n), n) for n in accepted]
    norm = [(n, orig) for n, orig in norm if n]
    cand = _norm_name(doc_receiver)
    text = _norm_name(doc_text)
    tail = "" if allow else " (no allowlist configured)"

    # 1) strongest evidence: the payee field itself matches an accepted name
    for n, orig in norm:
        if _field_matches(n, cand):
            return True, f"receiver matches '{orig}'{tail}"

    # 2) a readable payee that matches NOBODY accepted → the money went elsewhere
    if cand:
        who = str(doc_receiver).strip()
        if allow:
            return False, f"receiver '{who}' is not among accepted names for {lender}"
        return False, f"receiver '{who}' does not match {lender}{tail}"

    # 3) no payee field — accept only a distinct, whole-word name in the document text
    for n, orig in norm:
        if _word_in_text(n, text):
            return True, f"payee not in a field; '{orig}' found on document{tail}"
    if allow:
        return False, f"receiver unreadable and no accepted name found on document for {lender}"
    return False, (f"no accepted-receiver list configured for {lender}, "
                   f"and lender name not found on document")


def check_lan(doc_lan, sys_lan) -> tuple[bool, str]:
    d, s = _digits(doc_lan), _digits(sys_lan)
    if not d:
        return False, "loan account number not readable on document"
    if not s:
        return False, "system loan account number missing"
    if d == s or (len(s) >= 8 and (s in d or d in s)):
        return True, "loan account number matches"
    return False, "loan account number mismatch"


# ── direction guard ───────────────────────────────────────────────────────────
# Phrases marking money coming INTO the submitter's OWN account (an incoming credit /
# receipt of funds) — which is NOT proof of an outgoing loan repayment.
_INCOMING_CREDIT = (
    "credited to your account", "credited to your a/c", "credited to your acc",
    "credited in your account", "credited to your saving", "credited to your bank",
    "amount credited to your", "received in your account", "has been credited to your",
)
# Any outgoing-payment phrase means the document IS about a payment going out, so the
# guard stands down (real repayment receipts say paid / debited / sent-to).
_OUTGOING_PAYMENT = (
    "debited", "debit", "paid to", "amount paid", "you paid", "you have paid",
    "payment to", "payment of", "sent to", "transferred to", "paid successfully",
    "paid rs", "paid inr", "debited from",
)


def looks_incoming_credit(text) -> bool:
    """Deterministic: does the proof read as an incoming credit to the submitter's OWN
    account (money received) rather than an outgoing loan repayment? Such a document is
    ambiguous evidence and must never auto-verify. An outgoing-payment phrase anywhere
    stands the guard down, so genuine payment/debit receipts are unaffected."""
    t = (text or "").lower()
    if any(m in t for m in _OUTGOING_PAYMENT):
        return False
    return any(m in t for m in _INCOMING_CREDIT)


# ── orchestrated verification ─────────────────────────────────────────────────
def run(doc, row: dict) -> tuple[str, dict]:
    """Returns (verification_status, outcome_dict)."""
    lender = row.get("institute_name", "")
    rule = rule_for(lender)

    # reconciliation-dependent lenders (e.g. SMFG_RURAL cash collection) cannot be
    # verified from the document alone - the truth is whether the agent deposited
    # the cash. Never auto-verify; route to reconciliation/review.
    if rule.get("reconciliation_dependent"):
        return UNVERIFIED, {
            "reason": "requires cash-deposit reconciliation - not verifiable from document alone",
            "failed_fields": ["reconciliation"],
            "needs": "cash_reconciliation_feed",
        }

    tol_amt = rule["amount_tolerance_rupees"]
    tol_date = rule["date_tolerance_days"]

    checks = {
        "date": check_date(doc.date, row.get("payment_date"), tol_date),
        "amount": check_amount(doc.amount, row.get("payment_amount"), tol_amt),
        "receiver": check_receiver(doc.receiver_name, doc.raw_text, lender),
    }
    if rule["needs_lan"]:
        checks["loan_account_number"] = check_lan(doc.loan_account_number,
                                                  row.get("loan_account_number"))

    # numeric match margins — structured, for analytics/Grafana. Computed alongside
    # the checks; they do NOT influence the verdict (which is decided by `checks`).
    margins = {}
    _dd, _sd = _parse_date(doc.date), _parse_date(row.get("payment_date"))
    if _dd and _sd:
        margins["date_gap_days"] = abs((_dd - _sd).days)
    _da, _sa = _num(doc.amount), _num(row.get("payment_amount"))
    if _da is not None and _sa is not None:
        margins["amount_delta"] = round(abs(_da - _sa), 2)

    mandatory = rule["mandatory_fields"]
    verified_fields = [f for f in mandatory if checks.get(f, (False, ""))[0]]
    failed = [(f, checks[f][1]) for f in mandatory if not checks.get(f, (False, ""))[0]]

    # Direction guard: an incoming credit to the submitter's own account is not proof
    # of an outgoing loan repayment. Never auto-verify it — route to human review even
    # if every field matches. Only downgrades verified->unverified; never the reverse.
    if looks_incoming_credit(doc.raw_text):
        return UNVERIFIED, {
            "reason": "looks like an incoming credit to your own account, not an outgoing loan repayment - needs review",
            "failed_fields": ["direction"],
            "matched_fields": verified_fields,
            "details": {f: checks[f][1] for f in mandatory},
            "margins": margins,
            "flag": "incoming_credit",
        }

    if not failed:
        outcome = {
            "verified_fields": verified_fields,
            "details": {f: checks[f][1] for f in mandatory},
            "margins": margins,
            "lender_rule": {"mandatory": mandatory, "needs_lan": rule["needs_lan"]},
        }
        return VERIFIED, outcome

    outcome = {
        "reason": "; ".join(f"{f}: {msg}" for f, msg in failed),
        "failed_fields": [f for f, _ in failed],
        "matched_fields": verified_fields,
        "details": {f: checks[f][1] for f in mandatory},
        "margins": margins,
    }
    return UNVERIFIED, outcome
