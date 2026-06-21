"""Parser for Montgomery County Probate Court docket pages.

Confirmed DOM (build 0.1.24 recon):

    <tr bgcolor="...">
      <td width="70">
        <a href="https://onbase.mcohio.org/aspweb/docpop/pdfpop.aspx?
                clienttype=html&docid=NNNNNNNNN&chksum=...">
          <img src="camera.gif" alt="Camera">
        </a>
      </td>
      <td width="150">MM-DD-YYYY</td>
      <td width="550">DOCKET ENTRY DESCRIPTION</td>
    </tr>

Some entries have no camera (no PDF available) — the first <td> is empty.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup


@dataclass
class DocketEntry:
    date: str                # ISO YYYY-MM-DD
    description: str
    pdf_url: str = ""        # OnBase PDF URL (or "" if no camera/PDF)


# ── Date normalization (reuses pattern from case_detail parser) ────────

_MDY_DASH = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b")
_MDY_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _normalize_date(s: str) -> str:
    s = (s or "").strip()
    for pat in (_MDY_DASH, _MDY_SLASH):
        m = pat.search(s)
        if m:
            mm, dd, yyyy = m.groups()
            return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    return ""


# ── Parser ─────────────────────────────────────────────────────────────

def parse_docket(html: str) -> list[DocketEntry]:
    """Parse a Montgomery probate docket page into a list of entries.

    Walks every <tr> and checks for the 3-cell pattern (camera | date | desc).
    Rows without exactly 3 cells (or with a different shape — e.g. header
    rows, divider rows) are skipped.
    """
    soup = BeautifulSoup(html, "html.parser")
    entries: list[DocketEntry] = []

    for tr in soup.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) != 3:
            continue

        # Cell 0: camera icon + (optional) <a href="pdf_url">
        pdf_a = cells[0].find("a", href=True)
        pdf_url = pdf_a.get("href", "") if pdf_a else ""

        # Cell 1: date string
        date_str = cells[1].get_text(" ", strip=True)
        if not _MDY_DASH.search(date_str) and not _MDY_SLASH.search(date_str):
            continue  # not a real docket row

        # Cell 2: description text
        desc = cells[2].get_text(" ", strip=True)
        desc = re.sub(r"\s+", " ", desc).strip()
        if not desc:
            continue

        entries.append(DocketEntry(
            date=_normalize_date(date_str),
            description=desc,
            pdf_url=pdf_url,
        ))

    return entries


# ── Application-PDF selection ──────────────────────────────────────────

# Per H3 SOP H3-SOP-MCO-002, the fiduciary contact + property comes from
# one of these "Application" PDFs. Order matters — most preferred first.
_APPLICATION_PATTERNS = [
    re.compile(r"\bAPPLICATION\s+FOR\s+AUTHORITY\s+TO\s+ADMINISTER\b", re.I),
    re.compile(r"\bAPPLICATION\s+FOR\s+SUMMARY\s+RELEASE\b", re.I),
    re.compile(r"\bAPPLICATION\s+FOR\s+RELEASE\s+OF\s+ADMINISTRATION\b", re.I),
    re.compile(r"\bAPPLICATION\s+FOR\s+TRANSFER\s+OF\s+CERTIFICATE\b", re.I),
    re.compile(r"\bFIDUCIARY\s+BOND\b", re.I),
    re.compile(r"\bNOTICE\s+OF\s+WILL\s+FOR\s+PROBATE\b", re.I),
    re.compile(r"\bAPPLICATION\s+TO\s+PROBATE\s+WILL\b", re.I),
    re.compile(r"\bAPPLICATION\s+TO\s+RELIEVE\s+ESTATE\b", re.I),
    re.compile(r"\bCERTIFICATE\s+OF\s+TRANSFER\b", re.I),
]


def select_application_pdf(
    entries: list[DocketEntry],
) -> Optional[DocketEntry]:
    """Pick the best PDF for fiduciary-contact extraction.

    Returns the first entry whose description matches one of the H3 SOP's
    target application form names, AND has a non-empty pdf_url. If none
    match cleanly, returns None.
    """
    # First pass: try each pattern in priority order
    for pat in _APPLICATION_PATTERNS:
        for entry in entries:
            if pat.search(entry.description) and entry.pdf_url:
                return entry
    # Fallback: first entry with a PDF whose description contains "APPLICATION"
    for entry in entries:
        if entry.pdf_url and re.search(r"\bAPPLICATION\b", entry.description, re.I):
            return entry
    return None
