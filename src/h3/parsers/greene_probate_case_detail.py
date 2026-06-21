"""Parser for Greene County Probate Court case detail pages (equivant CourtView).

PLACEHOLDER — first iteration. DOM structure unknown until we capture a
real case detail page. The parser walks any structured label/value pairs
it can find and falls back to text scraping otherwise.

Will be refined after build 0.1.54 recon (3 case detail HTMLs captured).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass
class GreeneProbateDetail:
    case_number: str = ""
    case_type: str = ""
    file_date: str = ""
    # Decedent
    decedent_name: str = ""
    decedent_address: str = ""
    decedent_city_state_zip: str = ""
    date_of_death: str = ""
    # Fiduciary
    fiduciary_name: str = ""
    fiduciary_type: str = ""
    fiduciary_address: str = ""
    fiduciary_city_state_zip: str = ""
    fiduciary_phone: str = ""
    fiduciary_relationship: str = ""
    # Attorney
    attorney_name: str = ""
    attorney_phone: str = ""


_MDY_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _normalize_date(s: str) -> str:
    s = (s or "").strip()
    m = _MDY_SLASH.search(s)
    if not m:
        return ""
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().rstrip(":").strip()


def parse_case_detail(html: str) -> GreeneProbateDetail:
    """Best-effort first-pass parser. Walks the rendered text for any
    label/value pairs that match equivant's typical patterns.

    Iterate this function after the first recon shows the actual DOM."""
    detail = GreeneProbateDetail()
    soup = BeautifulSoup(html, "html.parser")

    # Pull case number from heading or title (equivant pages typically have
    # the case number in the breadcrumb or h1)
    m = re.search(r"\b\d{4}-EST-\d{1,5}\b", html)
    if m:
        detail.case_number = m.group(0)

    # Walk dt/dd or label/value patterns
    # equivant often uses <dt>Label</dt><dd>Value</dd>
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if not dd:
            continue
        label = _clean(dt.get_text())
        value = _clean(dd.get_text(" "))
        _assign_field(detail, label, value)

    # Also handle <th>Label</th><td>Value</td> patterns
    for th in soup.find_all("th"):
        td = th.find_next_sibling("td")
        if not td:
            continue
        label = _clean(th.get_text())
        value = _clean(td.get_text(" "))
        _assign_field(detail, label, value)

    return detail


def _assign_field(detail: GreeneProbateDetail, label: str, value: str) -> None:
    """Best-guess mapping of equivant labels to our ProbateDetail fields."""
    if not label or not value:
        return
    lower = label.lower()
    if "decedent" in lower or "concerning" in lower:
        if not detail.decedent_name:
            detail.decedent_name = value
    elif "date of death" in lower or "dod" in lower:
        detail.date_of_death = _normalize_date(value)
    elif "file date" in lower or "filed" in lower:
        if not detail.file_date:
            detail.file_date = _normalize_date(value)
    elif "case type" in lower:
        detail.case_type = value
    elif "fiduciary" in lower and ("address" not in lower and "phone" not in lower):
        if not detail.fiduciary_name:
            detail.fiduciary_name = value
    elif "phone" in lower or "telephone" in lower:
        if not detail.fiduciary_phone:
            detail.fiduciary_phone = value
    elif "address" in lower:
        # ambiguous — assign to decedent if not set, else fiduciary
        if not detail.decedent_address:
            detail.decedent_address = value
        elif not detail.fiduciary_address:
            detail.fiduciary_address = value
    elif "attorney" in lower:
        if not detail.attorney_name:
            detail.attorney_name = value
    elif "relationship" in lower:
        detail.fiduciary_relationship = value
