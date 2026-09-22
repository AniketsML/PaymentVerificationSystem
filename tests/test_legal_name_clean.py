"""
Regression for name cleaning (workspaces/legal/name_clean.py).

Cases are real name values from the runs on record, plus the traps that a careless cleaner would
fall into: firm names containing "AND", surnames that are also Hindi relation words, D.O.B.
"""
import pytest

from workspaces.legal.name_clean import clean_address, clean_name, same_person


@pytest.mark.parametrize("raw,name,relation", [
    ("Bharatlal s/o Gopalbil", "Bharatlal", "S/O Gopalbil"),
    ("Ganesh Singh s/o Nainsingh, caste - Rajpoot, age - 32 years, resident - Phaskeda, tehsil - Su",
     "Ganesh Singh", "S/O Nainsingh"),
    ("Bagdai Panchal wife of Shri Bhurilal Panchal", "Bagdai Panchal", "W/O Shri Bhurilal Panchal"),
    ("Chitra (daughter of Shri Shanti Mahajan)", "Chitra", "D/O Shri Shanti Mahajan"),
    ("Ku. Rekha Bairagi pita Shri Purshottamdas ji Bairagi jati Bairagi", "Rekha Bairagi",
     "Father: Shri Purshottamdas ji Bairagi"),
    ("Ramesh putra Shri Mohan Lal niwasi Jaipur", "Ramesh", "S/O Shri Mohan Lal"),
    ("Borrower: Mrs. Sunita Devi W/O Ram Lal, aged 40", "Sunita Devi", "W/O Ram Lal"),
    ("Mr. Firaj Bagwan", "Firaj Bagwan", ""),
    ("GOVINDARAJ (WORKS CONTRACTOR)", "GOVINDARAJ", ""),
    ("Ravi Kumar D.O.B 12/03/1980", "Ravi Kumar", ""),        # not "daughter of"
    ("Ravi Kumar PAN ABCDE1234F", "Ravi Kumar", ""),
    ("Suresh Kumar 45 years", "Suresh Kumar", ""),
])
def test_real_party_clauses(raw, name, relation):
    c = clean_name(raw)
    assert (c.name, c.relation) == (name, relation)
    assert c.original == raw                                   # nothing is lost


@pytest.mark.parametrize("raw", [
    "BOTREE AND CO.", "KHETESHWAR BIKANERI SWEET AND RESTORANT", "GRISHMAHI STORE AND LADIES WEAR",
    "Ramesh Pati",       # Pati is a surname, and a Hindi word for husband
    "Rakesh Pan",        # Pan is a surname, and looks like "PAN"
])
def test_names_that_look_like_clauses_are_left_alone(raw):
    c = clean_name(raw)
    assert c.name == raw and not c.relation


def test_what_cannot_be_fixed_is_flagged_not_guessed():
    handle = clean_name("anandsinghrajpurohit94")
    assert handle.name == "anandsinghrajpurohit94"
    assert "looks like an email or username, not a name" in handle.flags
    two = clean_name("Kishore Chand and Jayanti Lal, sons of Shri Shanti Mahajan")
    assert two.name == "Kishore Chand and Jayanti Lal"
    assert "may name more than one person" in two.flags
    assert "not in English script" in clean_name("लकी कुमार जैन").flags
    assert "marked 'Late' (deceased)" in clean_name("Late Shri Mohan Lal").flags


def test_addresses_keep_their_commas():
    c = clean_address("Address: 12, Gandhi Road, Jaipur 302001")
    assert c.name == "12, Gandhi Road, Jaipur 302001"


@pytest.mark.parametrize("a,b,same", [
    ("Bharat Lal", "BHARAT LAL BEER", True),         # a dropped surname
    ("Firoz Bagwan", "FIROZ BAGWAN", True),
    ("Mr. Firaj Bagwan", "Firaj Bagwan", True),
    ("Lal Bharat", "Bharat Lal", True),              # words reordered
    ("Kherun Bi", "Barakat Bi", False),              # two people sharing a surname
    ("Sharda Devi", "Parvati Devi", False),
    ("", "Ravi", False),
])
def test_same_person(a, b, same):
    assert same_person(a, b) is same
