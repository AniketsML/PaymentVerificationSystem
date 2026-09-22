"""
End to end through process_lead, with a fake model and a fake database.

The replies below are shaped like real ones: party clauses in name fields, keys the prompt never
asked for, bookkeeping without its underscore, and two documents that each number their people
from one. What gets saved must be the closed, cleaned, consolidated result.
"""
import json
import os

import pytest

fitz = pytest.importorskip("fitz")

from workspaces.legal import pipeline  # noqa: E402

PROMPT = "Extract name and address of borrower and co borrower from loan agreement"


class _Logger:
    def __init__(self):
        self.events = []

    def log(self, lead_id, stage, status, document_id=None, reason="", ms=0.0, metrics=None,
            data=None, is_test=False):
        self.events.append((stage, status, reason))


class _Model:
    """Replies by document, like the real client is called once per document batch."""
    def __init__(self, replies):
        self.replies = replies

    def extract_from_images(self, **kw):
        self.last_schema_block = kw.get("schema_block", "")
        for needle, reply in self.replies.items():
            if needle in kw["document_type"]:
                return json.loads(json.dumps(reply))
        return {}


def _pdf(path, lines):
    d = fitz.open()
    for text in lines:
        d.new_page().insert_text((72, 72), text)
    d.save(path)
    d.close()


@pytest.fixture
def saved(monkeypatch, tmp_path):
    """Run process_lead and return the extracted_data it saved."""
    out = {}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if sql.lstrip().startswith("UPDATE legal_leads SET status = %s, extracted_data"):
                out["status"], out["data"] = params[0], json.loads(params[1])
            return self

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class Pool:
        def connection(self):
            return Conn()

    import db.pg as pgmod
    from workspaces.legal import field_schema, prompt_analyzer
    monkeypatch.setattr(pgmod, "pool", lambda: Pool())
    monkeypatch.setattr(pipeline, "update_document_status", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_lead_doc_counts", lambda *a, **k: None)
    # no model calls for planning: the deterministic keyword reading only
    monkeypatch.setattr(prompt_analyzer, "analyze_extraction_prompt",
                        lambda p: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(field_schema, "schema_for_prompt", lambda p, plan=None: field_schema.build_schema(p))

    a = os.path.join(tmp_path, "Loan Agreement.pdf")
    b = os.path.join(tmp_path, "Loan Agreement Annexure.pdf")
    _pdf(a, ["Schedule", "Borrower Bharatlal residing at Kurnool, co-borrower Sharda Bai Beer"])
    _pdf(b, ["Annexure", "Co-borrower Gopal Lal Beer"])
    docs = [{"document_id": "DA", "filename": "Loan Agreement.pdf", "file_path": a, "metadata": {}},
            {"document_id": "DB", "filename": "Loan Agreement Annexure.pdf", "file_path": b, "metadata": {}}]
    meta = {"model": "Medha", "prompt_tokens": 900, "completion_tokens": 40, "ms": 5000}
    model = _Model({
        "Annexure": {  # the second document numbers its person from one too
            "co_borrower_1_name": "Gopal Lal Beer s/o Bharatlal",
            "co_borrower_1_address": "Kurnool",
            "_field_evidence": {"co_borrower_1_name": {"page": 2, "script": "handwritten",
                                                       "legibility": "partial", "issue": "faint ink"}},
            "_meta": meta, "_ocr_route": "medha_vlm", "_raw_response": "{...}"},
        "Loan Agreement.pdf": {
            "borrower_name": "Mr. Bharatlal s/o Gopalbil, caste - Beer, age - 52 years",
            "borrower_address": "Kurnool",
            "co_borrower_1_name": "Sharda Bai Beer",
            "sanction_amount": "500000",                              # never asked for
            "field_scripts": {"borrower_name": "printed"},            # no underscore
            "_field_evidence": {"borrower_name": {"page": 2, "script": "printed", "legibility": "clear"}},
            "_meta": meta, "_ocr_route": "medha_vlm", "_raw_response": "{...}"},
    })
    logger = _Logger()
    pipeline.process_lead("LEAD-T", "T", "UGTEST0000000001", docs, model, logger, extraction_prompt=PROMPT)
    out["model"], out["events"] = model, logger.events
    return out


def test_the_model_is_told_the_closed_key_list(saved):
    block = saved["model"].last_schema_block
    assert "- borrower_name:" in block and "never invent other keys" in block
    assert "sanction_amount" not in block


def test_only_schema_fields_and_identity_are_saved(saved):
    values = {k for k, v in saved["data"].items() if not k.startswith("_")
              and not isinstance(v, (dict, list)) and k not in ("telemetry",)}
    assert values <= {"borrower_name", "borrower_address", "co_borrower_1_name", "co_borrower_1_address",
                      "co_borrower_2_name", "co_borrower_2_address", "account_no_lan"}
    assert "sanction_amount" in saved["data"]["_extras"]
    assert not any(k.startswith("field_scripts") for k in saved["data"])


def test_names_are_clean_and_the_relation_is_kept_aside(saved):
    d = saved["data"]
    assert d["borrower_name"] == "Bharatlal"
    notes = d["_field_notes"]["borrower_name"]
    assert notes["relation"] == "S/O Gopalbil"
    assert "caste" in notes["raw"]


def test_both_documents_co_borrowers_survive(saved):
    d = saved["data"]
    co = {d.get("co_borrower_1_name"), d.get("co_borrower_2_name")}
    assert co == {"Sharda Bai Beer", "Gopal Lal Beer"}


def test_the_models_legibility_verdict_reaches_the_dossier(saved):
    d = saved["data"]
    key = next(k for k, v in d.items() if v == "Gopal Lal Beer")
    assert d["_field_notes"][key]["legibility"] == "partial"
    assert d["_field_notes"][key]["issue"] == "faint ink"
    assert d["_schema"]["families"] == {"co_borrower": ["name", "address"]}
