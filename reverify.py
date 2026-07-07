"""
Deterministic re-verification.

Re-runs ONLY stage 4 (verify) on leads that were already extracted, using the CURRENT
rules/config — with NO vision-model calls. Updates `lead_results` (and `jobs`) in place
and logs a `reverify` event per changed lead, so the correction is auditable.

Use after a verify-logic or config change to bring existing data in line without paying
for the expensive model again. Only `verified`/`unverified` leads are re-verifiable
(`non_document`/`duplicate` short-circuit before stage 4). Dedup-flagged leads
(`different_loan_account_same_lead_code`) are left untouched — they need a human regardless.

    python reverify.py            # apply
    python reverify.py --dry-run  # report only, change nothing
"""
from __future__ import annotations

import sys

from psycopg.types.json import Jsonb

from db import pg
from observability.pg_logger import PgLeadLogger
from pipeline import verify
from pipeline.models import ExtractedDocument

_FIELDS = set(ExtractedDocument().__dict__.keys())


def _doc(ex: dict) -> ExtractedDocument:
    return ExtractedDocument(**{k: v for k, v in (ex or {}).items() if k in _FIELDS})


def reverify_all(apply: bool = True) -> dict:
    logger = PgLeadLogger()
    with pg.pool().connection() as c:
        rows = c.execute("""
            SELECT r.lead_id, r.lender, r.verification_status AS old,
                   r.outcome, r.extracted, j.row_json
            FROM lead_results r JOIN jobs j ON j.job_id = r.lead_id
            WHERE r.verification_status IN ('verified','unverified')
        """).fetchall()

    scanned = changed = skipped = 0
    moves: dict[str, int] = {}
    for r in rows:
        scanned += 1
        if (r["outcome"] or {}).get("flag") == "different_loan_account_same_lead_code":
            skipped += 1                       # dedup-wrapped: leave for human review
            continue
        new_status, new_outcome = verify.run(_doc(r["extracted"]), r["row_json"] or {})
        if new_status == r["old"]:
            continue
        changed += 1
        moves[f"{r['old']}->{new_status}"] = moves.get(f"{r['old']}->{new_status}", 0) + 1
        if not apply:
            continue
        with pg.pool().connection() as c:
            # updated_at intentionally NOT bumped — the lead's processing time is unchanged;
            # the correction is recorded as a reverify event instead of reshuffling the table.
            c.execute("UPDATE lead_results SET verification_status=%s, outcome=%s WHERE lead_id=%s",
                      (new_status, Jsonb(new_outcome), r["lead_id"]))
            c.execute("UPDATE jobs SET verification_status=%s WHERE job_id=%s",
                      (new_status, r["lead_id"]))
        logger.log(r["lead_id"], "reverify",
                   "PASS" if new_status == "verified" else "FAIL",
                   reason=f"{r['old']} -> {new_status} (deterministic re-verify, no model call)",
                   data={"old_status": r["old"], "new_status": new_status,
                         "reason": new_outcome.get("reason", "")})
    return {"scanned": scanned, "changed": changed, "skipped_dedup": skipped, "moves": moves}


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    res = reverify_all(apply=not dry)
    print(("[dry-run] " if dry else "[applied] ") + str(res))
