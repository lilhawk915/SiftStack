"""Butler County (Ohio) Probate Court — probatecourt.bcohio.gov scraper.

DIFFERENT portal from Butler foreclosure (which is clerkservices.bcohio.gov).
PHP-based system. The portal URL contains a session-key token in the
`k=` query parameter (e.g. `searchForm0909HtM6...`) — that token may need to be
obtained fresh per visit, or it may be a permanent magic value. Recon will
tell us.

Per H3 SOP H3-SOP-BCO-002:
  1. GET https://probatecourt.bcohio.gov/recordSearch.php?k=<token>
  2. Click Court Records Tab → Court Records Search
  3. Leave Name/Company + Case Number BLANK; fill File Date instead
  4. Select ESTATE from Case Type dropdown
  5. Click Begin Search
  6. Results show one row per case (case number = PEYY-MM-NNNN)
  7. Open each case → extract Decedent, DOD, Property, Fiduciary
  8. Open docket → application PDF → extract fiduciary email + missing info

CRITICAL constraint: search engine accepts ONLY a single date, not a range.
For multi-day pulls we must iterate over each day.

This first version is recon-mode only — captures the landing page HTML and
screenshot so we can identify the actual DOM selectors. The selectors below
are best guesses from the SOP and standard PHP form conventions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from h3.parsers.butler_probate_case_detail import (
    ButlerProbateDetail,
    parse_case_detail,
)
from h3.output_writers.probate_format import ProbateRecord


# ── Portal config (per H3 SOP H3-SOP-BCO-002) ───────────────────────────

PORTAL_URL = (
    "https://probatecourt.bcohio.gov/recordSearch.php"
    "?k=searchForm0909HtM6hnumXd2kFLd4eq4uxqDZmcEBxmxVi1o4grOS"
)
CASE_TYPE_VALUE = "ESTATE"

# Butler probate case-number format: PEYY-MM-NNNN (e.g. PE26-03-0254)
CASE_NUMBER_RE = re.compile(r"\bPE\d{2}-\d{2}-\d{4}\b")

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ── Helpers ─────────────────────────────────────────────────────────────

def _parse_proxy_url(url: str) -> dict[str, str]:
    p = urlparse(url)
    return {
        "server": f"{p.scheme}://{p.hostname}:{p.port}",
        "username": p.username or "",
        "password": p.password or "",
    }


def _to_us_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD → MM/DD/YYYY (Butler form likely expects US format)."""
    if not iso_date:
        return ""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return iso_date


def _dates_in_range(date_from: str, date_to: str) -> list[str]:
    """Generate list of ISO dates inclusive [date_from, date_to]."""
    if not date_from or not date_to:
        return []
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        return []
    out = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


# ── Parsed data structures ──────────────────────────────────────────────

@dataclass
class ButlerProbateCase:
    case_number: str
    decedent_name: str = ""
    date_filed: str = ""
    detail_url: str = ""
    raw_row_text: str = ""


@dataclass
class CaseDetailCapture:
    case_number: str
    final_url: str = ""
    html: str = ""
    docket_html: str = ""
    application_pdf_url: str = ""
    application_pdf_bytes: bytes = b""
    application_pdf_description: str = ""
    error: str = ""


@dataclass
class ReconCapture:
    landing_html: str = ""           # the portal homepage HTML (pre-search)
    landing_screenshot: bytes = b""
    results_html: str = ""           # first day's results HTML (sample)
    results_screenshot: bytes = b""
    parsed_cases: list[ButlerProbateCase] = field(default_factory=list)
    case_details: list[CaseDetailCapture] = field(default_factory=list)
    debug_log: list[dict[str, Any]] = field(default_factory=list)
    # Structured ProbateRecord rows (one per parsed case detail page) —
    # consumed by main.py to generate the H3 Excel output.
    probate_records: list[ProbateRecord] = field(default_factory=list)


# ── Best-guess parser (will refine after first recon) ──────────────────

def _combine_address(line1: str, line2: str) -> str:
    """Combine 'street' + 'city, state zip' lines into one address string."""
    parts = [p.strip() for p in (line1, line2) if p and p.strip()]
    return ", ".join(parts)


def _detail_to_record(detail: ButlerProbateDetail) -> ProbateRecord:
    """Convert a Butler case-detail page into a ProbateRecord row.

    Butler gives us 11 of 12 columns directly from the case detail page.
    Only Fiduciary Email comes from an application PDF (deferred).

    Mapping (Butler → DM probate schema):
      Case Number      → case_number
      Filing Type      → action (placeholder; the DM's "Action" column comes
                                 from a docket entry like
                                 "Application for Authority to Administer Estate"
                                 — that's a future enhancement)
      File Date        → date_filed
      Decedent         → decedent_name
      Date of Death    → date_of_death
      Relationship     → relationship
      Fiduciary        → fiduciary_name
      Fiduciary Addr   → fiduciary_address (street + city/state/zip combined)
      Phone Number     → fiduciary_phone
      Email            → blank for now (PDF parsing TODO)
      Decedent Addr    → subject_property (the property the estate owns)
    """
    return ProbateRecord(
        case_number=detail.case_number,
        case_type=detail.filing_type,
        date_filed=detail.file_date,
        decedent_name=detail.decedent_name,
        date_of_death=detail.date_of_death,
        action="",   # TODO: fill from docket entry
        relationship=detail.fiduciary_relationship,
        fiduciary_name=detail.fiduciary_name,
        fiduciary_address=_combine_address(
            detail.fiduciary_address, detail.fiduciary_city_state_zip,
        ),
        fiduciary_phone=detail.fiduciary_phone,
        fiduciary_email="",    # TODO: extract from application PDF
        subject_property=_combine_address(
            detail.decedent_address, detail.decedent_city_state_zip,
        ),
        co_fiduciary_name=detail.co_fiduciary_name,
        notes=(
            f"Type: {detail.fiduciary_type}"
            + (f"; Atty: {detail.attorney_name}"
               if detail.attorney_name else "")
            + (f"; D.B.A: {detail.decedent_dba}"
               if detail.decedent_dba else "")
        ),
    )


_BUTLER_CASE_URL_RE = re.compile(
    r'/recordSearch\.php\?k=case0909[^"\']+',
)
_BUTLER_DOCKET_URL_RE = re.compile(
    r'/recordSearch\.php\?k=docket0909[^"\']+',
)


@dataclass
class ButlerProbateCaseV2:
    """Richer per-case structure parsed from the results page table."""
    case_number: str
    decedent_name: str = ""
    date_filed: str = ""
    case_type: str = ""            # "Estate" etc.
    case_url: str = ""             # /recordSearch.php?k=case0909...
    docket_url: str = ""           # /recordSearch.php?k=docket0909...


def parse_results_html(html: str) -> list[ButlerProbateCase]:
    """Parse the Butler probate results page table. Each result row has:
      - case number (PEYY-MM-NNNN)
      - decedent name (after "Concerning:")
      - file date (after "Filed:")
      - "Visit Case" link → /recordSearch.php?k=case0909... (case detail)
      - "Docket" icon link → /recordSearch.php?k=docket0909...

    We also extract decedent_name and date_filed here so that downstream
    has basic case info even if the case-detail capture fails (Caselook's
    URL tokens get session-corrupted when navigating many cases).
    """
    soup = BeautifulSoup(html, "html.parser")
    cases: list[ButlerProbateCase] = []

    # Each result row sits in a div like:
    #   <div class="caseTitle"><span class="caseCounter">N</span>
    #     <span class="fullCaseNumber">PE26-05-0490</span>
    #     <span class="concerningName">Anderson-Campbell, Lisa M.</span></div>
    #   <div class="caseInfo">
    #     <div class="caseField fileDate"><label>Filed:</label> 05/26/2026</div>
    #     ...
    #     <a class="caseLink" href="/recordSearch.php?k=case0909...">Case</a>
    #     <a class="docketLink" href="/recordSearch.php?k=docket0909...">Docket</a>
    #   </div>
    seen: set[str] = set()
    for record in soup.find_all("div", class_="record"):
        title = record.find("div", class_="caseTitle")
        if not title:
            continue
        case_num_span = title.find("span", class_="fullCaseNumber")
        if not case_num_span:
            continue
        case_number = case_num_span.get_text(strip=True)
        if not case_number or not CASE_NUMBER_RE.fullmatch(case_number):
            continue
        if case_number in seen:
            continue
        seen.add(case_number)

        name_span = title.find("span", class_="concerningName")
        decedent = name_span.get_text(strip=True) if name_span else ""

        # File date — find caseField div with label "Filed:"
        date_filed = ""
        for fd in record.find_all("div", class_="caseField"):
            label = fd.find("label")
            if label and "Filed" in label.get_text():
                # Date appears after the label
                txt = fd.get_text(" ", strip=True)
                m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", txt)
                if m:
                    try:
                        date_filed = datetime.strptime(
                            m.group(1), "%m/%d/%Y"
                        ).strftime("%Y-%m-%d")
                    except ValueError:
                        pass
                break

        # Case-detail + docket URLs
        case_a = record.find("a", class_="caseLink")
        docket_a = record.find("a", class_="docketLink")
        case_url = case_a.get("href", "") if case_a else ""
        docket_url = docket_a.get("href", "") if docket_a else ""

        cases.append(ButlerProbateCase(
            case_number=case_number,
            decedent_name=decedent,
            date_filed=date_filed,
            detail_url=case_url,
            raw_row_text=docket_url,
        ))

    # Fallback to regex parsing if the structured DOM walk found nothing
    # (e.g., if the page layout differs from what we saw in recon)
    if not cases:
        raw_html = str(soup)
        case_urls = _BUTLER_CASE_URL_RE.findall(raw_html)
        docket_urls = _BUTLER_DOCKET_URL_RE.findall(raw_html)
        text = soup.get_text(" ", strip=True)
        i = 0
        for m in CASE_NUMBER_RE.finditer(text):
            cn = m.group(0)
            if cn in seen:
                continue
            seen.add(cn)
            cases.append(ButlerProbateCase(
                case_number=cn,
                detail_url=case_urls[i] if i < len(case_urls) else "",
                raw_row_text=docket_urls[i] if i < len(docket_urls) else "",
            ))
            i += 1

    return cases


# ── The scraper ─────────────────────────────────────────────────────────

class ButlerProbateScraper:
    """Playwright scraper for Butler County Probate Court (PHP).

    Recon flow: hit landing page, capture it. Then if we figure out the
    search form, fill it for ONE day in the range and capture results.
    """

    def __init__(
        self,
        *,
        date_from: str = "",
        date_to: str = "",
        proxy_config_url: str | None = None,
        headless: bool = True,
        mode: str = "recon",
        max_cases: int = 500,
        capture_case_details: int = 0,
        download_pdfs: bool = False,
        logger: Any = None,
    ):
        self.date_from = date_from           # ISO YYYY-MM-DD
        self.date_to = date_to               # ISO YYYY-MM-DD
        self.proxy_url = proxy_config_url
        self.headless = headless
        self.mode = mode
        self.max_cases = max_cases
        self.capture_case_details = capture_case_details
        self.download_pdfs = download_pdfs
        self.log = logger if logger else _StdoutLog()
        self.recon: ReconCapture = ReconCapture()

    async def run(self) -> None:
        self.log.info(
            f"ButlerProbateScraper start | mode={self.mode} | "
            f"dates {self.date_from or '-'} → {self.date_to or '-'} | "
            f"headless={self.headless} | capture_case_details={self.capture_case_details}"
        )
        async with async_playwright() as p:
            browser, ctx = await self._launch_browser(p)
            try:
                page = await ctx.new_page()
                await self._goto_portal(page)
                # Capture landing page first — recon will reveal real DOM
                self.recon.landing_html = await page.content()
                self.recon.landing_screenshot = await page.screenshot(full_page=True)
                self._dlog(
                    "landing_captured",
                    final_url=page.url,
                    html_bytes=len(self.recon.landing_html),
                )

                # Butler's Caselook portal accepts only a single date in its
                # search form, so we iterate each day in the window. After
                # the first disclaimer-accept, the session cookie persists,
                # so subsequent searches don't need to re-accept.
                #
                # Case-detail URLs use per-search session tokens that may
                # expire when we re-search, so we capture details
                # immediately after each day's search before moving on.
                if self.date_from:
                    try:
                        await self._navigate_to_search(page)
                        dates = _dates_in_range(
                            self.date_from, self.date_to or self.date_from
                        )
                        if not dates:
                            dates = [self.date_from]
                        self.log.info(
                            f"Iterating {len(dates)} day(s): "
                            f"{dates[0]} → {dates[-1]}"
                        )
                        captured_so_far = 0
                        cap_total = self.capture_case_details or 0
                        for day_iso in dates:
                            try:
                                day_cases = await self._search_one_day(
                                    page, day_iso
                                )
                                # Capture details for this day's cases
                                # immediately (URL tokens are fresh now).
                                if cap_total > 0 and captured_so_far < cap_total:
                                    remaining = cap_total - captured_so_far
                                    await self._capture_details_for(
                                        page, day_cases[:remaining]
                                    )
                                    captured_so_far += min(
                                        len(day_cases), remaining
                                    )
                            except Exception as e:
                                self.log.warning(
                                    f"  {day_iso}: search failed: {e}"
                                )
                                self._dlog("day_search_error",
                                           date=day_iso, error=str(e))

                        self.log.info(
                            f"Total: {len(self.recon.parsed_cases)} cases, "
                            f"{len(self.recon.probate_records)} records "
                            f"across {len(dates)} day(s)"
                        )
                    except Exception as e:
                        self.log.warning(f"Search flow failed: {e}")
                        self._dlog("search_flow_error", error=str(e))
            finally:
                await ctx.close()
                await browser.close()

    # ── Browser setup ───────────────────────────────────────────────

    async def _launch_browser(
        self, p: Playwright
    ) -> tuple[Browser, BrowserContext]:
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.proxy_url:
            launch_kwargs["proxy"] = _parse_proxy_url(self.proxy_url)
            self.log.info(f"Using proxy server: {launch_kwargs['proxy']['server']}")

        browser = await p.chromium.launch(**launch_kwargs)
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=DEFAULT_UA,
            locale="en-US",
            timezone_id="America/New_York",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        return browser, ctx

    # ── Portal flow ─────────────────────────────────────────────────

    async def _goto_portal(self, page: Page) -> None:
        self.log.info(f"GET {PORTAL_URL}")
        resp = await page.goto(PORTAL_URL,
                                wait_until="domcontentloaded",
                                timeout=30000)
        status = resp.status if resp else 0
        self._dlog("goto", url=PORTAL_URL, status=status, final_url=page.url)
        if status >= 400:
            raise RuntimeError(f"Portal returned HTTP {status}")
        await page.wait_for_timeout(3000)

    async def _navigate_to_search(self, page: Page) -> None:
        """Accept disclaimer by extracting the Continue link's href from the
        landing page and navigating to it directly. The Continue link looks
        like: <a href="/recordSearch.php?k=acceptAgreement<original-token>">

        Much more reliable than DOM-clicking through the Cancel/Continue
        buttons (both are <a> tags styled as buttons, no form submit).
        """
        import re as _re
        # Pull the Continue link href out of the rendered HTML
        html = await page.content()
        # Find any link whose href starts with /recordSearch.php?k=acceptAgreement...
        m = _re.search(
            r'href\s*=\s*["\'](/recordSearch\.php\?k=acceptAgreement[^"\']*)["\']',
            html,
        )
        if m:
            accept_url = "https://probatecourt.bcohio.gov" + m.group(1)
            self.log.info(f"Accepting disclaimer via direct nav: ...{accept_url[-60:]}")
            try:
                resp = await page.goto(
                    accept_url,
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                self._dlog(
                    "disclaimer_accepted_via_url",
                    final_url=page.url,
                    status=resp.status if resp else 0,
                )
                await page.wait_for_timeout(2000)
            except Exception as e:
                self.log.warning(f"Direct disclaimer nav failed: {e}")
                self._dlog("disclaimer_nav_error", error=str(e))
        else:
            self._dlog("disclaimer_link_not_found",
                       note="No acceptAgreement href in landing page")

        # After accepting, we should be on the search form page. If there's
        # an additional nav step (e.g. Court Records Tab), handle it here.
        self.log.info("Looking for Court Records Search form ...")
        for sel in [
            "a:has-text('Court Records Search')",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    self._dlog("nav_to_search_clicked", selector=sel)
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=15000
                    )
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

    async def _fill_search_form(self, page: Page, iso_date: str) -> None:
        """Fill File Date (separate Month/Day/Year selects) and check the
        Estate (PE) case-type box.

        Confirmed form fields (build 0.1.30 recon):
          searchFMonth   <select>  values 1-12
          searchFDay     <select>  values 1-31
          searchFYear    <select>  values like "2026", "2025"
          searchCaseType[] <checkbox> value="PE" for Estate
                                       value="PG" for Guardianship
                                       value="PC" for Civil
        """
        # Parse the ISO date
        try:
            dt = datetime.strptime(iso_date, "%Y-%m-%d")
        except ValueError:
            self._dlog("invalid_date", value=iso_date)
            return
        month_val = str(dt.month)
        day_val = str(dt.day)
        year_val = str(dt.year)
        self.log.info(
            f"Filling File Date = {month_val}/{day_val}/{year_val}, "
            f"Case Type = Estate (PE)"
        )

        # Fill the 3 date selects
        for name, value in [
            ("searchFMonth", month_val),
            ("searchFDay", day_val),
            ("searchFYear", year_val),
        ]:
            try:
                sel = f"select[name='{name}']"
                if await page.locator(sel).count() > 0:
                    await page.locator(sel).first.select_option(value=value)
                    self._dlog("date_select_filled", name=name, value=value)
                else:
                    self._dlog("date_select_not_found", name=name)
            except Exception as e:
                self._dlog("date_select_error", name=name, error=str(e))

        # Check the Estate (PE) case-type checkbox
        try:
            estate_box = page.locator(
                "input[type='checkbox'][name='searchCaseType[]'][value='PE']"
            ).first
            if await estate_box.count() > 0:
                # Only check it if not already checked
                if not await estate_box.is_checked():
                    await estate_box.check()
                self._dlog("case_type_estate_checked")
            else:
                self._dlog("case_type_estate_checkbox_not_found")
        except Exception as e:
            self._dlog("case_type_check_error", error=str(e))

    async def _submit_search(self, page: Page) -> None:
        """Click 'Begin Search' submit button."""
        self.log.info("Submitting search (Begin Search) ...")
        submitted = False
        for sel in [
            "input[type='submit'][value='Begin Search']",
            "input[type='submit'][value*='Begin' i]",
            "input[type='submit'][value*='Search' i]",
            "button:has-text('Begin Search')",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    self._dlog("search_submitted", selector=sel)
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            self._dlog("search_submit_failed")
            self.log.warning("Could not find Begin Search button")
            return

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)

    async def _capture_case_details_and_dockets(self, page: Page) -> None:
        """Backwards-compat: capture details for the first N parsed cases.
        Multi-day run() uses _capture_details_for() instead.
        """
        n = min(self.capture_case_details, len(self.recon.parsed_cases))
        if n == 0:
            return
        await self._capture_details_for(page, self.recon.parsed_cases[:n])

    async def _capture_details_for(
        self, page: Page, cases: list
    ) -> None:
        """For each given case, fetch case-detail + docket HTML and parse
        into a ProbateRecord. Called per-day so URL tokens are fresh.

        IMPORTANT: each case is opened in a NEW page (tab). Butler's
        Caselook URLs are session-scoped — navigating sequentially on a
        single page corrupts the session state and later cases come back
        with WRONG case data. Using a fresh page per case keeps each URL
        token's session pristine.
        """
        if not cases:
            return
        self.log.info(
            f"    Capturing {len(cases)} case detail page(s) ..."
        )

        base_prefix = "https://probatecourt.bcohio.gov"
        for i, case in enumerate(cases):
            cap = CaseDetailCapture(case_number=case.case_number)
            detail: ButlerProbateDetail | None = None
            # Open in a fresh tab so URL tokens stay independent
            case_page = await page.context.new_page()
            # Case detail
            if case.detail_url:
                url = (case.detail_url if case.detail_url.startswith("http")
                       else base_prefix + case.detail_url)
                try:
                    resp = await case_page.goto(
                        url, wait_until="domcontentloaded", timeout=20000
                    )
                    await case_page.wait_for_timeout(1500)
                    cap.html = await case_page.content()
                    cap.final_url = case_page.url
                    self._dlog("case_detail_captured",
                               case_number=case.case_number,
                               status=resp.status if resp else 0,
                               html_bytes=len(cap.html))
                    # Parse the captured HTML into a structured record
                    try:
                        detail = parse_case_detail(cap.html)
                        self._dlog("case_detail_parsed",
                                   case_number=case.case_number,
                                   decedent=detail.decedent_name,
                                   fiduciary=detail.fiduciary_name,
                                   phone=detail.fiduciary_phone)
                    except Exception as e:
                        self.log.warning(
                            f"    parser failed for {case.case_number}: {e}"
                        )
                        self._dlog("case_detail_parse_error",
                                   case_number=case.case_number,
                                   error=str(e))
                except Exception as e:
                    cap.error = str(e)
                    self._dlog("case_detail_error",
                               case_number=case.case_number, error=str(e))
            # Docket (stashed in raw_row_text per parser convention)
            docket_url = case.raw_row_text or ""
            if docket_url:
                url = (docket_url if docket_url.startswith("http")
                       else base_prefix + docket_url)
                try:
                    resp = await case_page.goto(
                        url, wait_until="domcontentloaded", timeout=20000
                    )
                    await case_page.wait_for_timeout(1500)
                    cap.docket_html = await case_page.content()
                    self._dlog("docket_captured",
                               case_number=case.case_number,
                               status=resp.status if resp else 0,
                               html_bytes=len(cap.docket_html))
                except Exception as e:
                    self._dlog("docket_error",
                               case_number=case.case_number, error=str(e))
            self.recon.case_details.append(cap)

            # Build the structured ProbateRecord. Even when case-detail
            # capture fails (decedent name empty → wrong case returned),
            # we still emit a row using results-page data so the DM at
            # least sees the case existed.
            if detail and detail.decedent_name:
                # Full record from case detail
                rec = _detail_to_record(detail)
                self.recon.probate_records.append(rec)
            else:
                # Fallback: use results-page data (no fiduciary contact)
                rec = ProbateRecord(
                    case_number=case.case_number,
                    case_type="",
                    date_filed=case.date_filed,
                    decedent_name=case.decedent_name,
                    notes="Case-detail capture failed (needs manual lookup)",
                )
                self.recon.probate_records.append(rec)
                self._dlog("fallback_record_emitted",
                           case_number=case.case_number)

            # Close the tab — frees memory and prevents stale tokens
            await case_page.close()

    async def _search_one_day(self, page: Page, iso_date: str) -> list:
        """Run the search form for one day and return parsed cases.

        Caselook's portal caches search results in the session, so we clear
        cookies between days to force a fresh form load. This costs us a
        disclaimer-accept per day (cheap — just one extra navigation).
        """
        self.log.info(f"  Day {iso_date}: searching...")
        # Clear session so portal doesn't return cached previous-day results
        await page.context.clear_cookies()
        await page.goto(PORTAL_URL, wait_until="domcontentloaded",
                         timeout=30000)
        await page.wait_for_timeout(1500)

        # After cookie-clear we'll always hit the disclaimer first — accept it
        if await page.locator("a:has-text('Continue')").count() > 0:
            await self._navigate_to_search(page)

        await self._fill_search_form(page, iso_date)
        await self._submit_search(page)
        html = await page.content()
        day_cases = parse_results_html(html)
        self.recon.parsed_cases.extend(day_cases)
        self.recon.results_html = html
        self._dlog("day_results",
                   date=iso_date, cases_found=len(day_cases))
        self.log.info(f"  Day {iso_date}: {len(day_cases)} Estate case(s)")
        return day_cases

    async def _capture_results(self, page: Page) -> None:
        self.log.info("Capturing results page HTML + screenshot")
        self.recon.results_html = await page.content()
        self.recon.results_screenshot = await page.screenshot(full_page=True)

        cases = parse_results_html(self.recon.results_html)
        if len(cases) > self.max_cases:
            cases = cases[: self.max_cases]
        self.recon.parsed_cases = cases
        self.log.info(
            f"Parsed {len(cases)} Butler probate case numbers from results."
        )
        self._dlog("results_parsed",
                   total_cases=len(cases),
                   html_bytes=len(self.recon.results_html))

    # ── Internal ────────────────────────────────────────────────────

    def _dlog(self, event: str, **kwargs: Any) -> None:
        entry = {"event": event, "ts": datetime.utcnow().isoformat() + "Z", **kwargs}
        self.recon.debug_log.append(entry)


class _StdoutLog:
    def info(self, msg: str) -> None: print(f"[INFO] {msg}")
    def warning(self, msg: str) -> None: print(f"[WARN] {msg}")
    def error(self, msg: str) -> None: print(f"[ERROR] {msg}")
