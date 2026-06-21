"""Parser for Montgomery County Probate Court case detail pages.

Confirmed DOM structure (build 0.1.22 recon):

    <tr>
      <td width="30%" valign="top">&nbsp;<LABEL></td>
      <td width="70%" valign="top">&nbsp;<VALUE>&nbsp; </td>
    </tr>

Field map:
    Decedent's Name        → decedent_name
    Date of Death           → date_of_death (MM/DD/YYYY → YYYY-MM-DD)
    Case Number             → case_number
    Case Type               → case_type (strip leading numeric ID + whitespace)
    Case Status             → status + status_date ("OPEN 01-06-2026"
                                                    or "CLOSED 01-16-2026")
    Appointment date        → appointment_date (MM-DD-YYYY → YYYY-MM-DD)
    Attorney                → attorney_name + attorney_phone ("LASTNAME, FIRST - 937-...")
    Fiduciary               → fiduciary_name (line 1) + fiduciary_address (line 2)
    Co-Fiduciary            → co_fiduciary_name + co_fiduciary_address (often blank)
    Magistrate Name         → magistrate (in the "Magistrate Name:" footer)

Date Filed approximation:
    For OPEN cases, Status Date IS the filing date.
    For CLOSED cases, Status Date is the closing date — we still record it
    but flag it as such.

Property + Fiduciary phone/email come from the docket PDFs, not this page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup


@dataclass
class MontgomeryProbateDetail:
    case_number: str = ""
    decedent_name: str = ""
    date_of_death: str = ""           # ISO YYYY-MM-DD
    case_type: str = ""               # text label only (numeric ID stripped)
    case_type_id: str = ""            # the leading numeric ID (e.g. "2", "14")
    case_status: str = ""             # OPEN / CLOSED / etc.
    case_status_date: str = ""        # ISO YYYY-MM-DD
    appointment_date: str = ""        # ISO YYYY-MM-DD
    attorney_name: str = ""
    attorney_phone: str = ""
    fiduciary_name: str = ""
    fiduciary_address: str = ""
    co_fiduciary_name: str = ""
    co_fiduciary_address: str = ""
    magistrate: str = ""
    balance: str = ""                 # optional, e.g. "0.00" or "54.95"


# ── Date normalization ─────────────────────────────────────────────────

_MDY_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_MDY_DASH = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b")


def _normalize_date(s: str) -> str:
    """Convert MM/DD/YYYY or MM-DD-YYYY to ISO YYYY-MM-DD. Returns '' on fail."""
    s = (s or "").strip()
    if not s:
        return ""
    for pat in (_MDY_SLASH, _MDY_DASH):
        m = pat.search(s)
        if m:
            mm, dd, yyyy = m.groups()
            return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    return ""


# ── Field extraction ───────────────────────────────────────────────────

def _strip_value(s: str) -> str:
    """Clean a value cell: strip whitespace and stray &nbsp;-like chars."""
    return re.sub(r"\s+", " ", (s or "")).replace("\xa0", " ").strip()


def _extract_label_value_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """Walk the case detail page table and build a {label: raw_value} map.

    Most labels appear in a TD with width="30%" and the value in the next
    sibling TD with width="70%". We use BeautifulSoup to grab them
    structurally so we don't have to rely on fragile string offsets.
    """
    out: dict[str, str] = {}

    # Find every TD that looks like a label (30% width)
    for label_td in soup.find_all("td", attrs={"width": "30%"}):
        label = label_td.get_text(" ", strip=True)
        if not label:
            continue
        # Value should be the very next td sibling
        value_td = label_td.find_next_sibling("td")
        if value_td is None:
            continue

        # Some fields have line breaks inside — preserve them as " | " so we
        # can split a Fiduciary into name + address.
        for br in value_td.find_all("br"):
            br.replace_with(" | ")
        value = _strip_value(value_td.get_text(" "))

        # Normalize the label by stripping trailing colons and asterisks
        clean_label = re.sub(r"[:\*\s]+$", "", label).strip()
        # If we somehow already have this label, keep the longer value
        if clean_label in out and len(out[clean_label]) >= len(value):
            continue
        out[clean_label] = value

    return out


# ── Specific field parsers ─────────────────────────────────────────────

_CASE_TYPE_NUMERIC = re.compile(r"^\s*(\d+)\s+(.*)$", re.DOTALL)


def _parse_case_type(raw: str) -> tuple[str, str]:
    """Split '2 FULL ADMIN; W/O WILL' → ('2', 'FULL ADMIN; W/O WILL')."""
    if not raw:
        return "", ""
    m = _CASE_TYPE_NUMERIC.match(raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", raw.strip()


_STATUS_RE = re.compile(
    r"\s*(?P<status>[A-Z]+)\s+(?P<date>\d{1,2}[-/]\d{1,2}[-/]\d{4})\s*"
)


def _parse_status(raw: str) -> tuple[str, str]:
    """'OPEN 01-06-2026' → ('OPEN', '2026-01-06')."""
    if not raw:
        return "", ""
    m = _STATUS_RE.match(raw)
    if not m:
        # Sometimes status appears alone with no date
        return raw.strip().upper(), ""
    return m.group("status"), _normalize_date(m.group("date"))


_ATTORNEY_RE = re.compile(
    r"^\s*(?P<name>[^-]+?)\s*-\s*(?P<phone>\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4})\s*$"
)


def _parse_attorney(raw: str) -> tuple[str, str]:
    """'SEBESY, DOUGLAS - 937-461-6328' → ('SEBESY, DOUGLAS', '937-461-6328')."""
    if not raw:
        return "", ""
    m = _ATTORNEY_RE.match(raw)
    if m:
        return m.group("name").strip(), m.group("phone").strip()
    return raw.strip(), ""


def _parse_fiduciary(raw: str) -> tuple[str, str]:
    """Fiduciary cell contains 'NAME | ADDRESS' (we replaced <br> with ' | ')."""
    if not raw:
        return "", ""
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# ── Magistrate (footer) ────────────────────────────────────────────────

_MAGISTRATE_RE = re.compile(
    r"Magistrate\s+Name\s*:\s*([^\n<]+?)(?:<|$)",
    re.I,
)


def _extract_magistrate(html: str) -> str:
    m = _MAGISTRATE_RE.search(html)
    if not m:
        return ""
    raw = m.group(1)
    # HTML-decode &nbsp; and collapse whitespace
    raw = raw.replace("&nbsp;", " ").replace("&#160;", " ")
    return re.sub(r"\s+", " ", raw).strip()


# ── Main parser ────────────────────────────────────────────────────────

def parse_case_detail(html: str) -> MontgomeryProbateDetail:
    """Parse a Montgomery probate case detail HTML page into a structured record."""
    soup = BeautifulSoup(html, "html.parser")
    fields = _extract_label_value_pairs(soup)

    detail = MontgomeryProbateDetail()
    detail.case_number = fields.get("Case Number", "")
    detail.decedent_name = fields.get("Decedent's Name", "")
    detail.date_of_death = _normalize_date(fields.get("Date of Death", ""))

    detail.case_type_id, detail.case_type = _parse_case_type(
        fields.get("Case Type", "")
    )

    detail.case_status, detail.case_status_date = _parse_status(
        fields.get("Case Status", "")
    )

    detail.appointment_date = _normalize_date(
        fields.get("Appointment date", "")
    )

    detail.attorney_name, detail.attorney_phone = _parse_attorney(
        fields.get("Attorney", "")
    )

    detail.fiduciary_name, detail.fiduciary_address = _parse_fiduciary(
        fields.get("Fiduciary", "")
    )

    detail.co_fiduciary_name, detail.co_fiduciary_address = _parse_fiduciary(
        fields.get("Co-Fiduciary", "")
    )

    detail.balance = fields.get("Balance", "")
    detail.magistrate = _extract_magistrate(html)

    return detail
