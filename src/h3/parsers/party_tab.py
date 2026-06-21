"""Parser for the Montgomery County case-detail Party tab HTML.

Each party in #tblPartyBody is rendered as 3 consecutive <tr> rows:

  1. Header row    (<tr class="table-info">):
       <strong>NAME</strong> <small>ROLE</small>
  2. Address row   (<tr>):
       <strong>Address:</strong>
       LINE 1
       LINE 2
       CITY, STATE ZIP
  3. Attorney row  (<tr>):
       <strong>Attorney(s):</strong>
       NAME
       LINE 1
       CITY, STATE ZIP
       — OR — "No Attorney on File."

We pull this into a structured list of PartyEntry objects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag


@dataclass
class PartyEntry:
    name: str
    role: str = ""               # PLAINTIFF | DEFENDANT | etc.
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    attorney_name: str = ""
    attorney_street: str = ""
    attorney_city: str = ""
    attorney_state: str = ""
    attorney_zip: str = ""


# Matches "CITY, STATE ZIP" or "CITY, ST ZIP-EXT"
_CITY_STATE_ZIP = re.compile(
    r"^(?P<city>[A-Z][A-Z\s\.\-']+?),\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)


def _tr_lines(tr: Tag) -> list[str]:
    """Return the non-empty text lines inside a <tr>, with the leading
    'Address:' or 'Attorney:' label stripped."""
    raw = tr.get_text("\n", strip=True)
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    # Drop leading "Address:" / "Attorney(s):" labels
    if lines and lines[0].rstrip(":").lower() in {"address", "attorney", "attorney(s)"}:
        lines = lines[1:]
    return lines


def _split_address_lines(lines: list[str]) -> tuple[str, str, str, str]:
    """Split address lines into (street, city, state, zip).

    Last line is expected to be 'CITY, STATE ZIP'.
    Everything before is concatenated into street (may include unit/suite).
    """
    if not lines:
        return "", "", "", ""

    last = lines[-1]
    m = _CITY_STATE_ZIP.match(last)
    if m:
        street = " ".join(lines[:-1])
        return street, m.group("city").strip(), m.group("state"), m.group("zip")

    # Couldn't recognize last line as city/state/zip — put it all in street
    return " ".join(lines), "", "", ""


def parse_party_tab(html: str) -> list[PartyEntry]:
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.select_one("#tblPartyBody")
    if tbody is None:
        return []

    rows = tbody.select("tr")
    entries: list[PartyEntry] = []

    i = 0
    while i < len(rows):
        tr = rows[i]
        is_header = "table-info" in (tr.get("class") or [])
        if not is_header:
            i += 1
            continue

        # Header row: <strong>name</strong> <small>role</small>
        name_el = tr.select_one("strong")
        role_el = tr.select_one("small")
        name = name_el.get_text(strip=True) if name_el else ""
        role = role_el.get_text(strip=True) if role_el else ""

        entry = PartyEntry(name=name, role=role)

        # Look at the next 1-2 rows for address + attorney
        for offset in (1, 2):
            j = i + offset
            if j >= len(rows):
                break
            tr2 = rows[j]
            if "table-info" in (tr2.get("class") or []):
                break    # next party — stop scanning this one
            lines = _tr_lines(tr2)
            if not lines:
                continue

            # Distinguish Address vs Attorney by the first label in the raw text
            raw = tr2.get_text(" ", strip=True)
            if raw.lower().startswith("address"):
                street, city, state, zp = _split_address_lines(lines)
                entry.street, entry.city, entry.state, entry.zip = street, city, state, zp
            elif raw.lower().startswith("attorney"):
                if lines and lines[0].lower().startswith("no attorney"):
                    continue
                entry.attorney_name = lines[0] if lines else ""
                if len(lines) > 1:
                    street, city, state, zp = _split_address_lines(lines[1:])
                    entry.attorney_street = street
                    entry.attorney_city = city
                    entry.attorney_state = state
                    entry.attorney_zip = zp

        entries.append(entry)
        i += 1
    return entries


if __name__ == "__main__":
    import sys
    from pathlib import Path

    path = (Path(sys.argv[1]) if len(sys.argv) > 1 else
            Path(__file__).resolve().parents[1] / "output" /
            "phase_b_run_3" / "case_03024_party.html")
    parties = parse_party_tab(path.read_text())
    print(f"=== Parsed {len(parties)} parties from {path.name} ===\n")
    for p in parties:
        print(f"  [{p.role}] {p.name}")
        print(f"    Address : {p.street}, {p.city}, {p.state} {p.zip}")
        if p.attorney_name:
            print(f"    Attorney: {p.attorney_name}")
            print(f"              {p.attorney_street}, {p.attorney_city}, {p.attorney_state} {p.attorney_zip}")
        print()
