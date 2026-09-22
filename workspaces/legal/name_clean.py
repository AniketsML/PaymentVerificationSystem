"""
A name field holds a name.

Measured on real runs, 14% of name values carried something else. 44 of the 55 were one
pattern — the party clause of an Indian loan document copied whole:

    Bharatlal s/o Gopalbil
    Ganesh Singh s/o Nainsingh, caste - Rajpoot, age - 32 years, resident - Phaskeda, tehsil - …

The prompt now asks for the name alone. This module is the backstop, and it only removes what it
can positively identify: a relation marker and what follows it (kept separately, as the relation),
trailing age / caste / residence / ID clauses, a leading role label, and civil honorifics. It never
splits on "and" — "BOTREE AND CO." and "KHETESHWAR BIKANERI SWEET AND RESTORANT" are correct firm
names — and anything it cannot fix (digits, an email handle, a script that is not English) is
flagged for review rather than guessed at. The original text is always kept.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import List

# S/O, W/O, D/O, H/O, son of, wife of … — the relation, which belongs in its own field
# Includes the transliterated Hindi forms of deeds — "Rekha pita Shri Purshottamdas" (father),
# "Ramesh putra Shri Mohan" (son of) — but only when an honorific follows, as it does in a deed:
# Pati is also a surname, and "Ramesh Pati" must stay whole. The lookahead on the abbreviated
# form keeps "D.O.B" from reading as "daughter of".
_RELATION = re.compile(
    r"\b(?:(?P<abbr>[swdh])\s*[/\\.]\s*o\b(?!\s*[./]?\s*b\b)\.?"
    r"|(?P<word>son|wife|daughter|husband)\s+of\b"
    r"|(?P<hindi>putra|putri|patni|pita|pati)\s+(?=(?:shri|sri|sh|smt|late|swargiya|sw)\b))", re.I)
_MARKER = {"s": "S/O", "w": "W/O", "d": "D/O", "h": "H/O",
           "son": "S/O", "wife": "W/O", "daughter": "D/O", "husband": "H/O",
           "putra": "S/O", "putri": "D/O", "patni": "W/O", "pita": "Father:", "pati": "Husband:"}
# C/O is an address marker, not a family relation: everything from it on is dropped
_CARE_OF = re.compile(r"\bc\s*[/\\.]\s*o\b\.?|\bcare\s+of\b", re.I)
# where the name ends and the rest of the party clause begins
# Words only where they cannot be part of a name: "pan" is deliberately absent because Pan is a
# surname ("Rakesh Pan"); a PAN, Aadhaar or phone number is caught by its digit pattern instead.
_CLAUSE = re.compile(
    r"[,;(]|\s\d|(?:\bpan\s*(?:no\.?|number|card)?\s*[:\-]?\s*)?\b[A-Z]{5}\d{4}[A-Z]\b"
    r"|\b(?:aged?|age\s*[:\-]|caste|jati|jaati|religion|resident|residing|niwasi|r\s*/\s*o|occupation|by\s+occupation"
    r"|aadhaa?r|mobile|phone|dob|d\.o\.b|email|e-mail|nationality|years?\s+old)\b", re.I)
_ROLE_LABEL = re.compile(
    r"^\s*(?:primary\s+)?(?:co[\s\-]?)?(?:borrower|applicant|guarantor|mortgagor|owner)"
    r"(?:\s*(?:no\.?)?\s*\d+)?\s*[:\-–]\s*", re.I)
_HONORIFIC = re.compile(r"^\s*(?:mr|mrs|ms|miss|shri|sri|sh|smt|kum|kumari|km|ku|m\s*/\s*s|messrs|dr)\b\.?\s+", re.I)
_SEVERAL = re.compile(r"\s(?:and|&)\s|\bsons\s+of\b", re.I)
_LATE = re.compile(r"^\s*late\b\.?\s+", re.I)
_EMAILISH = re.compile(r"@|\.(?:com|in|net|org)\b|^[a-z][a-z0-9._]*\d{2,}$", re.I)
_FIRM = re.compile(r"\b(?:and\s+co|& ?co|traders?|enterprises?|industries|pvt|private|ltd|limited|llp|store|stores"
                   r"|sons|brothers|bros|agency|agencies|company|corporation|works|foods?|restaurant|restorant"
                   r"|sweets?|house|centre|center|mart|textiles?|jewell?ers?|hotel|motors?|associates)\b", re.I)
_NON_LATIN = re.compile(r"[^\x00-\x7F]")
_ABBREV_END = re.compile(r"\b(?:co|ltd|pvt|inc|bros|corp)\.$", re.I)


@dataclass
class CleanName:
    name: str
    relation: str = ""          # "S/O Gopalbil"
    dropped: str = ""           # what was cut off, so nothing is silently lost
    flags: List[str] = field(default_factory=list)
    original: str = ""

    @property
    def changed(self) -> bool:
        return self.name != self.original


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = s.strip(" ,;:-–—|()[]")
    if s.endswith(".") and not _ABBREV_END.search(s):
        s = s[:-1].rstrip()
    return s


def clean_name(value: str) -> CleanName:
    """The name, and only the name, from a party clause."""
    original = str(value or "")
    s = re.sub(r"\s+", " ", original).strip()
    flags: List[str] = []
    dropped: List[str] = []

    m = _ROLE_LABEL.match(s)
    if m:
        s = s[m.end():]
    if _LATE.match(s):
        flags.append("marked 'Late' (deceased)")
        s = _LATE.sub("", s)
    while _HONORIFIC.match(s):
        s = _HONORIFIC.sub("", s, count=1)

    relation = ""
    rel = _RELATION.search(s)
    care = _CARE_OF.search(s)
    if care and (not rel or care.start() < rel.start()):
        dropped.append(s[care.start():].strip())
        s = s[:care.start()]
    elif rel:
        tail = s[rel.end():]
        cut = _CLAUSE.search(tail)
        who = _tidy(tail[:cut.start()] if cut else tail)
        if cut:
            dropped.append(tail[cut.start():].strip(" ,;"))
        token = (rel.group("abbr") or rel.group("word") or rel.group("hindi")).lower()
        relation = f"{_MARKER[token]} {who}".strip() if who else ""
        s = s[:rel.start()]

    cut = _CLAUSE.search(s)
    if cut and cut.start() > 0:
        dropped.append(s[cut.start():].strip(" ,;"))
        s = s[:cut.start()]

    name = _tidy(s)
    if not name:                     # nothing recognisable was left — keep what was given
        name = _tidy(original)
        flags.append("could not separate the name from the surrounding text")
    if re.search(r"\d", name):
        flags.append("contains digits")
    if _EMAILISH.search(name.replace(" ", "")) and " " not in name.strip():
        flags.append("looks like an email or username, not a name")
    if _NON_LATIN.search(name):
        flags.append("not in English script")
    if len(name.split()) > 6 and not _FIRM.search(name):
        flags.append("unusually long for a name")
    if _SEVERAL.search(name) and not _FIRM.search(name):
        # never split — "BOTREE AND CO." is one firm — but two people in one field need a person
        flags.append("may name more than one person")
    return CleanName(name=name, relation=relation, dropped="; ".join(d for d in dropped if d),
                     flags=flags, original=original)


def clean_address(value: str) -> CleanName:
    """Addresses keep their commas; only a leading label is removed."""
    original = str(value or "")
    s = re.sub(r"\s+", " ", original).strip()
    s = re.sub(r"^\s*(?:address|addr|add|residential address|permanent address)\s*[:\-–]\s*", "", s, flags=re.I)
    s = re.sub(r"^\s*(?:r\s*/\s*o|resident of|residing at)\s*[:\-–]?\s*", "", s, flags=re.I)
    s = _tidy(s)
    flags = ["not in English script"] if _NON_LATIN.search(s) else []
    return CleanName(name=s or _tidy(original), flags=flags, original=original)


# ── the same person? ─────────────────────────────────────────────────────────
def person_key(name: str) -> str:
    """Letters only, lowercase, honorifics removed: the comparable core of a name."""
    s = _HONORIFIC.sub("", str(name or ""))
    return re.sub(r"[^a-z]", "", s.lower())


def same_person(a: str, b: str) -> bool:
    """Two reads of one person — allowing for spacing, case, a dropped surname or a one-letter
    misread, but not for two different people who share a surname."""
    ka, kb = person_key(a), person_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = sorted((ka, kb), key=len)
    if len(short) >= 5 and long_.startswith(short) and len(short) / len(long_) >= 0.6:
        return True                        # "Bharat Lal" vs "Bharat Lal Beer"
    ta = sorted(re.findall(r"[a-z]+", str(a).lower()))
    tb = sorted(re.findall(r"[a-z]+", str(b).lower()))
    if ta and ta == tb:
        return True                        # the same words in another order
    return len(short) >= 6 and difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.9
