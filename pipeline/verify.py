"""
STAGE 4 - Deterministic verification.

Matches the document against the system record on the mandatory fields for the
lender:
    - every lender : date, amount, receiver-name (tiered: lender's own name ->
                     approved allowlist -> receiver UPI id on the document)
    - SMFG lenders : ALSO loan_account_number (LAN)

100% deterministic, no model calls. A field is only "matched" on positive
evidence; anything unconfirmed keeps the lead OUT of `verified`. Receiver
matching is soft but token-anchored: a match must carry a DISTINCTIVE element
of the accepted name (hdb/smfg/jana...), never just generic words
(bank/finance/services...). See check_receiver for the full hierarchy.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime

from config import settings
from pipeline.models import VERIFIED, UNVERIFIED

# ── load config once ──────────────────────────────────────────────────────────
with open(settings.LENDER_RECEIVERS_PATH, encoding="utf-8") as f:
    LENDER_RECEIVERS = json.load(f)
with open(settings.LENDER_RULES_PATH, encoding="utf-8") as f:
    LENDER_RULES = json.load(f)

_CONFIG_LOCK = threading.Lock()


def reload_config() -> None:
    """Re-read the lender config from disk IN PLACE, so every module holding a
    reference to these dicts (metrics, workers) sees the update immediately."""
    with open(settings.LENDER_RECEIVERS_PATH, encoding="utf-8") as f:
        LENDER_RECEIVERS.clear()
        LENDER_RECEIVERS.update(json.load(f))
    with open(settings.LENDER_RULES_PATH, encoding="utf-8") as f:
        LENDER_RULES.clear()
        LENDER_RULES.update(json.load(f))


def add_receiver(lender: str, name: str) -> bool:
    """Append an approved receiver name to the lender's allowlist and persist it
    (atomic temp-file replace, thread-safe). Returns False if already present.
    This is how a human receiver-approval permanently teaches the config."""
    lender, name = str(lender or "").strip(), str(name or "").strip()
    if not lender or not name:
        return False
    with _CONFIG_LOCK:
        with open(settings.LENDER_RECEIVERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        existing = data.get(lender, [])
        if any(_norm_name(n) == _norm_name(name) for n in existing):
            reload_config()                      # ensure memory matches disk anyway
            return False
        data[lender] = existing + [name]
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(settings.LENDER_RECEIVERS_PATH),
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, settings.LENDER_RECEIVERS_PATH)
        reload_config()
    return True


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


# ── soft (token-anchored) matching ────────────────────────────────────────────
# Generic corporate/finance words carry no identity: a soft match must anchor on a
# DISTINCTIVE token of the accepted name ("hdb", "smfg", "jana"), never on these.
# "Ram Financial Services" must NOT match "HDB Financial Services" even though the
# generic tail is identical.
_GENERIC_TOKENS = frozenset("""
    bank banking finance financial finserv fintech services service limited ltd
    pvt private company co corporation corp enterprises enterprise group india
    indian small credit card cards loan loans capital payment payments pay app
    technologies technology solutions international care and the of for
""".split())


def _distinctive(norm_name: str) -> list:
    return [t for t in norm_name.split() if len(t) >= 3 and t not in _GENERIC_TOKENS]


def _lev_le(a: str, b: str, k: int) -> bool:
    """Levenshtein distance <= k (bounded DP with early exit)."""
    if abs(len(a) - len(b)) > k:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > k:
            return False
        prev = cur
    return prev[-1] <= k


def _tok_match(t: str, c: str) -> bool:
    """A distinctive token matches a receiver token exactly, or within 1 typo for
    tokens >= 4 chars (2 typos >= 8). 3-char tokens ('hdb', 'bob') must be exact."""
    if t == c:
        return True
    if len(t) >= 8:
        return _lev_le(t, c, 2)
    if len(t) >= 4:
        return _lev_le(t, c, 1)
    return False


def _soft_match(cand: str, name: str) -> bool:
    """Field-level soft match: today's equality/containment, plus a distinctive-token
    anchor — 'HDB Fin Services' matches 'HDB Financial Services Limited' because the
    main element 'hdb' is present; generic words alone can never carry a match."""
    n = _norm_name(name)
    if not n or not cand:
        return False
    if _field_matches(n, cand):
        return True
    ctoks = cand.split()
    return any(_tok_match(t, c) for t in _distinctive(n) for c in ctoks)


# ── receiver UPI id (tier 3) ──────────────────────────────────────────────────
# A UPI handle names the beneficiary account, so it can rescue a payee printed under
# a personal/shop name. Domain must not be followed by .tld — that's an email, and a
# support email on the receipt is NOT evidence of where the money went.
_UPI_RE = re.compile(r"\b([a-z0-9][a-z0-9._-]{1,63})@([a-z][a-z0-9]{1,15})(?!\.?[a-z])",
                     re.IGNORECASE)


def _upi_handles(raw_text) -> list:
    """Deterministic UPI-handle extraction from the ORIGINAL OCR text (normalisation
    strips '@'). Returns unique (local_part, full_handle), lowercased."""
    out, seen = [], set()
    for m in _UPI_RE.finditer(str(raw_text or "").lower()):
        full = m.group(0)
        if full not in seen:
            seen.add(full)
            out.append((m.group(1), full))
    return out


def _seg_carries_name(seg: str, toks: list, distinct: set) -> bool:
    """Can this handle segment be read ENTIRELY as pieces of the accepted name —
    whole tokens, prefixes (>=3 chars) of tokens, or digit runs — with at least one
    full DISTINCTIVE token present? 'janabank' = jana+bank -> yes; 'janardhan' has
    'jana' but 'rdhan' is not part of the name -> no false positive."""
    n = len(seg)
    dp = [0] * (n + 1)          # 0 unreachable · 1 reachable · 2 reachable w/ anchor
    dp[0] = 1
    for i in range(n):
        if not dp[i]:
            continue
        j = i
        while j < n and seg[j].isdigit():
            j += 1
        if j > i:
            dp[j] = max(dp[j], dp[i])
        for t in toks:
            cp = 0
            while cp < len(t) and i + cp < n and seg[i + cp] == t[cp]:
                cp += 1
            for length in range(3, cp + 1):
                if length == len(t):
                    dp[i + length] = max(dp[i + length], 2 if t in distinct else dp[i])
                else:
                    dp[i + length] = max(dp[i + length], dp[i])
            if len(t) < 3 and cp == len(t):          # short whole tokens ('of', 'pl')
                dp[i + cp] = max(dp[i + cp], dp[i])
    return dp[n] == 2


def _upi_receiver_hit(raw_text, names) -> tuple | None:
    """Does any UPI handle on the document carry an accepted name? Checks each
    dot/dash-separated segment of the local part. Returns (matched_name, handle)."""
    handles = _upi_handles(raw_text)
    if not handles:
        return None
    for name in names:
        n = _norm_name(name)
        toks = n.split()
        distinct = set(_distinctive(n))
        if not distinct:
            continue
        for local, full in handles:
            for seg in re.split(r"[._-]", local):
                if seg and _seg_carries_name(seg, toks, distinct):
                    return name, full
    return None


def _word_in_text(n: str, text: str) -> bool:
    """Whole-word (boundary) match of an accepted name inside the OCR body text. Stricter
    than a raw substring so a generic fragment can't leak a false match — used only when
    the payee wasn't captured as a field."""
    if not n or not text:
        return False
    return re.search(r"\b" + re.escape(n) + r"\b", text) is not None


def check_receiver(doc_receiver, doc_text, lender) -> tuple[bool, str]:
    """Receiver check — a strict tier hierarchy, 100% deterministic (no model calls):

      1. Payee FIELD vs the lender's OWN name (always first, soft token-anchored).
      2. Payee FIELD vs the lender's approved allowlist (soft token-anchored).
      3. Receiver UPI ID on the document vs both sets. A UPI handle identifies the
         beneficiary ACCOUNT, so it may rescue a payee printed under another name
         ("Ram Kirana Store" + smfgindia@icici -> verified). Lookalikes are rejected:
         the handle must decompose into the accepted name's own tokens.
      4. Payee field EMPTY -> whole-word match of an accepted name in the body OCR
         text (word-boundary only, so a generic fragment can't manufacture a match).
      5. Otherwise FAIL closed -> the lead stays unverified (Approvals queue).

    "Soft" means anchored on a DISTINCTIVE token of the accepted name (exact, or
    within 1 typo for tokens >= 4 chars); generic words (bank/finance/services...)
    can never carry a match on their own.
    """
    allow = accepted_receivers(lender)
    self_names = _lender_self_names(lender)
    cand = _norm_name(doc_receiver)
    text = _norm_name(doc_text)

    # 1) the payee is the lender itself
    for orig in self_names:
        if _soft_match(cand, orig):
            return True, f"receiver matches lender name '{orig}'"

    # 2) the payee is an approved receiver for this lender
    for orig in allow:
        if _soft_match(cand, orig):
            return True, f"receiver matches approved receiver '{orig}'"

    # 3) the receiver UPI id carries the lender / an approved receiver name
    hit = _upi_receiver_hit(doc_text, self_names + allow)
    if hit:
        name, handle = hit
        return True, f"receiver UPI id '{handle}' carries '{name}'"

    # 4) a readable payee that matches NOBODY accepted → the money went elsewhere
    if cand:
        who = str(doc_receiver).strip()
        if allow:
            return False, f"receiver '{who}' is not among accepted names for {lender}"
        return False, f"receiver '{who}' does not match {lender} (no allowlist configured)"

    # 5) no payee field — accept only a distinct, whole-word name in the document text
    for orig in (allow + self_names):
        n = _norm_name(orig)
        if n and _word_in_text(n, text):
            return True, f"payee not in a field; '{orig}' found on document"
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
