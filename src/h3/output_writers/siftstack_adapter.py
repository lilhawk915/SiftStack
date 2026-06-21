"""H3 → SiftStack csv-import adapter.

Converts H3 CaseRecord objects into the 74-column SiftStack scraper-output
schema (SIFT_COLUMNS from ~/Desktop/SiftStack/src/data_formatter.py). This
format is the entry point for SiftStack's csv-import mode: it feeds the
records into the existing enrichment pipeline (Smarty → Zillow → obituary
search → skip trace) and ultimately uploads to DataSift via Playwright.

Applies the 3-rule folder routing precedence (see docs/routing_rules.md):

  1. DECEASED override   → notice_type = "probate"          (Probate list)
  2. TAX DELINQUENT      → notice_type = "tax_delinquent"   (Tax Delinquent list)
  3. DEFAULT             → notice_type = "foreclosure"      (Foreclosure list)

Multi-defendant handling:

  - Deceased + living heir  → 1 record (heir as Owner, decedent in Decedent Name)
  - Alive co-owners         → N records (1 per defendant, cross-referenced in Notes)
  - Multiple heirs          → N records (1 per heir, all tagged multi_heir)
  - All-deceased no heir    → 1 record, Data Flags = "needs_manual_review"

Enrichment fields (Smarty, Zillow, phones, emails, MLS, etc.) are left
empty — SiftStack's enrichment pipeline fills them after csv-import.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Sequence

from h3.output_writers.h3_format import CaseRecord, Defendant


# ── Target schema — exact match for SiftStack's SIFT_COLUMNS ────────────

SIFT_COLUMNS = [
    "full_name", "address", "city", "state", "zip",
    "first_name", "last_name",
    "Owner Street", "Owner City", "Owner State", "Owner ZIP Code",
    "Date Added",
    "notice_type", "county", "decedent_name", "auction_date",
    "zip_plus4", "latitude", "longitude", "dpv_match_code", "vacant", "rdi",
    "mls_status", "mls_listing_price", "mls_last_sold_date", "mls_last_sold_price",
    "estimated_value", "estimated_equity", "equity_percent",
    "property_type", "bedrooms", "bathrooms", "sqft", "year_built", "lot_size",
    "parcel_id", "tax_delinquent_amount", "tax_delinquent_years",
    "deceased_indicator", "tax_owner_name",
    "owner_deceased", "date_of_death", "obituary_url",
    "decision_maker_name", "decision_maker_relationship",
    "decision_maker_status", "decision_maker_source",
    "decision_maker_street", "decision_maker_city",
    "decision_maker_state", "decision_maker_zip",
    "decision_maker_2_name", "decision_maker_2_relationship", "decision_maker_2_status",
    "decision_maker_3_name", "decision_maker_3_relationship", "decision_maker_3_status",
    "obituary_source_type", "heir_search_depth",
    "heirs_verified_living", "heirs_verified_deceased", "heirs_unverified",
    "dm_confidence", "dm_confidence_reason", "missing_data_flags", "heir_map_json",
    "mailable",
    "entity_type", "entity_person_name", "entity_person_role",
    "entity_research_source", "entity_research_confidence",
    "source_url", "run_id",
]


# ── Defendant role detection ────────────────────────────────────────────

_DECEASED_RE = re.compile(r"\(deceased\)|\bdeceased\b\s*(?:defendant)?|DECEASED\s*$", re.I)

_HEIR_PATTERNS = [
    # Known-heir patterns (a living person stepped up to represent estate)
    r"\(Aff(?:idavit)?\s+of\s+Inheritance",
    r"ADMINISTRATOR\s+OF\s+THE\s+ESTATE",
    r"FIDUCIARY\s+OF\s+THE\s+ESTATE",
    r"Successor\s+Trustee",
    r"Personal\s+Representative",
    r"\(Survivorship\s+Deed\)",
    r"Trustee\s+of\s+(?:the\s+)?\w+",
]
_HEIR_RE = re.compile("|".join(_HEIR_PATTERNS), re.I)

# Unknown-heirs / placeholder-defendant patterns. These indicate the owner
# is deceased AND no heir has stepped up to represent the estate. Validated
# against real Montgomery foreclosure data (~5% of weekly volume).
# Examples:
#   "UNKNOWN HEIRS DEVISEES LEGATEES OR REPRESENTATIVES OF MICHAEL D. JOHNSON DECEASED"
#   "UNKNOWN HEIRS OF ANNA M WHEATLEY"
#   "JOHN DOE JACQUELINE MCJUNKINS"
#   "JANE DOE"
_UNKNOWN_HEIRS_PATTERNS = [
    r"UNKNOWN\s+HEIRS",                   # most common: "UNKNOWN HEIRS OF ..."
    r"UNKNOWN\s+(?:SPOUSE|EXECUTOR|ADMINISTRATOR)",
    r"UNNAMED\s+HEIRS",
    r"DEVISEES,?\s+(?:AND|OR)\s+LEGATEES",  # "DEVISEES OR LEGATEES OF ..."
    r"HEIRS\s+(?:AT\s+LAW|DEVISEES|LEGATEES)",
    r"\bJOHN\s+DOE\b",                    # placeholder defendant
    r"\bJANE\s+DOE\b",                    # placeholder defendant
    r"REPRESENTATIVES\s+OF\s+\w+\s+DECEASED",  # "REPRESENTATIVES OF [name] DECEASED"
]
_UNKNOWN_HEIRS_RE = re.compile("|".join(_UNKNOWN_HEIRS_PATTERNS), re.I)


def _is_deceased(name: str) -> bool:
    return bool(_DECEASED_RE.search(name or ""))


def _is_heir(name: str) -> bool:
    return bool(_HEIR_RE.search(name or ""))


def _is_unknown_heir(name: str) -> bool:
    """True when the defendant name is a court-placeholder for an unknown
    heir (e.g. 'UNKNOWN HEIRS OF X', 'JOHN DOE'). These cases mean the
    property owner is deceased AND no heir has been identified — they are
    high-value H3 leads because the property is in foreclosure, owner is
    deceased, and no one else is bidding.
    """
    return bool(_UNKNOWN_HEIRS_RE.search(name or ""))


def _strip_role_suffix(name: str) -> str:
    """Remove '(deceased)', '(Aff of Inheritance 2023)', role descriptors etc."""
    return re.sub(r"\s*\([^)]+\)\s*", " ", name or "").strip()


def _split_name(name: str) -> tuple[str, str]:
    """Heuristic first/last split. Returns (first_name, last_name)."""
    cleaned = _strip_role_suffix(name).strip().rstrip(",")
    if not cleaned:
        return "", ""
    if "," in cleaned:
        # "LAST, FIRST MIDDLE" — common in court records
        last, _, first = cleaned.partition(",")
        return first.strip().title(), last.strip().title()
    parts = cleaned.split()
    if len(parts) == 1:
        return "", parts[0].title()
    return " ".join(parts[:-1]).title(), parts[-1].title()


# ── Notes parsing (extract structured data from free-text) ──────────────

_MONEY_RE = re.compile(r"\$([0-9,]+(?:\.\d{2})?)")
_YEAR_RE = re.compile(
    r"(?:delinquent\s+since|in\s+default\s+since|certified\s+delinquent\s+on)"
    r"\b[^0-9]*?(\d{4})",
    re.I,
)


def _parse_tax_amount(notes: str) -> str:
    m = _MONEY_RE.search(notes or "")
    return m.group(1).replace(",", "") if m else ""


def _parse_delinquent_year(notes: str) -> str:
    m = _YEAR_RE.search(notes or "")
    return m.group(1) if m else ""


# ── Routing — the 3-rule precedence ────────────────────────────────────

def _is_tax_filing(filing_type: str) -> bool:
    return "delinquent real estate taxes" in (filing_type or "").lower()


def _route_to_notice_type(
    case: CaseRecord,
    has_deceased_or_heir: bool,
) -> str:
    """Apply the 3-rule precedence to pick the SiftStack notice_type.

    SiftStack maps notice_type → DataSift list:
      probate         → Probate
      tax_delinquent  → Tax Delinquent
      foreclosure     → Foreclosure
    """
    if has_deceased_or_heir:
        return "probate"           # Rule 1 — deceased wins (confirmed Ryan 2026-06-06)
    if _is_tax_filing(case.filing_type):
        return "tax_delinquent"    # Rule 2
    return "foreclosure"           # Rule 3


# ── Defendant-level helpers ─────────────────────────────────────────────

def _heir_relationship(heir_name: str) -> str:
    """Pull the role/relationship descriptor out of the parenthetical."""
    m = re.search(r"\(([^)]+)\)", heir_name or "")
    if not m:
        return "named successor"
    return m.group(1).strip().lower()


def _classify_defendants(defendants: list[Defendant]) -> dict:
    """Bucket defendants into deceased / heir / unknown-heir / living groups.

    Bucket precedence (most specific first):
      deceased         — explicit (deceased) marker
      unknown_heirs    — court placeholder (UNKNOWN HEIRS OF X, JOHN DOE)
      heirs            — known heir/administrator stepped up
      living           — alive owner / regular foreclosure defendant
    """
    deceased, unknown_heirs, heirs, living = [], [], [], []
    for d in defendants:
        if _is_deceased(d.name):
            deceased.append(d)
        elif _is_unknown_heir(d.name):
            unknown_heirs.append(d)
        elif _is_heir(d.name):
            heirs.append(d)
        else:
            living.append(d)
    return {
        "deceased": deceased,
        "unknown_heirs": unknown_heirs,
        "heirs": heirs,
        "living": living,
    }


# ── Tag + Notes builders ────────────────────────────────────────────────

def _build_tags(
    case: CaseRecord,
    notice_type: str,
    is_deceased_case: bool,
    n_alive_coowners: int,
    extra: list[str] | None = None,
    county: str = "montgomery",
) -> str:
    county_tag = (county or "montgomery").lower()
    tags = ["Courthouse Data", notice_type, county_tag]
    if case.date_filed:
        try:
            dt = datetime.strptime(case.date_filed, "%m/%d/%Y")
            tags.append(dt.strftime("%Y-%m"))
        except ValueError:
            pass
    if is_deceased_case:
        tags += ["deceased", "has_dm_address"]
    else:
        tags.append("living")
        if n_alive_coowners > 1:
            tags.append("co_owner_pair")
    if _is_tax_filing(case.filing_type):
        if notice_type != "tax_delinquent":
            tags.append("tax_delinquent")     # avoid duplicate when it's also the notice_type
        if notice_type == "probate":
            tags.append("foreclosure_trigger"  # surfaces probate via tax foreclosure
            )
    # Deep-prospect tag: address was only recovered by PJR-OCR, COMPLAINT-OCR,
    # or service-tab synthesis. These cases are reachable by us but NOT by
    # competing scrapers that pull just the public case-detail page. DataSift
    # routes these to a slower, personalized cadence.
    if (case.deep_prospect_unreachable or "").upper() == "Y":
        tags.append("deep_prospect_unreachable")
        if case.deep_prospect_source:
            tags.append(
                f"deep_source:{case.deep_prospect_source.lower()}"
            )
    if extra:
        tags += extra
    # Dedupe while preserving order
    seen = set()
    deduped = [t for t in tags if not (t in seen or seen.add(t))]
    return ",".join(deduped)


def _build_notes(
    case: CaseRecord,
    primary: Defendant,
    *,
    notice_type: str,
    decedent: Defendant | None = None,
    co_owners: list[Defendant] | None = None,
    other_heirs: list[Defendant] | None = None,
) -> str:
    """Multi-section Notes block. Structure mirrors SiftStack's _build_notes."""
    sections: list[str] = []

    # Header — case context
    header = (
        f"=== CASE {case.case_number} ===\n"
        f"Filing: {case.filing_type}\n"
        f"Date filed: {case.date_filed}\n"
        f"County: Montgomery, OH"
    )
    sections.append(header)

    # Deceased-owner section
    if decedent is not None:
        rel = _heir_relationship(primary.name)
        dec_section = (
            "=== DECEASED OWNER ===\n"
            f"Decedent: {_strip_role_suffix(decedent.name)}\n"
            f"Property: {case.property_street}, {case.property_city} {case.property_state} {case.property_zip}\n"
            f"Decision Maker: {_strip_role_suffix(primary.name)} ({rel})\n"
            f"DM mailing address: {primary.street}, {primary.city} {primary.state} {primary.zip}"
        )
        sections.append(dec_section)

    # Other heirs (if multi-heir case)
    if other_heirs:
        lines = [
            f"  - {_strip_role_suffix(h.name)} — {_heir_relationship(h.name)} "
            f"({h.street}, {h.city} {h.state} {h.zip})"
            for h in other_heirs
        ]
        sections.append("=== OTHER HEIRS ===\n" + "\n".join(lines))

    # Co-owner cross-reference
    if co_owners:
        for co in co_owners:
            sections.append(f"Co-owner: {_strip_role_suffix(co.name)} (same property)")

    # Litigation narrative — preserves data manager's hand-curated context
    if case.notes:
        sections.append(f"=== LITIGATION NOTES ===\n{case.notes}")

    return "\n\n".join(sections)


# ── Core conversion ─────────────────────────────────────────────────────

def _blank_row() -> dict:
    return {col: "" for col in SIFT_COLUMNS}


_COUNTY_DISPLAY = {
    "montgomery": "Montgomery",
    "warren": "Warren",
    "clermont": "Clermont",
    "clark": "Clark",
    "greene": "Greene",
    "butler": "Butler",
    "miami": "Miami",
}

_COUNTY_SOURCE_URLS = {
    "montgomery": "https://pro.mcohio.org/case/{cn}",
    "warren": "https://clerkofcourt.co.warren.oh.us/BenchmarkCP/Home.aspx/Search",
    "clermont": "https://eservices.clermontclerk.org/commonpleas/home.page",
    "clark": "https://eservices.clarkcountyohiocourt.com",
    "greene": "https://courts.greenecountyohio.gov/eservices/",
    "butler": "https://clerkservices.bcohio.gov/eservices/",
    "miami": "https://courts.miamicountyohio.gov/eservices/",
}


def _populate_common(
    row: dict,
    case: CaseRecord,
    *,
    notice_type: str,
    run_id: str,
    county: str = "montgomery",
) -> None:
    """Fields that are the same regardless of which defendant the row is for."""
    row["address"] = case.property_street
    row["city"] = case.property_city
    row["state"] = case.property_state
    row["zip"] = case.property_zip
    row["Date Added"] = datetime.now().strftime("%-m/%-d/%Y")
    row["notice_type"] = notice_type
    county_key = (county or "montgomery").lower()
    row["county"] = _COUNTY_DISPLAY.get(county_key, county.title() if county else "Montgomery")

    if _is_tax_filing(case.filing_type):
        amt = _parse_tax_amount(case.notes)
        yr = _parse_delinquent_year(case.notes)
        if amt:
            row["tax_delinquent_amount"] = amt
        if yr:
            # delinquent_years = current_year - delinquent_since_year
            try:
                years = max(1, datetime.now().year - int(yr))
                row["tax_delinquent_years"] = str(years)
            except ValueError:
                pass

    # Source URL — Montgomery has stable per-case PRO links; the others
    # use Wicket/Ajax tokens that aren't bookmarkable, so we fall back
    # to the portal landing page + case number in a query string.
    src_template = _COUNTY_SOURCE_URLS.get(county_key)
    if src_template and "{cn}" in src_template:
        row["source_url"] = src_template.format(
            cn=case.case_number.replace(' ', '')
        )
    elif src_template:
        row["source_url"] = (
            f"{src_template}?case_number={case.case_number.replace(' ', '+')}"
        )
    else:
        row["source_url"] = f"https://pro.mcohio.org/case/{case.case_number.replace(' ', '')}"
    row["run_id"] = run_id
    row["mailable"] = "True"

    # Deep-prospect markers: present whenever the address was recovered
    # by a fallback layer (PJR-OCR, COMPLAINT-OCR, or service-tab
    # synthesis). These are cases competitor scrapers can't reach.
    if (case.deep_prospect_unreachable or "").upper() == "Y":
        row["deep_prospect_unreachable"] = "Y"
        row["deep_prospect_source"] = case.deep_prospect_source or ""
    else:
        row["deep_prospect_unreachable"] = ""
        row["deep_prospect_source"] = ""


def _populate_owner(row: dict, d: Defendant) -> None:
    """Fill owner / mailing fields for a single defendant as the contact."""
    cleaned = _strip_role_suffix(d.name)
    first, last = _split_name(d.name)
    row["full_name"] = cleaned
    row["first_name"] = first
    row["last_name"] = last
    row["Owner Street"] = d.street
    row["Owner City"] = d.city
    row["Owner State"] = d.state
    row["Owner ZIP Code"] = d.zip


def _populate_deceased(row: dict, decedent: Defendant, heir: Defendant) -> None:
    """Fill decedent + DM fields when an heir is the contact."""
    row["decedent_name"] = _strip_role_suffix(decedent.name)
    row["deceased_indicator"] = "Y"
    row["owner_deceased"] = "yes"
    row["decision_maker_name"] = _strip_role_suffix(heir.name)
    row["decision_maker_relationship"] = _heir_relationship(heir.name)
    row["decision_maker_status"] = "living_verified"
    row["decision_maker_source"] = "court_record"
    row["decision_maker_street"] = heir.street
    row["decision_maker_city"] = heir.city
    row["decision_maker_state"] = heir.state
    row["decision_maker_zip"] = heir.zip
    row["dm_confidence"] = "0.97"
    row["dm_confidence_reason"] = "court_named_successor"
    row["obituary_source_type"] = "court_record"


def case_to_records(
    case: CaseRecord,
    run_id: str,
    county: str = "montgomery",
) -> list[dict]:
    """Convert one CaseRecord into 1+ SiftStack-format rows.

    Returns a list of dicts keyed by SIFT_COLUMNS. Length depends on pattern:
      - Deceased + heir(s): one record per heir, decedent populated
      - Alive co-owners: one record per defendant, co-owners cross-referenced
      - All deceased, no heir: one record with needs_manual_review flag
    """
    if not case.defendants:
        return []

    buckets = _classify_defendants(case.defendants)
    deceased = buckets["deceased"]
    unknown_heirs = buckets["unknown_heirs"]
    heirs = buckets["heirs"]
    living = buckets["living"]

    # Unknown heirs ALSO route to probate (owner is deceased, just no
    # heir has stepped up yet).
    has_deceased_or_heir = bool(deceased or heirs or unknown_heirs)
    notice_type = _route_to_notice_type(case, has_deceased_or_heir)

    records: list[dict] = []

    if heirs:
        # ── DECEASED + HEIR(S) pattern ──────────────────────────────
        primary_decedent = deceased[0] if deceased else None
        for i, heir in enumerate(heirs):
            row = _blank_row()
            _populate_common(row, case, notice_type=notice_type, run_id=run_id, county=county)
            _populate_owner(row, heir)
            if primary_decedent:
                _populate_deceased(row, primary_decedent, heir)
            other_heirs = [h for h in heirs if h is not heir]
            row["Tags"] = _build_tags(
                case, notice_type,
                is_deceased_case=True,
                n_alive_coowners=0,
                extra=["multi_heir"] if len(heirs) > 1 else None,
                county=county
            )
            row["Lists"] = "Probate"
            row["Notes"] = _build_notes(
                case, heir,
                notice_type=notice_type,
                decedent=primary_decedent,
                other_heirs=other_heirs,
            )
            row["heirs_verified_living"] = str(len(heirs))
            row["heir_search_depth"] = "0"  # already known from court record
            records.append(row)

    elif deceased and not heirs:
        # ── ALL DECEASED, NO HEIR NAMED ─────────────────────────────
        row = _blank_row()
        _populate_common(row, case, notice_type=notice_type, run_id=run_id, county=county)
        primary = deceased[0]
        _populate_owner(row, primary)
        row["decedent_name"] = _strip_role_suffix(primary.name)
        row["deceased_indicator"] = "Y"
        row["owner_deceased"] = "yes"
        row["missing_data_flags"] = "no_heir_identified,needs_manual_review"
        row["dm_confidence"] = "0.0"
        row["dm_confidence_reason"] = "no_named_successor_in_complaint"
        row["mailable"] = "False"
        row["Tags"] = _build_tags(
            case, notice_type, is_deceased_case=True, n_alive_coowners=0,
            extra=["needs_manual_review"],
                county=county
        )
        row["Lists"] = "Probate"
        row["Notes"] = _build_notes(case, primary, notice_type=notice_type)
        records.append(row)

    elif unknown_heirs:
        # ── UNKNOWN HEIRS / PLACEHOLDER DEFENDANTS ──────────────────
        # Owner is deceased, no heir has stepped up. These are high-value
        # H3 leads but require skip-tracing to find the actual heirs.
        for uh in unknown_heirs:
            row = _blank_row()
            _populate_common(row, case, notice_type=notice_type, run_id=run_id, county=county)
            _populate_owner(row, uh)
            # Extract the decedent name from the placeholder text
            # "UNKNOWN HEIRS OF MICHAEL D. JOHNSON" → "MICHAEL D. JOHNSON"
            decedent_match = re.search(
                r"(?:UNKNOWN\s+HEIRS|HEIRS(?:,?\s+(?:DEVISEES|LEGATEES))?|"
                r"REPRESENTATIVES)\s+OF\s+(.+?)(?:\s+DECEASED|$)",
                uh.name or "",
                re.I,
            )
            if decedent_match:
                row["decedent_name"] = decedent_match.group(1).strip()
            row["deceased_indicator"] = "Y"
            row["owner_deceased"] = "yes"
            row["heirs_unverified"] = "1"
            row["missing_data_flags"] = (
                "unknown_heirs,needs_skip_trace,no_mailing_address"
            )
            row["dm_confidence"] = "0.0"
            row["dm_confidence_reason"] = "unknown_heirs_court_placeholder"
            row["mailable"] = "False"
            row["heir_search_depth"] = "0"
            row["Tags"] = _build_tags(
                case, notice_type, is_deceased_case=True, n_alive_coowners=0,
                extra=["unknown_heirs", "needs_skip_trace"],
                county=county
            )
            row["Lists"] = "Probate"
            row["Notes"] = _build_notes(case, uh, notice_type=notice_type)
            records.append(row)

    else:
        # ── ALL ALIVE — one record per defendant, cross-referenced ──
        for d in living:
            row = _blank_row()
            _populate_common(row, case, notice_type=notice_type, run_id=run_id, county=county)
            _populate_owner(row, d)
            row["owner_deceased"] = "False"
            other_living = [o for o in living if o is not d]
            row["Tags"] = _build_tags(
                case, notice_type,
                is_deceased_case=False,
                n_alive_coowners=len(living),
                county=county
            )
            list_name = {
                "tax_delinquent": "Tax Delinquent",
                "foreclosure": "Foreclosure",
            }.get(notice_type, "Foreclosure")
            row["Lists"] = list_name
            row["Notes"] = _build_notes(
                case, d, notice_type=notice_type, co_owners=other_living,
            )
            records.append(row)

    # Add Tags/Lists/Notes columns to SIFT_COLUMNS dict if not present
    # (SIFT_COLUMNS doesn't include them — SiftStack's csv-import uses them
    # via separate handling, so we attach them out-of-band on each dict)
    return records


# ── Top-level writer ────────────────────────────────────────────────────

def write_siftstack_csv(
    cases: Sequence[CaseRecord],
    out_path: Path,
    run_id: str | None = None,
    county: str = "montgomery",
) -> Path:
    """Produce a SiftStack csv-import CSV from a list of CaseRecord objects."""
    if run_id is None:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    all_records: list[dict] = []
    for case in cases:
        all_records.extend(case_to_records(case, run_id, county=county))

    # Extended schema: SIFT_COLUMNS + Tags/Lists/Notes (which SiftStack
    # handles via the DataSift formatter; csv-import passes them through)
    # + deep_prospect_unreachable/deep_prospect_source so the DM can
    # filter / route these "no one else can reach them" cases into a
    # separate, slower, personalized marketing cadence.
    extended_cols = SIFT_COLUMNS + [
        "Tags", "Lists", "Notes",
        "deep_prospect_unreachable", "deep_prospect_source",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=extended_cols, extrasaction="ignore")
        writer.writeheader()
        for row in all_records:
            # Backfill any column the record didn't set
            for col in extended_cols:
                row.setdefault(col, "")
            writer.writerow(row)

    return out_path
