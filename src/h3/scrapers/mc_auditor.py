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


async def lookup_by_parcel(
    page: Page,
    parcel: str,
    *,
    timeout_ms: int = 20000,
) -> AuditorResult | None:
    """Look up the property address for a parcel number directly.

    Used by the yearly tax_delinquent enrichment pass. Tax-delinquent
    records carry parcel + owner + amount but no address; this fills
    the address gap by hitting the iasWorld parcel-ID search.

    Returns ``None`` when the parcel returns no Datalet or the
    detail page has no street.
    """
    if not parcel:
        return None
    try:
        result = await _open_detail_by_parcel(
            page, parcel, timeout_ms=timeout_ms,
        )
    except Exception as e:
        logger.warning("mc_auditor parcel lookup failed for %r: %s",
                       parcel, e)
        return None
    return result if result.found else None


# ── Address-based lookup (BUG-05 owner backfill for FC without parcel_id) ──


# Street suffix tokens we strip before submitting to iasWorld's address
# search. The auditor form's `inpStreet` does substring matching on the
# street name — a bare "HEDGESTONE" hits any HEDGESTONE DR / DRIVE /
# STREET variant. Passing "HEDGESTONE DRIVE" over-constrains and can
# miss a parcel recorded as "HEDGESTONE DR" in the CAMA data.
_STREET_SUFFIXES = frozenset({
    "AVE", "AVENUE",
    "BLVD", "BOULEVARD",
    "CIR", "CIRCLE",
    "CT", "COURT",
    "DR", "DRIVE",
    "HWY", "HIGHWAY",
    "LN", "LANE",
    "PKWY", "PARKWAY",
    "PL", "PLACE",
    "RD", "ROAD",
    "RT", "RTE", "ROUTE",
    "ST", "STREET",
    "TERR", "TERRACE",
    "TR", "TRAIL",
    "WAY",
    "PIKE",
    "ROW",
    "SQ", "SQUARE",
})


def _split_address_for_search(address: str) -> tuple[str, str]:
    """Split a property address into (street_number, street_name).

    Drops the street suffix (DR/DRIVE/ST/etc.) — iasWorld's address
    search substring-matches on the name field, so a bare
    ``HEDGESTONE`` hits ``HEDGESTONE DR`` but ``HEDGESTONE DRIVE``
    can miss it. Directional prefixes (N/S/E/W) are kept — they're
    part of the recorded street name in CAMA.

    Examples:
        ``"1700 HEDGESTONE DRIVE"`` → ``("1700", "HEDGESTONE")``
        ``"521 N MAIN ST"``         → ``("521", "N MAIN")``
        ``"37 W SECOND STREET"``    → ``("37", "W SECOND")``
        ``"BROOKVILLE PIKE"``       → ``("", "")`` — no number

    Returns ``("", "")`` when the address doesn't start with a numeric
    house number — the address search requires a number.
    """
    if not address:
        return "", ""
    parts = address.strip().upper().split()
    if not parts:
        return "", ""
    # First token must be a numeric house number (with optional suffix
    # like 1700A / 200-B). Numeric-only tokens are the vast majority.
    if not re.match(r"^\d+[A-Z-]?$", parts[0]):
        return "", ""
    num = parts[0]
    name_tokens = parts[1:]
    # Strip trailing street suffix
    while name_tokens and name_tokens[-1].rstrip(".") in _STREET_SUFFIXES:
        name_tokens.pop()
    if not name_tokens:
        # Suffix-only street name (e.g. "10 BROADWAY" → stripped to
        # empty). Restore the original tail — the auditor will still
        # substring-match on it.
        name_tokens = parts[1:]
    return num, " ".join(name_tokens)


async def lookup_by_address(
    page: Page,
    address: str,
    *,
    timeout_ms: int = 20000,
) -> AuditorResult | None:
    """Find a Montgomery parcel by property street address.

    Address-based counterpart to :func:`lookup_by_parcel`. Used for
    foreclosure/sheriff-sale records where the case-detail parser
    couldn't recover a parcel_id (so :func:`enrich_records_owner_by_parcel`
    can't help) but where the property address IS known.

    Ambiguity policy: when the auditor returns multiple parcels for
    the same address (split parcels, duplex/multi-unit), only proceed
    when exactly one row has the property address starting with the
    requested house number. Otherwise return ``None`` — better to
    leave the owner blank than to ship the wrong one.

    Returns ``None`` when:
      * ``address`` doesn't split into (number, street) cleanly
      * the auditor returns zero results
      * multiple results are ambiguous
      * the detail-fetch fails
    """
    num, street = _split_address_for_search(address)
    if not num or not street:
        return None

    try:
        await page.goto(f"{AUDITOR_BASE}/search/commonsearch.aspx?mode=address",
                        wait_until="domcontentloaded", timeout=timeout_ms)
        await page.locator("input[name='inpNumber']").fill(num)
        await page.locator("input[name='inpStreet']").fill(street)
        await page.locator("button:has-text('Search')").first.click()
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(800)
    except Exception as e:
        logger.warning("mc_auditor address search failed for %r: %s",
                       address, e)
        return None

    # Unique-hit fast path — auditor auto-redirects to Datalet
    if "Datalet" in page.url:
        try:
            html = await page.content()
            return parse_detail_html(html)
        except Exception as e:
            logger.warning("mc_auditor Datalet parse failed for %r: %s",
                           address, e)
            return None

    # Multi-hit path — parse the result table
    try:
        table_count = await page.locator("table#searchResults").count()
        if table_count == 0:
            return None
        table_html = await page.locator("table#searchResults").inner_html()
    except Exception as e:
        logger.warning("mc_auditor address result-parse failed for %r: %s",
                       address, e)
        return None

    rows = _parse_search_results(table_html)
    if not rows:
        return None

    # Prefer rows whose location field begins with the requested house
    # number — filters out the "any street with HEDGESTONE in the name"
    # substring-match noise.
    exact = [r for r in rows if r.location.upper().startswith(f"{num} ")]
    candidates = exact or rows

    # Dedup by parcel — the auditor's #searchResults table sometimes
    # renders the same parcel twice (observed for 27 FIVE OAKS AVE).
    # If all remaining rows collapse to a single parcel, it's not
    # ambiguous, it's row noise.
    seen: set[str] = set()
    unique: list = []
    for r in candidates:
        key = r.parcel.strip()
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(r)
    candidates = unique

    if len(candidates) != 1:
        logger.info("mc_auditor address: %r has %d results (ambiguous)",
                    address, len(candidates))
        return None

    hit = candidates[0]
    try:
        # Reuse the parcel-ID path for the actual detail — bypasses
        # the iasWorld session-cache bug that selectSearchRow triggers.
        # See _open_detail_by_parcel's docstring for the full trail.
        result = await _open_detail_by_parcel(
            page, hit.parcel, timeout_ms=timeout_ms,
        )
    except Exception as e:
        logger.warning("mc_auditor detail-fetch failed for address %r: %s",
                       address, e)
        return None

    # Preserve list-view owner text when Datalet parser doesn't populate
    # (same pattern as lookup_by_decedent_name)
    if not result.owner and hit.owner:
        result.owner = hit.owner
    return result if result.found else None


async def enrich_tax_delinquent_with_auditor(
    notices: list,
    *,
    headless: bool = True,
    concurrency: int = 5,
) -> int:
    """Concurrent parcel→address enrichment for tax_delinquent NoticeData.

    Each notice with a ``parcel_id`` and an empty ``address`` gets a
    parcel-ID lookup at mcrealestate.org. The auditor returns ~10 sec
    per parcel; running ``concurrency=5`` parallel contexts cuts a
    ~75 min sequential pass down to ~15 min for a typical
    451-record Montgomery list.

    Mutates the input list in place (sets ``.address`` / ``.city``
    / ``.state`` / ``.zip``). Returns the count successfully enriched.

    Same fresh-context-per-lookup pattern as
    :func:`enrich_probate_records_with_auditor` — iasWorld session
    state pollutes Datalet results across queries in the same context,
    so each worker spawns a new context per parcel.
    """
    targets = [
        n for n in notices
        if not getattr(n, "address", "")
        and getattr(n, "parcel_id", "")
    ]
    if not targets:
        return 0

    enriched = 0
    sem = asyncio.Semaphore(concurrency)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        async def _one(n) -> bool:
            async with sem:
                ctx = await browser.new_context()
                page = await ctx.new_page()
                try:
                    hit = await lookup_by_parcel(page, n.parcel_id)
                    if hit and hit.found:
                        n.address = hit.street
                        n.city = hit.city
                        n.state = hit.state or "OH"
                        n.zip = hit.zip
                        return True
                    return False
                finally:
                    await ctx.close()

        try:
            results = await asyncio.gather(
                *(_one(n) for n in targets),
                return_exceptions=True,
            )
            enriched = sum(1 for r in results if r is True)
            errs = [r for r in results if isinstance(r, Exception)]
            if errs:
                logger.warning("mc_auditor parcel-enrich: %d errors "
                               "across %d targets (first: %s)",
                               len(errs), len(targets), errs[0])
        finally:
            await browser.close()
    return enriched


async def enrich_records_owner_by_parcel(
    notices: list,
    *,
    headless: bool = True,
    concurrency: int = 5,
    sidecar_path: "Path | None" = None,
) -> dict:
    """Populate owner_name + owner mailing address + entity_type by
    parcel-ID lookup at mcrealestate.org.

    Targets: NoticeData records that have a ``parcel_id`` AND blank
    owner name (empty ``owner_name``). Written for sheriff sale rows
    (which the RealForeclose scraper cannot populate with an owner
    name — the auction listing doesn't expose one) and for foreclosure
    rows where the case-detail parser couldn't extract a defendant
    (rare but happens on redacted/sealed dockets).

    For each hit, the function writes:
      * ``owner_name``        — raw owner string from the auditor
      * ``owner_street``      — auditor's mailing address
      * ``owner_city``        — parsed from "City, State, Zip"
      * ``owner_state``       — same
      * ``owner_zip``         — same
      * ``entity_type``       — set only if the owner name classifies as
        an entity (LLC, corp, trust, estate, lp, other). Downstream
        ``entity_researcher.enrich_entity_records`` picks these up and
        resolves the person behind the entity.

    The property address (``notice.address`` / ``city`` / ``state`` /
    ``zip``) is left ALONE — for sheriff sale rows the RealForeclose
    scraper already put the auction property location there, and we
    don't want to overwrite it with the owner's separate mailing
    address (which can differ for rentals or LLC-held properties).

    Records that fail the auditor lookup are appended to
    ``sidecar_path`` (default: ``output/needs_manual_lookup.csv``) with
    case_number + property_address + reason so the operator can
    triage them manually. The main call NEVER fails on individual
    lookup errors — they're logged and counted, execution continues.

    Args:
        notices: List of NoticeData to consider. Only records with
            ``parcel_id`` and blank ``owner_name`` are touched.
        headless: Playwright headless mode (default True).
        concurrency: Parallel auditor lookups (default 5, mirrors
            ``enrich_tax_delinquent_with_auditor``).
        sidecar_path: Where to log failures. ``None`` uses the default
            ``output/needs_manual_lookup.csv``.

    Returns:
        dict with keys:
          * ``targets``      — count of records that qualified
          * ``enriched``     — count with owner_name populated after
          * ``entity_count`` — subset flagged as entity
          * ``failed``       — count that fell through to sidecar
    """
    import csv as _csv
    from pathlib import Path as _Path
    # Local imports so this module stays importable without the
    # datasift_formatter side-effects (config load, etc.) at cold start.
    from datasift_formatter import _is_entity_name

    # Filter to records that need + can be looked up
    targets = [
        n for n in notices
        if getattr(n, "parcel_id", "")
        and not (getattr(n, "owner_name", "") or "").strip()
    ]
    stats = {
        "targets": len(targets),
        "enriched": 0,
        "entity_count": 0,
        "failed": 0,
    }
    if not targets:
        return stats

    logger.info(
        "mc_auditor owner-enrich: %d records with parcel_id + blank "
        "owner_name (concurrency=%d)",
        len(targets), concurrency,
    )

    if sidecar_path is None:
        sidecar_path = _Path("output") / "needs_manual_lookup.csv"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(concurrency)
    failed_rows: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        async def _one(n) -> str:
            """Returns one of 'enriched', 'entity', or 'failed'.

            The whole body is inside the try/except (including context
            + page creation) so that transient browser-resource errors
            get sidecar-logged instead of vanishing silently. Observed
            2026-07-02: sheriff_sale row for 2017 MAYFAIR (parcel
            R72 11702 0063) came through today's CSV with blank owner
            despite the lookup working in isolation and no sidecar
            entry — the exception path was ``new_context()`` failing
            under load, and the try/except above wasn't wide enough
            to catch it.
            """
            async with sem:
                ctx = None
                try:
                    ctx = await browser.new_context()
                    page = await ctx.new_page()
                    hit = await lookup_by_parcel(page, n.parcel_id)
                    if not hit or not hit.found or not hit.owner:
                        failed_rows.append({
                            "case_number": getattr(n, "case_number", "") or "",
                            "parcel_id":   n.parcel_id,
                            "property_address": (
                                f"{getattr(n, 'address', '')}, "
                                f"{getattr(n, 'city', '')} "
                                f"{getattr(n, 'zip', '')}"
                            ).strip(", "),
                            "notice_type": getattr(n, "notice_type", ""),
                            "reason": (
                                "auditor_lookup_failed"
                                if not (hit and hit.found)
                                else "auditor_hit_but_no_owner"
                            ),
                        })
                        return "failed"

                    n.owner_name = hit.owner
                    n.owner_street = hit.street
                    n.owner_city = hit.city
                    n.owner_state = hit.state or "OH"
                    n.owner_zip = hit.zip
                    if _is_entity_name(hit.owner):
                        # Let entity_researcher resolve the person behind
                        # the entity downstream — it inspects entity_type
                        # to decide which resolution branch to run.
                        from entity_researcher import _classify_entity
                        n.entity_type = _classify_entity(hit.owner) or "other"
                        return "entity"
                    return "enriched"
                except Exception as e:
                    logger.warning(
                        "mc_auditor parcel-enrich exception for %r "
                        "(parcel=%r): %s: %s",
                        getattr(n, "case_number", "?"), n.parcel_id,
                        type(e).__name__, e,
                    )
                    failed_rows.append({
                        "case_number": getattr(n, "case_number", "") or "",
                        "parcel_id":   n.parcel_id,
                        "property_address": (
                            f"{getattr(n, 'address', '')}, "
                            f"{getattr(n, 'city', '')} "
                            f"{getattr(n, 'zip', '')}"
                        ).strip(", "),
                        "notice_type": getattr(n, "notice_type", ""),
                        "reason": f"exception:{type(e).__name__}",
                    })
                    return "failed"
                finally:
                    if ctx is not None:
                        try:
                            await ctx.close()
                        except Exception:
                            pass

        try:
            results = await asyncio.gather(
                *(_one(n) for n in targets),
                return_exceptions=True,
            )
        finally:
            await browser.close()

    # Tally results — treat unexpected exceptions as failed
    for r in results:
        if r == "enriched":
            stats["enriched"] += 1
        elif r == "entity":
            stats["enriched"] += 1
            stats["entity_count"] += 1
        else:
            stats["failed"] += 1

    # Append failures to sidecar (create file with header if missing)
    if failed_rows:
        header = list(failed_rows[0].keys())
        exists = sidecar_path.exists()
        with sidecar_path.open("a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=header)
            if not exists:
                w.writeheader()
            w.writerows(failed_rows)

    logger.info(
        "mc_auditor owner-enrich complete: %d/%d enriched "
        "(%d as entity), %d failed → %s",
        stats["enriched"], stats["targets"],
        stats["entity_count"], stats["failed"],
        sidecar_path if failed_rows else "no sidecar entries",
    )
    return stats


async def enrich_records_owner_by_address(
    notices: list,
    *,
    headless: bool = True,
    concurrency: int = 5,
    sidecar_path: "Path | None" = None,
) -> dict:
    """Owner backfill by property-address lookup — fallback for records
    the parcel-based backfill couldn't help.

    Runs AFTER :func:`enrich_records_owner_by_parcel` in the Ohio
    orchestrator, targeting only records that still have blank
    ``owner_name`` and blank ``parcel_id`` but do have a property
    ``address``. Common cause: mcohio's case-detail parser failed to
    extract a defendant name AND the docket didn't expose a parcel
    number — verified 2026-07-02 with 4/14 foreclosure rows blank on
    that day's Montgomery daily.

    Behaviour is structurally identical to the parcel variant:
      * writes ``owner_name``, ``owner_street/city/state/zip``,
        ``entity_type``
      * leaves ``notice.address`` alone (owner mailing may differ)
      * failures append to ``output/needs_manual_lookup.csv`` (shared
        sidecar with the parcel variant)

    Args:
        notices: list of NoticeData to consider.
        headless: Playwright headless mode.
        concurrency: parallel auditor lookups (default 5).
        sidecar_path: default ``output/needs_manual_lookup.csv``.

    Returns dict with ``targets``, ``enriched``, ``entity_count``,
    ``failed``.
    """
    import csv as _csv
    from pathlib import Path as _Path
    from datasift_formatter import _is_entity_name

    targets = [
        n for n in notices
        if not (getattr(n, "owner_name", "") or "").strip()
        and not (getattr(n, "parcel_id", "") or "").strip()
        and (getattr(n, "address", "") or "").strip()
    ]
    stats = {
        "targets": len(targets),
        "enriched": 0,
        "entity_count": 0,
        "failed": 0,
    }
    if not targets:
        return stats

    logger.info(
        "mc_auditor owner-enrich by address: %d records with blank "
        "owner_name + blank parcel_id + populated address "
        "(concurrency=%d)",
        len(targets), concurrency,
    )

    if sidecar_path is None:
        sidecar_path = _Path("output") / "needs_manual_lookup.csv"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(concurrency)
    failed_rows: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        async def _one(n) -> str:
            """Returns one of 'enriched', 'entity', or 'failed'.

            Full body inside try/except so transient browser errors
            during ``new_context()`` get sidecar-logged instead of
            silently propagating to gather() as exception objects.
            """
            async with sem:
                ctx = None
                try:
                    ctx = await browser.new_context()
                    page = await ctx.new_page()
                    hit = await lookup_by_address(page, n.address)
                    if not hit or not hit.found or not hit.owner:
                        failed_rows.append({
                            "case_number": getattr(n, "case_number", "") or "",
                            "parcel_id":   "",
                            "property_address": (
                                f"{getattr(n, 'address', '')}, "
                                f"{getattr(n, 'city', '')} "
                                f"{getattr(n, 'zip', '')}"
                            ).strip(", "),
                            "notice_type": getattr(n, "notice_type", ""),
                            "reason": (
                                "auditor_address_no_results"
                                if not (hit and hit.found)
                                else "auditor_hit_but_no_owner"
                            ),
                        })
                        return "failed"

                    n.owner_name = hit.owner
                    n.owner_street = hit.street
                    n.owner_city = hit.city
                    n.owner_state = hit.state or "OH"
                    n.owner_zip = hit.zip
                    # Backfill the parcel too so any downstream lookup
                    # (property lookup, tax enrichment, dedup) benefits.
                    if hit.parcel and not (getattr(n, "parcel_id", "") or "").strip():
                        n.parcel_id = hit.parcel
                    if _is_entity_name(hit.owner):
                        from entity_researcher import _classify_entity
                        n.entity_type = _classify_entity(hit.owner) or "other"
                        return "entity"
                    return "enriched"
                except Exception as e:
                    logger.warning(
                        "mc_auditor address-enrich exception for %r "
                        "(address=%r): %s: %s",
                        getattr(n, "case_number", "?"),
                        getattr(n, "address", ""),
                        type(e).__name__, e,
                    )
                    failed_rows.append({
                        "case_number": getattr(n, "case_number", "") or "",
                        "parcel_id":   "",
                        "property_address": (
                            f"{getattr(n, 'address', '')}, "
                            f"{getattr(n, 'city', '')} "
                            f"{getattr(n, 'zip', '')}"
                        ).strip(", "),
                        "notice_type": getattr(n, "notice_type", ""),
                        "reason": f"exception:{type(e).__name__}",
                    })
                    return "failed"
                finally:
                    if ctx is not None:
                        try:
                            await ctx.close()
                        except Exception:
                            pass

        try:
            results = await asyncio.gather(
                *(_one(n) for n in targets),
                return_exceptions=True,
            )
        finally:
            await browser.close()

    for r in results:
        if r == "enriched":
            stats["enriched"] += 1
        elif r == "entity":
            stats["enriched"] += 1
            stats["entity_count"] += 1
        else:
            stats["failed"] += 1

    if failed_rows:
        header = list(failed_rows[0].keys())
        exists = sidecar_path.exists()
        with sidecar_path.open("a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=header)
            if not exists:
                w.writeheader()
            w.writerows(failed_rows)

    logger.info(
        "mc_auditor owner-enrich by address complete: %d/%d enriched "
        "(%d as entity), %d failed → %s",
        stats["enriched"], stats["targets"],
        stats["entity_count"], stats["failed"],
        sidecar_path if failed_rows else "no sidecar entries",
    )
    return stats


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
