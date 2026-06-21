"""Warren County (Ohio) Probate Court — probate.co.warren.oh.us scraper.

Different URL pattern than Butler/Miami: uses `/search.php` instead of
`/recordSearch.php`. Could be the same PHP vendor with renaming, OR a
completely different system. First recon will tell us.

Per H3 SOP H3-SOP-WCO-002 (rough outline):
  1. GET https://probate.co.warren.oh.us/search.php
  2. Search by date / select case type
  3. Open each case → extract Decedent, DOD, Property, Fiduciary
  4. Open docket → application PDF → extract fiduciary email

This first version is recon-mode only — captures the landing page so we
can identify the DOM and plan the form-fill flow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse


def _dates_in_range(date_from: str, date_to: str) -> list[str]:
    """List of ISO dates inclusive [date_from, date_to]."""
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

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from h3.output_writers.probate_format import ProbateRecord
from h3.parsers.warren_probate_case_detail import (
    WarrenProbateDetail,
    parse_case_detail,
)


def _combine_address(line1: str, line2: str) -> str:
    parts = [p.strip() for p in (line1, line2) if p and p.strip()]
    return ", ".join(parts)


def _detail_to_record(detail: WarrenProbateDetail) -> ProbateRecord:
    """Convert Warren case-detail → ProbateRecord. 11/12 cols populated;
    only Fiduciary Email requires PDF parsing (future)."""
    return ProbateRecord(
        case_number=detail.case_number,
        case_type=detail.fiduciary_type,        # ADM/EXR — placeholder
        date_filed=detail.file_date,
        decedent_name=detail.decedent_name,
        date_of_death=detail.date_of_death,
        action="",                               # TODO from docket
        relationship=detail.fiduciary_relationship,
        fiduciary_name=detail.fiduciary_name,
        fiduciary_address=_combine_address(
            detail.fiduciary_address, detail.fiduciary_city_state_zip,
        ),
        fiduciary_phone=detail.fiduciary_phone,
        fiduciary_email="",                      # TODO from PDF
        subject_property=_combine_address(
            detail.decedent_address, detail.decedent_city_state_zip,
        ),
        notes=(
            f"Atty: {detail.attorney_name}"
            + (f" ({detail.attorney_phone})"
               if detail.attorney_phone else "")
        ),
    )


PORTAL_URL = "https://probate.co.warren.oh.us/search.php"
PORTAL_HOST = "https://probate.co.warren.oh.us"

# Warren probate Estate case numbers are 8-digit pure-numeric (e.g. 20261334).
# The URL pattern `pre=PE` distinguishes Estate from other probate types:
#   Estate    → pre=PE
#   Marriage  → pre=PR
#   Minor     → pre=PM
#   Guardianship → pre=PG (likely)
WARREN_ESTATE_LINK_RE = re.compile(
    r'href=["\']pcaseno\.cgi\?pre=PE&(?:amp;)?num=(\d+)[^"\']*["\']',
    re.I,
)
WARREN_ESTATE_DOCKET_RE = re.compile(
    r'href=["\']pdocket\.cgi\?pre=PE&(?:amp;)?num=(\d+)[^"\']*["\']',
    re.I,
)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _parse_proxy_url(url: str) -> dict[str, str]:
    p = urlparse(url)
    return {
        "server": f"{p.scheme}://{p.hostname}:{p.port}",
        "username": p.username or "",
        "password": p.password or "",
    }


@dataclass
class WarrenProbateCase:
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
    error: str = ""


@dataclass
class ReconCapture:
    landing_html: str = ""
    landing_screenshot: bytes = b""
    results_html: str = ""
    results_screenshot: bytes = b""
    parsed_cases: list[WarrenProbateCase] = field(default_factory=list)
    case_details: list[CaseDetailCapture] = field(default_factory=list)
    debug_log: list[dict[str, Any]] = field(default_factory=list)
    probate_records: list[ProbateRecord] = field(default_factory=list)


def parse_results_html(html: str) -> list[WarrenProbateCase]:
    """Parse Warren probate results page and filter to Estate cases only.

    Each result is a 2-column <tr>:
      Left:  Concerning: <name>, Also: <name>, Filed: <date>
      Right: Case: <link><num></link>, Docket: <link>Click</link>,
             Case Type: <Estate/Marriage/etc.>

    Filtering: we keep only rows whose link href contains `pre=PE`
    (Estate prefix). Marriage (PR), Minor (PM), Guardianship (PG) are dropped.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    cases: list[WarrenProbateCase] = []
    seen: set[str] = set()

    for tr in soup.find_all("tr"):
        row_html = str(tr)
        # Estate filter — only rows with a pre=PE link
        link_m = WARREN_ESTATE_LINK_RE.search(row_html)
        if not link_m:
            continue
        case_number = link_m.group(1)
        if case_number in seen:
            continue
        seen.add(case_number)

        # Extract case detail URL
        href_match = re.search(
            r'href=["\'](pcaseno\.cgi\?pre=PE[^"\']*)["\']',
            row_html,
            re.I,
        )
        detail_url = href_match.group(1).replace("&amp;", "&") if href_match else ""

        # Extract docket URL
        docket_match = re.search(
            r'href=["\'](pdocket\.cgi\?pre=PE[^"\']*)["\']',
            row_html,
            re.I,
        )
        docket_url = docket_match.group(1).replace("&amp;", "&") if docket_match else ""

        # Extract decedent name (after "Concerning:")
        concerning_match = re.search(
            r"Concerning:\s*</b>\s*([^<\n]+)",
            row_html,
            re.I,
        )
        decedent = concerning_match.group(1).strip() if concerning_match else ""

        # Extract filed date (after "Filed:")
        filed_match = re.search(
            r"Filed:\s*</b>\s*(\d{2}/\d{2}/\d{4})",
            row_html,
            re.I,
        )
        filed_raw = filed_match.group(1) if filed_match else ""
        if filed_raw:
            try:
                filed_iso = datetime.strptime(
                    filed_raw, "%m/%d/%Y"
                ).strftime("%Y-%m-%d")
            except ValueError:
                filed_iso = ""
        else:
            filed_iso = ""

        case = WarrenProbateCase(
            case_number=case_number,
            decedent_name=decedent,
            date_filed=filed_iso,
            detail_url=detail_url,
            raw_row_text=docket_url,  # stash docket URL here per scraper convention
        )
        cases.append(case)

    return cases


class WarrenProbateScraper:
    """Recon-mode scraper for Warren County Probate Court.

    Until first recon reveals the actual DOM, this just captures the landing
    page HTML and screenshot for inspection. Subsequent versions will add
    form-fill logic based on the discovered structure.
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
        self.date_from = date_from
        self.date_to = date_to
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
            f"WarrenProbateScraper start | mode={self.mode} | "
            f"dates {self.date_from or '-'} → {self.date_to or '-'} | "
            f"headless={self.headless}"
        )
        async with async_playwright() as p:
            browser, ctx = await self._launch_browser(p)
            try:
                page = await ctx.new_page()
                self.log.info(f"GET {PORTAL_URL}")
                resp = await page.goto(
                    PORTAL_URL, wait_until="domcontentloaded", timeout=30000
                )
                self._dlog("goto", url=PORTAL_URL,
                           status=resp.status if resp else 0,
                           final_url=page.url)
                await page.wait_for_timeout(2000)
                self.recon.landing_html = await page.content()
                self.recon.landing_screenshot = await page.screenshot(
                    full_page=True
                )
                self._dlog("landing_captured",
                           html_bytes=len(self.recon.landing_html))

                # Warren's Henschen CGI form takes a single date per search.
                # Iterate each day in the window. No CAPTCHA, no disclaimer
                # — just re-hit the portal between days.
                if self.date_from:
                    try:
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

    async def _search_one_day(self, page: Page, iso_date: str) -> list:
        """Run search for one day and return parsed cases.

        Clears session cookies between days so the portal returns fresh
        per-day results instead of cached previous-day data.
        """
        self.log.info(f"  Day {iso_date}: searching...")
        # Clear session so we get fresh per-day results
        await page.context.clear_cookies()
        await page.goto(PORTAL_URL, wait_until="domcontentloaded",
                         timeout=30000)
        await page.wait_for_timeout(1500)

        # Stash the date_from temporarily so _fill_and_search uses this day
        original_from = self.date_from
        original_to = self.date_to
        self.date_from = iso_date
        self.date_to = iso_date
        try:
            await self._fill_and_search(page, iso_date)
        finally:
            self.date_from = original_from
            self.date_to = original_to

        html = await page.content()
        day_cases = parse_results_html(html)
        self.recon.parsed_cases.extend(day_cases)
        self.recon.results_html = html
        self._dlog("day_results", date=iso_date,
                   cases_found=len(day_cases))
        self.log.info(f"  Day {iso_date}: {len(day_cases)} Estate case(s)")
        return day_cases

    async def _capture_details_for(
        self, page: Page, cases: list
    ) -> None:
        """Capture detail HTML + parse to ProbateRecord for given cases."""
        if not cases:
            return
        self.log.info(
            f"    Capturing {len(cases)} case detail page(s) ..."
        )
        for i, case in enumerate(cases):
            if not case.detail_url:
                continue
            if case.detail_url.startswith("http"):
                url = case.detail_url
            elif case.detail_url.startswith("/"):
                url = PORTAL_HOST + case.detail_url
            else:
                url = PORTAL_HOST + "/cgi-bin/" + case.detail_url
            cap = CaseDetailCapture(case_number=case.case_number)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded",
                                        timeout=20000)
                await page.wait_for_timeout(1500)
                cap.html = await page.content()
                cap.final_url = page.url
                self._dlog("case_detail_captured",
                           case_number=case.case_number,
                           status=resp.status if resp else 0,
                           html_bytes=len(cap.html))
                try:
                    detail = parse_case_detail(cap.html)
                    rec = _detail_to_record(detail)
                    self.recon.probate_records.append(rec)
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
                               case_number=case.case_number, error=str(e))
            except Exception as e:
                cap.error = str(e)
                self._dlog("case_detail_error",
                           case_number=case.case_number, error=str(e))
            self.recon.case_details.append(cap)
            await page.wait_for_timeout(500)

    async def _capture_case_details(self, page: Page) -> None:
        """Backwards-compat: capture details for first N parsed cases."""
        n = min(self.capture_case_details, len(self.recon.parsed_cases))
        if n == 0:
            return
        self.log.info(f"Phase 2: capturing {n} case detail pages ...")
        for i, case in enumerate(self.recon.parsed_cases[:n]):
            if not case.detail_url:
                continue
            # Warren's relative URLs are relative to /cgi-bin/ NOT root
            if case.detail_url.startswith("http"):
                url = case.detail_url
            elif case.detail_url.startswith("/"):
                url = PORTAL_HOST + case.detail_url
            else:
                url = PORTAL_HOST + "/cgi-bin/" + case.detail_url

            cap = CaseDetailCapture(case_number=case.case_number)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded",
                                        timeout=20000)
                await page.wait_for_timeout(1500)
                cap.html = await page.content()
                cap.final_url = page.url
                self._dlog("case_detail_captured",
                           case_number=case.case_number,
                           status=resp.status if resp else 0,
                           html_bytes=len(cap.html))
                # Parse the page into a ProbateRecord
                try:
                    detail = parse_case_detail(cap.html)
                    rec = _detail_to_record(detail)
                    self.recon.probate_records.append(rec)
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
                               case_number=case.case_number, error=str(e))
            except Exception as e:
                cap.error = str(e)
                self._dlog("case_detail_error",
                           case_number=case.case_number, error=str(e))
            self.recon.case_details.append(cap)
            await page.wait_for_timeout(500)

    async def _fill_and_search(self, page: Page, iso_date: str) -> None:
        """Fill Warren's File Date form (fmonth/fday/fyear) and click Search.

        Confirmed form spec (build 0.1.37 recon):
          POST /cgi-bin/search.cgi
          fmonth   <select>  zero-padded "01"-"12"
          fday     <select>  zero-padded "01"-"31"
          fyear    <input type="text">  "2026"
          file_type=4, search_type=1, agency_num=8303 are hidden defaults
        """
        try:
            dt = datetime.strptime(iso_date, "%Y-%m-%d")
        except ValueError:
            self._dlog("invalid_date", value=iso_date)
            return
        mm = f"{dt.month:02d}"
        dd = f"{dt.day:02d}"
        yyyy = str(dt.year)
        self.log.info(f"Filling File Date = {mm}/{dd}/{yyyy}")

        for name, value in [("fmonth", mm), ("fday", dd)]:
            try:
                sel = f"select[name='{name}']"
                if await page.locator(sel).count() > 0:
                    await page.locator(sel).first.select_option(value=value)
                    self._dlog("date_select_filled", name=name, value=value)
            except Exception as e:
                self._dlog("date_select_error", name=name, error=str(e))

        # Year is a text input, not a select
        try:
            year_input = page.locator("input[name='fyear']").first
            if await year_input.count() > 0:
                # Clear placeholder ("Year") then fill
                await year_input.fill(yyyy)
                self._dlog("year_filled", value=yyyy)
        except Exception as e:
            self._dlog("year_fill_error", error=str(e))

        # Set the hidden search_type=9 (date search) BEFORE submitting.
        # The form's onclick="pop_radio(N)" JS handlers on each input field
        # would normally do this, but Playwright's select_option/fill don't
        # trigger onclick. Without this, Warren defaults to search_type=1
        # (search by Name) and bounces with "Missing search criteria".
        try:
            await page.evaluate(
                """() => {
                    const el = document.querySelector("input[name='search_type']");
                    if (el) el.value = '9';
                }"""
            )
            self._dlog("search_type_set_to_9")
        except Exception as e:
            self._dlog("search_type_set_error", error=str(e))

        # Click Search (submit input named 'submit')
        self.log.info("Submitting search ...")
        for sel in [
            "input[type='submit'][value='Search']",
            "input[type='submit'][name='submit']",
            "input[type='submit']",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    self._dlog("search_submitted", selector=sel)
                    break
            except Exception:
                continue

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

    async def _capture_results(self, page: Page) -> None:
        self.log.info("Capturing results page HTML + screenshot")
        self.recon.results_html = await page.content()
        self.recon.results_screenshot = await page.screenshot(full_page=True)
        self.recon.parsed_cases = parse_results_html(self.recon.results_html)
        self.log.info(
            f"Parsed {len(self.recon.parsed_cases)} Estate cases "
            f"(filtered from all probate types)"
        )
        self._dlog(
            "results_captured",
            html_bytes=len(self.recon.results_html),
            estate_cases=[c.case_number for c in self.recon.parsed_cases],
        )

    async def _launch_browser(
        self, p: Playwright
    ) -> tuple[Browser, BrowserContext]:
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.proxy_url:
            launch_kwargs["proxy"] = _parse_proxy_url(self.proxy_url)
        browser = await p.chromium.launch(**launch_kwargs)
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=DEFAULT_UA,
            locale="en-US",
            timezone_id="America/New_York",
            # Warren's portal serves an invalid certificate
            # (ERR_CERT_AUTHORITY_INVALID). It's a government site, low
            # security risk, but Playwright refuses to connect by default.
            ignore_https_errors=True,
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        return browser, ctx

    def _dlog(self, event: str, **kwargs: Any) -> None:
        self.recon.debug_log.append({
            "event": event,
            "ts": datetime.utcnow().isoformat() + "Z",
            **kwargs,
        })


class _StdoutLog:
    def info(self, msg: str) -> None: print(f"[INFO] {msg}")
    def warning(self, msg: str) -> None: print(f"[WARN] {msg}")
    def error(self, msg: str) -> None: print(f"[ERROR] {msg}")
