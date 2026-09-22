"""
Regression for consolidation (workspaces/legal/consolidate.py).

Batches number the people they find from one, and values used to merge by key — the first
"co_borrower_1" won and every other person with that key was silently dropped (77 of 204
dossiers). These tests pin the people-first merge, and the guard against the opposite failure:
sellers and witnesses in title deeds being promoted to co-borrowers.
"""
from workspaces.legal.consolidate import consolidate
from workspaces.legal.field_schema import build_schema

SCHEMA = build_schema("Extract name and address of borrower and co borrower from loan agreement. "
                      "Extract property details")
LOAN = "loan agreement.pdf"


def rec(page, fields, filename=LOAN, doc="D1"):
    return {"document_id": doc, "filename": filename, "page_number": page, "fields": dict(fields)}


def doc_type(r):
    from workspaces.legal.doc_filter import classify_doc_by_filename
    return classify_doc_by_filename(r.get("filename", ""))


def test_two_batches_using_the_same_key_for_different_people_keep_both():
    records = [rec(2, {"borrower_name": "Ravi Kumar", "co_borrower_1_name": "Sita Devi"}),
               rec(24, {"borrower_name": "Ravi Kumar", "co_borrower_1_name": "Mohan Lal"})]
    out = consolidate(records, SCHEMA, doc_type)
    names = {v for k, v in out.values.items() if k.endswith("_name")}
    assert names == {"Ravi Kumar", "Sita Devi", "Mohan Lal"}
    # and the page records now agree with the dossier about who is co-borrower 2
    who = {k: v for r in records for k, v in r["fields"].items()}
    assert who["co_borrower_1_name"] == "Sita Devi" and who["co_borrower_2_name"] == "Mohan Lal"


def test_spellings_of_one_person_are_one_person():
    records = [rec(2, {"borrower_name": "Bharat Lal", "co_borrower_1_name": "Firoz Bagwan"}),
               rec(9, {"borrower_name": "BHARAT LAL BEER", "co_borrower_1_name": "FIROZ BAGWAN"})]
    out = consolidate(records, SCHEMA, doc_type)
    assert sorted(k for k in out.values if k.endswith("_name")) == ["borrower_name", "co_borrower_1_name"]
    assert out.notes["borrower_name"]["variants"]


def test_kyc_forms_calling_everyone_applicant_do_not_contest_the_borrower():
    records = [rec(3, {"borrower_name": "Ravi Kumar", "co_borrower_1_name": "Sita Devi"}),
               rec(1, {"borrower_name": "SITA DEVI"}, filename="kyc.pdf", doc="K")]
    out = consolidate(records, SCHEMA, doc_type)
    assert out.values["borrower_name"] == "Ravi Kumar"
    assert not any(n.get("conflict") for n in out.notes.values())


def test_a_seller_named_once_in_a_deed_is_mentioned_not_a_co_borrower():
    records = [rec(3, {"borrower_name": "Ravi Kumar", "co_borrower_1_name": "Sita Devi"}),
               rec(140, {"co_borrower_1_name": "Ram Chandra"}, filename="sale deed.pdf", doc="S")]
    out = consolidate(records, SCHEMA, doc_type)
    assert "Ram Chandra" not in out.values.values()
    assert [m["name"] for m in out.mentioned] == ["Ram Chandra"]


def test_a_genuine_disagreement_about_the_borrower_is_recorded():
    records = [rec(3, {"borrower_name": "Ravi Kumar", "co_borrower_1_name": "Mohan Lal"}),
               rec(4, {"borrower_name": "Ravi Kumar", "co_borrower_1_name": "Mohan Lal"}),
               rec(9, {"borrower_name": "Mohan Lal", "co_borrower_1_name": "Ravi Kumar"})]
    out = consolidate(records, SCHEMA, doc_type)
    assert out.values["borrower_name"] == "Ravi Kumar"
    assert "also read as the borrower" in out.notes["co_borrower_1_name"]["conflict"]


def test_single_fields_agree_by_majority_and_record_disagreement():
    records = [rec(5, {"property_details": "Plot 12, Survey 44"}),
               rec(6, {"property_details": "Plot 12, Survey 44"}),
               rec(7, {"property_details": "Plot 91, Survey 3"})]
    out = consolidate(records, SCHEMA, doc_type)
    assert out.values["property_details"] == "Plot 12, Survey 44"
    assert "Plot 91" in out.notes["property_details"]["conflict"]


def test_a_conflict_names_each_page_once():
    records = [rec(5, {"property_details": "Plot 12"}), rec(6, {"property_details": "Plot 12"}),
               rec(9, {"property_details": "Plot 91"}), rec(9, {"property_details": "Plot 91"})]
    out = consolidate(records, SCHEMA, doc_type)
    assert out.notes["property_details"]["conflict"] == "Plot 91 (p. 9)"


def test_a_fuller_reading_is_not_a_conflict():
    records = [rec(5, {"property_details": "Plot 12"}), rec(6, {"property_details": "Plot 12, Survey 44"})]
    out = consolidate(records, SCHEMA, doc_type)
    assert out.values["property_details"] == "Plot 12, Survey 44"
    assert "conflict" not in out.notes["property_details"]


def test_notes_follow_a_renumbered_field():
    records = [rec(2, {"borrower_name": "Ravi", "co_borrower_1_name": "Sita Devi"}),
               {**rec(24, {"borrower_name": "Ravi", "co_borrower_1_name": "Mohan Lal"}),
                "field_notes": {"co_borrower_1_name": {"relation": "S/O Hari"}}}]
    consolidate(records, SCHEMA, doc_type)
    assert records[1]["field_notes"] == {"co_borrower_2_name": {"relation": "S/O Hari"}}
