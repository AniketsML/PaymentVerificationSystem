"""
The mapping/validation contract — the firewall between the source schema and the pipeline.

Loads `source_mapping.json`, turns a raw source row into the canonical pipeline `row` dict,
and validates it. A row that fails validation is QUARANTINED with a precise reason and never
reaches the pipeline — so malformed / schema-drifted data can never produce a bogus verdict.
0 quarantined is normal; a spike is the early-warning that the source schema changed.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation


def _strip_doc(d: dict) -> dict:
    """Drop keys beginning with '_' (inline documentation in the JSON)."""
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def _num_ok(s) -> bool:
    m = re.search(r"[0-9]+(?:\.[0-9]{1,2})?", str(s or "").replace(",", ""))
    if not m:
        return False
    try:
        Decimal(m.group(0))
        return True
    except InvalidOperation:
        return False


def _date_ok(s) -> bool:
    if s is None or str(s).strip() == "":
        return False
    from dateutil import parser as dp
    txt = str(s)
    dayfirst = not re.match(r"^\s*\d{4}-\d{1,2}-\d{1,2}", txt)
    try:
        dp.parse(txt, dayfirst=dayfirst, fuzzy=True)
        return True
    except Exception:
        return False


def _present(v) -> bool:
    return v is not None and str(v).strip().lower() not in ("", "null", "none", "nan")


class Mapping:
    def __init__(self, cfg: dict):
        cur = cfg.get("cursor", {})
        lid = cfg.get("lead_id", {})
        self.table = cfg.get("table", "")
        self.created_at_col = cur.get("created_at_column", "created_at")
        self.id_col = cur.get("id_column", "id")
        self.lead_id_col = lid.get("column", self.id_col)
        self.lead_id_prefix = lid.get("prefix", "")
        self.fields = _strip_doc(cfg.get("fields", {}))          # canonical -> source column
        self.image_col = self.fields.get("image", "")            # source column holding the doc URL
        self.required = list(cfg.get("required", []))
        val = _strip_doc(cfg.get("validate", {}))
        self.check_amount = bool(val.get("amount_numeric", True))
        self.check_date = bool(val.get("date_parseable", True))
        self.passthrough = bool(cfg.get("extra_columns_passthrough", True))

    # canonical field -> the source value on a raw row
    def _canon(self, raw: dict) -> dict:
        out = {}
        for canonical, src_col in self.fields.items():
            if canonical == "image":
                continue                                          # handled separately
            out[canonical] = raw.get(src_col)
        return out

    def lead_id_of(self, raw: dict) -> str:
        v = raw.get(self.lead_id_col)
        v = "" if v is None else str(v).strip()
        return (self.lead_id_prefix + v) if v else ""

    def cursor_of(self, raw: dict):
        return raw.get(self.created_at_col), (
            "" if raw.get(self.id_col) is None else str(raw.get(self.id_col)))

    def map_row(self, raw: dict):
        """Returns (pipeline_row | None, lead_id | None, reason | None).
        On a validation failure the first element is None and `reason` explains why."""
        lead_id = self.lead_id_of(raw)
        if not lead_id:
            return None, None, f"missing lead id (column '{self.lead_id_col}')"

        cur_ts, _cur_id = self.cursor_of(raw)
        if cur_ts is None:
            return None, lead_id, f"missing cursor timestamp (column '{self.created_at_col}')"

        row = self._canon(raw)

        # image → the column the pipeline's resolver looks at first, plus keep the raw
        # column so the fallback URL-scanner still works.
        image_val = raw.get(self.image_col) if self.image_col else None
        if image_val is not None:
            row["payment_document"] = image_val

        # passthrough: carry every other source column (audit + the image fallback scanner),
        # without clobbering a canonical field we already set.
        if self.passthrough:
            mapped_src = set(self.fields.values())
            for k, v in raw.items():
                if k in mapped_src or k in row:
                    continue
                row[k] = v

        # ── validation (fail closed → quarantine) ─────────────────────────────
        for canonical in self.required:
            if not _present(row.get(canonical)):
                return None, lead_id, f"missing required field '{canonical}'"
        if self.check_amount and _present(row.get("payment_amount")) and not _num_ok(row["payment_amount"]):
            return None, lead_id, f"payment_amount not numeric: {row.get('payment_amount')!r}"
        if self.check_date and _present(row.get("payment_date")) and not _date_ok(row["payment_date"]):
            return None, lead_id, f"payment_date not parseable: {row.get('payment_date')!r}"

        return row, lead_id, None


def load_mapping(path: str) -> Mapping:
    with open(path, encoding="utf-8") as f:
        return Mapping(json.load(f))
