"""
Regression for the trust rule (workspaces/legal/trust.py) and page quality (page_quality.py).

The rule, agreed 2026-09-22: a value is trusted when it is found in the document's own text, or
when it is printed, on a page that passes the quality checks, and the model read it clearly.
"""
from workspaces.legal.page_quality import judge
from workspaces.legal.trust import (
    CLEAR, CONFIRMED, CORRECTED, LEAD_EMPTY, LEAD_EXTRACTED, LEAD_REVIEW, LEAD_REVIEWED,
    LEAD_UNVERIFIED, REVIEW, UNCHECKED, VERIFIED, evaluate,
)


def dossier(value="Ravi Kumar", source="model", script="printed", legibility="clear",
            page=3, quality_flags=(), notes=None):
    ev = {"script": script, "legibility": legibility} if legibility is not None else {"script": script}
    return {
        "borrower_name": value,
        "_field_notes": {"borrower_name": dict(notes or {})},
        "_page_extractions": [{"document_id": "D", "page_number": page,
                               "fields": {"borrower_name": value},
                               "field_sources": {"borrower_name": source},
                               "field_evidence": {"borrower_name": ev}}],
        "_page_quality": {"D": {str(page): {"flags": list(quality_flags)}}},
    }


def state(ed, **kw):
    t = evaluate(ed, **kw)
    return t["fields"]["borrower_name"]["state"], t["fields"]["borrower_name"]["reasons"], t["state"]


def test_found_in_the_documents_own_text_is_verified_whatever_the_image():
    s, reasons, lead = state(dossier(source="text_layer", script="handwritten", legibility=None,
                                     quality_flags=["blurry"]))
    assert (s, reasons, lead) == (VERIFIED, [], LEAD_EXTRACTED)


def test_printed_on_a_good_page_and_read_clearly_is_clear():
    assert state(dossier())[:2] == (CLEAR, [])


def test_handwriting_always_needs_review():
    s, reasons, lead = state(dossier(script="handwritten"))
    assert (s, reasons, lead) == (REVIEW, ["handwritten"], LEAD_REVIEW)


def test_a_poor_page_needs_review_even_if_the_model_felt_sure():
    s, reasons, _ = state(dossier(quality_flags=["blurry", "faint"]))
    assert s == REVIEW and reasons == ["blurry", "faint"]


def test_the_models_own_doubt_is_enough():
    assert state(dossier(legibility="partial"))[1] == ["partial"]
    assert state(dossier(legibility="illegible"))[1] == ["illegible"]


def test_no_verdict_is_not_verified_rather_than_a_problem():
    s, reasons, lead = state(dossier(legibility=None))
    assert (s, reasons, lead) == (UNCHECKED, ["unchecked"], LEAD_UNVERIFIED)


def test_disagreement_and_bad_formats_need_review_even_when_verified():
    ed = dossier(source="text_layer", notes={"conflict": "Mohan Lal (p. 9)"})
    assert state(ed)[1] == ["conflict"]
    ed = dossier(notes={"flags": ["looks like an email or username, not a name"]})
    assert state(ed)[1] == ["name"]
    assert state(dossier(value="लकी कुमार"))[1] == ["not_english"]


def test_a_persons_word_settles_the_field():
    ed = dossier(script="handwritten")
    s, _, lead = state(ed, corrections={"borrower_name": {"action": CORRECTED}})
    assert (s, lead) == (CORRECTED, LEAD_REVIEWED)
    s, _, lead = state(ed, corrections={"borrower_name": {"action": CONFIRMED}})
    assert (s, lead) == (CONFIRMED, LEAD_REVIEWED)


def test_a_dossier_with_nothing_in_it():
    assert evaluate({"_page_extractions": []})["state"] == LEAD_EMPTY


def test_identity_is_never_up_for_review():
    ed = dossier()
    ed["account_no_lan"] = "HL123"
    assert "account_no_lan" not in evaluate(ed)["fields"]


# ── page quality thresholds, as calibrated on the corpus ──
def m(**over):
    base = {"edge": 140, "paper": 250, "ink": 30, "ink_share": 0.05, "skew": 0, "dpi": 200}
    base.update(over)
    return base


def test_a_good_page_has_no_flags():
    assert judge(m()) == []


def test_each_threshold():
    assert judge(m(edge=90)) == ["blurry"]
    assert judge(m(ink=121)) == ["faint"]
    assert judge(m(paper=150)) == ["dark"]
    assert judge(m(dpi=80)) == ["low_resolution"]
    assert judge(m(skew=-6)) == ["rotated"]
    assert judge(m(skew=4)) == []


def test_a_blank_page_says_only_that():
    assert judge(m(ink=248, edge=10)) == ["blank"]
    assert judge(m(ink_share=0.0002)) == ["blank"]
