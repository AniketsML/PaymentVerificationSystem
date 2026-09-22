"""
A person's word on a value.

A reviewer looking at the page preview can either confirm a value as read or type the right one.
Both are recorded as events in `legal_field_corrections` — who, when, what it was, what it became —
and never edit history: reverting is another event.

How a correction reaches every screen without each screen knowing about corrections:

  - write-through: the corrected value is written into the stored extraction itself, so the
    dashboard, the drawer, the export and observability all read it where they already read values
  - `_model_values` keeps what the model read, so the drawer can show both and a revert restores it
  - `_overrides` holds every person-set value, including a value deliberately cleared; the read path
    applies it last, so a cleared field can't be refilled from a page record behind it
  - `_trust` is re-evaluated: a corrected or confirmed field is settled, whatever the evidence said

A re-run keeps corrections: the pipeline re-applies them after extracting (apply_to). A
confirmation is of one specific value — if a re-run reads something different, the confirmation no
longer applies and the field is judged afresh. Saves use optimistic locking: a reviewer's save is
refused if the value changed since they loaded it, so two reviewers can't overwrite each other.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from psycopg.types.json import Jsonb

from db import pg

CORRECTED, CONFIRMED, REVERTED = "corrected", "confirmed", "reverted"
ACTIONS = (CORRECTED, CONFIRMED, REVERTED)
MAX_VALUE_LEN = 2000


class CorrectionError(ValueError):
    """A save that must not happen; `status` is the HTTP status the route should answer with."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def latest(lead_id: str) -> Dict[str, Dict[str, Any]]:
    """The standing person-set state of each field: {field: {action, value, previous, reviewer, ts}}.
    A field whose latest event is a revert has no standing correction."""
    with pg.pool().connection() as c:
        rows = c.execute(
            "SELECT DISTINCT ON (field) field, action, value, previous, reviewer, note, ts "
            "FROM legal_field_corrections WHERE lead_id = %s ORDER BY field, id DESC", (lead_id,)).fetchall()
    return {r["field"]: {"action": r["action"], "value": r["value"], "previous": r["previous"],
                         "reviewer": r["reviewer"], "note": r["note"],
                         "ts": r["ts"].isoformat() if r.get("ts") else None}
            for r in rows if r["action"] != REVERTED}


def history(lead_id: str, field: Optional[str] = None) -> List[Dict[str, Any]]:
    sql, params = "SELECT * FROM legal_field_corrections WHERE lead_id = %s", [lead_id]
    if field:
        sql += " AND field = %s"
        params.append(field)
    with pg.pool().connection() as c:
        rows = c.execute(sql + " ORDER BY id DESC LIMIT 200", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ts"] = d["ts"].isoformat() if d.get("ts") else None
        out.append(d)
    return out


def apply_to(ed: Dict[str, Any], corrections: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Overlay standing corrections onto a (freshly extracted) extraction, in place.

    Called by the pipeline after a re-run so a person's corrections survive it; a confirmation
    that no longer matches what the model reads is dropped from `corrections` (in place too)."""
    model_values = ed.setdefault("_model_values", {})
    overrides = ed.setdefault("_overrides", {})
    for field in list(corrections):
        c = corrections[field]
        current = ed.get(field)
        if c["action"] == CORRECTED:
            if field not in model_values:
                model_values[field] = current
            _set(ed, field, c["value"])
            overrides[field] = c["value"]
        elif c["action"] == CONFIRMED and _same(current, c["value"]):
            continue
        else:
            corrections.pop(field)                  # confirmed a value that is no longer there
    return ed


def _same(a: Any, b: Any) -> bool:
    return str(a if a is not None else "").strip() == str(b if b is not None else "").strip()


def _set(ed: Dict[str, Any], field: str, value: Any) -> None:
    if value in (None, ""):
        ed.pop(field, None)
    else:
        ed[field] = value


def _editable(ed: Dict[str, Any], field: str) -> bool:
    """A value field of this dossier: allowed by its schema (or present, for pre-schema rows) —
    never bookkeeping."""
    from workspaces.legal.field_schema import IDENTITY_KEYS, RunSchema
    from workspaces.legal.meta import is_meta_key
    if not field or field.startswith("_") or is_meta_key(field) or field in ("telemetry", "page_extractions"):
        return False
    schema = RunSchema.from_json(ed.get("_schema"))
    if schema is not None and not schema.is_empty():
        return schema.allows(field) or field in IDENTITY_KEYS
    return field in ed


def record(lead_id: str, field: str, action: str, value: Any = None, expected: Any = None,
           reviewer: str = "reviewer", note: str = "") -> Dict[str, Any]:
    """Save one person's decision about one field, and return the field's new standing.

    `expected` is the value the reviewer was looking at; if the stored value has changed since,
    nothing is saved (409) — reload and decide again."""
    from workspaces.legal.trust import evaluate

    action = (action or "").strip().lower()
    if action not in ACTIONS:
        raise CorrectionError(f"unknown action {action!r}")
    if value is not None and len(str(value)) > MAX_VALUE_LEN:
        raise CorrectionError(f"value is longer than {MAX_VALUE_LEN} characters")
    value = str(value).strip() if value is not None else None

    with pg.pool().connection() as c:
        with c.transaction():
            row = c.execute("SELECT extracted_data, is_test FROM legal_leads WHERE lead_id = %s FOR UPDATE",
                            (lead_id,)).fetchone()
            if not row:
                raise CorrectionError("no such dossier", 404)
            ed = row["extracted_data"] or {}
            if not _editable(ed, field):
                raise CorrectionError(f"{field!r} is not a value of this dossier")
            current = ed.get(field)
            if expected is not None and not _same(current, expected):
                raise CorrectionError("this value changed since you loaded it — reload and check again", 409)

            model_values = ed.setdefault("_model_values", {})
            overrides = ed.setdefault("_overrides", {})
            if action == CORRECTED:
                if _same(current, value) and field not in overrides:
                    action = CONFIRMED                     # typing the same value back is a confirmation
                else:
                    model_values.setdefault(field, current)
                    _set(ed, field, value)
                    overrides[field] = value
            if action == CONFIRMED:
                value = current
            if action == REVERTED:
                if field in model_values or field in overrides:
                    value = model_values.pop(field, current)       # back to what the model read
                    overrides.pop(field, None)
                    _set(ed, field, value)
                elif field in latest_in(c, lead_id):
                    value = current                                # un-confirming changes no value
                else:
                    raise CorrectionError("nothing to revert")

            c.execute("INSERT INTO legal_field_corrections(lead_id, field, action, value, previous, "
                      "reviewer, note, is_test) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                      (lead_id, field, action, value, current, reviewer, note or None, bool(row["is_test"])))
            standing = latest_in(c, lead_id)
            ed["_trust"] = evaluate(ed, corrections=standing)
            # updated_at is left alone on purpose: a review is not new extraction work, and the
            # dashboard (sorted by it) must not reorder under a reviewer working down the list
            c.execute("UPDATE legal_leads SET extracted_data = %s WHERE lead_id = %s", (Jsonb(ed), lead_id))
            c.execute("UPDATE legal_lead_results SET raw_extractions = %s, applicant_name = %s, "
                      "applicant_address = %s WHERE lead_id = %s",
                      (Jsonb(ed), ed.get("borrower_name"), ed.get("borrower_address"), lead_id))
    return {"lead_id": lead_id, "field": field, "action": action, "value": ed.get(field),
            "model_value": ed.get("_model_values", {}).get(field),
            "trust": ed["_trust"]["fields"].get(field), "lead_state": ed["_trust"]["state"],
            "reasons": ed["_trust"]["reasons"]}


def latest_in(c, lead_id: str) -> Dict[str, Dict[str, Any]]:
    """latest(), inside an open transaction so it sees the event just written."""
    rows = c.execute(
        "SELECT DISTINCT ON (field) field, action, value, reviewer, ts "
        "FROM legal_field_corrections WHERE lead_id = %s ORDER BY field, id DESC", (lead_id,)).fetchall()
    return {r["field"]: {"action": r["action"], "value": r["value"], "reviewer": r["reviewer"]}
            for r in rows if r["action"] != REVERTED}
