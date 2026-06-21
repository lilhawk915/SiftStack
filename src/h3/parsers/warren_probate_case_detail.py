"""Parser for Warren County Probate Court case detail pages.

DOM pattern (confirmed via build 0.1.40 curl test):

    <td colspan="2" class="back-sb">Estate Information</td>     ← section header
    <td width="50%" class="back-lt">
      <b>Decedent:</b> Hilley, Nelson L                          <br>
      <b>Address:</b> 3811 Robinson Vail Road                    <br>
      <b>City/State/ZIP:</b> Franklin, OH 45005                  <br>
      <b>Filed:</b> 06/04/2026
    </td>
    <td width="50%" class="back-lt">
      <b>Case Number:</b> 20261334                               <br>
      <b>Date of Death:</b> 03/30/2026                           <br>
      ...
    </td>

Sections (in order):
  "Estate Information"          → Decedent + Case Number metadata
  "Most Recent Appointment"     → Fiduciary
  "Attorney Information"        → Attorney
  "Timeline"                    → Letters Issued / Will Admitted / etc.

We parse by finding `<b>LABEL:</b> VALUE <br>` patterns within each section.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass
class WarrenProbateDetail:
    case_number: str = ""
    # Decedent / case metadata
    decedent_name: str = ""
    decedent_address: str = ""           # = SUBJECT PROPERTY
    decedent_city_state_zip: str = ""
    date_of_death: str = ""              # ISO YYYY-MM-DD
    file_date: str = ""                  # ISO
    case_opened: str = ""                # ISO
    case_closed: str = ""                # ISO
    # Fiduciary
    fiduciary_name: str = ""
    fiduciary_type: str = ""             # ADM, EXR, etc.
    fiduciary_address: str = ""
    fiduciary_city_state_zip: str = ""
    fiduciary_phone: str = ""            # = "Telephone:" field
    fiduciary_date_appointed: str = ""   # ISO
    fiduciary_relationship: str = ""     # Daughter, Spouse, etc.
    # Attorney
    attorney_name: str = ""
    attorney_phone: str = ""
    attorney_address: str = ""
    attorney_city_state_zip: str = ""
    # Timeline
    letters_issued: str = ""             # ISO
    will_admitted: str = ""              # ISO


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
    return re.sub(r"\s+", " ", (s or "")).strip()


# ── Section-based parser ───────────────────────────────────────────────

# Section header text → key in our sections dict
SECTION_HEADERS = {
    "Estate Information": "estate",
    "Most Recent Appointment": "appointment",
    "Attorney Information": "attorney",
    "Timeline": "timeline",
}


def _split_by_sections(html: str) -> dict[str, str]:
    """Split the HTML into chunks keyed by section name.

    Looks for `<td colspan="2" class="back-sb">SECTION NAME</td>` (or close
    to it) and slices the HTML between consecutive headers.
    """
    out: dict[str, str] = {}
    # Find all section header positions
    pattern = re.compile(
        r'<td[^>]+class=["\']back-sb["\'][^>]*>\s*([^<]+?)\s*</td>',
        re.I,
    )
    matches = list(pattern.finditer(html))
    for i, m in enumerate(matches):
        section_name = _clean(m.group(1))
        key = SECTION_HEADERS.get(section_name)
        if not key:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        out[key] = html[start:end]
    return out


_LABEL_VALUE_RE = re.compile(
    r"<b>\s*([^:<]+?)\s*:\s*</b>\s*([^<]*?)\s*(?:<br>|</td>|<b>|$)",
    re.I | re.DOTALL,
)


def _extract_label_values(chunk: str) -> dict[str, str]:
    """Pull out every `<b>Label:</b> Value <br>` pair in a chunk."""
    out: dict[str, str] = {}
    for m in _LABEL_VALUE_RE.finditer(chunk):
        label = _clean(m.group(1))
        value = _clean(m.group(2))
        # Strip trailing punctuation from labels
        label = label.rstrip(":").strip()
        if label and label not in out:
            out[label] = value
    return out


def parse_case_detail(html: str) -> WarrenProbateDetail:
    """Parse a Warren probate case detail HTML page."""
    detail = WarrenProbateDetail()
    sections = _split_by_sections(html)

    # Estate Information section: Decedent metadata + Case Number/DOD on right
    estate_fields = _extract_label_values(sections.get("estate", ""))
    detail.decedent_name = estate_fields.get("Decedent", "")
    detail.decedent_address = estate_fields.get("Address", "")
    detail.decedent_city_state_zip = estate_fields.get("City/State/ZIP", "")
    detail.file_date = _normalize_date(estate_fields.get("Filed", ""))
    detail.case_number = estate_fields.get("Case Number", "")
    detail.date_of_death = _normalize_date(
        estate_fields.get("Date of Death", "")
    )
    detail.case_opened = _normalize_date(estate_fields.get("Opened", ""))
    detail.case_closed = _normalize_date(estate_fields.get("Closed", ""))

    # Most Recent Appointment section: Fiduciary
    fid_fields = _extract_label_values(sections.get("appointment", ""))
    detail.fiduciary_name = fid_fields.get("Fiduciary #1", "")
    detail.fiduciary_type = fid_fields.get("Type", "")
    detail.fiduciary_address = fid_fields.get("Address", "")
    detail.fiduciary_city_state_zip = fid_fields.get("City/State/ZIP", "")
    detail.fiduciary_phone = fid_fields.get("Telephone", "")
    detail.fiduciary_date_appointed = _normalize_date(
        fid_fields.get("Date Appointed", "")
    )
    detail.fiduciary_relationship = fid_fields.get("Relationship", "")

    # Attorney Information section
    atty_fields = _extract_label_values(sections.get("attorney", ""))
    detail.attorney_name = atty_fields.get("Attorney", "")
    detail.attorney_phone = atty_fields.get("Telephone", "")
    detail.attorney_address = atty_fields.get("Address", "")
    detail.attorney_city_state_zip = atty_fields.get("City/State/ZIP", "")

    # Timeline section
    timeline_fields = _extract_label_values(sections.get("timeline", ""))
    detail.letters_issued = _normalize_date(
        timeline_fields.get("Letters Issued", "")
    )
    detail.will_admitted = _normalize_date(
        timeline_fields.get("Will Admitted", "")
    )

    return detail
