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


def check_receiver(doc_receiver, doc_text, lender) -> tuple[bool, str]:
    allow = accepted_receivers(lender)
    if allow:
        cand = _norm_name(doc_receiver)
        text = _norm_name(doc_text)
        for name in allow:
            n = _norm_name(name)
            if not n:
                continue
            # matched if the receiver field equals/contains it, or it appears in the receipt text
            if cand and (n == cand or n in cand or cand in n):
                return True, f"receiver matches '{name}'"
            if n in text:
                return True, f"receiver '{name}' found on document"
        return False, f"receiver not among accepted names for {lender}"

    # ── no allowlist configured: fall back to the lender's OWN name ────────────
    # A document whose payee clearly IS the lender is still positive evidence. We
    # require the full lender name to appear in the receiver field or anywhere in
    # the OCR text (strict direction only — we do NOT accept the receiver being a
    # mere fragment of the lender name), so this never manufactures a false match.
    cand = _norm_name(doc_receiver)
    text = _norm_name(doc_text)
    for name in _lender_self_names(lender):
        n = _norm_name(name)
        if cand and (n == cand or n in cand):
            return True, f"receiver matches lender name '{name}' (no allowlist configured)"
        if n and n in text:
            return True, f"lender name '{name}' found on document (no allowlist configured)"
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

    mandatory = rule["mandatory_fields"]
    verified_fields = [f for f in mandatory if checks.get(f, (False, ""))[0]]
    failed = [(f, checks[f][1]) for f in mandatory if not checks.get(f, (False, ""))[0]]

    if not failed:
        outcome = {
            "verified_fields": verified_fields,
            "details": {f: checks[f][1] for f in mandatory},
            "lender_rule": {"mandatory": mandatory, "needs_lan": rule["needs_lan"]},
        }
        return VERIFIED, outcome

    outcome = {
        "reason": "; ".join(f"{f}: {msg}" for f, msg in failed),
        "failed_fields": [f for f, _ in failed],
        "matched_fields": verified_fields,
        "details": {f: checks[f][1] for f in mandatory},
    }
    return UNVERIFIED, outcome
