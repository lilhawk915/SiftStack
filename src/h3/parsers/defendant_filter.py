"""Filter parsed party-tab rows down to real lead defendants.

Court foreclosure cases include many non-lead parties:
  - Plaintiffs (the lender suing) — never a lead
  - The Treasurer / State of Ohio Dept of Taxation — always present, not a lead
  - Junior lien holders (banks, credit unions, debt buyers) — not a lead
  - "DOE" placeholders for unknown spouses — sometimes signal but not the name itself
  - AKA aliases of the real owner — duplicate, dedupe to canonical

We want the actual humans (or entities) who own the property.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from h3.parsers.party_tab import PartyEntry


# Substring patterns (case-insensitive) for "never a real lead" entities.
# Government entities, tax authorities, lenders, etc.
_NEVER_LEAD_FRAGMENTS = (
    # Local government — Montgomery County variants (incl. common misspellings)
    "MONTGOMERY COUNTY TREASURER", "MONTGMERY COUNTY",  # observed misspelling
    "MONTGOMERY COUNTY OHIO TREASURER",
    "CITY OF DAYTON", "CITY OF KETTERING", "CITY OF HUBER HEIGHTS",
    "CITY OF TROTWOOD", "CITY OF MIAMISBURG", "CITY OF FAIRBORN",
    "CITY OF MORAINE", "CITY OF VANDALIA",
    # State + federal
    "STATE OF OHIO", "OHIO DEPARTMENT", "OHIO ATTORNEY GENERAL",
    "UNITED STATES OF AMERICA", "SECRETARY OF HOUSING", "SECRETARY OF VETERANS",
    "INTERNAL REVENUE SERVICE", " HUD ", "HOUSING AND URBAN DEVELOPMENT",
    # Banks / lenders / debt buyers
    " BANK", " CREDIT UNION", " MORTGAGE ", " FINANCIAL", " FUNDING",
    "CAPITAL ONE", "WELLS FARGO", "JPMORGAN", "U S BANCORP",
    "U.S. BANK", "USAA", "DISCOVER FINANCIAL",
    "MIDLAND CREDIT", "PORTFOLIO RECOVERY", "LVNV FUNDING",
    "EQUITY TRUST", "CCR LLC", "MERS",
    "MORTGAGE ELECTRONIC REGISTRATION",
    "FREEDOM MORTGAGE", "QUICKEN LOANS",
    # Tax-certificate plaintiffs
    "TAX EASE", "TAX LIEN",
)

# "DOE" placeholder patterns (UNKNOWN SPOUSE OF X, JOHN DOE, etc.)
_DOE_RE = re.compile(
    r"^(?:[A-Z]+\s+)*(?:JANE|JOHN|RYAN|TAMMY|JANE|WILLOW|UNKNOWN)\s+DOE\b",
    re.I,
)
# Some courts list "UNKNOWN SPOUSE OF X" without DOE prefix
_UNKNOWN_SPOUSE_RE = re.compile(r"\bUNKNOWN\s+SPOUSE\s+OF\b", re.I)
# AKA aliases — "AKA NAME"
_AKA_RE = re.compile(r"^\s*AKA\s+", re.I)
# Unknown heirs / devisees — these ARE lead-relevant (probate-flavored)
_HEIRS_RE = re.compile(r"\bUNKNOWN\s+HEIRS?(?:\s+,\s+DEVISEES)?", re.I)


@dataclass
class FilteredDefendant:
    """A real lead defendant — survived the filter."""
    name: str
    role: str = "DEFENDANT"
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    is_primary: bool = False         # Matches Case Info Sheet main_defendant
    classification: str = ""         # "owner" | "heir_unknown" | "spouse_unknown"
    notes: str = ""


def _classify(party: PartyEntry) -> tuple[bool, str, str]:
    """Decide whether to keep this party, with classification + reason."""
    upper = party.name.upper().strip()

    # Always drop: plaintiffs + government entities + known lenders
    if party.role.upper() in {"PLAINTIFF"}:
        return False, "skip", "plaintiff"
    for frag in _NEVER_LEAD_FRAGMENTS:
        if frag.upper() in upper:
            return False, "skip", f"never-lead: matched '{frag.strip()}'"

    # AKAs: keep but mark as alias (dedupe will handle)
    if _AKA_RE.match(party.name):
        return True, "aka", "alias of another defendant"

    # Unknown heirs — important signal for probate routing
    if _HEIRS_RE.search(upper):
        return True, "heir_unknown", "unknown heirs"

    # "JOHN DOE / JANE DOE" — unknown spouse placeholders
    if _DOE_RE.match(party.name) or _UNKNOWN_SPOUSE_RE.search(upper):
        return True, "spouse_unknown", "unknown spouse placeholder"

    # Default: real owner candidate
    return True, "owner", ""


def _canonical_key(name: str) -> str:
    """Build a dedup key from a name — strips AKA, normalizes spacing/case."""
    cleaned = _AKA_RE.sub("", name).strip()
    # Strip "DOE" infixes (TAMMY DOE THOMAS SCOTT DAVIS → THOMAS SCOTT DAVIS)
    cleaned = re.sub(
        r"^[A-Z]+\s+DOE\s+", "", cleaned, flags=re.I
    )
    return re.sub(r"\s+", " ", cleaned.upper()).strip()


def filter_defendants(
    parties: list[PartyEntry],
    main_defendant_name: str = "",
) -> list[FilteredDefendant]:
    """Filter and dedupe a list of PartyEntry into lead defendants only.

    The Case Info Sheet's main_defendant (passed in via main_defendant_name)
    is marked is_primary=True if present.
    """
    main_upper = (main_defendant_name or "").upper().strip()
    keep: list[FilteredDefendant] = []
    seen_keys: dict[str, FilteredDefendant] = {}

    for p in parties:
        decided, classification, note = _classify(p)
        if not decided:
            continue

        key = _canonical_key(p.name)
        if key in seen_keys and classification == "aka":
            # AKA pointing to a name we already have — skip duplicate
            continue

        is_primary = bool(main_upper) and main_upper in p.name.upper()

        fd = FilteredDefendant(
            name=p.name,
            role=p.role or "DEFENDANT",
            street=p.street,
            city=p.city,
            state=p.state,
            zip=p.zip,
            is_primary=is_primary,
            classification=classification,
            notes=note,
        )
        if key not in seen_keys:
            seen_keys[key] = fd
            keep.append(fd)

    # Sort so primary comes first, then real owners, then heirs, then spouses
    order = {"owner": 0, "heir_unknown": 1, "spouse_unknown": 2, "aka": 3}
    keep.sort(key=lambda f: (not f.is_primary, order.get(f.classification, 9)))
    return keep


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from parsers.party_tab import parse_party_tab
    from parsers.case_info_sheet import parse_cis

    party_html = (Path(__file__).resolve().parents[1] / "output" /
                  "phase_b_run_3" / "case_03024_party.html").read_text()
    cis_bytes = (Path(__file__).resolve().parents[1] / "output" /
                 "phase_b_run_4" / "cis_raw").read_bytes()

    parties = parse_party_tab(party_html)
    cis = parse_cis(cis_bytes)

    filtered = filter_defendants(parties, main_defendant_name=cis.main_defendant)
    print(f"=== Filtered: {len(parties)} parties → {len(filtered)} leads ===\n")
    for f in filtered:
        flag = "★ PRIMARY" if f.is_primary else f"  ({f.classification})"
        print(f"  {flag:<22} {f.name}")
        print(f"                          {f.street}, {f.city}, {f.state} {f.zip}")
        if f.notes:
            print(f"                          [{f.notes}]")
        print()
