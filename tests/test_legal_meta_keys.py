"""
Regression: the model's bookkeeping must never become a value column.

The model is asked for `_field_scripts`, `_cited_pages` and `_field_page_sources`. It sometimes
drops the underscore, and a nested "field_scripts" object was then flattened by the normaliser
into value keys — "field_scripts_borrower_name" appeared as a dashboard column and a CSV header.
"""
from workspaces.legal.meta import canonical_meta, is_flattened_leak, is_meta_key, strip_flattened
from workspaces.legal.ocr import _parse_json
from workspaces.legal.pipeline import _normalize_extracted_fields


def test_unprefixed_bookkeeping_is_moved_under_its_underscore_name():
    parsed = canonical_meta({"borrower_name": "Ravi Kumar",
                             "field_scripts": {"borrower_name": "printed"},
                             "cited_pages": [3]})
    assert parsed == {"borrower_name": "Ravi Kumar",
                      "_field_scripts": {"borrower_name": "printed"}, "_cited_pages": [3]}


def test_the_underscore_spelling_wins_when_both_arrive():
    parsed = canonical_meta({"_field_scripts": {"a": "printed"}, "field_scripts": {"a": "handwritten"}})
    assert parsed["_field_scripts"] == {"a": "printed"}
    assert "field_scripts" not in parsed


def test_parser_canonicalises_every_json_shape():
    fenced = '```json\n{"borrower_name": "X", "field_scripts": {"borrower_name": "printed"}}\n```'
    for raw in ('{"borrower_name": "X", "field_scripts": {"borrower_name": "printed"}}',
                fenced,
                'Here you go: {"borrower_name": "X", "field_scripts": {"borrower_name": "printed"}} done'):
        out = _parse_json(raw)
        assert "field_scripts" not in out and "_field_scripts" in out, raw


def test_normaliser_never_emits_bookkeeping_as_values():
    # exactly the shape that produced field_scripts_* columns
    out = _normalize_extracted_fields({
        "borrower_name": "Ravi Kumar",
        "field_scripts": {"borrower_name": "printed", "co_borrower_1_name": "handwritten"},
        "field_sources": {"borrower_name": "text_layer"},
        "field_evidence": {"borrower_name": {"page": 3}},
    })
    assert out == {"borrower_name": "Ravi Kumar"}


def test_already_flattened_keys_from_older_runs_are_recognised():
    assert is_meta_key("field_scripts_borrower_name")
    assert is_flattened_leak("field_scripts_co_borrower_2_address")
    assert not is_flattened_leak("_field_scripts")          # the legitimate note
    assert not is_flattened_leak("telemetry")               # a deliberate unprefixed copy
    assert not is_flattened_leak("borrower_name")


def test_real_fields_that_merely_resemble_bookkeeping_survive():
    # none of these are notes — they must stay values
    for key in ("property_status", "loan_status", "evidence_of_title", "error_code_no"):
        assert not is_flattened_leak(key), key


def test_strip_flattened_removes_only_the_leak():
    values = {"borrower_name": "Ravi", "field_scripts_borrower_name": "printed",
              "telemetry": {"x": 1}, "_field_scripts": {"borrower_name": "printed"}}
    assert strip_flattened(values) == 1
    assert values == {"borrower_name": "Ravi", "telemetry": {"x": 1},
                      "_field_scripts": {"borrower_name": "printed"}}
