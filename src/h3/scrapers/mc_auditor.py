"""Montgomery County Auditor (mcrealestate.org) — owner-name lookup.

The portal is Tyler Technologies iasWorld (CAMA assessment system,
same framework Greene/Clermont/Clark counties also use). Two entry
points used here:

  - ``/search/commonsearch.aspx?mode=owner`` — owner-name search form
  - ``/Datalets/Datalet.aspx?sIndex=0&idx=<N>`` — parcel detail page

The probate adapter calls :func:`lookup_by_decedent_name` after the
case-detail scrape to populate ``ProbateRecord.subject_property``
for cases where the decedent owned property in Montgomery County.
This is what enables DataSift tag-stacking between probate records
and existing foreclosure / sheriff-sale records for the same address.

History note: this file was previously stubbed because the auditor
site was under maintenance (2026-06-07). The site came back up at an
unspecified later date — this module re-establishes the lookup.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup
from playwright.async_api import Page, async_playwright


AUDITOR_BASE = "https://www.mcrealestate.org"
OWNER_SEARCH_URL = f"{AUDITOR_BASE}/search/commonsearch.aspx?mode=owner"
PARID_SEARCH_URL = f"{AUDITOR_BASE}/search/commonsearch.aspx?mode=parid"

logger = logging.getLogger(__name__)


# ── Result dataclass ────────────────────────────────────────────────────


@dataclass
class AuditorResult:
    """One parcel hit. The MVP populates address fields; the bonus
    fields (year_built, sqft, beds, baths, etc.) are extracted when
    available so callers can enrich NoticeData beyond just the address.
    """
    parcel: str = ""
    owner: str = ""
    # Address (street is required to count as 'found'; rest are best-effort)
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    # Bonus property attributes from the iasWorld datalet
    year_built: str = ""
    living_sqft: str = ""
    bedrooms: str = ""
    bathrooms: str = ""
    acres: str = ""
    structure_type: str = ""  # e.g. "RANCH", "TWO STORY"
    estimated_value: str = ""
    # Diagnostics
    error: str = ""

    @property
    def found(self) -> bool:
        return bool(self.street)

    @property
    def full_address(self) -> str:
        """Combined ``STREET, CITY, ST ZIP`` — the format the probate
        bridge's ``_parse_combined_address`` expects for
        ``ProbateRecord.subject_property``.
        """
        parts = []
        if self.street: parts.append(self.street)
        cs = ", ".join(p for p in [self.city, self.state] if p)
        if cs and self.zip:
            parts.append(f"{cs} {self.zip}")
        elif cs:
            parts.append(cs)
        elif self.zip:
            parts.append(self.zip)
        return ", ".join(parts)


# ── Name normalization ──────────────────────────────────────────────────


def _normalize_owner_search_name(name: str) -> str:
    """Convert a decedent name to the LAST-FIRST format iasWorld expects.

    Probate parsers store decedent names in various formats:

      * ``"MARY BOYER"``               → ``"BOYER MARY"``
      * ``"MARY A BOYER"``             → ``"BOYER MARY A"``
      * ``"MARY ANN BOYER"``           → ``"BOYER MARY ANN"``
      * ``"BOYER, MARY ANN"``          → ``"BOYER MARY ANN"``  (already last-first)
      * ``"JOHN SMITH JR"``            → ``"SMITH JOHN JR"``
      * ``"JOHN SMITH, JR."``          → ``"SMITH JOHN JR"``
      * ``"JOHN VAN DER BERG"``        → ``"VAN DER BERG JOHN"``  (best-effort)

    For multi-word last names ("VAN DER BERG"), this is a best-guess —
    iasWorld also does substring matching, so even an imperfect
    normalization usually still hits the right parcel.
    """
    if not name:
        return ""
    s = name.strip().upper()
    # Strip common suffixes that don't help the auditor
    s = re.sub(r",?\s+(JR|SR|II|III|IV|V)\.?\s*$", "", s)
    s = s.replace(".", "").replace(",", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    tokens = s.split()
    if len(tokens) == 1:
        return tokens[0]
    # "BOYER, MARY" → tokens = ["BOYER", "MARY"] — already last-first IF original had comma
    if "," in name:
        return s
    # FIRST [MIDDLE...] LAST  → LAST FIRST [MIDDLE...]
    last = tokens[-1]
    first_middle = " ".join(tokens[:-1])
    return f"{last} {first_middle}"


def _decedent_tokens(name: str) -> set:
    """Last-name + first-name tokens for matching against the
    portal-returned owner field. Used to filter ambiguous result lists.
    """
    if not name:
        return set()
    s = name.upper().replace(",", " ").replace(".", "")
    s = re.sub(r"\s+(JR|SR|II|III|IV|V)\s*$", "", s)
    return set(t for t in s.split() if len(t) >= 2)


# ── Owner-search → result list ──────────────────────────────────────────


@dataclass
class _SearchRow:
    parcel: str
    owner: str
    location: str
    idx: int    # row index for selectSearchRow (1-based)


async def _submit_owner_search(page: Page, last_first_name: str,
                                timeout_ms: int = 20000) -> list[_SearchRow]:
    """Visit the owner-search form, fill name, submit, parse result table.

    Returns an empty list when the portal stays on the search form
    (its silent "0 results" behaviour — there's no error banner, the
    input just keeps your query). We detect this case by counting
    ``#searchResults`` tables rather than waiting for one to appear.
    """
    await page.goto(OWNER_SEARCH_URL,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms)
    await page.locator("input[name='inpOwner']").fill(last_first_name)
    # The iasWorld portal has a single Search button
    await page.locator("button:has-text('Search')").first.click()
    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(800)

    # When there are 0 results, the portal silently returns to the
    # search form (no error message, no empty table). Detect that
    # explicitly instead of waiting for a table that'll never appear.
    table_count = await page.locator("table#searchResults").count()
    if table_count == 0:
        return []
    html = await page.locator("table#searchResults").inner_html()
    return _parse_search_results(html)


def _parse_search_results(table_html: str) -> list[_SearchRow]:
    """Parse the #searchResults table HTML into row tuples.

    Each data row's ``<tr onclick="...selectSearchRow('../Datalets/
    Datalet.aspx?sIndex=0&idx=N')...">`` carries the row index in
    the onclick URL. We extract that so the caller can click through.
    """
    soup = BeautifulSoup(f"<table>{table_html}</table>", "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        oc = tr.get("onclick") or ""
        m = re.search(r"idx=(\d+)", oc)
        if not m:
            continue
        idx = int(m.group(1))
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        # Expect 3 cells: parcel, owner, location
        if len(cells) < 3:
            continue
        rows.append(_SearchRow(
            parcel=cells[0], owner=cells[1], location=cells[2], idx=idx,
        ))
    return rows


# ── Parcel detail page → AuditorResult ─────────────────────────────────


_CITY_STATE_ZIP_RE = re.compile(
    r"([A-Z][A-Z .'-]+?)\s*,\s*([A-Z]{2})\s+(\d{5})(?:[\s-](\d{4}))?",
)


def parse_detail_html(html: str, parcel_hint: str = "") -> AuditorResult:
    """Extract address + bonus property fields from an iasWorld
    parcel-detail page. Exposed for testing against fixture HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = AuditorResult(parcel=parcel_hint)

    # Owner name + mailing address come from DataletSideHeading/Data pairs.
    pairs = {}
    for tr in soup.find_all("tr"):
        lc = tr.find("td", class_="DataletSideHeading")
        vc = tr.find("td", class_="DataletData")
        if not lc or not vc:
            continue
        label = lc.get_text(" ", strip=True)
        value = vc.get_text(" ", strip=True)
        if label:
            # Last value wins if duplicate labels (rare)
            pairs[label] = value

    # Owner — iasWorld may show up to 3 "Name" rows; keep the first non-empty
    for k, v in pairs.items():
        if k.lower() == "name" and v:
            out.owner = v
            break

    # Street — prefer "Mailing Address". For owner-occupied properties this
    # equals the parcel location. We sanity-check below.
    out.street = pairs.get("Mailing Address", "")

    # City / State / Zip — combined in one field by the portal.
    csz = pairs.get("City, State, Zip", "")
    m = _CITY_STATE_ZIP_RE.search(csz)
    if m:
        out.city = m.group(1).strip().title()
        out.state = m.group(2).upper()
        zip5 = m.group(3)
        plus4 = m.group(4)
        out.zip = f"{zip5}-{plus4}" if plus4 else zip5

    # If no street, try the DataletHeaderBottom "PARCEL LOCATION:" header
    if not out.street:
        for td in soup.find_all("td", class_="DataletHeaderBottom"):
            text = td.get_text(" ", strip=True)
            mm = re.match(r"PARCEL LOCATION:\s*(.+?)\s*$", text, re.I)
            if mm:
                out.street = mm.group(1).strip()
                break

    # Bonus fields — silently empty if not present
    out.year_built = pairs.get("Year Built", "")
    out.living_sqft = (pairs.get("Square Feet of Living Area", "")
                       or pairs.get("Total Square Footage", ""))
    out.acres = pairs.get("Acres", "")
    out.structure_type = pairs.get("Building Style", "")

    # Beds/Baths come as "5/3/1/1" = Total/Beds/Baths/HalfBaths
    rms = pairs.get("Total Rms/Bedrms/Baths/Half Ba", "")
    if rms:
        parts = re.split(r"\s*/\s*", rms)
        if len(parts) >= 4:
            out.bedrooms = parts[1]
            out.bathrooms = parts[2]

    # Estimated value — Total row has "land_assessed_value market_value"
    # split by space (e.g. "65,580 187,370"). The MARKET value is the
    # second figure; that's what we want.
    total = pairs.get("Total", "")
    if total:
        nums = re.findall(r"[\d,]+", total)
        if len(nums) >= 2:
            out.estimated_value = nums[1]

    return out


async def _open_detail_by_parcel(page: Page, parcel: str,
                                   timeout_ms: int = 20000) -> AuditorResult:
    """Re-search by parcel ID and click into the (single) result.

    We use a SEPARATE parcel-ID search rather than reusing the
    owner-search results, because iasWorld's session-state caching
    causes ``Datalet.aspx?sIndex=0&idx=N`` to return the LAST-CACHED
    parcel rather than the most-recent search's row N. Confirmed via
    repro: after consecutive owner-searches for BRADEN-ANNA and
    ANGERER-RITA, both Datalet visits returned BRADEN's parcel.
    Even invoking the portal's own ``selectSearchRow()`` JS function
    didn't refresh the cached session state.

    The parcel-ID search bypasses session caching: each parid lookup
    is a fresh server-side search keyed on the parcel#, so the result
    is always the correct parcel.
    """
    await page.goto(PARID_SEARCH_URL,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms)
    await page.locator("input[name='inpParid']").fill(parcel)
    await page.locator("button:has-text('Search')").first.click()
    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(800)

    # Parcel-search auto-redirects to the Datalet for unique hits.
    # For multi-hits (rare with full parcel#) it shows a result list
    # like the owner search — we'd handle that, but a full parcel#
    # is unique by design.
    if "Datalet" in page.url:
        html = await page.content()
        return parse_detail_html(html, parcel_hint=parcel)

    # Otherwise we got a result list — click the first match via JS.
    table_html = await page.locator("table#searchResults").inner_html()
    rows = _parse_search_results(table_html)
    if not rows:
        return AuditorResult(parcel=parcel, error="parid search empty")
    # selectSearchRow IS reliable when called fresh-from-search, before
    # any other navigation. Use it here.
    await page.evaluate(
        f"selectSearchRow('../Datalets/Datalet.aspx?sIndex=0&idx={rows[0].idx}')"
    )
    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(800)
    html = await page.content()
    return parse_detail_html(html, parcel_hint=parcel)


# ── Top-level lookup entrypoint ────────────────────────────────────────


async def lookup_by_decedent_name(
    page: Page,
    decedent_name: str,
    *,
    timeout_ms: int = 20000,
) -> AuditorResult | None:
    """Find the Montgomery property owned by the decedent.

    Behaviour:

      * **0 result rows** → return ``None`` (decedent didn't own
        Montgomery property — common; many decedents lived in
        other counties or didn't own real estate)
      * **1 result row whose owner field looks like the decedent**
        → click through, return populated ``AuditorResult``
      * **>1 result rows** → only proceed if exactly one of them
        token-matches the decedent name. Otherwise return ``None``
        (ambiguous — better to leave ``subject_property`` empty than
        ship a wrong address).

    Token matching: the decedent's first + last name tokens (length ≥
    2) must ALL appear in the result's owner field. This handles
    trust/decedent suffixes ("BOYER MARY ANN TR", "SMITH JOHN DECD",
    "JONES BETTY ETAL") cleanly.
    """
    if not decedent_name:
        return None
    last_first = _normalize_owner_search_name(decedent_name)
    if not last_first:
        return None

    try:
        rows = await _submit_owner_search(page, last_first,
                                            timeout_ms=timeout_ms)
    except Exception as e:
        logger.warning("mc_auditor search failed for %r: %s",
                       decedent_name, e)
        return None

    if not rows:
        return None

    # Token-match against decedent name
    tokens = _decedent_tokens(decedent_name)
    matches = [
        r for r in rows
        if tokens.issubset(set(r.owner.upper().split()))
    ]
    if len(matches) == 0:
        # Try a looser match — just last-name token, in case the
        # search returned a property with the surname but the first
        # name was different (rare but happens when the decedent's
        # spouse is the recorded owner)
        last_token = decedent_name.upper().strip().split()[-1]
        matches = [
            r for r in rows
            if last_token in r.owner.upper().split()
        ]
        if len(matches) != 1:
            logger.info("mc_auditor: %r has %d rows, %d match (ambiguous)",
                        decedent_name, len(rows), len(matches))
            return None
    elif len(matches) > 1:
        logger.info("mc_auditor: %r has %d rows, %d match (ambiguous)",
                    decedent_name, len(rows), len(matches))
        return None

    hit = matches[0]
    try:
        result = await _open_detail_by_parcel(
            page, hit.parcel, timeout_ms=timeout_ms,
        )
    except Exception as e:
        logger.warning("mc_auditor detail fetch failed for %r: %s",
                       decedent_name, e)
        return None
    # Preserve owner text from the search list (in case Datalet parser
    # missed it — list view is authoritative for owner of record)
    if not result.owner and hit.owner:
        result.owner = hit.owner
    return result if result.found else None


async def enrich_probate_records_with_auditor(
    records: list,
    *,
    headless: bool = True,
    max_lookups: int = 50,
) -> int:
    """Side-effecting post-processor: walks a list of ProbateRecord,
    fills in ``rec.subject_property`` via the auditor for records
    that don't already have one. Returns the number of records
    successfully enriched.

    Spawns its own Playwright browser — does NOT share with the
    probate scraper (the probate scraper closes its context before
    returning records).

    **Fresh-context-per-lookup**: each decedent lookup gets a brand
    new ``BrowserContext`` (fresh cookie jar). This is required to
    work around iasWorld's session-state caching, which makes
    sequential lookups in the same context return stale data
    (confirmed via repro: after a BRADEN-ANNA lookup, all subsequent
    Datalet visits returned BRADEN's parcel regardless of the
    search). Browser stays open across lookups; only contexts cycle.

    ``max_lookups`` is a safety cap: typical weekly Montgomery
    probate volume is 10-15 records, but in case some future code
    path feeds in 1000s, we cut off rather than hammer the auditor.
    """
    targets = [
        r for r in records
        if not getattr(r, "subject_property", "")
        and getattr(r, "decedent_name", "")
    ]
    if not targets:
        return 0
    if len(targets) > max_lookups:
        logger.warning(
            "mc_auditor: %d records to enrich exceeds cap %d — "
            "skipping the tail. Bump max_lookups if intentional.",
            len(targets), max_lookups,
        )
        targets = targets[:max_lookups]

    enriched = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            for rec in targets:
                # Fresh context = fresh cookies. iasWorld session-state
                # caching needs this to return correct Datalet pages.
                ctx = await browser.new_context()
                page = await ctx.new_page()
                try:
                    hit = await lookup_by_decedent_name(page, rec.decedent_name)
                    if hit and hit.found:
                        rec.subject_property = hit.full_address
                        enriched += 1
                        logger.info(
                            "  auditor: %s → %s (parcel %s)",
                            rec.decedent_name, hit.full_address, hit.parcel,
                        )
                finally:
                    await ctx.close()
                # Be polite — small pause between auditor hits
                await asyncio.sleep(0.5)
        finally:
            await browser.close()
    return enriched
