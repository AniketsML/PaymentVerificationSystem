"""
Duplicate ledger - the guard that keeps `verified` free of false positives from
re-submitted payments (the ALREADY_ADDED case that date+amount+receiver cannot
catch on their own).

Key = the payment REFERENCE ID alone (UTR / RRN / Txn id) - read from the document,
falling back to the system transaction_id. The check ONLY runs when a reference id
is present; if none is available we skip the duplicate check entirely (a payment
proof without a reference cannot be safely de-duplicated, so we never guess).

In production: backfill this table once from the full payment history, and point
it at the same Postgres as the rest. Then it catches duplicates whose original was
submitted at any time in the past, not just within the current batch.
"""
from __future__ import annotations

import re
import sqlite3
import threading

_LOCK = threading.Lock()


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


class DuplicateLedger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with _LOCK, sqlite3.connect(self.db_path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS seen_payments (
                            dedup_key TEXT PRIMARY KEY,
                            lead_id   TEXT,
                            ts        TEXT DEFAULT CURRENT_TIMESTAMP )""")

    def key(self, row: dict, doc) -> str:
        # reference id is the SOLE dedup key; empty -> no key -> no duplicate check
        return _norm(getattr(doc, "reference_id", None)) or _norm(row.get("transaction_id"))

    def is_duplicate(self, row: dict, doc) -> tuple[bool, str]:
        k = self.key(row, doc)
        if not k:
            return False, "no reference id present - duplicate check skipped"
        with sqlite3.connect(self.db_path) as c:
            hit = c.execute("SELECT lead_id FROM seen_payments WHERE dedup_key=?", (k,)).fetchone()
        if hit:
            return True, f"reference already submitted (original lead {hit[0]})"
        return False, "reference not seen before"

    def record(self, row: dict, doc, lead_id: str):
        k = self.key(row, doc)
        if not k:
            return
        with _LOCK, sqlite3.connect(self.db_path) as c:
            c.execute("INSERT OR IGNORE INTO seen_payments(dedup_key,lead_id) VALUES(?,?)",
                      (k, lead_id))
