"""
Bring dossiers extracted before the closed schema up to date — without a single model call.

Everything the new ingest path does after the model has answered can be replayed on what the
old path stored: file values under the run's canonical keys, clean names (keeping the originals),
resolve people across batches, prove pages against the document text, and quarantine keys the
prompt never asked for. What cannot be replayed is anything that needs the model to look again —
English transliteration and the per-field legibility verdict come only with a re-run.

Every rewrite is reversible: the stored extraction is snapshotted first, and `restore` puts it
back exactly.

    python -m workspaces.legal.backfill                 # dry run: report, change nothing
    python -m workspaces.legal.backfill --apply         # rewrite
    python -m workspaces.legal.backfill --restore       # undo
"""
from __future__ import annotations

import copy
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from psycopg.types.json import Jsonb

from db import pg

REASON = "closed-schema-v1"
_FINISHED = ("completed", "partial", "missing_documents", "failed")
_KEEP_TOP = ("telemetry", "page_extractions")          # deliberate unprefixed copies


def _rebuild(ed: Dict[str, Any], prompt: str, doc_paths: Dict[str, str]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """A stored extraction rebuilt through the current ingest path. Pure apart from reading the
    source documents for page proof; returns (new extraction, stats)."""
    from workspaces.legal.consolidate import consolidate
    from workspaces.legal.doc_filter import classify_doc_by_filename
    from workspaces.legal.field_schema import RunSchema, conform, schema_for_prompt
    from workspaces.legal.page_attribution import reattribute
    from workspaces.legal.pipeline import _ingest_batch

    schema = schema_for_prompt(prompt) if (prompt or "").strip() else RunSchema()
    doc_type_of = lambda rec: classify_doc_by_filename(rec.get("filename", "") or "")  # noqa: E731
    old_records = ed.get("_page_extractions") or ed.get("page_extractions") or []
    stats = {"values_before": 0, "values_after": 0, "extras": 0, "names_cleaned": 0}
    stats["values_before"] = sum(1 for k, v in ed.items() if not k.startswith("_")
                                 and k not in _KEEP_TOP and not isinstance(v, (dict, list)))

    records: List[Dict[str, Any]] = []
    extras: Dict[str, Any] = dict(ed.get("_extras") or {})
    for rec in old_records:
        if not isinstance(rec, dict) or not rec.get("fields"):
            continue
        page = int(rec.get("page_number") or 0) or 1
        reply = {**copy.deepcopy(rec["fields"]),
                 "_field_scripts": rec.get("field_scripts") or {},
                 "_field_evidence": rec.get("field_evidence") or {}}
        base = {k: rec.get(k) for k in ("document_id", "filename", "ocr_route", "telemetry",
                                        "raw_response", "raw_ocr_text")}
        new_recs, rec_extras, _ = _ingest_batch(reply, schema, [page], base)
        for nr in new_recs:
            stats["names_cleaned"] += sum(1 for n in nr.get("field_notes", {}).values() if n.get("raw"))
        records.extend(new_recs)
        for k, v in rec_extras.items():
            extras.setdefault(f"{k} (p. {page})", v)

    out: Dict[str, Any] = {k: v for k, v in ed.items() if k.startswith("_") or k in _KEEP_TOP
                           or isinstance(v, (dict, list))}
    lan = ed.get("account_no_lan")
    if records:
        first = consolidate(records, schema, doc_type_of)      # sets non-parties aside
        records, _ = reattribute(records, doc_paths)
        cons = consolidate(records, schema, doc_type_of)
        values = dict(cons.values)
        notes: Dict[str, Dict[str, Any]] = {}
        for px in records:
            for fk, fv in (px.get("fields") or {}).items():
                if values.get(fk) == fv and fk not in notes:
                    n = dict((px.get("field_notes") or {}).get(fk) or {})
                    evv = (px.get("field_evidence") or {}).get(fk) or {}
                    for ek in ("legibility", "issue", "script", "original"):
                        if evv.get(ek):
                            n[ek] = evv[ek]
                    n["page"] = px.get("page_number")
                    notes[fk] = n
        for fk, cn in cons.notes.items():
            notes.setdefault(fk, {}).update(cn)
        out["_mentioned"] = first.mentioned + cons.mentioned
        extras.update(first.extras)
        extras.update(cons.extras)
    else:
        # no page records to replay: conform what was stored at the top level
        top = {k: v for k, v in ed.items() if not k.startswith("_") and k not in _KEEP_TOP
               and not isinstance(v, (dict, list))}
        values, top_extras = conform(top, schema)
        extras.update(top_extras)
        notes = dict(ed.get("_field_notes") or {})
        out.setdefault("_mentioned", [])
    if lan:
        values["account_no_lan"] = lan
    out.update(values)
    out["_field_notes"] = notes
    out["_extras"] = extras
    out["_schema"] = schema.to_json() if not schema.is_empty() else None
    out["_page_extractions"] = records if records else old_records
    out["page_extractions"] = out["_page_extractions"]
    out["_cited_pages"] = sorted({int(p["page_number"]) for p in out["_page_extractions"]
                                  if isinstance(p, dict) and str(p.get("page_number", "")).isdigit()})
    stats["values_after"] = len(values)
    stats["extras"] = len(extras)
    return out, stats


def run(apply: bool = False, lead_ids: Optional[List[str]] = None, log=print) -> Dict[str, int]:
    """Rebuild every finished dossier (or `lead_ids`). Dry run unless `apply`."""
    from workspaces.legal.db import init_schema
    init_schema()
    where, params = "l.extracted_data IS NOT NULL AND l.status = ANY(%s)", [list(_FINISHED)]
    if lead_ids:
        where += " AND l.lead_id = ANY(%s)"
        params.append(list(lead_ids))
    with pg.pool().connection() as c:
        leads = c.execute(f"SELECT l.lead_id, l.extraction_prompt, l.extracted_data, r.raw_extractions "
                          f"FROM legal_leads l LEFT JOIN legal_lead_results r ON r.lead_id = l.lead_id "
                          f"WHERE {where} ORDER BY l.lead_id", params).fetchall()
        docs = {d["document_id"]: d["file_path"] for d in
                c.execute("SELECT document_id, file_path FROM legal_lead_documents").fetchall()}
    totals = {"dossiers": 0, "rewritten": 0, "values_before": 0, "values_after": 0, "extras": 0,
              "names_cleaned": 0, "failed": 0}
    t0 = time.time()
    for i, l in enumerate(leads, 1):
        totals["dossiers"] += 1
        try:
            new, st = _rebuild(copy.deepcopy(l["extracted_data"] or {}), l["extraction_prompt"] or "", docs)
        except Exception as e:  # noqa: BLE001 — one bad dossier must not stop the rest
            totals["failed"] += 1
            log(f"  ! {l['lead_id']}: {type(e).__name__}: {e}")
            continue
        for k in ("values_before", "values_after", "extras", "names_cleaned"):
            totals[k] += st[k]
        if apply:
            with pg.pool().connection() as c:
                c.execute("INSERT INTO legal_extraction_snapshots(lead_id, reason, data, results) "
                          "VALUES (%s,%s,%s,%s) ON CONFLICT (lead_id, reason) DO NOTHING",
                          (l["lead_id"], REASON, Jsonb(l["extracted_data"]),
                           Jsonb(l["raw_extractions"]) if l["raw_extractions"] is not None else None))
                # updated_at left alone: a migration is not new work on the dossier
                c.execute("UPDATE legal_leads SET extracted_data = %s WHERE lead_id = %s",
                          (Jsonb(new), l["lead_id"]))
                c.execute("UPDATE legal_lead_results SET raw_extractions = %s, "
                          "applicant_name = %s, applicant_address = %s WHERE lead_id = %s",
                          (Jsonb(new), new.get("borrower_name"), new.get("borrower_address"), l["lead_id"]))
            totals["rewritten"] += 1
        if i % 50 == 0:
            log(f"  {i}/{len(leads)}  ({time.time() - t0:.0f}s)")
    return totals


def restore(lead_ids: Optional[List[str]] = None, reason: str = REASON) -> int:
    """Put back exactly what a migration replaced."""
    where, params = "reason = %s", [reason]
    if lead_ids:
        where += " AND lead_id = ANY(%s)"
        params.append(list(lead_ids))
    n = 0
    with pg.pool().connection() as c:
        for s in c.execute(f"SELECT lead_id, data, results FROM legal_extraction_snapshots WHERE {where}",
                           params).fetchall():
            c.execute("UPDATE legal_leads SET extracted_data = %s WHERE lead_id = %s",
                      (Jsonb(s["data"]), s["lead_id"]))
            if s["results"] is not None:
                c.execute("UPDATE legal_lead_results SET raw_extractions = %s WHERE lead_id = %s",
                          (Jsonb(s["results"]), s["lead_id"]))
            n += 1
        c.execute(f"DELETE FROM legal_extraction_snapshots WHERE {where}", params)
    return n


if __name__ == "__main__":
    if "--restore" in sys.argv:
        print(f"restored {restore()} dossiers")
    else:
        res = run(apply="--apply" in sys.argv)
        print(("APPLIED" if "--apply" in sys.argv else "DRY RUN (nothing written)"), res)
