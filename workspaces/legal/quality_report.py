"""
Did a change make extraction better? One run's numbers, or two runs side by side.

Every prompt or threshold change should be judged here, not by eyeballing a few dossiers:

    python -m workspaces.legal.quality_report                       # every run, one table each
    python -m workspaces.legal.quality_report --batch <id>
    python -m workspaces.legal.quality_report --compare <before> <after>

What it measures, all read-only from what the pipeline and reviewers already stored:

  review        share of dossiers extracted / needing review / not verified
  trust         share of values verified in the text, read clearly, needing review
  reasons       why values needed review
  schema        fields returned that the prompt didn't ask for, per dossier (should be ~0 on new runs)
  names         name values the cleaner had to fix, and names still flagged
  people        co-borrowers per dossier, and people set aside as "mentioned"
  cost          tokens per dossier and per value
  agreement     of the values people checked, the share that were right as read

The last one is the ground truth: it grows as reviewers confirm and correct values, and a run
with a higher agreement rate is a better run, whatever the other numbers say.
"""
from __future__ import annotations

import re
import sys
from typing import Any, Dict, List, Optional

from db import pg

_FAMILY = re.compile(r"^co_borrower_\d+_name$")
_NAME = re.compile(r"^(borrower|co_borrower_\d+|guarantor_\d+)_name$")


def report(batch_id: Optional[str] = None) -> Dict[str, Any]:
    from workspaces.legal.metrics import Slice, review
    rv = review(Slice("all", batch_id, None))
    where, params = ("l.batch_id = %s", [batch_id]) if batch_id else ("TRUE", [])
    with pg.pool().connection() as c:
        rows = c.execute(
            f"SELECT l.extracted_data FROM legal_leads l WHERE {where} AND l.extracted_data IS NOT NULL "
            f"AND l.status IN ('completed','partial','missing_documents','failed')", params).fetchall()
    n = len(rows)
    names = cleaned = flagged = co = mentioned = tokens = values = 0
    for r in rows:
        ed = r["extracted_data"] or {}
        notes = ed.get("_field_notes") or {}
        for k, v in ed.items():
            if _NAME.match(k) and v:
                names += 1
                cleaned += bool((notes.get(k) or {}).get("raw"))
                flagged += bool((notes.get(k) or {}).get("flags"))
            if _FAMILY.match(k) and v:
                co += 1
        mentioned += len(ed.get("_mentioned") or [])
        tokens += int(((ed.get("_telemetry") or {}).get("total_tokens")) or 0)
        values += len(((ed.get("_trust") or {}).get("fields")) or {})
    fin = rv["finished"] or 1
    vals = sum(rv["values"].values()) or 1
    pct = lambda a, b: round(100.0 * a / b, 1) if b else 0.0  # noqa: E731
    return {
        "batch_id": batch_id or "(all runs)",
        "dossiers": rv["finished"],
        "review": {k: pct(rv["states"].get(k, 0), fin) for k in
                   ("extracted", "reviewed", "needs_review", "unverified", "no_values")},
        "trust": {k: pct(rv["values"].get(k, 0), vals) for k in
                  ("verified", "clear", "confirmed", "corrected", "review", "unchecked")},
        "reasons": {r["reason"]: r["dossiers"] for r in rv["reasons"]},
        "schema_extras_per_dossier": rv["extras_per_dossier"],
        "names_cleaned_pct": pct(cleaned, names),
        "names_flagged_pct": pct(flagged, names),
        "co_borrowers_per_dossier": round(co / n, 2) if n else 0,
        "mentioned_per_dossier": round(mentioned / n, 2) if n else 0,
        "tokens_per_dossier": round(tokens / n) if n else 0,
        "tokens_per_value": round(tokens / values) if values else 0,
        "agreement_pct": rv["corrections"]["agreement"],
        "values_checked_by_people": rv["corrections"]["checked"],
    }


def _print(rep: Dict[str, Any], other: Optional[Dict[str, Any]] = None) -> None:
    def row(label: str, a: Any, b: Any = None, better: str = "") -> None:
        if other is None:
            print(f"  {label:<36} {a}")
            return
        delta = ""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            d = b - a
            arrow = "" if d == 0 else ("better" if (d > 0) == (better == "up") else "worse") if better else ""
            delta = f"{d:+.1f} {arrow}".rstrip()
        print(f"  {label:<36} {str(a):>10}  ->  {str(b):>10}   {delta}")

    o = other or {}
    print(f"\n{rep['batch_id']}" + (f"  vs  {o.get('batch_id')}" if other else "") +
          f"   ({rep['dossiers']} dossiers" + (f" vs {o.get('dossiers')}" if other else "") + ")")
    for k in ("extracted", "needs_review", "unverified", "no_values"):
        row(f"dossiers {k.replace('_', ' ')} %", rep["review"][k], o.get("review", {}).get(k),
            "up" if k == "extracted" else "down")
    for k in ("verified", "clear", "review", "unchecked"):
        row(f"values {k} %", rep["trust"][k], o.get("trust", {}).get(k), "up" if k in ("verified", "clear") else "down")
    row("unrequested fields per dossier", rep["schema_extras_per_dossier"], o.get("schema_extras_per_dossier"), "down")
    row("names the cleaner fixed %", rep["names_cleaned_pct"], o.get("names_cleaned_pct"), "down")
    row("names still flagged %", rep["names_flagged_pct"], o.get("names_flagged_pct"), "down")
    row("co-borrowers per dossier", rep["co_borrowers_per_dossier"], o.get("co_borrowers_per_dossier"))
    row("people set aside as mentioned", rep["mentioned_per_dossier"], o.get("mentioned_per_dossier"))
    row("tokens per dossier", rep["tokens_per_dossier"], o.get("tokens_per_dossier"), "down")
    row("model right on checked values %", rep["agreement_pct"], o.get("agreement_pct"), "up")
    row("values checked by people", rep["values_checked_by_people"], o.get("values_checked_by_people"))
    reasons = ", ".join(f"{k} {v}" for k, v in list(rep["reasons"].items())[:6])
    print(f"  {'top reasons (dossiers)':<36} {reasons or '—'}")


def _batches() -> List[str]:
    with pg.pool().connection() as c:
        return [r["batch_id"] for r in c.execute(
            "SELECT batch_id, max(updated_at) t FROM legal_leads WHERE extracted_data IS NOT NULL "
            "GROUP BY 1 ORDER BY 2 DESC").fetchall()]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--compare"] and len(args) == 3:
        _print(report(args[1]), report(args[2]))
    elif args[:1] == ["--batch"] and len(args) == 2:
        _print(report(args[1]))
    else:
        for b in _batches():
            _print(report(b))
