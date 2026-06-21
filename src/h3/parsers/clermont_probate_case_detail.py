"""Parser for Clermont County Probate Court case detail pages (equivant CourtView).

DOM (confirmed via build 0.1.61 recon, case 2026 ES 00304):

  <h1>2026 ES 00304 Estate of: Paul M Haumesser</h1>

  <dl>
    <dt>Case Type:</dt><dd>Estate - ES</dd>
    <dt>Case Status:</dt><dd>Open</dd>
    <dt>File Date:</dt><dd>05/26/2026</dd>
    <dt>Action:</dt><dd>Full Administration With Will</dd>
    <dt>Status Date:</dt><dd>05/26/2026</dd>
    <dt>Case Judge:</dt><dd>Shriver, James A.</dd>
  </dl>

  <h2>Party Information</h2>
  <table>
    Haumesser, Paul M  - Decedent
    Zeisler, Amy M     - Applicant
    Tekulve, Lauri     - Attorney
    Zeisler, Amy M     - Fiduciary
  </table>

  <h2>Docket Information</h2>
  <table>...</table>

DM probate schema mapping:
  Case Number          → from h1 ("2026 ES 00304")
  Case Type            → "Estate - ES"
  Date Filed           → "05/26/2026"
  Testator/Decedent    → name labeled "- Decedent" in Party section
  Action               → "Full Administration With Will"
  Fiduciary Name       → name labeled "- Fiduciary" in Party section

Not on this page (need party-detail clicks):
  DOD, Fiduciary Address, Fiduciary Phone, Fiduciary Email,
  Subject Property, Relationship
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass
class ClermontProbateDetail:
    case_number: str = ""
    case_type: str = ""             # "Estate - ES"
    case_status: str = ""           # "Open" / "Closed"
    file_date: str = ""             # ISO YYYY-MM-DD
    action: str = ""                # e.g. "Full Administration With Will"
    status_date: str = ""           # ISO
    case_judge: str = ""
    # Decedent
    decedent_name: str = ""
    # Fiduciary (name only — address/phone/email come from party-detail clicks)
    fiduciary_name: str = ""
    # Attorney
    attorney_name: str = ""
    # Applicant (sometimes different from Fiduciary)
    applicant_name: str = ""


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


def parse_case_detail(html: str) -> ClermontProbateDetail:
    detail = ClermontProbateDetail()
    soup = BeautifulSoup(html, "html.parser")

    # Case number from the heading (h1 or large text)
    m = re.search(r"\b(\d{4}\s+ES\s+\d{1,5})\b", html)
    if m:
        detail.case_number = m.group(1)

    # Case header uses equivant's <li class="caseHdrLabel">Label:</li>
    # followed by <li class="caseHdrInfo">Value</li> pattern.
    fields: dict[str, str] = {}
    for li_label in soup.find_all("li", class_="caseHdrLabel"):
        li_value = li_label.find_next_sibling("li", class_="caseHdrInfo")
        if not li_value:
            continue
        label = _clean(li_label.get_text())
        value = _clean(li_value.get_text(" "))
        if label and label not in fields:
            fields[label] = value

    detail.case_type = fields.get("Case Type", "")
    detail.case_status = fields.get("Case Status", "")
    detail.file_date = _normalize_date(fields.get("File Date", ""))
    detail.action = fields.get("Action", "")
    detail.status_date = _normalize_date(fields.get("Status Date", ""))
    detail.case_judge = fields.get("Case Judge", "")

    # Party section uses equivant's <div class="ptyInfoLabel"> for name +
    # <div class="ptyType"> for role (e.g. " - Decedent"). Walk every
    # ptyInfoLabel and grab its sibling ptyType.
    role_re = re.compile(
        r"\s*-\s*(Decedent|Fiduciary|Attorney|Applicant|Beneficiary)",
        re.I,
    )
    for label_div in soup.find_all("div", class_="ptyInfoLabel"):
        name = _clean(label_div.get_text(" "))
        if not name:
            continue
        # Collapse double spaces equivant uses between name parts
        name = re.sub(r"\s+,\s+", ", ", name)
        name = re.sub(r"\s{2,}", " ", name)

        # Role lives in a sibling <div class="ptyType">
        # (sometimes inside the same <h5> wrapper)
        type_div = label_div.find_next("div", class_="ptyType")
        if not type_div:
            continue
        role_text = _clean(type_div.get_text(" "))
        m = role_re.search(role_text)
        if not m:
            continue
        role = m.group(1).title()

        if role == "Decedent" and not detail.decedent_name:
            detail.decedent_name = name
        elif role == "Fiduciary" and not detail.fiduciary_name:
            detail.fiduciary_name = name
        elif role == "Attorney" and not detail.attorney_name:
            detail.attorney_name = name
        elif role == "Applicant" and not detail.applicant_name:
            detail.applicant_name = name

    return detail
