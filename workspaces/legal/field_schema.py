"""
The fields a run is allowed to produce.

Before this module the model was handed the user's prompt and left to name its own keys. It
invented them per call: one run asking for about eight things produced 108 distinct fields, and
the plot's pincode arrived as `pincode`, `property_pincode`, `mortgaged_property_pincode` and
`mortgaged_property_details_pincode`. Every invented name became a dashboard column.

Now each run gets a closed schema, derived once from its prompt:

  - one canonical vocabulary (the one the stored data and the dashboard already use:
    borrower_name, co_borrower_1_name, …), with every alias the model or the prompt analyzer is
    known to use folded onto it
  - repeating groups (co-borrowers, guarantors) declared as families, because a prompt says
    "co-borrower" once while a dossier has anywhere from one to six of them
  - fields the user asks for that have no canonical name become custom fields with a stable slug

The schema is sent to the model as an explicit key list, and anything returned outside it is
quarantined in `_extras` — kept as evidence in the drawer, never promoted to a column.

The schema depends only on the prompt, and is cached per prompt in `legal_run_schemas`, so every
dossier of a run — and a re-run months later — gets exactly the same columns.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

BUILDER_VERSION = "2"        # bump when the mapping rules change, so cached schemas recompute


# ── the canonical vocabulary ─────────────────────────────────────────────────
@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: str                  # name | address | text | amount | date | rate | id
    group: str                 # borrower | parties | property | loan | dates | custom
    hint: str = ""             # one line for the model


REGISTRY: Dict[str, FieldSpec] = {s.key: s for s in (
    FieldSpec("borrower_name", "Borrower name", "name", "borrower",
              "the primary borrower's name only — one person or one firm"),
    FieldSpec("borrower_address", "Borrower address", "address", "borrower",
              "the primary borrower's address"),
    FieldSpec("property_owner", "Property owner / mortgagor", "name", "parties",
              "the owner or mortgagor of the property, if different from the borrower"),
    FieldSpec("account_no_lan", "Loan account number", "id", "loan",
              "the loan account number (LAN)"),

    FieldSpec("property_details", "Property details", "text", "property",
              "the full description of the mortgaged property as written in the schedule"),
    FieldSpec("property_address", "Property address", "address", "property",
              "the address or location of the mortgaged property"),
    FieldSpec("property_plot_no", "Plot no.", "id", "property", "plot number"),
    FieldSpec("property_survey_no", "Survey / khasra no.", "id", "property",
              "survey, khasra, khata or CTS number"),
    FieldSpec("property_building_no", "Building / house no.", "id", "property",
              "building, house, door or flat number"),
    FieldSpec("property_area", "Plot / land area", "text", "property",
              "the plot or land area, with its unit"),
    FieldSpec("property_built_up_area", "Built-up area", "text", "property",
              "built-up, carpet or saleable area, with its unit"),
    FieldSpec("property_pincode", "Property pincode", "id", "property",
              "the 6-digit pincode of the property"),
    FieldSpec("property_boundaries", "Boundaries", "text", "property",
              "the property's boundaries (north, south, east, west)"),

    FieldSpec("sanction_amount", "Sanction amount", "amount", "loan", "sanctioned loan amount"),
    FieldSpec("sanction_amount_in_words", "Sanction amount (words)", "text", "loan",
              "sanctioned amount written in words"),
    FieldSpec("roi_in_number", "Rate of interest", "rate", "loan", "rate of interest, as a number"),
    FieldSpec("emi_amount", "EMI", "amount", "loan", "monthly instalment amount"),
    FieldSpec("loan_tenure", "Tenure", "text", "loan", "loan tenure with its unit"),
    FieldSpec("tos", "Total outstanding", "amount", "loan", "total outstanding / total dues"),
    FieldSpec("future_principal", "Future principal", "amount", "loan", ""),
    FieldSpec("principal_overdue", "Principal overdue", "amount", "loan", ""),
    FieldSpec("interest_overdue", "Interest overdue", "amount", "loan", ""),
    FieldSpec("interest_on_termination", "Interest on termination", "amount", "loan", ""),
    FieldSpec("late_payment_penal", "Late payment / penal charges", "amount", "loan", ""),
    FieldSpec("cheque_bounce_inc_gst", "Cheque bounce charges", "amount", "loan", ""),
    FieldSpec("other_charges_inc_gst", "Other charges", "amount", "loan", ""),
    FieldSpec("foreclosure_charges", "Foreclosure charges", "amount", "loan", ""),
    FieldSpec("litigation_charges", "Litigation charges", "amount", "loan", ""),
    FieldSpec("excess_amount", "Excess amount", "amount", "loan", ""),

    FieldSpec("sanction_date", "Sanction date", "date", "dates", "date of sanction"),
    FieldSpec("disbursal_date", "Disbursal date", "date", "dates", "date of disbursement"),
    FieldSpec("npa_date", "NPA date", "date", "dates", "date the account was classified NPA"),
)}

# repeating groups: co_borrower_1_name, co_borrower_2_address, guarantor_1_name, …
FAMILIES: Dict[str, Tuple[str, str]] = {
    "co_borrower": ("Co-borrower", "every co-borrower / co-applicant"),
    "guarantor": ("Guarantor", "every guarantor"),
}
FAMILY_ATTRS = ("name", "address")
FAMILY_RE = re.compile(r"^(co_borrower|guarantor)_(\d+)_(name|address)$")

# Who the dossier is, rather than something the prompt asked for: always kept, never a column of
# its own (the dashboard shows the LAN inside the Dossier column), never listed to the model.
IDENTITY_KEYS = frozenset(("account_no_lan",))

_COMPASS = {"north": "North", "south": "South", "east": "East", "west": "West",
            "n": "North", "s": "South", "e": "East", "w": "West"}


# ── phrase normalisation, shared by keys and prompts ─────────────────────────
_UNIT_TOKENS = {"sqft", "sq", "ft", "feet", "sqm", "sqmt", "mtr", "meter", "metre", "meters",
                "yards", "yd", "sqyd", "acre", "acres", "cents", "guntha", "gunta", "hectare"}


def _phrase(text: Any) -> str:
    """Lowercase words, one space apart, with the spellings people and models use folded:
    'Co-Applicant No. 2' -> 'co borrower no 2', "borrower's names" -> 'borrower name'."""
    s = str(text or "").lower().replace("&", " and ")
    s = re.sub(r"(?<![a-z])co[\s_\-]*(borrower|applicant)s?", r"co borrower", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = []
    for w in s.split():
        if w in ("number", "num", "nos"):
            w = "no"
        elif w in ("borrowers", "applicants", "applicant"):
            w = "borrower"
        elif w in ("names",):
            w = "name"
        elif w in ("addresses", "add", "addr"):
            w = "address"
        elif w in ("guarantors",):
            w = "guarantor"
        elif w == "s":                          # the possessive of "borrower's"
            continue
        words.append(w)
    return " ".join(words)


def slug(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _phrase(text)).strip("_")[:48]


# aliases -> canonical key; phrases are compared after _phrase()
_ALIASES: Dict[str, Iterable[str]] = {
    "borrower_name": ("borrower name", "borrower", "primary borrower", "primary borrower name",
                      "name of borrower", "name of the borrower", "customer name", "main borrower",
                      "borrower primary name", "applicant name", "primary applicant"),
    "borrower_address": ("borrower address", "address of borrower", "address of the borrower",
                         "primary borrower address", "customer address", "applicant address"),
    "property_owner": ("property owner", "property owner mortgagor", "mortgagor", "owner",
                       "owner name", "mortgagor name"),
    "account_no_lan": ("lan", "loan account no", "loan account", "account no", "account no lan",
                       "loan account no lan", "loan no", "loan account number lan"),
    "property_details": ("property details", "property detail", "property description",
                         "mortgaged property details", "mortgaged property detail",
                         "mortgaged property detail 1", "mortgaged property", "schedule property",
                         "property", "collateral details", "security details", "property schedule"),
    "property_address": ("property address", "address of property", "address of the property",
                         "property location", "location of property", "mortgaged property address"),
    "property_plot_no": ("plot no", "plot", "property plot no"),
    "property_survey_no": ("survey no", "khasra no", "khata no", "cts no", "survey"),
    "property_building_no": ("building no", "house no", "door no", "flat no", "building", "house"),
    "property_area": ("area", "plot area", "area of the plot", "area of plot", "land area",
                      "extent", "site area", "property area"),
    "property_built_up_area": ("built up area", "builtup area", "carpet area", "salable area",
                               "saleable area", "super built up area", "building area",
                               "construction area"),
    "property_pincode": ("pincode", "pin code", "pin", "postal code", "property pincode"),
    "property_boundaries": ("boundaries", "boundary", "directions", "four boundaries",
                            "compass", "property boundaries"),
    "sanction_amount": ("sanction amount", "sanctioned amount", "loan amount", "sanctioned loan amount"),
    "sanction_amount_in_words": ("sanction amount in words", "amount in words"),
    "roi_in_number": ("roi", "rate of interest", "interest rate", "roi in number"),
    "emi_amount": ("emi", "emi amount", "monthly instalment", "installment", "instalment"),
    "loan_tenure": ("tenure", "loan tenure", "loan term", "term"),
    "tos": ("tos", "total outstanding", "total dues", "total outstanding amount", "outstanding"),
    "future_principal": ("future principal",),
    "principal_overdue": ("principal overdue",),
    "interest_overdue": ("interest overdue",),
    "interest_on_termination": ("interest on termination",),
    "late_payment_penal": ("late payment", "penal charges", "late payment penal", "penal interest"),
    "cheque_bounce_inc_gst": ("cheque bounce", "cheque bounce charges", "cheque bounce inc gst"),
    "other_charges_inc_gst": ("other charges", "other charges inc gst"),
    "foreclosure_charges": ("foreclosure charges",),
    "litigation_charges": ("litigation charges",),
    "excess_amount": ("excess amount",),
    "sanction_date": ("sanction date", "date of sanction"),
    "disbursal_date": ("disbursal date", "disbursement date", "date of disbursal", "date of disbursement"),
    "npa_date": ("npa date", "classification date", "default date", "date of npa"),
}
_ALIAS_INDEX: Dict[str, str] = {}
for _key, _names in _ALIASES.items():
    for _n in list(_names) + [_key]:
        _ALIAS_INDEX.setdefault(_phrase(_n), _key)

# what the model or the analyzer puts in front of a property attribute
_PROPERTY_PREFIXES = ("mortgaged property details", "mortgaged property detail", "mortgaged property",
                      "property details", "property detail", "property")
# the analyzer's legacy names that are not plain aliases
_LEGACY = {
    "applicant_name": "borrower_name", "applicant_address": "borrower_address",
    "property_owner_mortgagor": "property_owner", "mortgaged_property_detail_1": "property_details",
    "directions": "property_boundaries",
}


def _compass_side(raw: Any) -> str:
    """'North' for 'boundaries_north' / 'property boundary n' / 'direction north', else ''."""
    words = _phrase(raw).split()
    if len(words) >= 2 and words[-1] in _COMPASS and any(
            w.startswith(("boundar", "direction")) for w in words[:-1]):
        return _COMPASS[words[-1]]
    return ""


def canonical_key(raw: Any) -> Optional[str]:
    """The canonical key for a field name the model, the analyzer or a user wrote — or None when
    it has no canonical meaning. Family members come back numbered: 'co_borrower_2_address'."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text in REGISTRY:
        return text
    if text in _LEGACY:
        return _LEGACY[text]
    p = _phrase(text)
    if not p:
        return None

    # families, in the orders they are written: "co borrower 2 name", "co borrower name 2",
    # "co borrower address 1" (the analyzer), "guarantor 1 add", and the bare "co borrower 3"
    m = (re.fullmatch(r"(co borrower|guarantor)(?: no)? (\d+)(?: (name|address))?", p)
         or re.fullmatch(r"(co borrower|guarantor) (name|address)(?: no)? (\d+)", p))
    if m:
        fam = "co_borrower" if m.group(1) == "co borrower" else "guarantor"
        if m.group(2).isdigit():
            n, attr = m.group(2), (m.group(3) or "name")
        else:
            attr, n = m.group(2), m.group(3)
        return f"{fam}_{int(n)}_{attr}" if int(n) > 0 else None

    if p in _ALIAS_INDEX:
        return _ALIAS_INDEX[p]

    # "boundaries_east", "direction north", "property boundaries west" — one side of one field
    if _compass_side(text):
        return "property_boundaries"

    # "mortgaged property details plot no", "property details land area sqft", …
    for prefix in _PROPERTY_PREFIXES:
        if p.startswith(prefix + " "):
            rest = " ".join(w for w in p[len(prefix) + 1:].split() if w not in _UNIT_TOKENS)
            rest = re.sub(r" \d+$", "", rest)          # "... detail 2" style suffixes
            if not rest:
                return "property_details"
            hit = _ALIAS_INDEX.get(rest) or _ALIAS_INDEX.get(f"property {rest}")
            if hit and REGISTRY.get(hit) and REGISTRY[hit].group == "property":
                return hit
            if rest in ("address", "location", "locality"):
                return "property_address"
            if rest in ("details", "detail", "description"):
                return "property_details"
            return None
    return None


# ── a run's schema ───────────────────────────────────────────────────────────
@dataclass
class SchemaField:
    key: str
    label: str
    kind: str
    group: str
    hint: str = ""
    doc_types: List[str] = field(default_factory=list)   # [] = anywhere


@dataclass
class RunSchema:
    fields: List[SchemaField] = field(default_factory=list)
    families: Dict[str, List[str]] = field(default_factory=dict)   # family -> attrs
    source: str = ""
    version: str = BUILDER_VERSION

    # -- membership ------------------------------------------------------------
    @property
    def keys(self) -> List[str]:
        return [f.key for f in self.fields]

    def allows(self, key: str) -> bool:
        if key in self.keys:
            return True
        m = FAMILY_RE.match(key or "")
        return bool(m) and m.group(3) in self.families.get(m.group(1), [])

    def spec(self, key: str) -> Optional[SchemaField]:
        for f in self.fields:
            if f.key == key:
                return f
        m = FAMILY_RE.match(key or "")
        if m and m.group(3) in self.families.get(m.group(1), []):
            label = FAMILIES[m.group(1)][0]
            return SchemaField(key, f"{label} {m.group(2)} {m.group(3)}",
                               "name" if m.group(3) == "name" else "address", "parties")
        return None

    def kind(self, key: str) -> str:
        s = self.spec(key)
        return s.kind if s else "text"

    def is_empty(self) -> bool:
        return not self.fields and not self.families

    # -- ingest -----------------------------------------------------------------
    def canonical(self, raw_key: str) -> Optional[str]:
        """The schema key a returned field belongs to, or None to quarantine it."""
        ck = canonical_key(raw_key)
        if ck and self.allows(ck):
            return ck
        s = slug(raw_key)
        if s and s in self.keys:                    # a custom field the user asked for
            return s
        return None

    # -- the model's instructions -----------------------------------------------
    def prompt_block(self) -> str:
        lines = ["Return ONLY the keys below. Omit a key you cannot find; never invent other keys."]
        for f in self.fields:
            where = f" (the user wants this from: {', '.join(d.replace('_', ' ') for d in f.doc_types)})" \
                if f.doc_types else ""
            lines.append(f"- {f.key}: {f.hint or f.label}{where}")
        for fam, attrs in self.families.items():
            label, hint = FAMILIES[fam]
            for attr in attrs:
                lines.append(f"- {fam}_1_{attr}, {fam}_2_{attr}, {fam}_3_{attr}, …: the {attr} of "
                             f"{hint}, one numbered key per person, starting at 1"
                             + ("; the same number as their name" if attr == "address" else ""))
        return "\n".join(lines)

    # -- persistence ------------------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        return {"version": self.version, "source": self.source,
                "fields": [asdict(f) for f in self.fields], "families": self.families}

    @classmethod
    def from_json(cls, data: Any) -> Optional["RunSchema"]:
        if not isinstance(data, dict) or "fields" not in data:
            return None
        return cls(fields=[SchemaField(**f) for f in data.get("fields") or []],
                   families={k: list(v) for k, v in (data.get("families") or {}).items()},
                   source=data.get("source", ""), version=str(data.get("version", "")))


class _Builder:
    def __init__(self):
        self.schema = RunSchema()

    def add(self, key: str, doc_type: str = "", label: str = "", hint: str = ""):
        m = FAMILY_RE.match(key)
        if m:
            attrs = self.schema.families.setdefault(m.group(1), [])
            if m.group(3) not in attrs:
                attrs.append(m.group(3))
            return
        existing = self.schema.spec(key)
        if existing is None:
            spec = REGISTRY.get(key)
            existing = SchemaField(key, spec.label if spec else (label or key.replace("_", " ").capitalize()),
                                   spec.kind if spec else "text", spec.group if spec else "custom",
                                   spec.hint if spec else hint)
            self.schema.fields.append(existing)
        if doc_type and doc_type not in existing.doc_types:
            existing.doc_types.append(doc_type)

    def add_family(self, fam: str, attr: str):
        self.add(f"{fam}_1_{attr}")


# the words in a prompt that name a party, and the sentence-level attributes that go with them
_PARTY_WORDS = (("co_borrower", r"\bco borrower\b"),
                ("borrower", r"(?<!co )\bborrower\b"),
                ("guarantor", r"\bguarantor\b"))


def _keyword_fields(prompt: str, b: _Builder) -> None:
    """A deterministic reading of the prompt — the floor under the analyzer, so a failed or
    flaky analyzer call can never leave a run without its basic columns."""
    # a sentence ends at a newline, a semicolon, or a full stop before a capital — never at the
    # abbreviation dots in "building no., plot no., pincode", which would strip the later
    # attributes of their "property" context
    for sentence in re.split(r"[\n;]+|\.\s+(?=[A-Z])", prompt or ""):
        p = _phrase(sentence)
        if not p:
            continue
        parties = [name for name, rx in _PARTY_WORDS if re.search(rx, p)]
        wants_name = bool(re.search(r"\bname\b", p))
        wants_addr = bool(re.search(r"\baddress\b", p))
        for party in parties:
            attrs = [a for a, on in (("name", wants_name or not wants_addr), ("address", wants_addr)) if on]
            for attr in attrs:
                if party == "borrower":
                    b.add(f"borrower_{attr}")
                else:
                    b.add_family(party, attr)

        if re.search(r"\b(property|collateral|mortgaged)\b", p):
            subs = []
            for phrase, key in (("plot no", "property_plot_no"), ("plot area", "property_area"),
                                ("area of the plot", "property_area"), ("area of plot", "property_area"),
                                ("land area", "property_area"), ("built up area", "property_built_up_area"),
                                ("carpet area", "property_built_up_area"), ("building no", "property_building_no"),
                                ("house no", "property_building_no"), ("survey no", "property_survey_no"),
                                ("khasra", "property_survey_no"), ("pincode", "property_pincode"),
                                ("pin code", "property_pincode"), ("boundar", "property_boundaries"),
                                ("property address", "property_address")):
                if re.search(rf"\b{phrase}", p):
                    subs.append(key)
            if re.search(r"\barea\b", p) and "property_area" not in subs and "property_built_up_area" not in subs:
                subs.append("property_area")
            if re.search(r"\b(detail|details|description|schedule)\b", p) or not subs:
                b.add("property_details")
            for key in subs:
                b.add(key)

        for phrase, key in (("sanction amount", "sanction_amount"), ("sanctioned amount", "sanction_amount"),
                            ("loan amount", "sanction_amount"), ("rate of interest", "roi_in_number"),
                            ("roi", "roi_in_number"), ("sanction date", "sanction_date"),
                            ("disbursal date", "disbursal_date"), ("disbursement date", "disbursal_date"),
                            ("npa", "npa_date"), ("total outstanding", "tos"), ("tos", "tos"),
                            ("total dues", "tos"), ("emi", "emi_amount"), ("tenure", "loan_tenure"),
                            ("loan account no", "account_no_lan"), ("lan", "account_no_lan")):
            if re.search(rf"\b{phrase}\b", p):       # whole words: "npa" is not inside "company"
                b.add(key)


# The analyzer sometimes turns an instruction into a "field" — "translate the extracted text into
# english" came back as translate_to_english. An instruction is never a column.
# Anchored where a real field could share the word: "reference_no" and "remarks" stay fields.
_INSTRUCTION = re.compile(r"(translat|english|vernacular|language|transliterat|instruction|if_any"
                          r"|if_mentioned|^extract_|^refer_|^output_|^convert_)")


def build_schema(prompt: str, plan: Any = None) -> RunSchema:
    """The closed schema for a prompt: the analyzer's reading (when there is one) on top of a
    deterministic keyword reading that always runs."""
    b = _Builder()
    sources = []
    for inst in getattr(plan, "instructions", None) or []:
        for f in getattr(inst, "fields", None) or []:
            ck = canonical_key(f)
            if ck:
                b.add(ck, getattr(inst, "doc_type", "") or "")
            elif _INSTRUCTION.search(slug(f)):
                continue
            elif str(f).strip():
                # a field the user asked for that has no canonical name: keep it, under one slug
                b.add(slug(f), getattr(inst, "doc_type", "") or "", label=str(f).strip().capitalize(),
                      hint=str(f).strip())
        sources.append("analyzer")
    _keyword_fields(prompt, b)
    sources.append("keywords")
    b.schema.source = "+".join(dict.fromkeys(sources))
    return b.schema


# ── cache: one schema per prompt, stable across dossiers and re-runs ─────────
def prompt_hash(prompt: str) -> str:
    return hashlib.sha1(f"{BUILDER_VERSION}\n{(prompt or '').strip()}".encode("utf-8")).hexdigest()


def schema_for_prompt(prompt: str, plan: Any = None) -> RunSchema:
    """The schema for this prompt, from the cache or built (and cached) now."""
    from db import pg
    from psycopg.types.json import Jsonb
    h = prompt_hash(prompt)
    try:
        with pg.pool().connection() as c:
            row = c.execute("SELECT schema FROM legal_run_schemas WHERE prompt_hash=%s", (h,)).fetchone()
        cached = RunSchema.from_json(row["schema"]) if row else None
        if cached and not cached.is_empty():
            return cached
    except Exception as e:  # noqa: BLE001 — a cache miss must never stop a run
        sys.stderr.write(f"[field_schema] cache read failed: {e}\n")

    if plan is None and (prompt or "").strip():
        try:
            from workspaces.legal.prompt_analyzer import analyze_extraction_prompt
            plan = analyze_extraction_prompt(prompt)
        except Exception as e:  # noqa: BLE001 — the keyword reading still stands
            sys.stderr.write(f"[field_schema] analyzer unavailable, keywords only: {e}\n")
    schema = build_schema(prompt, plan)
    if not schema.is_empty():
        try:
            with pg.pool().connection() as c:
                c.execute("INSERT INTO legal_run_schemas(prompt_hash, prompt, schema) VALUES (%s,%s,%s) "
                          "ON CONFLICT (prompt_hash) DO NOTHING", (h, prompt, Jsonb(schema.to_json())))
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[field_schema] cache write failed: {e}\n")
    return schema


# ── does the value look like what the field holds? ────────────────────────────
def validate(key: str, value: Any, schema: Optional[RunSchema] = None) -> List[str]:
    """Format checks that flag a read for review — never correct it. A building number that is
    a sentence ("wherein a school building is constructed by …") or a 5-digit pincode was misread
    or mis-filed, and a person should look before anyone relies on it."""
    if not isinstance(value, str) or not value.strip():
        return []
    v = value.strip()
    kind = schema.kind(key) if schema is not None else (REGISTRY[key].kind if key in REGISTRY else "text")
    words = len(v.split())
    flags: List[str] = []
    if key == "property_pincode":
        if not re.fullmatch(r"\d{6}", re.sub(r"[\s\-]", "", v)):
            flags.append("not a 6-digit pincode")
    elif kind == "id":
        if words > 6:
            flags.append("too long to be a number")
        elif not re.search(r"\d", v) and key != "account_no_lan":
            flags.append("has no number in it")
    elif kind in ("amount", "rate") and not re.search(r"\d", v):
        flags.append("has no figure in it")
    elif kind == "date" and not re.search(r"\d", v):
        flags.append("has no date in it")
    return flags


# ── ingest ───────────────────────────────────────────────────────────────────
def conform(values: Dict[str, Any], schema: Optional[RunSchema]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Split returned values into (kept under schema keys, quarantined extras).

    With no schema (an empty prompt, or a row from before schemas existed) everything is kept, so
    old behaviour is unchanged. Property sub-values the prompt did not ask for as columns are not
    lost when it DID ask for property details: they are folded into that one field."""
    if not schema or schema.is_empty():
        return dict(values), {}
    kept: Dict[str, Any] = {}
    extras: Dict[str, Any] = {}
    stray_property: List[Tuple[str, Any]] = []
    sides: List[Tuple[str, Any]] = []
    for k, v in values.items():
        if str(k).startswith("_"):
            continue
        if canonical_key(k) in IDENTITY_KEYS:
            kept[canonical_key(k)] = v
            continue
        ck = schema.canonical(k)
        if ck == "property_boundaries" and _compass_side(k):
            sides.append((_compass_side(k), v))       # assembled into one value below
            continue
        if ck:
            if ck not in kept or (isinstance(v, str) and len(v) > len(str(kept[ck]))):
                kept[ck] = v
            continue
        if _compass_side(k):
            stray_property.append((f"{_compass_side(k)} boundary", v))
            extras[k] = v
            continue
        raw_ck = canonical_key(k)
        if raw_ck and REGISTRY.get(raw_ck) and REGISTRY[raw_ck].group == "property":
            stray_property.append((REGISTRY[raw_ck].label, v))
        extras[k] = v
    if sides and "property_boundaries" not in kept:
        kept["property_boundaries"] = "; ".join(f"{side}: {v}" for side, v in sides if v)
    if "property_details" in schema.keys and "property_details" not in kept and stray_property:
        kept["property_details"] = "; ".join(f"{label}: {v}" for label, v in stray_property if v)
    return kept, extras
