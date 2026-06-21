"""Warren County Auditor parcel-based property-address lookup.

The court case detail HTML gives us the parcel number; the auditor's
public Property Search returns the property address keyed by parcel.
Using this instead of OCR-ing scanned complaint PDFs is faster and more
reliable.

Endpoint:
    GET https://auditor.warrencountyohio.gov/PropertySearch/Search/PerformParcelSearch
        ?ParcelNumber=<parcel>&TaxYearId=<uuid>

The TaxYearId is the current tax-year GUID; we resolve it on first call
by scraping the parcel-search form, then cache it for the rest of the
session. The auditor publishes new GUIDs each tax year, so refreshing
is cheap.

Result HTML layout (pertinent rows):

    <td class="heading">Property Address</td>
    <td class="value pl-2">
        <span>
            4868  JESSICA SUZANNE   DR<br />
            MORROW 45152
        </span>
    </td>

We collapse runs of whitespace inside the street line and parse the
trailing line as "CITY ZIP" (or "CITY, STATE ZIP" — only Warren entries
in Ohio are returned, so we default state=OH if absent).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup


AUDITOR_BASE = "https://auditor.warrencountyohio.gov"
PARCEL_FORM_URL = f"{AUDITOR_BASE}/PropertySearch/Search/Parcel"
PARCEL_SEARCH_URL = (
    f"{AUDITOR_BASE}/PropertySearch/Search/PerformParcelSearch"
)


@dataclass
class ParcelAddress:
    street: str = ""
    city: str = ""
    state: str = "OH"
    zip: str = ""

    def is_empty(self) -> bool:
        return not (self.street and self.zip)


_tax_year_cache: dict[str, str] = {}


def _resolve_tax_year_id(client: httpx.Client) -> str:
    """Scrape the parcel-search form for the current TaxYearId."""
    if "id" in _tax_year_cache:
        return _tax_year_cache["id"]
    try:
        r = client.get(PARCEL_FORM_URL, timeout=15)
        r.raise_for_status()
    except Exception:
        return ""
    m = re.search(
        r'name="TaxYearId"\s+value="([0-9a-fA-F\-]{36})"',
        r.text,
    )
    if not m:
        return ""
    _tax_year_cache["id"] = m.group(1)
    return _tax_year_cache["id"]


_STREET_SUFFIXES = (
    "ROAD", "RD", "DRIVE", "DR", "STREET", "ST", "AVENUE", "AVE",
    "BOULEVARD", "BLVD", "LANE", "LN", "COURT", "CT", "PLACE", "PL",
    "WAY", "PARKWAY", "PKWY", "CIRCLE", "CIR", "TERRACE", "TER",
    "TRAIL", "TRL", "HIGHWAY", "HWY", "ROUTE", "RT", "TURNPIKE", "TPKE",
    "SQUARE", "SQ", "ALLEY", "PIKE", "PATH", "RUN", "PT", "POINT",
    "RIDGE", "RDG", "MEWS", "GROVE",
)


def _parse_one_line_address(text: str) -> ParcelAddress:
    """Parse "STREET CITY, STATE ZIP" all on one line.

    Auditor renders multi-result addresses as one line with no comma
    between street and city (e.g. "9137 DEARDOFF ROAD FRANKLIN, OH
    45005"). We split on a known street suffix to find the boundary,
    then take everything between the suffix and the comma as the city.
    """
    text = re.sub(r"\s+", " ", text).strip()
    # State + zip after the comma
    m = re.match(
        r"^(?P<pre>.+?)\s*,\s*(?P<state>[A-Z]{2})\s+"
        r"(?P<zip>\d{5}(?:-\d{4})?)\s*$",
        text,
    )
    if not m:
        return ParcelAddress(street=text)
    pre = m.group("pre").strip()
    state = m.group("state").upper()
    zip_code = m.group("zip")

    # Find the last street suffix in `pre`; city = everything after it.
    suffix_re = re.compile(
        r"\b(" + "|".join(_STREET_SUFFIXES) + r")\b",
        re.IGNORECASE,
    )
    matches = list(suffix_re.finditer(pre))
    if matches:
        last = matches[-1]
        street = pre[: last.end()].strip()
        city = pre[last.end():].strip(" ,.-").title()
    else:
        # No recognizable suffix — give up and dump everything into street
        street, city = pre, ""
    return ParcelAddress(
        street=street, city=city, state=state, zip=zip_code,
    )


def _parse_address_block(html: str) -> ParcelAddress:
    """Pull the property address out of a parcel-search response.

    Auditor returns one of two shapes:
      A. Direct hit → detail page with
         `<td class="heading">Property Address</td>` + value cell.
      B. Multi-row hit → results list with table header containing
         "Physical Address"; rows have the address inline as
         "STREET CITY, STATE ZIP".
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── Layout A: detail page ─────────────────────────────────────────
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        for i, td in enumerate(cells):
            if "heading" not in (td.get("class") or []):
                continue
            if td.get_text(strip=True) != "Property Address":
                continue
            if i + 1 >= len(cells):
                continue
            value = cells[i + 1]
            text_chunks = []
            for elem in value.descendants:
                name = getattr(elem, "name", None)
                if name == "br":
                    text_chunks.append("\n")
                elif name is None:
                    text_chunks.append(str(elem))
            raw = "".join(text_chunks).strip()
            lines = [
                re.sub(r"\s+", " ", ln).strip()
                for ln in raw.splitlines() if ln.strip()
            ]
            if len(lines) >= 2:
                street, last = lines[0], lines[1]
                m = re.match(
                    r"^(?P<city>[A-Z][A-Z\.\-' ]+?)"
                    r"(?:,\s*(?P<state>[A-Z]{2}))?"
                    r"\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$",
                    last,
                )
                if m:
                    return ParcelAddress(
                        street=street.strip(),
                        city=m.group("city").strip().title(),
                        state=(m.group("state") or "OH").upper(),
                        zip=m.group("zip"),
                    )
                return ParcelAddress(street=f"{street} {last}".strip())
            if len(lines) == 1:
                return ParcelAddress(street=lines[0].strip())

    # ── Layout B: multi-row results table ─────────────────────────────
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        hdr_cells = rows[0].find_all(["th", "td"])
        headers = [c.get_text(strip=True) for c in hdr_cells]
        try:
            addr_col = headers.index("Physical Address")
        except ValueError:
            continue
        for r in rows[1:]:
            cells = r.find_all(["th", "td"])
            if len(cells) <= addr_col:
                continue
            text = cells[addr_col].get_text(" ", strip=True)
            if not text:
                continue
            return _parse_one_line_address(text)
    return ParcelAddress()


def lookup_property_by_parcel(parcel_number: str) -> ParcelAddress:
    """Fetch the property address for a Warren County parcel.

    Strips spaces / dashes from the parcel before sending. Returns an
    empty ParcelAddress on lookup failure (network error, parcel not
    found, or unexpected HTML layout). Safe to call repeatedly.
    """
    if not parcel_number:
        return ParcelAddress()
    clean = re.sub(r"[\s\-]", "", parcel_number)
    # Auditor uses a 13-char dashed format; their search also accepts
    # the bare digit string we get from the court site.
    with httpx.Client(
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 H3Bot/1.0"},
        timeout=20,
    ) as client:
        tax_id = _resolve_tax_year_id(client)
        if not tax_id:
            return ParcelAddress()
        try:
            r = client.get(
                PARCEL_SEARCH_URL,
                params={"ParcelNumber": clean, "TaxYearId": tax_id},
            )
            r.raise_for_status()
        except Exception:
            return ParcelAddress()
        return _parse_address_block(r.text)
