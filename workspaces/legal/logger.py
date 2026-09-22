"""
PostgreSQL logger for the SARFAESI Legal workspace.
31-column result persistence, event logging, query store.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from psycopg.types.json import Jsonb

from db import pg
from workspaces.legal.db import init_schema


# The lead row is the live state; `legal_lead_results` keeps the verdict of the LAST finished
# extraction and survives a re-run. Reading the result row first therefore shows a re-queued
# dossier as "completed" while it is still waiting or being read — so the lead row always wins,
# and the result row is only a fallback. 'draft' is the split second during enqueue: show it as
# queued rather than as a state of its own.
# A finished verdict is also not shown while the dossier still has documents nobody has decided
# about: a lead that reads "completed" over unread work is the single most misleading thing this
# table can say. Every document ends up processed, skipped or failed, so this only catches work
# genuinely still in flight.
LEAD_STATUS_SQL = (
    "CASE WHEN l.status = 'draft' THEN 'pending' "
    "     WHEN l.status IN ('completed','partial','missing_documents') "
    "          AND EXISTS (SELECT 1 FROM legal_lead_documents d "
    "                       WHERE d.lead_id = l.lead_id AND d.processing_status = 'pending') "
    "       THEN 'processing' "
    "     ELSE COALESCE(l.status, r.processing_status, 'pending') END")


class PgLegalLeadLogger:
    """PostgreSQL-backed event logger & query store for Legal Leads."""

    def __init__(self):
        init_schema()

    def log(self, lead_id: str, stage: str, status: str, document_id: str = None,
            reason: str = "", ms: float = 0.0, metrics: Optional[Dict] = None,
            data: Optional[Dict] = None, is_test: bool = False) -> None:
        try:
            with pg.pool().connection() as c:
                c.execute(
                    "INSERT INTO legal_processing_events(lead_id,document_id,stage,status,reason,ms,metrics,data,is_test) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (lead_id, document_id, stage, status, reason or None, float(ms),
                     Jsonb(metrics or {}), Jsonb(data or {}), is_test))
        except Exception as e:
            import sys; sys.stderr.write(f"[legal_logger] event insert failed: {e}\n")

    def save_lead_result(self, lead_id: str, result: Dict[str, Any],
                         processing_status: str = "completed", is_test: bool = False,
                         phase_timings: Dict = None, ocr_routes_used: List = None,
                         raw_extractions: Dict = None) -> None:
        def _sf(val):
            if val is None:
                return None
            try:
                return float(str(val).replace(',', ''))
            except (ValueError, TypeError):
                return None
        try:
            with pg.pool().connection() as c:
                c.execute("""
                    INSERT INTO legal_lead_results (
                        lead_id, account_no_lan, property_owner_mortgagor,
                        applicant_name, applicant_address,
                        co_applicant_1, co_applicant_address_1,
                        co_applicant_2, co_applicant_address_2,
                        co_applicant_3, co_applicant_address_3,
                        guarantor_1, guarantor_1_add, guarantor_2, guarantor_2_add,
                        sanction_amount, sanction_amount_in_words, roi_in_number,
                        sanction_date, disbursal_date, npa_date,
                        future_principal, principal_overdue, interest_overdue,
                        interest_on_termination, late_payment_penal,
                        cheque_bounce_inc_gst, other_charges_inc_gst,
                        foreclosure_charges, litigation_charges, excess_amount, tos,
                        mortgaged_property_detail_1, directions,
                        mortgaged_property_detail_2, directions_2,
                        property_source_doc, property_source_priority,
                        property_cross_verified_with, property_verification_status,
                        property_verification_details, third_party_mortgagor_flag,
                        processing_status, confidence_score, summary, flags,
                        raw_extractions, phase_timings, ocr_routes_used,
                        is_test, updated_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,now()
                    ) ON CONFLICT (lead_id) DO UPDATE SET
                        account_no_lan=EXCLUDED.account_no_lan,
                        property_owner_mortgagor=EXCLUDED.property_owner_mortgagor,
                        applicant_name=EXCLUDED.applicant_name,
                        applicant_address=EXCLUDED.applicant_address,
                        co_applicant_1=EXCLUDED.co_applicant_1,
                        co_applicant_address_1=EXCLUDED.co_applicant_address_1,
                        co_applicant_2=EXCLUDED.co_applicant_2,
                        co_applicant_address_2=EXCLUDED.co_applicant_address_2,
                        co_applicant_3=EXCLUDED.co_applicant_3,
                        co_applicant_address_3=EXCLUDED.co_applicant_address_3,
                        guarantor_1=EXCLUDED.guarantor_1,
                        guarantor_1_add=EXCLUDED.guarantor_1_add,
                        guarantor_2=EXCLUDED.guarantor_2,
                        guarantor_2_add=EXCLUDED.guarantor_2_add,
                        sanction_amount=EXCLUDED.sanction_amount,
                        sanction_amount_in_words=EXCLUDED.sanction_amount_in_words,
                        roi_in_number=EXCLUDED.roi_in_number,
                        sanction_date=EXCLUDED.sanction_date,
                        disbursal_date=EXCLUDED.disbursal_date,
                        npa_date=EXCLUDED.npa_date,
                        future_principal=EXCLUDED.future_principal,
                        principal_overdue=EXCLUDED.principal_overdue,
                        interest_overdue=EXCLUDED.interest_overdue,
                        interest_on_termination=EXCLUDED.interest_on_termination,
                        late_payment_penal=EXCLUDED.late_payment_penal,
                        cheque_bounce_inc_gst=EXCLUDED.cheque_bounce_inc_gst,
                        other_charges_inc_gst=EXCLUDED.other_charges_inc_gst,
                        foreclosure_charges=EXCLUDED.foreclosure_charges,
                        litigation_charges=EXCLUDED.litigation_charges,
                        excess_amount=EXCLUDED.excess_amount,
                        tos=EXCLUDED.tos,
                        mortgaged_property_detail_1=EXCLUDED.mortgaged_property_detail_1,
                        directions=EXCLUDED.directions,
                        mortgaged_property_detail_2=EXCLUDED.mortgaged_property_detail_2,
                        directions_2=EXCLUDED.directions_2,
                        property_source_doc=EXCLUDED.property_source_doc,
                        property_source_priority=EXCLUDED.property_source_priority,
                        property_cross_verified_with=EXCLUDED.property_cross_verified_with,
                        property_verification_status=EXCLUDED.property_verification_status,
                        property_verification_details=EXCLUDED.property_verification_details,
                        third_party_mortgagor_flag=EXCLUDED.third_party_mortgagor_flag,
                        processing_status=EXCLUDED.processing_status,
                        confidence_score=EXCLUDED.confidence_score,
                        summary=EXCLUDED.summary,
                        flags=EXCLUDED.flags,
                        raw_extractions=EXCLUDED.raw_extractions,
                        phase_timings=EXCLUDED.phase_timings,
                        ocr_routes_used=EXCLUDED.ocr_routes_used,
                        is_test=EXCLUDED.is_test,
                        updated_at=now();
                """, (
                    lead_id,
                    result.get("account_no_lan", ""),
                    result.get("property_owner_mortgagor", ""),
                    result.get("applicant_name", ""),
                    result.get("applicant_address", ""),
                    result.get("co_applicant_1", ""),
                    result.get("co_applicant_address_1", ""),
                    result.get("co_applicant_2", ""),
                    result.get("co_applicant_address_2", ""),
                    result.get("co_applicant_3", ""),
                    result.get("co_applicant_address_3", ""),
                    result.get("guarantor_1", ""),
                    result.get("guarantor_1_add", ""),
                    result.get("guarantor_2", ""),
                    result.get("guarantor_2_add", ""),
                    _sf(result.get("sanction_amount")),
                    result.get("sanction_amount_in_words", ""),
                    result.get("roi_in_number", ""),
                    result.get("sanction_date", ""),
                    result.get("disbursal_date", ""),
                    result.get("npa_date", ""),
                    _sf(result.get("future_principal")),
                    _sf(result.get("principal_overdue")),
                    _sf(result.get("interest_overdue")),
                    _sf(result.get("interest_on_termination")),
                    _sf(result.get("late_payment_penal")),
                    _sf(result.get("cheque_bounce_inc_gst")),
                    _sf(result.get("other_charges_inc_gst")),
                    _sf(result.get("foreclosure_charges")),
                    _sf(result.get("litigation_charges")),
                    _sf(result.get("excess_amount")),
                    _sf(result.get("tos")),
                    result.get("mortgaged_property_detail_1", ""),
                    result.get("directions", ""),
                    result.get("mortgaged_property_detail_2", ""),
                    result.get("directions_2", ""),
                    result.get("property_source_doc", ""),
                    result.get("property_source_priority", 0),
                    result.get("property_cross_verified_with", ""),
                    result.get("property_verification_status", ""),
                    result.get("property_verification_details", ""),
                    result.get("third_party_mortgagor_flag", ""),
                    processing_status,
                    float(result.get("confidence_score", 0.0) or 0.0),
                    result.get("summary", ""),
                    Jsonb(result.get("flags", [])),
                    Jsonb(raw_extractions or result.get("raw_extractions", {})),
                    Jsonb(phase_timings or result.get("phase_timings", {})),
                    Jsonb(ocr_routes_used or result.get("ocr_routes_used", [])),
                    is_test,
                ))
        except Exception as e:
            import sys; sys.stderr.write(f"[legal_logger] save_lead_result failed: {e}\n")

    def get_lead_journey(self, lead_id: str) -> Dict[str, Any]:
        with pg.pool().connection() as c:
            final = c.execute("SELECT * FROM legal_lead_results WHERE lead_id=%s", (lead_id,)).fetchone()
            
            events = c.execute(
                "SELECT id,lead_id,document_id,stage,status,reason,round(ms::numeric,1) as ms,metrics,data,"
                "ts AT TIME ZONE 'UTC' as ts FROM legal_processing_events WHERE lead_id=%s ORDER BY id ASC",
                (lead_id,)).fetchall()
            docs = c.execute("SELECT * FROM legal_lead_documents WHERE lead_id=%s ORDER BY filename",
                             (lead_id,)).fetchall()
            lead_info = c.execute("SELECT * FROM legal_leads WHERE lead_id=%s", (lead_id,)).fetchone()

            ed = (lead_info.get("extracted_data") if (lead_info and isinstance(lead_info.get("extracted_data"), dict)) else {}) or {}

            # Build or enrich final result
            if not final:
                final = {
                    "processing_status": lead_info["status"] if lead_info else "completed",
                    "raw_extractions": {
                        "page_extractions": ed.get("_page_extractions", []),
                        "cited_pages": ed.get("_cited_pages", []),
                        "telemetry": ed.get("_telemetry", {}),
                    },
                    "telemetry": ed.get("_telemetry", {}),
                    "phase_timings": ed.get("_phase_timings", {}),
                    "extracted_data": {
                        "custom_fields": {k: v for k, v in ed.items() if not k.startswith("_")}
                    },
                    **{k: v for k, v in ed.items() if not k.startswith("_")}
                }
            else:
                final = dict(final)
                raw_ex = final.get("raw_extractions")
                if isinstance(raw_ex, str):
                    try:
                        raw_ex = json.loads(raw_ex)
                    except Exception:
                        raw_ex = {}
                elif not isinstance(raw_ex, dict):
                    raw_ex = {}

                if ed:
                    if "_page_extractions" in ed:
                        raw_ex["page_extractions"] = ed["_page_extractions"]
                    if "_telemetry" in ed:
                        raw_ex["telemetry"] = ed["_telemetry"]
                        final["telemetry"] = ed["_telemetry"]
                    if "_cited_pages" in ed:
                        raw_ex["cited_pages"] = ed["_cited_pages"]
                    if "_phase_timings" in ed:
                        final["phase_timings"] = ed["_phase_timings"]
                    
                    if not final.get("extracted_data"):
                        final["extracted_data"] = {}
                    final["extracted_data"]["custom_fields"] = {k: v for k, v in ed.items() if not k.startswith("_")}
                    for k, v in ed.items():
                        if not k.startswith("_") and k not in final:
                            final[k] = v

                # Filter page_extractions to strictly pages that contain extracted business data
                raw_pxs = raw_ex.get("page_extractions") or raw_ex.get("_page_extractions") or []
                clean_pxs = []
                for px in raw_pxs:
                    if isinstance(px, dict) and isinstance(px.get("fields"), dict):
                        real_flds = {k: v for k, v in px["fields"].items() if not k.startswith("_") and v not in (None, "", "—", {})}
                        if real_flds:
                            try:
                                from workspaces.legal.pipeline import _normalize_extracted_fields
                                norm_flds = _normalize_extracted_fields(real_flds)
                                if norm_flds:
                                    real_flds.update(norm_flds)
                            except Exception:
                                pass
                            for sk in ("borrower_details", "co_borrower_details", "details_of_borrower", "details_of_co_borrower", "borrower", "co_borrowers"):
                                real_flds.pop(sk, None)
                            if real_flds:
                                px_copy = dict(px)
                                px_copy["fields"] = real_flds
                                clean_pxs.append(px_copy)
                raw_ex["page_extractions"] = clean_pxs
                raw_ex["_page_extractions"] = clean_pxs

                final["raw_extractions"] = raw_ex

        if final:
            if final.get("updated_at") and hasattr(final["updated_at"], "isoformat"):
                final["updated_at"] = final["updated_at"].isoformat()
            for fld in ("sanction_amount", "future_principal", "principal_overdue",
                        "interest_overdue", "interest_on_termination", "late_payment_penal",
                        "cheque_bounce_inc_gst", "other_charges_inc_gst", "foreclosure_charges",
                        "litigation_charges", "excess_amount", "tos"):
                if final.get(fld) is not None:
                    try:
                        final[fld] = float(final[fld])
                    except (ValueError, TypeError):
                        pass

        ev_list = []
        for e in events:
            row = dict(e)
            if row.get("ts"):
                row["ts"] = row["ts"].isoformat()
            ev_list.append(row)

        doc_list = []
        for d in docs:
            dd = dict(d)
            for tf in ("created_at", "updated_at"):
                if dd.get(tf):
                    dd[tf] = dd[tf].isoformat()
            doc_list.append(dd)

        lead_dict = None
        if lead_info:
            lead_dict = dict(lead_info)
            for tf in ("created_at", "updated_at"):
                if lead_dict.get(tf):
                    lead_dict[tf] = lead_dict[tf].isoformat()

        return {"lead_id": lead_id, "lead": lead_dict, "final": final,
                "documents": doc_list, "journey": ev_list}

    def query_leads(self, status: str = "all", q: str = "", limit: int = 300,
                    scope: str = "real", batch_id: str = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if batch_id:
            clauses.append("l.batch_id = %s")
            params.append(batch_id)
        elif scope == "real":
            clauses.append("l.is_test = false")
        elif scope == "test":
            clauses.append("l.is_test = true")
        if status and status != "all":
            clauses.append(f"{LEAD_STATUS_SQL} = %s")
            params.append(status)
        if q:
            term = f"%{q.strip()}%"
            clauses.append(
                "(l.lead_id ILIKE %s OR COALESCE(r.applicant_name, l.lead_name) ILIKE %s OR "
                "COALESCE(r.account_no_lan, l.account_lan) ILIKE %s OR COALESCE(r.summary, l.folder_name) ILIKE %s)")
            params.extend([term] * 4)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""SELECT
                    l.lead_id,
                    l.lead_name,
                    l.account_lan,
                    l.folder_name,
                    l.batch_id,
                    {LEAD_STATUS_SQL} as processing_status,
                    l.extracted_data,
                    r.raw_extractions,
                    r.applicant_name,
                    r.applicant_address,
                    r.account_no_lan,
                    l.total_documents,
                    l.processed_documents,
                    l.failed_documents,
                    p.tag AS script_tag,
                    COALESCE((p.counts->>'blurred')::int, 0) > 0 AS has_blur,
                    l.created_at,
                    l.updated_at
                FROM legal_leads l
                LEFT JOIN legal_lead_results r ON l.lead_id = r.lead_id
                LEFT JOIN legal_field_provenance p ON p.lead_id = l.lead_id AND p.version = %s
                {where}
                ORDER BY l.updated_at DESC
                LIMIT {limit}
        """
        # the provenance join's %s comes first in the statement, so its value leads the params.
        # A verdict from older tagging rules reads as NULL here, which is what makes the
        # dashboard queue it for a re-scan instead of showing a stale tag forever.
        from workspaces.legal.provenance import VERSION as PROV_VERSION
        with pg.pool().connection() as c:
            rows = c.execute(sql, [PROV_VERSION] + params).fetchall()
            ret = []
            for r in rows:
                d = dict(r)
                for f in ("created_at", "updated_at"):
                    if d.get(f):
                        d[f] = d[f].isoformat()
                
                # Flatten extracted_data into root level, keeping internal fields private
                extracted = d.pop("extracted_data") or {}
                if isinstance(extracted, str):
                    try:
                        extracted = json.loads(extracted)
                    except Exception:
                        extracted = {}

                raw_r = d.pop("raw_extractions", None) or {}
                if isinstance(raw_r, str):
                    try:
                        raw_r = json.loads(raw_r)
                    except Exception:
                        raw_r = {}

                # Merge both sources
                merged = {**raw_r, **extracted}
                try:
                    from workspaces.legal.pipeline import _normalize_extracted_fields
                    norm = _normalize_extracted_fields(merged)
                    merged.update(norm)

                    # Also pull from page_extractions if fields are missing
                    pxs = merged.get("_page_extractions") or merged.get("page_extractions") or []
                    if isinstance(pxs, list):
                        for px in pxs:
                            if isinstance(px, dict) and isinstance(px.get("fields"), dict):
                                p_norm = _normalize_extracted_fields(px["fields"])
                                for pk, pv in p_norm.items():
                                    if pk not in merged or not merged[pk]:
                                        merged[pk] = pv
                except Exception:
                    pass

                # Fallback to direct columns if needed
                if "borrower_name" not in merged and d.get("applicant_name"):
                    merged["borrower_name"] = d["applicant_name"]
                if "borrower_name" not in merged and d.get("lead_name"):
                    merged["borrower_name"] = d["lead_name"]
                if "borrower_address" not in merged and d.get("applicant_address"):
                    merged["borrower_address"] = d["applicant_address"]
                if "account_no_lan" not in merged and (d.get("account_no_lan") or d.get("account_lan")):
                    merged["account_no_lan"] = d.get("account_no_lan") or d.get("account_lan")

                from workspaces.legal.pipeline import _is_meta_key
                from workspaces.legal.field_schema import IDENTITY_KEYS, RunSchema
                # a row extracted under a schema shows only the schema's fields — nothing the
                # prompt did not ask for can resurface as a column, even from a page record
                row_schema = RunSchema.from_json(extracted.get("_schema"))
                if row_schema is not None and row_schema.is_empty():
                    row_schema = None
                d["_schema"] = extracted.get("_schema")
                d["_field_notes"] = extracted.get("_field_notes") or {}
                for key, val in merged.items():
                    if key not in d and not key.startswith("_") and not key.endswith("_page_sources") and key not in (
                        "page_extractions", "telemetry", "borrower_details", "co_borrower_details",
                        "details_of_borrower", "details_of_co_borrower", "details_of_the_borrower",
                        "borrower", "co_borrowers", "co_applicants"
                    ) and not key.startswith("telemetry_") and not _is_meta_key(key):
                        if row_schema is not None and key not in IDENTITY_KEYS and not row_schema.allows(key):
                            continue
                        if not isinstance(val, (dict, list)):
                            d[key] = val
                # a person's corrections go last, so nothing above — least of all the fallback to
                # page records — can put back a value someone deliberately changed or cleared
                for key, val in (extracted.get("_overrides") or {}).items():
                    if val in (None, ""):
                        d.pop(key, None)
                    else:
                        d[key] = val
                d["_trust"] = extracted.get("_trust")
                d["_overrides"] = extracted.get("_overrides") or {}
                        
                if ("_telemetry" in merged or "telemetry" in merged) and "telemetry" not in d:
                    d["telemetry"] = merged.get("telemetry") or merged.get("_telemetry")

                ret.append(d)
            return ret

    def status_counts(self, scope: str = "real", batch_id: str = None) -> Dict[str, int]:
        clauses, params = [], []
        if scope == "real": clauses.append("l.is_test=false")
        elif scope == "test": clauses.append("l.is_test=true")
        if batch_id:
            clauses.append("l.batch_id = %s")
            params.append(batch_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params = tuple(params)
        with pg.pool().connection() as c:
            rows = c.execute(f"""
                SELECT {LEAD_STATUS_SQL} as st, count(*) as n
                FROM legal_leads l
                LEFT JOIN legal_lead_results r ON l.lead_id = r.lead_id
                {where}
                GROUP BY 1
            """, params).fetchall()
            total = c.execute(f"SELECT count(*) as total FROM legal_leads l {where}", params).fetchone()
            test_cnt = c.execute("SELECT count(*) as cnt FROM legal_leads WHERE is_test=true").fetchone()
            doc_total = c.execute(f"""
                SELECT count(*) as cnt
                FROM legal_lead_documents d
                JOIN legal_leads l ON d.lead_id = l.lead_id
                {where}
            """, params).fetchone()
        counts = {"completed": 0, "partial": 0, "pending": 0, "processing": 0, "failed": 0,
                  "total": total["total"] if total else 0}
        for r in rows:
            st = (r["st"] or "pending").lower()
            counts[st] = counts.get(st, 0) + r["n"]
        counts["test_count"] = test_cnt["cnt"] if test_cnt else 0
        counts["total_documents"] = doc_total["cnt"] if doc_total else 0
        return counts

    def document_type_counts(self, scope: str = "real", batch_id: str = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if scope == "real": clauses.append("l.is_test=false")
        elif scope == "test": clauses.append("l.is_test=true")
        if batch_id:
            clauses.append("l.batch_id = %s")
            params.append(batch_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with pg.pool().connection() as c:
            rows = c.execute(
                f"SELECT COALESCE(NULLIF(d.document_type,''),'unclassified') as doc_type,count(*) as n "
                f"FROM legal_lead_documents d JOIN legal_leads l ON d.lead_id=l.lead_id "
                f"{where} GROUP BY 1 ORDER BY 2 DESC LIMIT 15", tuple(params)).fetchall()
        return [{"document_type": r["doc_type"], "n": r["n"]} for r in rows]

    def save_review(self, lead_id: str, system_status: str, decision: str, document_id: str = None,
                    corrected_data: Dict = None, reviewer: str = "reviewer",
                    note: str = "", is_test: bool = False) -> Dict[str, Any]:
        with pg.pool().connection() as c:
            rec = c.execute(
                "INSERT INTO legal_reviews(lead_id,document_id,system_status,decision,corrected_data,reviewer,note,is_test) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (lead_id, document_id, system_status, decision, Jsonb(corrected_data or {}), reviewer, note, is_test)
            ).fetchone()
        return dict(rec)

    def latest_review(self, lead_id: str) -> Optional[Dict[str, Any]]:
        with pg.pool().connection() as c:
            r = c.execute("SELECT * FROM legal_reviews WHERE lead_id=%s ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
        return dict(r) if r else None

    def review_history(self, lead_id: str) -> List[Dict[str, Any]]:
        with pg.pool().connection() as c:
            rows = c.execute("SELECT * FROM legal_reviews WHERE lead_id=%s ORDER BY id DESC", (lead_id,)).fetchall()
        return [dict(r) for r in rows]
