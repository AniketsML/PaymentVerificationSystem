"""
Regression for page re-attribution (workspaces/legal/page_attribution.py).

The model cites the page it read a value on by reading a stamped "PAGE N" badge. Measured over
a real 172-dossier run it was right 42% of the time, and where it cited nothing the pipeline
filed the value under the first page of the 20-page batch — a guess presented as a fact. These
tests pin the rules that replaced that: the document's own text decides, several batches citing
one value collapse to one row, and a value that cannot be checked is flagged rather than
quietly trusted.
"""
import pytest

from workspaces.legal import page_attribution as PA


@pytest.fixture
def texts(monkeypatch):
    """A 5-page document whose text we control, standing in for a real PDF."""
    pages = {
        1: "cover page",
        2: "this agreement is made between the parties",
        3: "borrower bharat lal beer resident of kurnool",
        4: "borrower bharat lal beer and co borrower sharda bai beer of kurnool",
        5: "signature page",
    }
    monkeypatch.setattr(PA, "_page_texts", lambda path: {k: PA._norm(v) for k, v in pages.items()})
    return pages


def _px(page, fields, **kw):
    rec = {"document_id": "D1", "filename": "loan.pdf", "page_number": page, "fields": fields}
    rec.update(kw)
    return rec


def test_a_value_is_filed_under_the_page_the_document_proves(texts):
    # the model said page 1, which does not contain the name at all; pages 3 and 4 do, and with
    # nothing else in the record to weigh them against, the earlier page wins
    out, stats = PA.reattribute([_px(1, {"borrower_name": "BHARAT LAL BEER"})], {"D1": "x.pdf"})
    assert [r["page_number"] for r in out] == [3]
    assert out[0]["field_sources"]["borrower_name"] == "text_layer"
    assert stats == {"values": 1, "verified": 1, "moved": 1, "unverified": 0}


def test_the_page_carrying_the_most_of_the_record_wins(texts):
    # both names appear on p4; only the borrower appears on p3
    out, _ = PA.reattribute(
        [_px(1, {"borrower_name": "Bharat Lal Beer", "co_borrower_1_name": "Sharda Bai Beer"})],
        {"D1": "x.pdf"})
    assert [r["page_number"] for r in out] == [4]
    assert set(out[0]["fields"]) == {"borrower_name", "co_borrower_1_name"}


def test_a_correct_citation_is_never_overridden(texts):
    # p3 and p4 both contain the borrower; the model said p3, so p3 stands
    out, stats = PA.reattribute([_px(3, {"borrower_name": "Bharat Lal Beer"})], {"D1": "x.pdf"})
    assert [r["page_number"] for r in out] == [3]
    assert stats["moved"] == 0


def test_four_batches_citing_one_value_collapse_to_one_row(texts):
    # this is what produced duplicate rows in the drawer before reconciliation
    dupes = [_px(p, {"borrower_name": "BHARAT LAL BEER"}) for p in (1, 2, 5, 3)]
    out, stats = PA.reattribute(dupes, {"D1": "x.pdf"})
    assert len(out) == 1
    assert stats["values"] == 1


def test_a_value_with_no_text_to_check_keeps_the_model_citation_and_is_flagged(texts):
    out, stats = PA.reattribute([_px(2, {"sanction_amount": "Rs. 24,87,912"})], {"D1": "x.pdf"})
    assert [r["page_number"] for r in out] == [2]
    assert out[0]["field_sources"]["sanction_amount"] == "model"
    assert stats == {"values": 1, "verified": 0, "moved": 0, "unverified": 1}


def test_a_document_that_cannot_be_read_leaves_every_page_as_cited():
    out, stats = PA.reattribute([_px(7, {"borrower_name": "Someone Here"})], {"D1": "/nope.pdf"})
    assert [r["page_number"] for r in out] == [7]
    assert stats["unverified"] == 1


def test_values_are_never_changed_added_or_dropped(texts):
    given = [_px(1, {"borrower_name": "Bharat Lal Beer", "roi_in_number": "13.5"}),
             _px(2, {"co_borrower_1_name": "Sharda Bai Beer"})]
    out, _ = PA.reattribute(given, {"D1": "x.pdf"})
    got = {k: v for r in out for k, v in r["fields"].items()}
    assert got == {"borrower_name": "Bharat Lal Beer", "roi_in_number": "13.5",
                   "co_borrower_1_name": "Sharda Bai Beer"}


def test_script_flags_follow_their_own_field(texts):
    # the whole chunk's flags used to be copied onto every page of that chunk
    given = [_px(1, {"borrower_name": "Bharat Lal Beer", "roi_in_number": "13.5"},
                 field_scripts={"borrower_name": "handwritten", "roi_in_number": "printed"})]
    out, _ = PA.reattribute(given, {"D1": "x.pdf"})
    for rec in out:
        assert set(rec["field_scripts"]) <= set(rec["fields"])
    flat = {k: v for r in out for k, v in r["field_scripts"].items()}
    assert flat["borrower_name"] == "handwritten"


def test_short_values_prove_nothing(texts):
    # "2012" would match any page that happens to mention it
    out, stats = PA.reattribute([_px(5, {"npa_date": "2012"})], {"D1": "x.pdf"})
    assert stats["verified"] == 0
    assert out[0]["page_number"] == 5


def test_an_approximate_transcription_still_finds_its_page(texts):
    # the model dropped a word; exact matching fails, word overlap does not
    out, stats = PA.reattribute([_px(1, {"borrower_address": "bharat lal beer kurnool"})],
                                {"D1": "x.pdf"})
    assert stats["verified"] == 1
    assert out[0]["page_number"] in (3, 4)


def test_reconciling_twice_changes_nothing(texts):
    once, _ = PA.reattribute([_px(1, {"borrower_name": "BHARAT LAL BEER"})], {"D1": "x.pdf"})
    twice, _ = PA.reattribute(once, {"D1": "x.pdf"})
    assert [r["page_number"] for r in once] == [r["page_number"] for r in twice]
    assert once[0]["fields"] == twice[0]["fields"]


def test_empty_input_is_handled():
    out, stats = PA.reattribute([], {})
    assert out == []
    assert stats["values"] == 0
