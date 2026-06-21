"""Montgomery County Auditor parcel-to-address lookup.

Powers the property-address resolution step for absentee-owner cases.

Currently STUBBED — the auditor site (mcrealestate.org) is under maintenance
as of 2026-06-07. The class structure + URL pattern below match what we
observed before maintenance went up, ready to drop in the moment the site
is back. Call sites tolerate `lookup_property() = None` cleanly.

When the site returns, finalize:
  - the exact form-post URL (currently best guess)
  - any ASP.NET viewstate handling
  - the result-page selectors for street/city/state/zip
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
from playwright.async_api import Page


AUDITOR_BASE = "https://www.mcrealestate.org"
PARID_SEARCH_URL = f"{AUDITOR_BASE}/search/commonsearch.aspx?mode=parid"


@dataclass
class AuditorResult:
    parcel: str
    property_street: str = ""
    property_city: str = ""
    property_state: str = ""
    property_zip: str = ""
    raw_text: str = ""
    error: str = ""

    @property
    def found(self) -> bool:
        return bool(self.property_street)


async def lookup_property(
    page: Page,
    parcel: str,
    *,
    timeout_ms: int = 20000,
    logger: Any = None,
) -> AuditorResult:
    """Resolve a parcel number to a property address via mcrealestate.org.

    Uses an existing Playwright page (so we share the browser/session with
    the main scraper). Returns AuditorResult; check .found / .error.

    Right now this method returns an error result indicating the site is
    under maintenance. Replace _MAINTENANCE_MODE = True with False and
    finalize the selectors when the site is back.
    """
    log = logger or _stdout_log()
    result = AuditorResult(parcel=parcel)

    if _MAINTENANCE_MODE:
        result.error = "auditor under maintenance"
        return result

    try:
        log.info(f"Auditor: looking up parcel {parcel}")
        await page.goto(PARID_SEARCH_URL,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms)
        await page.wait_for_timeout(1000)

        # The iasWorld parcel-ID search has an input named "inpParid".
        # Submit the form by clicking the search button (or pressing Enter).
        await page.locator("input[name='inpParid']").fill(parcel)
        await page.locator("input[type='submit'][value*='Search']").first.click()
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1500)

        # Result page lists hits — usually a single row for a unique parcel.
        # Click into the first result.
        first_hit = page.locator("a[href*='parcel.aspx']").first
        if await first_hit.count() == 0:
            result.error = "no parcel hit on auditor"
            return result
        await first_hit.click()
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1500)

        # Parse the property location from the detail page.
        # Property Location typically appears in a labeled cell.
        html = await page.content()
        result.raw_text = html
        _parse_auditor_detail(html, result)
        return result

    except Exception as e:
        result.error = f"auditor exception: {e}"
        return result


_MAINTENANCE_MODE = True   # flip to False when mcrealestate.org is back


_PROPERTY_LOC_RE = re.compile(
    r"Property\s+Location[^A-Z0-9]*"
    r"([0-9]+\s+[A-Z0-9\s.,'/-]+)\s+"
    r"(?P<city>[A-Z\s]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)",
    re.I,
)


def _parse_auditor_detail(html: str, result: AuditorResult) -> None:
    """Extract Property Location from the parcel-detail HTML.

    Once the site is back, refine this by inspecting a real detail page.
    The regex below is a best-guess based on iasWorld's standard markup.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = _PROPERTY_LOC_RE.search(text)
    if m:
        street = m.group(1).strip().rstrip(",")
        result.property_street = street
        result.property_city = m.group("city").strip().title()
        result.property_state = m.group("state").upper()
        result.property_zip = m.group("zip")
    else:
        # Fall back: look for any "Location" label in the page
        loc_m = re.search(
            r"Location[:\s]+([0-9]+\s+[A-Z0-9\s.,'/-]+)",
            text, re.I,
        )
        if loc_m:
            result.property_street = loc_m.group(1).strip()


def _stdout_log():
    class _L:
        def info(self, m): print(f"[INFO ] {m}")
        def warning(self, m): print(f"[WARN ] {m}")
        def error(self, m): print(f"[ERROR] {m}")
    return _L()
