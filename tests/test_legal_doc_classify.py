"""
Regression for the legal filename classifier (workspaces/legal/doc_filter.py).

Real dossiers name the same document a dozen ways — "LOAN AGREEMENT.pdf", "loan_agreement.pdf",
"LoanAgreement-671cadb3672ab.pdf", "UGSUPTH0000014242 - LOAN AGREEMENT.pdf". The classifier used
to match raw substrings, so a keyword written with an underscore only ever matched a filename
written with an underscore, and "UGKOLTH0000073501 - LEGAL REPORT.pdf" went unrecognised.

The cases below are drawn from filenames actually present in the corpus.
"""
import pytest

from workspaces.legal.doc_filter import classify_doc_by_filename as classify

SEPARATORS = [
    "LOAN AGREEMENT.pdf", "Loan agreement.pdf", "loan_agreement.pdf", "Loan-Agreement.pdf",
    "LoanAgreement.pdf", "LOANAGREEMENT-67e80b17a8d5a.pdf", "loanagreement-6568910507dc7.pdf",
    "UGSUPTH0000014242 - LOAN AGREEMENT.pdf", "LoanAgreement-671cadb3672ab.pdf",
]


@pytest.mark.parametrize("filename", SEPARATORS)
def test_one_document_written_every_which_way(filename):
    assert classify(filename) == "loan_agreement"


@pytest.mark.parametrize("filename,expected", [
    ("UGKOLTH0000073501 - LEGAL REPORT.pdf", "legal_report"),
    ("LegalReport.pdf", "legal_report"),
    ("legal_report.pdf", "legal_report"),
    ("TitleSearchReport.pdf", "legal_report"),
    ("Sale Deed 4565-2018._compressed.pdf", "collateral_deed"),
    ("SaleDeed3371-2021.pdf", "collateral_deed"),
    ("LinkSaleDeed17298-2019.pdf", "collateral_deed"),
    ("Gift deed 1373.pdf", "collateral_deed"),
    ("PattadharPassbookCopy.pdf", "collateral_deed"),
    ("COLLATERALDOCUMENTS-.pdf", "collateral_deed"),
    ("Deposit of Title deed 7225 2012.pdf", "modt"),
    ("grishmahi StoreMODT-670f68151292d.pdf", "modt"),
    ("MODTD IN FAVOUR OF UGRO.pdf", "modt"),
    ("HCFKOLSEC00001018478fcl.pdf", "foreclosure_notice"),
    ("UGTAMMS0000081408--fcl.pdf", "foreclosure_notice"),
    ("DemandNotice.pdf", "foreclosure_notice"),
    ("SanctionLetter-671cae4596791.pdf", "sanction_letter"),
    ("sanctionletter-656890f31cc2a.pdf", "sanction_letter"),
])
def test_corpus_filenames(filename, expected):
    assert classify(filename) == expected


def test_longest_keyword_wins_at_the_same_position():
    # not a bare "agreement": the key facts statement is the more specific name
    assert classify("LOANAGREEMENTKEYFACTSSTATEMENT-679b072a2f45a.pdf") == "loan_agreement"


def test_earliest_keyword_wins_for_a_combined_document():
    assert classify("sanction letter and loan agreement.pdf") == "sanction_letter"
    assert classify("Loan Agreement and Sanction Letter.pdf") == "loan_agreement"


@pytest.mark.parametrize("filename", [
    "HSLA Corp profile.pdf",        # 'sl' inside a word is not a sanction letter
    "Disagreement note.pdf",        # 'agreement' inside a word is not a loan agreement
    "830 - 2008.pdf",               # a registration number carries no type at all
    "HOUSE ABSTRACKS.pdf",
    "",
])
def test_nothing_is_guessed(filename):
    assert classify(filename) == "document"


def test_pipeline_shares_the_same_classifier():
    # two copies of the keyword table drifted apart once; they must not again
    from workspaces.legal.pipeline import _classify_by_filename
    assert _classify_by_filename("UGKOLTH0000073501 - LEGAL REPORT.pdf") == "legal_report"
    assert _classify_by_filename("830 - 2008.pdf") == ""      # "" means "no idea", not "document"
