"""Miami County (Ohio) Probate Court — probate.miamicountyohio.gov scraper.

SAME VENDOR family as Miami probate (PHP `recordSearch.php` system).
Agency ID = 5503 in the URL token.

Per H3 SOP H3-SOP-MICO-002:
  1. GET https://probate.miamicountyohio.gov/recordSearch.php?k=<token>
  2. Accept disclaimer (Continue link → URL with acceptAgreement prefix)
  3. Fill File Date (single day — engine doesn't support ranges)
  4. Select ESTATE case type checkbox
  5. Click Begin Search
  6. Results show one row per case (case # format YYYYNNNN, e.g. 20261207)
  7. Open each case → extract Decedent, DOD, Property, Fiduciary, Phone
  8. (Future) Open docket → PDF → extract fiduciary email

Reuses Miami's case-detail parser since the vendor DOM is identical.
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

# Miami's case-detail DOM is identical to Butler's (same PHP vendor),
# so we reuse Butler's parser directly. Aliased for readability.
from h3.parsers.butler_probate_case_detail import (
    ButlerProbateDetail as MiamiProbateDetail,
    parse_case_detail,
)
from h3.output_writers.probate_format import ProbateRecord


# ── Portal config (per H3 SOP H3-SOP-MICO-002) ──────────────────────────

PORTAL_URL = (
    "https://probate.miamicountyohio.gov/recordSearch.php"
    "?k=searchForm5503"
)
CASE_TYPE_VALUE = "ESTATE"
PORTAL_HOST = "https://probate.miamicountyohio.gov"
# Miami uses agency ID 5503 (Miami is 5503). The acceptAgreement URL
# pattern is the same — just with this token.

# Miami probate case-number format: E + YYYYNNNN (e.g. E20261216)
# The "E" prefix denotes Estate cases (different prefixes for other types).
CASE_NUMBER_RE = re.compile(r"\bE\d{8}\b")

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
    """Convert YYYY-MM-DD → MM/DD/YYYY (Miami form likely expects US format)."""
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
class MiamiProbateCase:
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
    parsed_cases: list[MiamiProbateCase] = field(default_factory=list)
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


def _detail_to_record(detail: MiamiProbateDetail) -> ProbateRecord:
    """Convert a Miami case-detail page into a ProbateRecord row.

    Miami gives us 11 of 12 columns directly from the case detail page.
    Only Fiduciary Email comes from an application PDF (deferred).

    Mapping (Miami → DM probate schema):
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


_MIAMI_CASE_URL_RE = re.compile(
    r'/recordSearch\.php\?k=case5503[^"\']+',
)
_MIAMI_DOCKET_URL_RE = re.compile(
    r'/recordSearch\.php\?k=docket5503[^"\']+',
)


@dataclass
class MiamiProbateCaseV2:
    """Richer per-case structure parsed from the results page table."""
    case_number: str
    decedent_name: str = ""
    date_filed: str = ""
    case_type: str = ""            # "Estate" etc.
    case_url: str = ""             # /recordSearch.php?k=case5503...
    docket_url: str = ""           # /recordSearch.php?k=docket5503...


def parse_results_html(html: str) -> list[MiamiProbateCase]:
    """Parse Miami probate results — Caselook PHP DOM.

    Confirmed structure (build 0.1.52 recon, 5/26/2026, 17 cases):
      <div class="record">
        <div class="caseTitle">
          <span class="caseCounter">1</span>
          <span class="fullCaseNumber">E20261216</span>
          <span class="concerningName">Brown, Matthew J.</span>
        </div>
        <div class="caseInfo">
          <div class="caseField fileDate"><label>Filed:</label> 05/26/2026</div>
          <div class="caseField caseType"><label>Case Type:</label> Estate</div>
          <a href="/recordSearch.php?k=case5503..." class="caseLink ...">Case</a>
          <a href="/recordSearch.php?k=docket5503..." class="docketLink ...">Docket</a>
        </div>
      </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    cases: list[MiamiProbateCase] = []
    seen: set[str] = set()

    for record in soup.find_all("div", class_="record"):
        # Case number (E-prefixed)
        case_num_span = record.find("span", class_="fullCaseNumber")
        if not case_num_span:
            continue
        case_number = case_num_span.get_text(strip=True)
        if not case_number or not CASE_NUMBER_RE.fullmatch(case_number):
            continue
        if case_number in seen:
            continue
        seen.add(case_number)

        # Decedent name
        name_span = record.find("span", class_="concerningName")
        decedent = name_span.get_text(strip=True) if name_span else ""

        # File date
        date_div = record.find("div", class_="fileDate")
        date_filed = ""
        if date_div:
            txt = date_div.get_text(" ", strip=True)
            m = re.search(r"(\d{2}/\d{2}/\d{4})", txt)
            if m:
                try:
                    date_filed = datetime.strptime(
                        m.group(1), "%m/%d/%Y"
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    pass

        # Case + Docket URLs
        case_a = record.find("a", class_="caseLink")
        docket_a = record.find("a", class_="docketLink")
        case_url = case_a.get("href", "") if case_a else ""
        docket_url = docket_a.get("href", "") if docket_a else ""

        cases.append(MiamiProbateCase(
            case_number=case_number,
            decedent_name=decedent,
            date_filed=date_filed,
            detail_url=case_url,
            # Stash docket URL in raw_row_text per scraper convention
            raw_row_text=docket_url,
        ))

    return cases


# ── The scraper ─────────────────────────────────────────────────────────

class MiamiProbateScraper:
    """Playwright scraper for Miami County Probate Court (PHP).

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
        captcha_api_key: str = "",
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
        self.captcha_api_key = captcha_api_key
        self.log = logger if logger else _StdoutLog()
        self.recon: ReconCapture = ReconCapture()

    async def run(self) -> None:
        self.log.info(
            f"MiamiProbateScraper start | mode={self.mode} | "
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

                # Miami's Caselook portal only accepts one date per search,
                # so we iterate each day in the window. CAPTCHA is required
                # per page load (per search) — costs ~$0.0005 per day.
                # Case-detail URLs are session-scoped, so capture details
                # immediately after each day's search.
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
            accept_url = "https://probate.miamicountyohio.gov" + m.group(1)
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

    async def _solve_captcha(self, page: Page) -> None:
        """Capture Miami's CAPTCHA image, solve via 2Captcha, fill response.

        DOM (confirmed via recon v2):
          <img id="captchaImage" src="/captcha/showCaptcha.php?m=image">
          <input name="captchaResponse" id="captchaResponse" maxlength="6">
        """
        from captcha.twocaptcha import (
            get_api_key, solve_image_captcha, TwoCaptchaError,
        )
        api_key = get_api_key(self.captcha_api_key)
        if not api_key:
            self.log.warning(
                "No 2Captcha API key — Miami search will fail without it"
            )
            self._dlog("captcha_no_api_key")
            return

        # Fetch the CAPTCHA image via the page's request context (uses cookies)
        captcha_url = f"{PORTAL_HOST}/captcha/showCaptcha.php?m=image"
        try:
            self.log.info("Fetching CAPTCHA image ...")
            resp = await page.context.request.get(captcha_url, timeout=15000)
            if not resp.ok:
                self.log.warning(f"CAPTCHA image fetch HTTP {resp.status}")
                self._dlog("captcha_image_fetch_failed",
                           status=resp.status)
                return
            image_bytes = await resp.body()
            self._dlog("captcha_image_fetched", bytes=len(image_bytes))
        except Exception as e:
            self.log.warning(f"CAPTCHA image fetch error: {e}")
            self._dlog("captcha_image_fetch_error", error=str(e))
            return

        # Solve via 2Captcha
        try:
            self.log.info("Submitting CAPTCHA to 2Captcha ...")
            answer = await solve_image_captcha(
                image_bytes,
                api_key=api_key,
                case_sensitive=False,    # most court CAPTCHAs are case-insensitive
                min_length=4,
                max_length=6,
                logger=self.log,
            )
            self._dlog("captcha_solved", answer_length=len(answer))
        except TwoCaptchaError as e:
            self.log.warning(f"2Captcha failed: {e}")
            self._dlog("captcha_solve_failed", error=str(e))
            return

        # Fill the response field
        try:
            await page.locator("#captchaResponse").fill(answer)
            self._dlog("captcha_response_filled", value_length=len(answer))
            self.log.info(f"  CAPTCHA answer filled: {answer}")
        except Exception as e:
            self.log.warning(f"CAPTCHA fill error: {e}")
            self._dlog("captcha_fill_error", error=str(e))

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
        """Backwards-compat. Multi-day run() uses _capture_details_for()."""
        n = min(self.capture_case_details, len(self.recon.parsed_cases))
        if n == 0:
            return
        await self._capture_details_for(page, self.recon.parsed_cases[:n])

    async def _search_one_day(self, page: Page, iso_date: str) -> list:
        """Run search for one day (solving captcha) and return parsed cases.

        Clears session cookies between days so Caselook returns fresh
        per-day results (otherwise the portal caches and returns the same
        case for every day). Costs us one captcha solve per day.
        """
        self.log.info(f"  Day {iso_date}: searching...")
        # Clear session — Caselook caches results in the session
        await page.context.clear_cookies()
        await page.goto(PORTAL_URL, wait_until="domcontentloaded",
                         timeout=30000)
        await page.wait_for_timeout(1500)
        # After cookie-clear: disclaimer is back, must accept
        if await page.locator("a:has-text('Continue')").count() > 0:
            await self._navigate_to_search(page)

        await self._fill_search_form(page, iso_date)
        # Captcha must be solved fresh per search submit
        await self._solve_captcha(page)
        await self._submit_search(page)
        html = await page.content()
        day_cases = parse_results_html(html)
        self.recon.parsed_cases.extend(day_cases)
        self.recon.results_html = html
        self._dlog("day_results",
                   date=iso_date, cases_found=len(day_cases))
        self.log.info(f"  Day {iso_date}: {len(day_cases)} Estate case(s)")
        return day_cases

    async def _capture_details_for(
        self, page: Page, cases: list
    ) -> None:
        """For each given case, fetch case-detail + docket HTML, parse into
        ProbateRecord. Called per-day so session URL tokens are fresh."""
        if not cases:
            return
        self.log.info(
            f"    Capturing {len(cases)} case detail page(s) ..."
        )

        base_prefix = "https://probate.miamicountyohio.gov"
        for i, case in enumerate(cases):
            cap = CaseDetailCapture(case_number=case.case_number)
            detail: MiamiProbateDetail | None = None
            # Open in a fresh tab so URL tokens stay independent (Caselook
            # corrupts URL tokens when navigated sequentially on same page).
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
                        # Butler's parser extracts case_number via PE-regex,
                        # but Miami uses E-prefix format. Override with the
                        # case number we already got from the results page.
                        if not detail.case_number:
                            detail.case_number = case.case_number
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

            # Build the structured ProbateRecord from the parsed case detail.
            # Email + Action remain blank for now (need docket PDF parsing).
            if detail:
                rec = _detail_to_record(detail)
                self.recon.probate_records.append(rec)

            # Close fresh tab so URL tokens stay isolated per case
            await case_page.close()

            await page.wait_for_timeout(500)

    async def _capture_results(self, page: Page) -> None:
        self.log.info("Capturing results page HTML + screenshot")
        self.recon.results_html = await page.content()
        self.recon.results_screenshot = await page.screenshot(full_page=True)

        cases = parse_results_html(self.recon.results_html)
        if len(cases) > self.max_cases:
            cases = cases[: self.max_cases]
        self.recon.parsed_cases = cases
        self.log.info(
            f"Parsed {len(cases)} Miami probate case numbers from results."
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
