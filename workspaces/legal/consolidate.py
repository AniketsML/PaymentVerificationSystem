"""
From many page reads to one set of dossier values.

A dossier is read in batches of up to 20 pages, and each batch numbers the people it finds from
one: batch A's "co_borrower_1" and batch C's "co_borrower_1" are routinely different people.
Values used to be merged by key, first one wins — so everyone else was silently dropped. Measured
on real runs: 77 of 204 dossiers lost someone, 337 people in all.

This module merges PEOPLE, not keys:

  1. every name read on every page becomes a read with a role (borrower / co-borrower /
     guarantor), grouped into one person when the names are the same person (name_clean.same_person)
  2. the borrower is the person most often read as the borrower — reads from the document the
     prompt names for it count triple. Anyone else read as a borrower is kept, as a co-borrower,
     with the disagreement recorded: nobody is dropped
  3. everyone gets one stable number, and every page record is rewritten to use it, so the page
     panels in the drawer, the dossier values and the dashboard all agree about who is who

Single-valued fields (a date, an amount, the property description) are merged by agreement: the
value read most often wins, and a genuine disagreement between pages is recorded as a conflict
for review rather than resolved by whichever batch happened to run first.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from workspaces.legal.name_clean import same_person

_PARTY_KEY = re.compile(r"^(borrower|co_borrower_\d+|guarantor_\d+)_(name|address)$")
# per-field notes that travel with a field when it is renumbered
_PER_FIELD = ("field_scripts", "field_notes", "field_evidence", "field_sources")
# documents whose job is to say who the parties to the loan are — as opposed to title deeds and
# legal reports, which name sellers, previous owners and witnesses the model also labels as
# "co-borrowers". The prompt's own binding (e.g. "from the loan agreement") is added to this.
_PARTY_DOCS = frozenset(("loan_agreement", "sanction_letter", "foreclosure_notice"))


def _role(prefix: str) -> str:
    return "borrower" if prefix == "borrower" else prefix.rsplit("_", 1)[0]


def _norm_value(v: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


@dataclass
class _Read:
    role: str
    prefix: str             # the key it had on its page: "co_borrower_1"
    name: str
    address: str
    rec: int
    page: int
    weight: int
    solo: bool = False      # the page names no one else — a KYC form calls its one person the
                            # "applicant", which says nothing about who the borrower of the loan is


@dataclass
class _Person:
    names: Counter = field(default_factory=Counter)
    addresses: Counter = field(default_factory=Counter)
    roles: Counter = field(default_factory=Counter)       # from pages that name several parties
    solo_roles: Counter = field(default_factory=Counter)  # from pages that name only this person
    first: int = 10 ** 9
    pages: set = field(default_factory=set)
    recs: set = field(default_factory=set)
    authoritative: bool = False      # read in KYC or a document whose job is to name the parties

    def add(self, r: _Read, authoritative: bool = False) -> None:
        if r.name:
            self.names[r.name] += r.weight
        if r.address:
            self.addresses[r.address] += r.weight
        (self.solo_roles if r.solo else self.roles)[r.role] += r.weight
        self.first = min(self.first, r.rec)
        self.recs.add(r.rec)
        self.authoritative = self.authoritative or authoritative
        if r.page:
            self.pages.add(r.page)

    def matches(self, name: str) -> bool:
        return any(same_person(name, n) for n in self.names)

    @staticmethod
    def _best(c: Counter) -> str:
        return max(c.items(), key=lambda kv: (kv[1], len(kv[0])))[0] if c else ""

    @property
    def name(self) -> str:
        return self._best(self.names)

    @property
    def address(self) -> str:
        return self._best(self.addresses)


@dataclass
class Consolidated:
    values: Dict[str, Any] = field(default_factory=dict)
    notes: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # per field: reads, pages, conflicts
    extras: Dict[str, Any] = field(default_factory=dict)             # reads that belong to no one
    mentioned: List[Dict[str, Any]] = field(default_factory=list)    # people named, but not parties


def _weight_fn(schema, doc_type_of: Callable[[dict], str]) -> Callable[[dict, str], int]:
    """Reads from a document the prompt names for a field count triple."""
    def weight(rec: dict, key: str) -> int:
        spec = schema.spec(key) if schema is not None else None
        bound = list(getattr(spec, "doc_types", []) or [])
        if not bound and _PARTY_KEY.match(key) and schema is not None:
            b = schema.spec("borrower_name")          # the prompt binds parties as one group
            bound = list(getattr(b, "doc_types", []) or [])
        return 3 if bound and doc_type_of(rec) in bound else 1
    return weight


def consolidate(records: List[dict], schema=None,
                doc_type_of: Optional[Callable[[dict], str]] = None) -> Consolidated:
    """Resolve the people across page records (renumbering the records in place) and merge the
    single-valued fields. Values are never altered — only chosen between and filed."""
    doc_type_of = doc_type_of or (lambda rec: "")
    weight = _weight_fn(schema, doc_type_of)
    out = Consolidated()
    party_docs = set(_PARTY_DOCS)
    b_spec = schema.spec("borrower_name") if schema is not None else None
    party_docs.update(getattr(b_spec, "doc_types", []) or [])

    def authoritative(rec: dict) -> bool:
        return doc_type_of(rec) in party_docs or "kyc" in str(rec.get("filename", "")).lower()

    # 1. every party read on every page
    reads: List[_Read] = []
    orphan_addresses: List[Tuple[int, str, str]] = []
    for i, rec in enumerate(records):
        f = rec.get("fields") or {}
        prefixes = {m.group(1) for k in f for m in [_PARTY_KEY.match(k)] if m}
        named = [p for p in prefixes if str(f.get(f"{p}_name") or "").strip()]
        for prefix in sorted(prefixes):
            name = str(f.get(f"{prefix}_name") or "").strip()
            addr = str(f.get(f"{prefix}_address") or "").strip()
            if not name:
                if addr:
                    orphan_addresses.append((i, prefix, addr))
                continue
            reads.append(_Read(_role(prefix), prefix, name, addr, i,
                               int(rec.get("page_number") or 0), weight(rec, f"{prefix}_name"),
                               solo=len(named) == 1))

    # 2. group reads into people
    people: List[_Person] = []
    read_person: Dict[int, _Person] = {}
    for idx, r in enumerate(reads):
        p = next((p for p in people if p.matches(r.name)), None)
        if p is None:
            p = _Person()
            people.append(p)
        p.add(r, authoritative(records[r.rec]))
        read_person[idx] = p

    # 3. roles: one borrower, everyone else numbered once. Pages that name several parties decide
    # who the borrower is; single-party pages (KYC forms) only decide it when nothing else does.
    as_borrower = [p for p in people if p.roles["borrower"]]
    if not as_borrower:
        for p in people:
            p.roles.update(p.solo_roles)
        as_borrower = [p for p in people if p.roles["borrower"]]
    else:
        for p in people:                       # still count them — just not as a role claim
            for role, w in p.solo_roles.items():
                p.roles[role if role != "borrower" else "co_borrower"] += w
    borrower = max(as_borrower, key=lambda p: (p.roles["borrower"], sum(p.roles.values()), -p.first)) \
        if as_borrower else None

    # Who is a party to the loan? Someone named on a page alongside the borrower, in KYC or a
    # loan-type document, or on two different pages. A name read once in a title deed — a seller,
    # a previous owner, a witness — is recorded as mentioned, not added as a co-borrower.
    def is_party(p: _Person) -> bool:
        if borrower is None or p is borrower:
            return True
        return p.authoritative or bool(p.recs & borrower.recs) or len(p.pages) >= 2

    non_parties = [p for p in people if not is_party(p)]
    for p in non_parties:
        role = max((p.roles + p.solo_roles).items(), key=lambda kv: kv[1])[0] if (p.roles + p.solo_roles) else ""
        out.mentioned.append({"name": p.name, "pages": sorted(p.pages), "read_as": role.replace("_", "-")})
    rest = [p for p in people if p is not borrower and p not in non_parties]
    co = sorted([p for p in rest if p.roles["co_borrower"] or p.roles["borrower"]
                 or not p.roles["guarantor"]], key=lambda p: p.first)
    guarantors = sorted([p for p in rest if p not in co], key=lambda p: p.first)
    key_of: Dict[int, str] = {}
    if borrower is not None:
        key_of[id(borrower)] = "borrower"
    for n, p in enumerate(co, 1):
        key_of[id(p)] = f"co_borrower_{n}"
    for n, p in enumerate(guarantors, 1):
        key_of[id(p)] = f"guarantor_{n}"

    # a borrower's address read on a page that did not repeat the name still belongs to them
    if borrower is not None:
        for i, prefix, addr in orphan_addresses:
            if prefix == "borrower":
                borrower.addresses[addr] += 1

    # 4. rewrite every record's party keys to the stable numbering
    mapping: Dict[int, Dict[str, str]] = {}
    _MENTIONED = "__mentioned__"            # a non-party: off the page's values, onto `mentioned`
    for idx, r in enumerate(reads):
        mapping.setdefault(r.rec, {})[r.prefix] = key_of.get(id(read_person[idx]), _MENTIONED)
    for i, rec in enumerate(records):
        f = rec.get("fields") or {}
        if not any(_PARTY_KEY.match(k) for k in f):
            continue
        local = mapping.get(i, {})
        new_fields: Dict[str, Any] = {k: v for k, v in f.items() if not _PARTY_KEY.match(k)}
        moved: Dict[str, str] = {}
        for k, v in f.items():
            m = _PARTY_KEY.match(k)
            if not m:
                continue
            target_prefix = local.get(m.group(1))
            if target_prefix == _MENTIONED:
                continue
            if target_prefix is None and m.group(1) == "borrower" and borrower is not None:
                target_prefix = "borrower"
            if target_prefix is None:
                # an address with no name on its page can't be tied to a person — kept, not guessed
                out.extras[f"{k} (p. {rec.get('page_number')})"] = v
                continue
            target = f"{target_prefix}_{m.group(2)}"
            if target not in new_fields or len(str(v)) > len(str(new_fields[target])):
                new_fields[target] = v
                moved[k] = target
        rec["fields"] = new_fields
        for part in _PER_FIELD:
            notes = rec.get(part)
            if isinstance(notes, dict) and notes:
                rec[part] = {moved.get(k, k): v for k, v in notes.items()
                             if not _PARTY_KEY.match(k) or k in moved}

    # 5. the dossier's party values, and what was learned on the way
    def put(key: str, value: str, person: _Person, role_note: str = "") -> None:
        if not value or (schema is not None and not schema.is_empty() and not schema.allows(key)):
            return
        out.values[key] = value
        note: Dict[str, Any] = {"reads": sum(person.names.values()) if key.endswith("_name")
                                else sum(person.addresses.values()),
                                "pages": sorted(person.pages)}
        variants = [n for n in person.names if n != person.name] if key.endswith("_name") else []
        if variants:
            note["variants"] = variants[:5]
        if role_note:
            note["conflict"] = role_note
        out.notes[key] = note

    for p in people:
        prefix = key_of.get(id(p))
        if prefix is None:
            continue                         # mentioned, not a party
        role_note = ""
        if p is not borrower and p.roles["borrower"]:
            role_note = (f"also read as the borrower on page(s) {', '.join(map(str, sorted(p.pages)))}; "
                         f"{borrower.name if borrower else 'another person'} was read as the borrower more often")
        if p is borrower:
            # only other PARTIES contradict the choice; a seller in a deed called "borrower" does not
            others = [o.name for o in as_borrower if o is not borrower and o not in non_parties]
            if others:
                role_note = f"other parties were also read as the borrower: {', '.join(others[:3])}"
        put(f"{prefix}_name", p.name, p, role_note)
        put(f"{prefix}_address", p.address, p)

    # 6. single-valued fields: agreement wins, disagreement is recorded
    reads_by_key: Dict[str, List[Tuple[str, int, int]]] = {}
    for rec in records:
        for k, v in (rec.get("fields") or {}).items():
            if _PARTY_KEY.match(k) or str(k).startswith("_") or v in (None, "", "—"):
                continue
            reads_by_key.setdefault(k, []).append((str(v).strip() if isinstance(v, str) else v,
                                                   int(rec.get("page_number") or 0), weight(rec, k)))
    for k, rs in reads_by_key.items():
        groups: Dict[str, List[Tuple[Any, int, int]]] = {}
        for v, page, w in rs:
            groups.setdefault(_norm_value(v) or str(v), []).append((v, page, w))
        ranked = sorted(groups.values(), key=lambda g: (sum(w for _, _, w in g),
                                                        max(len(str(v)) for v, _, _ in g)), reverse=True)
        best = ranked[0]
        value = max((v for v, _, _ in best), key=lambda v: len(str(v)))
        out.values[k] = value
        note: Dict[str, Any] = {"reads": len(rs), "pages": sorted({p for _, p, _ in rs if p})}
        # a second reading that is not just a fuller or shorter form of the first is a conflict
        rivals = [g for g in ranked[1:]
                  if not (_norm_value(g[0][0]) in _norm_value(value) or _norm_value(value) in _norm_value(g[0][0]))]
        if rivals:
            note["conflict"] = "; ".join(f"{str(g[0][0])[:60]} (p. {', '.join(str(p) for _, p, _ in g if p)})"
                                         for g in rivals[:3])
        out.notes[k] = note
    return out


def distinct_people(records: List[dict]) -> int:
    """How many different people the records name — for tests and audits."""
    seen: List[str] = []
    for rec in records:
        for k, v in (rec.get("fields") or {}).items():
            if k.endswith("_name") and _PARTY_KEY.match(k) and v:
                if not any(same_person(v, s) for s in seen):
                    seen.append(v)
    return len(seen)
