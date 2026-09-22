"""
Regression for the closed per-run schema (workspaces/legal/field_schema.py).

One real run asking for about eight things produced 108 distinct fields, the pincode alone under
four names. These tests pin how prompts become schemas and how returned keys fold onto them.
The three prompts below are the real prompts of the runs on record.
"""
import pytest

from workspaces.legal.field_schema import (
    RunSchema, build_schema, canonical_key, conform, slug,
)

PROMPT_NAMES_ONLY = "Extract name and address of borrower and co borrower from loan agreement under the heading schedule"
PROMPT_PROPERTY = ("Extract borrower and co-borrower's name and address  \n\n"
                   "Extract mortgaged property details including area of the plot, building no., plot no., "
                   "if mentioned any, pincode.\n\ntranslate the extracted text into english from any vernacular language")
PROMPT_MIXED = ("Extract name and address of borrower and co borrower from loan document under the heading schedule, "
                "only from loan document. If no loan document is found, refer KYC/ Miscalleanous report for "
                "borrower/co-borrower names and addresses\n\nExtract property details only from collateral "
                "document/Legal report/valuation report, if any of these mentioned \n\nrefer sanction letter for "
                "extracting sanction date\n\ntranslate the extracted text into english from any vernacular language")


@pytest.mark.parametrize("raw,expected", [
    # the model's drifted names for one concept
    ("mortgaged_property_details_pincode", "property_pincode"),
    ("mortgaged_property_pincode", "property_pincode"),
    ("property_pincode", "property_pincode"),
    ("pincode", "property_pincode"),
    ("mortgaged_property_details_plot_no", "property_plot_no"),
    ("property_details_land_area_sqft", "property_area"),
    ("property_details_carpet_area_sqft", "property_built_up_area"),
    ("mortgaged_property_details_area_of_the_plot", "property_area"),
    ("mortgaged_property_details", "property_details"),
    ("boundaries_east", "property_boundaries"),
    # the prompt analyzer's legacy vocabulary
    ("applicant_name", "borrower_name"),
    ("applicant_address", "borrower_address"),
    ("co_applicant_1", "co_borrower_1_name"),
    ("co_applicant_address_2", "co_borrower_2_address"),
    ("guarantor_1_add", "guarantor_1_address"),
    ("mortgaged_property_detail_1", "property_details"),
    ("property_owner_mortgagor", "property_owner"),
    ("roi_in_number", "roi_in_number"),
    # families in the orders people and models write them
    ("co_borrower_3_name", "co_borrower_3_name"),
    ("Co-Applicant No. 2 Address", "co_borrower_2_address"),
    ("co borrower name 4", "co_borrower_4_name"),
    # things with no canonical meaning
    ("property_details_rooms", None),
    ("co_borrower_1_age", None),
    ("name", None),
    ("", None),
])
def test_canonical_key(raw, expected):
    assert canonical_key(raw) == expected


def test_names_only_prompt_gets_names_and_addresses_nothing_else():
    s = build_schema(PROMPT_NAMES_ONLY)
    assert s.keys == ["borrower_name", "borrower_address"]
    assert s.families == {"co_borrower": ["name", "address"]}


def test_property_prompt_keeps_abbreviations_in_their_sentence():
    # "building no., plot no., … pincode." must not be split at the abbreviation dots
    s = build_schema(PROMPT_PROPERTY)
    for key in ("property_details", "property_area", "property_building_no", "property_plot_no",
                "property_pincode"):
        assert key in s.keys, key
    assert "account_no_lan" not in s.keys          # never asked for


def test_mixed_prompt_picks_up_the_sanction_date_it_asks_for():
    s = build_schema(PROMPT_MIXED)
    assert "sanction_date" in s.keys and "property_details" in s.keys
    assert s.families["co_borrower"] == ["name", "address"]


def test_words_inside_other_words_are_not_requests():
    s = build_schema("Extract the company name of the borrower")
    assert "npa_date" not in s.keys and "account_no_lan" not in s.keys   # "company" holds "pan"/"npa"-like runs


def test_a_field_with_no_canonical_name_is_kept_under_one_slug():
    class Inst:
        fields = ["caste of borrower"]
        doc_type = ""

    class Plan:
        instructions = [Inst()]
    s = build_schema("", Plan())
    assert slug("caste of borrower") in s.keys
    assert s.canonical("caste of borrower") == slug("caste of borrower")


def test_an_instruction_is_never_a_field():
    class Inst:
        fields = ["borrower name", "translate_to_english", "reference number", "remarks"]
        doc_type = ""

    class Plan:
        instructions = [Inst()]
    s = build_schema("", Plan())
    assert "translate_to_english" not in s.keys          # the analyzer's real mistake
    assert "reference_no" in s.keys and "remarks" in s.keys


def test_conform_quarantines_unrequested_keys_and_keeps_identity():
    s = build_schema(PROMPT_NAMES_ONLY)
    kept, extras = conform({"borrower_name": "Ravi", "co_borrower_1_name": "Sita",
                            "sanction_date": "01/01/2020", "account_no_lan": "HL123",
                            "field_scripts_borrower_name": "printed"}, s)
    assert kept == {"borrower_name": "Ravi", "co_borrower_1_name": "Sita", "account_no_lan": "HL123"}
    assert "sanction_date" in extras


def test_conform_folds_stray_property_parts_into_the_requested_details():
    s = build_schema("Extract property details")
    kept, extras = conform({"property_plot_no": "12", "property_pincode": "411001"}, s)
    assert kept["property_details"] == "Plot no.: 12; Property pincode: 411001"
    assert set(extras) == {"property_plot_no", "property_pincode"}      # the parts are kept too


def test_conform_assembles_boundaries_from_their_sides():
    s = build_schema("Extract property boundaries")
    kept, _ = conform({"boundaries_east": "Road", "boundaries_west": "House of Ram"}, s)
    assert kept["property_boundaries"] == "East: Road; West: House of Ram"


def test_no_schema_means_old_behaviour():
    kept, extras = conform({"anything": 1}, RunSchema())
    assert kept == {"anything": 1} and extras == {}


def test_schema_round_trips_and_tells_the_model_the_keys():
    s = build_schema(PROMPT_PROPERTY)
    again = RunSchema.from_json(s.to_json())
    assert again.keys == s.keys and again.families == s.families
    block = s.prompt_block()
    assert "- borrower_name:" in block and "co_borrower_1_name, co_borrower_2_name" in block
    assert "never invent other keys" in block


def test_family_membership_is_unbounded_but_attribute_bound():
    s = build_schema("Extract co-borrower names")
    assert s.allows("co_borrower_7_name")
    assert not s.allows("co_borrower_1_address")        # names were asked for, not addresses
