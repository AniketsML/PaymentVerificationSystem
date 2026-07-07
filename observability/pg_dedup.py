"""
Payment de-duplication (Postgres) — CSV-identity based, runs BEFORE OCR.

Identity of a payment = (lead_code, loan_account_number, amount, payment-month).
All four come from the CSV row, so this is a cheap indexed lookup that needs no
model call — exact duplicates are caught before we spend an OCR request.

Verdicts (hybrid rule):
  skip          - not enough identity (missing lead_code / loan / amount / date)
  new           - lead_code never seen -> process normally
  emi           - lead_code + same loan seen before, different month/amount
                  -> legitimate new installment, process normally
  duplicate     - exact (lead_code + loan + amount + month) already processed
  manual_review - lead_code seen, but under a DIFFERENT loan account (suspicious)

Speed: one index hit on lead_code returns the few payments for that lead; the
exact/loan comparison is done in memory. Scales to millions of rows.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from db import pg


def _norm_loan(s) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def _norm_amount(s):
    m = re.search(r"[0-9]+(?:\.[0-9]{1,2})?", str(s or "").replace(",", ""))
    if not m:
        return None
    try:
        return Decimal(m.group(0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _pay_ym(s):
    if not s:
        return None
    from dateutil import parser as dp
    txt = str(s)
    # Indian dates are day-first (DD/MM/YYYY), but ISO YYYY-MM-DD must NOT be day-first
    dayfirst = not re.match(r"^\s*\d{4}-\d{1,2}-\d{1,2}", txt)
    try:
        d = dp.parse(txt, dayfirst=dayfirst, fuzzy=True).date()
        return f"{d.year:04d}-{d.month:02d}"
    except Exception:
        return None


class PaymentDedup:
    def __init__(self, *_a, **_k):
        pg.init_schema()

    def identity(self, row: dict):
        lc = str(row.get("lead_code", "") or "").strip()
        loan = _norm_loan(row.get("loan_account_number"))
        amt = _norm_amount(row.get("payment_amount"))
        ym = _pay_ym(row.get("payment_date"))
        if not (lc and loan and amt is not None and ym):
            return None
        return {"lead_code": lc, "loan_acct": loan, "amount": amt, "pay_ym": ym}

    def evaluate(self, row: dict, is_test: bool = False, self_lead_id: str | None = None):
        """Returns (verdict, reason, identity|None). is_test scopes the lookup to the
        matching (real vs sandbox) ledger so the Test workspace dedups independently.

        self_lead_id: rows recorded by THIS lead are ignored — a retry (model error,
        crashed-worker lease reclaim) must never see its own attempt-1 record and
        classify itself as a duplicate. A duplicate is a DIFFERENT submission of the
        same payment, never the same lead re-processing."""
        ident = self.identity(row)
        if ident is None:
            return "skip", "insufficient identity for dedup (need lead_code, loan a/c, amount, date)", None
        with pg.pool().connection() as c:
            rows = c.execute(
                "SELECT loan_acct, amount, pay_ym, lead_id FROM processed_payments "
                "WHERE lead_code = %s AND is_test = %s", (ident["lead_code"], is_test)).fetchall()
        if self_lead_id is not None:
            rows = [r for r in rows if r["lead_id"] != self_lead_id]
        if not rows:
            return "new", "first submission for this lead_code", ident
        for r in rows:
            if (r["loan_acct"] == ident["loan_acct"] and r["amount"] == ident["amount"]
                    and r["pay_ym"] == ident["pay_ym"]):
                return ("duplicate",
                        f"already processed (lead {r['lead_id']}, loan {ident['loan_acct']}, "
                        f"amount {ident['amount']}, month {ident['pay_ym']})", ident)
        if any(r["loan_acct"] == ident["loan_acct"] for r in rows):
            return "emi", "same lead & loan, different installment - processing normally", ident
        return ("manual_review",
                "different loan account under an already-seen lead_code - needs review", ident)

    def claim(self, ident: dict, lead_id: str, is_test: bool = False) -> str:
        """ATOMICALLY claim this payment identity; returns the OWNING lead_id.

        The unique index arbitrates concurrency: when two leads with the same identity
        are processed at the same moment, exactly one INSERT wins — the loser sees the
        winner's lead_id and must classify itself `duplicate`. A retry of the same lead
        conflicts with its OWN row and gets itself back (owner == lead_id -> proceed)."""
        if not ident:
            return lead_id
        with pg.pool().connection() as c:
            won = c.execute(
                "INSERT INTO processed_payments(lead_code, loan_acct, amount, pay_ym, lead_id, is_test) "
                "VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (lead_code, loan_acct, amount, pay_ym, is_test) DO NOTHING "
                "RETURNING lead_id",
                (ident["lead_code"], ident["loan_acct"], ident["amount"],
                 ident["pay_ym"], lead_id, is_test)).fetchone()
            if won:
                return lead_id
            owner = c.execute(
                "SELECT lead_id FROM processed_payments WHERE lead_code=%s AND loan_acct=%s "
                "AND amount=%s AND pay_ym=%s AND is_test=%s",
                (ident["lead_code"], ident["loan_acct"], ident["amount"],
                 ident["pay_ym"], is_test)).fetchone()
        return owner["lead_id"] if owner else lead_id

    def record(self, ident: dict, lead_id: str, is_test: bool = False):
        """Kept for backfill/manual seeding; the pipeline itself uses claim()."""
        if not ident:
            return
        self.claim(ident, lead_id, is_test)

    def backfill(self) -> int:
        """Seed the REAL ledger from already-processed real leads so dedup respects
        history. Test/sandbox leads are excluded — they must never claim real identities."""
        with pg.pool().connection() as c:
            rows = c.execute(
                "SELECT j.lead_id, j.row_json FROM jobs j "
                "JOIN lead_results r ON r.lead_id = j.lead_id "
                "WHERE NOT j.is_test AND NOT r.is_test").fetchall()
        n = 0
        for r in rows:
            ident = self.identity(r["row_json"] or {})
            if ident:
                self.record(ident, r["lead_id"])
                n += 1
        return n
