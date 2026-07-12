"""
Receiver-Approval queue — the human loop for receiver mismatches.

`unverified` leads whose RECEIVER check failed stay exactly where they are; this module
is a visibility lens over them, grouped by (lender, receiver-as-printed). One decision
covers every lead carrying that payee:

  approve -> the name is appended to lender_receivers.json (permanent config teaching),
             and every affected lead is deterministically re-verified (stage 4 only, no
             model call) — leads whose other fields matched flip to `verified`.
  reject  -> the pair is recorded as rejected and never re-enters the queue; the leads
             simply remain `unverified`.

Decisions are keyed by (lender, normalized receiver) — NOT by lead — so they persist
across data wipes and keep the queue clean on future runs. LIVE data only: approvals
mutate real lender config, so the sandbox (Test) workspace never feeds this queue.
"""
from __future__ import annotations

from db import pg
from pipeline import verify
from pipeline.verify import _norm_name
from reverify import reverify_leads

# unverified real leads that failed ON THE RECEIVER with a readable payee name.
# Dedup-flagged leads are excluded (they need a human regardless of receiver), and an
# empty payee has nothing to approve.
_CANDIDATES = """
    SELECT lead_id, lender, extracted->>'receiver_name' AS receiver,
           (outcome->'failed_fields' = '["receiver"]'::jsonb) AS ready,
           payment_method,
           extracted->>'amount'          AS amount,
           extracted->>'date'            AS doc_date,
           outcome->'failed_fields'      AS failed_fields,
           to_char(updated_at, 'YYYY-MM-DD') AS updated_at
    FROM lead_results_real
    WHERE verification_status = 'unverified'
      AND COALESCE(outcome->'failed_fields', '[]'::jsonb) ? 'receiver'
      AND COALESCE(outcome->>'flag', '') <> 'different_loan_account_same_lead_code'
      AND COALESCE(extracted->>'receiver_name', '') <> ''
"""


def _candidate_rows():
    with pg.pool().connection() as c:
        return c.execute(_CANDIDATES).fetchall()


def _decided_pairs() -> set:
    with pg.pool().connection() as c:
        rows = c.execute("SELECT lender, receiver_norm FROM receiver_approvals").fetchall()
    return {(r["lender"], r["receiver_norm"]) for r in rows}


def pending_queue() -> list:
    """The queue: one row per (lender, receiver) awaiting a decision, heaviest first.
    `ready` = leads where receiver is the ONLY failed field (approval flips them to
    verified); the rest stay unverified even if approved — surfaced honestly."""
    decided = _decided_pairs()
    groups: dict = {}
    for r in _candidate_rows():
        norm = _norm_name(r["receiver"])
        if not norm or (r["lender"], norm) in decided:
            continue
        g = groups.setdefault((r["lender"], norm), {
            "lender": r["lender"], "receiver": r["receiver"],
            "count": 0, "ready": 0, "lead_ids": [], "leads": []})
        g["count"] += 1
        g["ready"] += 1 if r["ready"] else 0
        g["lead_ids"].append(r["lead_id"])
        g["leads"].append({
            "lead_id": r["lead_id"],
            "amount": r["amount"],
            "date": r["doc_date"],
            "method": r["payment_method"],
            "other_failed": [f for f in (r["failed_fields"] or []) if f != "receiver"],
            "updated_at": r["updated_at"],
        })
    return sorted(groups.values(), key=lambda g: (-g["ready"], -g["count"]))


def recent_decisions(limit: int = 30) -> list:
    with pg.pool().connection() as c:
        rows = c.execute(
            "SELECT lender, receiver_name, decision, decided_by, note, affected, flipped, "
            "to_char(decided_at, 'YYYY-MM-DD HH24:MI') AS decided_at "
            "FROM receiver_approvals ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return rows


def decide(lender: str, receiver: str, decision: str, decided_by: str = "", note: str = "") -> dict:
    """Record a human decision on a (lender, receiver) pair and act on it."""
    lender = str(lender or "").strip()
    receiver = str(receiver or "").strip()
    norm = _norm_name(receiver)
    if not lender or not norm:
        return {"error": "lender and receiver are required"}
    if decision not in ("approved", "rejected"):
        return {"error": "decision must be 'approved' or 'rejected'"}
    if (lender, norm) in _decided_pairs():
        return {"error": f"'{receiver}' for {lender} was already decided"}

    # affected leads captured at decision time (same predicate the queue uses)
    lead_ids = [r["lead_id"] for r in _candidate_rows()
                if r["lender"] == lender and _norm_name(r["receiver"]) == norm]

    result = {"lender": lender, "receiver": receiver, "decision": decision,
              "affected": len(lead_ids), "flipped": 0, "config_added": False}
    if decision == "approved":
        result["config_added"] = verify.add_receiver(lender, receiver)
        rv = reverify_leads(lead_ids, reason_suffix=f"receiver '{receiver}' approved for {lender}")
        result["flipped"] = rv["flipped"]

    with pg.pool().connection() as c:
        won = c.execute(
            "INSERT INTO receiver_approvals(lender, receiver_norm, receiver_name, decision, "
            "decided_by, note, affected, flipped) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (lender, receiver_norm) DO NOTHING RETURNING id",
            (lender, norm, receiver, decision, decided_by, note,
             result["affected"], result["flipped"])).fetchone()
    if not won:
        return {"error": f"'{receiver}' for {lender} was already decided"}
    return result
