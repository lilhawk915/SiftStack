"""Parser for Butler County Probate Court case detail pages.

Confirmed DOM (build 0.1.32 recon, case PE26-06-0526):

    <h4 class="search">Decedent</h4>
    <table>
      <tr>
        <th class="column1">Decedent:</th>
        <td class="column2">Carter, Dorothy</td>
        <th class="column3">Attorney:</th>
        <td class="column4">EDWARD B SCHAEFER</td>
      </tr>
      ...
    </table>
    <h4 class="search">Fiduciary(s)</h4>
    <table>
      <tr>
        <th class="column1">Fiduciary 1:</th>
        ...
      </tr>
      ...
    </table>
    <h4 class="search">Case Information</h4>
    ...

Each row holds TWO label-value pairs (left + right). Section headers separate
groups so we can distinguish Decedent.Address from Fiduciary.Address.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag


@dataclass
class ButlerProbateDetail:
    case_number: str = ""
    # Decedent section
    decedent_name: str = ""
    decedent_dba: str = ""
    decedent_address: str = ""           # the SUBJECT PROPERTY for our purposes
    decedent_city_state_zip: str = ""
    date_of_death: str = ""              # ISO YYYY-MM-DD
    attorney_name: str = ""
    # Fiduciary section
    fiduciary_name: str = ""
    fiduciary_type: str = ""              # EXR, ADM, ADM CTA, etc.
    fiduciary_relationship: str = ""      # Spouse, Son, Daughter, "None", etc.
    fiduciary_date_appointed: str = ""    # ISO
    fiduciary_address: str = ""
    fiduciary_city_state_zip: str = ""
    fiduciary_phone: str = ""
    fiduciary_notice_waiver: str = ""
    # Co-fiduciary (if present)
    co_fiduciary_name: str = ""
    co_fiduciary_type: str = ""
    co_fiduciary_address: str = ""
    co_fiduciary_city_state_zip: str = ""
    co_fiduciary_phone: str = ""
    # Case Information section
    case_opened: str = ""                 # ISO
    file_date: str = ""                   # ISO
    case_closed: str = ""                 # ISO (blank if open)
    filing_type: str = ""


# ── Date normalization ─────────────────────────────────────────────────

_MDY_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _normalize_date(s: str) -> str:
    s = (s or "").strip()
    m = _MDY_SLASH.search(s)
    if not m:
        return ""
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def _clean(s: str) -> str:
    """Strip whitespace, collapse, and remove trailing colons."""
    s = re.sub(r"\s+", " ", (s or "")).replace("\xa0", " ").strip()
    return s.rstrip(":").strip()


# ── Parser ─────────────────────────────────────────────────────────────

def parse_case_detail(html: str) -> ButlerProbateDetail:
    """Parse a Butler probate case detail HTML page.

    Walks each `<h4 class="search">` section header to know which group of
    field labels belongs to which entity (Decedent vs Fiduciary 1 vs
    Co-Fiduciary vs Case Information). Within each section, walks the
    `<th class="columnN">` cells and reads the immediately-following
    `<td>` siblings for values.
    """
    soup = BeautifulSoup(html, "html.parser")
    detail = ButlerProbateDetail()

    # Pull case number from page heading (e.g. "Case Information: PE26-06-0526")
    case_no_match = re.search(r"\bPE\d{2}-\d{2}-\d{4}\b", html)
    if case_no_match:
        detail.case_number = case_no_match.group(0)

    # Walk section-by-section. Each `<h4 class="search">` opens a section;
    # everything until the next `<h4>` (or end of doc) belongs to it.
    h4s = soup.find_all("h4", class_="search")
    if not h4s:
        return detail

    # Build a (section_title, field_dict) map by parsing rows under each h4
    sections: dict[str, dict[str, str]] = {}
    current_section: str | None = None
    fields: dict[str, str] = {}

    for el in soup.find_all(True):
        # Section transition
        if el.name == "h4" and "search" in (el.get("class", []) or []):
            if current_section is not None:
                sections.setdefault(current_section, {}).update(fields)
            current_section = _clean(el.get_text())
            fields = {}
            continue

        # We only care about rows when we're inside a section
        if current_section is None or el.name != "tr":
            continue

        # Within a row, pair each <th class="columnN"> with the next <td>
        cells = el.find_all(["th", "td"], recursive=False)
        i = 0
        while i < len(cells) - 1:
            label_cell = cells[i]
            value_cell = cells[i + 1]
            if label_cell.name == "th":
                label = _clean(label_cell.get_text())
                value = _clean(value_cell.get_text())
                # Skip empty/sentinel labels
                if label and label != "&nbsp;" and label != "":
                    # If we've already stored this label in this section,
                    # don't overwrite (e.g. for Fiduciary there's a blank
                    # 2nd "Address" row that follows the populated one).
                    if not fields.get(label):
                        fields[label] = value
            i += 2

    # Flush the last section
    if current_section is not None:
        sections.setdefault(current_section, {}).update(fields)

    # ── Map labels into the structured record ───────────────────────

    # Section: Decedent
    decedent = sections.get("Decedent", {})
    detail.decedent_name = decedent.get("Decedent", "")
    detail.attorney_name = decedent.get("Attorney", "")
    detail.decedent_dba = decedent.get("D.B.A/A.K.A", "")
    detail.date_of_death = _normalize_date(decedent.get("Date of Death", ""))
    detail.decedent_address = decedent.get("Address", "")
    detail.decedent_city_state_zip = decedent.get("City/State/ZIP", "")

    # Section: Fiduciary(s) — note the parens in label
    fid_sect = sections.get("Fiduciary(s)", {})
    detail.fiduciary_name = fid_sect.get("Fiduciary 1", "")
    detail.fiduciary_type = fid_sect.get("Fiduciary Type", "")
    detail.fiduciary_date_appointed = _normalize_date(
        fid_sect.get("Date Appointed", "")
    )
    detail.fiduciary_relationship = fid_sect.get("Relationship", "")
    detail.fiduciary_notice_waiver = fid_sect.get("Notice Waiver", "")
    detail.fiduciary_address = fid_sect.get("Address", "")
    detail.fiduciary_city_state_zip = fid_sect.get("City/State/ZIP", "")
    detail.fiduciary_phone = fid_sect.get("Phone Number", "")
    # Co-fiduciary often labeled "Fiduciary 2"
    detail.co_fiduciary_name = fid_sect.get("Fiduciary 2", "")

    # Section: Case Information
    case_sect = sections.get("Case Information", {})
    detail.case_opened = _normalize_date(case_sect.get("Case Opened", ""))
    detail.file_date = _normalize_date(case_sect.get("File Date", ""))
    detail.case_closed = _normalize_date(case_sect.get("Case Closed", ""))
    detail.filing_type = case_sect.get("Filing Type", "")

    return detail
