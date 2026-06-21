"""Greene County (Ohio) Probate Court — eservices.greeneclerk.org/probate scraper.

CourtView (JWorks) family. First recon will reveal:
  - Whether a captcha gate exists
  - The search-form structure
  - Case-detail URL pattern
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from h3.output_writers.probate_format import ProbateRecord


PORTAL_URL = "https://courts.greenecountyohio.gov/probatejw"
SEARCH_URL = "https://courts.greenecountyohio.gov/probatejw/casesearch"
PORTAL_HOST = "https://courts.greenecountyohio.gov"

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
class GreeneProbateCase:
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
    parsed_cases: list[GreeneProbateCase] = field(default_factory=list)
    case_details: list[CaseDetailCapture] = field(default_factory=list)
    debug_log: list[dict[str, Any]] = field(default_factory=list)
    probate_records: list[ProbateRecord] = field(default_factory=list)


# ── Results parser + helpers ───────────────────────────────────────────

# Greene case number format: YYYY-EST-NNNN (e.g. 2026-EST-0177)
GREENE_CASE_NUMBER_RE = re.compile(r"\b\d{4}-EST-\d{1,5}\b")


def parse_results_html(html: str) -> list[GreeneProbateCase]:
    """Parse equivant CourtView results page.

    Each result row has:
      <a href="?x=<wicket-token>" id="grid~row-N~cell-3$link">
        <i>...</i>2026-EST-0177
      </a>
    and additional <td> cells for Case Type, Filed, Initiating Action, etc.

    Returns one GreeneProbateCase per unique case number, with detail_url
    set to the `?x=<token>` href that navigates to the case detail page.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    cases: list[GreeneProbateCase] = []
    seen: set[str] = set()

    # Walk every <a> that wraps a case-number text
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        m = GREENE_CASE_NUMBER_RE.search(text)
        if not m:
            continue
        case_number = m.group(0)
        if case_number in seen:
            continue
        seen.add(case_number)

        detail_url = a.get("href", "")
        # The row this <a> lives in also contains File Date + other cells
        tr = a.find_parent("tr")
        date_filed = ""
        if tr:
            tr_text = tr.get_text(" ", strip=True)
            date_m = re.search(
                r"(\d{1,2}/\d{1,2}/\d{4})", tr_text
            )
            if date_m:
                try:
                    date_filed = datetime.strptime(
                        date_m.group(1), "%m/%d/%Y"
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    pass

        cases.append(GreeneProbateCase(
            case_number=case_number,
            date_filed=date_filed,
            detail_url=detail_url,
        ))

    return cases


def _combine_address(line1: str, line2: str) -> str:
    parts = [p.strip() for p in (line1, line2) if p and p.strip()]
    return ", ".join(parts)


# Greene case detail parser — equivant CourtView DOM (lazy import + simple
# walker; placeholder until we see real case-detail HTML).
def parse_case_detail(html: str):
    """Parse a Greene equivant case-detail page.

    DOM not yet confirmed — first iteration uses BeautifulSoup to grab any
    visible label-value pairs we recognise. Refine after first recon.
    """
    from parsers.greene_probate_case_detail import (
        parse_case_detail as _parse,
    )
    return _parse(html)


def _detail_to_record(detail) -> ProbateRecord:
    return ProbateRecord(
        case_number=detail.case_number,
        case_type=detail.case_type,
        date_filed=detail.file_date,
        decedent_name=detail.decedent_name,
        date_of_death=detail.date_of_death,
        action="",
        relationship=detail.fiduciary_relationship,
        fiduciary_name=detail.fiduciary_name,
        fiduciary_address=_combine_address(
            detail.fiduciary_address, detail.fiduciary_city_state_zip,
        ),
        fiduciary_phone=detail.fiduciary_phone,
        fiduciary_email="",
        subject_property=_combine_address(
            detail.decedent_address, detail.decedent_city_state_zip,
        ),
    )


class GreeneProbateScraper:
    """Recon-mode scraper for Greene Probate Court (CourtView family).

    First version captures landing page only. Iterate based on actual DOM.
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
            f"GreeneProbateScraper start | mode={self.mode} | "
            f"dates {self.date_from or '-'} → {self.date_to or '-'}"
        )
        async with async_playwright() as p:
            browser, ctx = await self._launch_browser(p)
            try:
                page = await ctx.new_page()
                # Hit landing first to set any session cookies / accept terms
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

                # Now navigate to the case search form
                self.log.info(f"GET {SEARCH_URL}")
                resp = await page.goto(
                    SEARCH_URL, wait_until="domcontentloaded", timeout=30000
                )
                self._dlog("search_page_goto",
                           status=resp.status if resp else 0,
                           final_url=page.url)
                await page.wait_for_timeout(2000)

                # Submit search if we have a date
                if self.date_from:
                    try:
                        await self._fill_and_search(page, self.date_from)
                    except Exception as e:
                        self.log.warning(f"Search failed: {e}")
                        self._dlog("search_error", error=str(e))

                # Capture whatever page we ended up on (search form OR results)
                self.recon.results_html = await page.content()
                self.recon.results_screenshot = await page.screenshot(
                    full_page=True
                )
                self._dlog("final_page_captured",
                           html_bytes=len(self.recon.results_html),
                           final_url=page.url)

                # Parse the results table for case rows. Iterate pagination
                # until no more Next-page link is enabled. Each page has 25
                # ROWS but cases repeat per-party, so 25 rows can be only
                # 6-10 unique cases — we need pagination for any volume.
                all_html_parts = [self.recon.results_html]
                seen_case_numbers: set[str] = set()
                page_num = 1
                while True:
                    page_cases = parse_results_html(all_html_parts[-1])
                    fresh_cases = [c for c in page_cases
                                    if c.case_number not in seen_case_numbers]
                    for c in fresh_cases:
                        seen_case_numbers.add(c.case_number)
                        self.recon.parsed_cases.append(c)
                    self.log.info(
                        f"  Page {page_num}: {len(page_cases)} cases parsed "
                        f"({len(fresh_cases)} new). Total unique: "
                        f"{len(self.recon.parsed_cases)}"
                    )
                    # Try to click Next Page
                    next_page = page.locator(
                        "a[title='Go to next page']"
                    ).first
                    if await next_page.count() == 0:
                        break
                    # If the Next-page link is disabled, we're done. It's a
                    # <span> instead of <a> when disabled, so the locator
                    # above wouldn't match a span anyway.
                    try:
                        await next_page.click(timeout=5000)
                    except Exception as e:
                        self.log.info(f"  No more pages (or click failed): {e}")
                        break
                    await page.wait_for_timeout(3000)
                    new_html = await page.content()
                    if new_html == all_html_parts[-1]:
                        # Same content — pagination didn't actually advance
                        break
                    all_html_parts.append(new_html)
                    page_num += 1
                    if page_num > 10:  # safety stop
                        self.log.warning("Pagination safety stop at page 10")
                        break

                self.log.info(
                    f"Parsed {len(self.recon.parsed_cases)} unique Estate "
                    f"cases across {page_num} page(s)"
                )

                # Optionally navigate to first N case detail pages
                if self.capture_case_details > 0:
                    await self._capture_case_details(page)
            finally:
                await ctx.close()
                await browser.close()

    async def _capture_case_details(self, page: Page) -> None:
        """Navigate to first N case detail pages and capture HTML."""
        n = min(self.capture_case_details, len(self.recon.parsed_cases))
        if n == 0:
            return
        self.log.info(f"Phase 2: capturing {n} case detail pages ...")

        # Save current search results URL so we can use relative ?x= tokens
        base_url = page.url.split("?")[0]   # strip query, keep path

        for i, case in enumerate(self.recon.parsed_cases[:n]):
            if not case.detail_url:
                self._dlog("no_detail_url", case_number=case.case_number)
                continue
            # Wicket detail_urls are relative ?x=<token> appended to current path
            if case.detail_url.startswith("http"):
                url = case.detail_url
            elif case.detail_url.startswith("?"):
                url = base_url + case.detail_url
            elif case.detail_url.startswith("/"):
                url = PORTAL_HOST + case.detail_url
            else:
                url = base_url + "?" + case.detail_url

            cap = CaseDetailCapture(case_number=case.case_number)
            try:
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=20000
                )
                await page.wait_for_timeout(2000)
                cap.html = await page.content()
                cap.final_url = page.url
                self._dlog("case_detail_captured",
                           case_number=case.case_number,
                           status=resp.status if resp else 0,
                           html_bytes=len(cap.html))
                # Parse into ProbateRecord using Greene's case-detail parser
                try:
                    detail = parse_case_detail(cap.html)
                    if not detail.case_number:
                        detail.case_number = case.case_number
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
                               case_number=case.case_number,
                               error=str(e))
            except Exception as e:
                cap.error = str(e)
                self._dlog("case_detail_error",
                           case_number=case.case_number, error=str(e))

            self.recon.case_details.append(cap)
            await page.wait_for_timeout(500)

    async def _fill_and_search(self, page: Page, iso_date: str) -> None:
        """Fill Greene's equivant search form and submit.

        Uses self.date_from and self.date_to as the date range (Greene's
        equivant supports real date ranges — unlike Butler/Miami/Warren
        which take single dates only).
        """
        # Use date_from → date_to (range), defaulting end to same as begin
        try:
            begin_dt = datetime.strptime(self.date_from, "%Y-%m-%d")
        except (ValueError, TypeError):
            return
        try:
            end_dt = datetime.strptime(self.date_to, "%Y-%m-%d")
        except (ValueError, TypeError):
            end_dt = begin_dt
        us_begin = begin_dt.strftime("%m/%d/%Y")
        us_end = end_dt.strftime("%m/%d/%Y")
        self.log.info(
            f"Filling File Date range = {us_begin} → {us_end}, "
            f"Case Type = Estate"
        )

        # Switch from "Name" tab (default, requires Last/First Name) to the
        # "Case Type" tab which has no required text fields.
        self.log.info("Switching to Case Type tab ...")
        for sel in [
            "a:has-text('Case Type')",
            "li:has-text('Case Type') a",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    self._dlog("case_type_tab_clicked", selector=sel)
                    # Wicket fires AJAX on the tab click — wait for it
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        # Select Estate case type
        try:
            sel = "select[name='caseCd']"
            if await page.locator(sel).count() > 0:
                await page.locator(sel).first.select_option(label="Estate")
                self._dlog("case_type_selected", value="Estate")
        except Exception as e:
            self._dlog("case_type_error", error=str(e))

        # Fill file date range (begin + end)
        for field, value in [
            ("fileDateRange:dateInputBegin", us_begin),
            ("fileDateRange:dateInputEnd", us_end),
        ]:
            try:
                sel = f"input[name='{field}']"
                if await page.locator(sel).count() > 0:
                    await page.locator(sel).first.fill(value)
                    # Trigger Wicket's change handler via blur
                    await page.locator(sel).first.blur()
                    self._dlog("date_filled", field=field, value=value)
                    await page.wait_for_timeout(500)
            except Exception as e:
                self._dlog("date_fill_error", field=field, error=str(e))

        # Click Search
        self.log.info("Submitting search ...")
        for sel in [
            "input[type='submit'][value='Search']",
            "input[name='submitLink']",
            "button:has-text('Search')",
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
        await page.wait_for_timeout(3000)

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
