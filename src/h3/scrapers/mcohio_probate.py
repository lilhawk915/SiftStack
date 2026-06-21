"""Montgomery County (Ohio) Probate Court — go.mcohio.org scraper.

DIFFERENT portal from foreclosure side (which is pro.mcohio.org/PROv3 ASP.NET).
This is a ColdFusion-based system (.cfm extension) — simplest of all 7 probate
portals.

Per H3 SOP H3-SOP-MCO-002:
  1. Navigate to https://go.mcohio.org/applications/probate/prodcfm/casesearchx.cfm
  2. Enter Calendar Year (4 digits, e.g. "2026"), leave Case Number blank
  3. Click Search button
  4. Results list appears below — one row per case
  5. For each case row, click into the case detail page
  6. Extract: Decedent Name, Date of Death, Case Type, Fiduciary Name+Address
  7. Click "Click here to view DOCKET" button → opens PDF index
  8. Open the appropriate application PDF (depends on Case Type):
       - "Application for Authority to Administer Estate" — most common
       - "Application for Summary Release"
       - "Application for Release of Administration"
       - "Application for Transfer of Certificate"
       - "Fiduciary Bond"
       - "Notice of Will for Probate"
  9. Extract from PDF: Fiduciary Phone, Email, Subject Property address

WITH WILL vs WITHOUT WILL:
  - WITHOUT WILL: subject property listed on first page of admin application PDF
  - WITH WILL: open "Application for Authority to Administer Estate" specifically

Case number format: YYYYESTNNNNN (e.g. 2026EST00559)

Recon mode just captures the results page HTML + screenshot. Full mode
navigates each case and parses the PDFs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
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

from h3.parsers.probate_case_detail import (
    MontgomeryProbateDetail,
    parse_case_detail,
)
from h3.parsers.probate_docket import (
    DocketEntry,
    parse_docket,
    select_application_pdf,
)
from h3.output_writers.probate_format import ProbateRecord


# ── Portal config (per H3 SOP H3-SOP-MCO-002) ───────────────────────────

PORTAL_URL = (
    "https://go.mcohio.org/applications/probate/prodcfm/casesearchx.cfm"
)

# Case number pattern: YYYYESTNNNNN (e.g. 2026EST00559)
CASE_NUMBER_RE = re.compile(r"\b20\d{2}EST\d{4,6}\b")

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


_DOCKET_HREF_RE = re.compile(
    r'href\s*=\s*["\']([^"\']*CASESEARCH_DOCKETx\.cfm[^"\']*)["\']',
    re.I,
)


def _extract_docket_href(case_detail_html: str) -> str:
    """Find the href of the 'Click here to view DOCKET' link on a case
    detail page. Returns '' if not found."""
    m = _DOCKET_HREF_RE.search(case_detail_html)
    return m.group(1) if m else ""


def _detail_to_record(
    detail: MontgomeryProbateDetail,
    docket_entries: list | None = None,
) -> ProbateRecord:
    """Convert a parsed case-detail page into a ProbateRecord row.

    PDF-derived fields (fiduciary phone, email, subject property) are left
    blank for now — added in a follow-up iteration once docket PDF parsing
    is implemented.

    date_filed precedence (most accurate first):
      1. Earliest docket entry date (if docket_entries provided)
      2. Appointment date (close approximation of filing for OPEN cases)
      3. case_status_date (for OPEN cases this IS file date; for CLOSED
         this is the closure date — least accurate but always available)
    """
    # Pick the best filing-date estimate
    filing_date = ""
    if docket_entries:
        # Earliest docket entry by date (ISO sort works for YYYY-MM-DD)
        dated = [e.date for e in docket_entries if e.date]
        if dated:
            filing_date = min(dated)
    if not filing_date and detail.appointment_date:
        filing_date = detail.appointment_date
    if not filing_date:
        filing_date = detail.case_status_date

    return ProbateRecord(
        case_number=detail.case_number,
        case_type=detail.case_type,
        date_filed=filing_date,
        decedent_name=detail.decedent_name,
        date_of_death=detail.date_of_death,
        action="",
        relationship="",
        fiduciary_name=detail.fiduciary_name,
        fiduciary_address=detail.fiduciary_address,
        fiduciary_phone="",
        fiduciary_email="",
        subject_property="",
        co_fiduciary_name=detail.co_fiduciary_name,
        co_fiduciary_address=detail.co_fiduciary_address,
        notes=(
            f"Status: {detail.case_status}"
            + (f"; Appointed {detail.appointment_date}"
               if detail.appointment_date else "")
            + (f"; Atty: {detail.attorney_name} ({detail.attorney_phone})"
               if detail.attorney_name else "")
        ),
    )


# ── Parsed data structures ──────────────────────────────────────────────

@dataclass
class MontgomeryProbateCase:
    """One probate case row from the search results page."""
    case_number: str
    decedent_name: str = ""
    date_filed: str = ""              # ISO YYYY-MM-DD (only known after case detail navigation)
    case_type: str = ""               # e.g. "ESTATE WITH WILL"
    case_id: str = ""                 # internal numeric ID (e.g. "342333")
    detail_url: str = ""              # encrypted-token URL to case detail page
    raw_row_text: str = ""


@dataclass
class CaseDetailCapture:
    case_number: str
    final_url: str = ""
    html: str = ""
    docket_html: str = ""             # the docket-page HTML if we navigate there
    docket_entries: list[DocketEntry] = field(default_factory=list)
    # The PDF we decided to download for fiduciary-contact extraction
    application_pdf_description: str = ""
    application_pdf_url: str = ""
    application_pdf_bytes: bytes = b""
    error: str = ""


@dataclass
class ReconCapture:
    results_html: str = ""
    results_screenshot: bytes = b""
    parsed_cases: list[MontgomeryProbateCase] = field(default_factory=list)
    case_details: list[CaseDetailCapture] = field(default_factory=list)
    debug_log: list[dict[str, Any]] = field(default_factory=list)
    # Parsed structured records (one per captured case detail page).
    # Populated by the scraper after each case-detail HTML is fetched.
    probate_records: list[ProbateRecord] = field(default_factory=list)


# ── Parser ──────────────────────────────────────────────────────────────

def parse_results_html(html: str) -> list[MontgomeryProbateCase]:
    """Parse the Montgomery probate results page table.

    Structure (confirmed via v2 recon):
      <table> with 343 rows (1 header + 342 cases)
      Each data row: 3 <td> cells = [Case ID link, Case Number, Case Name]
      The Case ID cell contains <a href="casesearchresultx.cfm?<token>"> which
      navigates to the case detail page.
    """
    soup = BeautifulSoup(html, "html.parser")
    cases: list[MontgomeryProbateCase] = []
    seen: set[str] = set()

    # Find the data table (the one with header "Case ID", "Case Number", "Case Name")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        # Check header row to make sure this is the data table
        header_text = rows[0].get_text(" ", strip=True).upper()
        if "CASE NUMBER" not in header_text:
            continue

        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 3:
                continue

            # Cell 0: Case ID + detail link
            id_cell = cells[0]
            case_id_text = id_cell.get_text(strip=True)
            link = id_cell.find("a", href=True)
            detail_href = link.get("href", "") if link else ""

            # Cell 1: Case Number
            case_number = cells[1].get_text(strip=True)
            # Cell 2: Decedent name
            decedent = cells[2].get_text(strip=True)

            if not case_number or not CASE_NUMBER_RE.fullmatch(case_number):
                continue
            if case_number in seen:
                continue
            seen.add(case_number)

            cases.append(MontgomeryProbateCase(
                case_number=case_number,
                decedent_name=decedent,
                case_id=case_id_text,
                detail_url=detail_href,
            ))
        break  # only process the first matching table

    # Fallback: if table parsing failed, regex over raw text
    if not cases:
        text = soup.get_text(" ", strip=True)
        for m in CASE_NUMBER_RE.finditer(text):
            cn = m.group(0)
            if cn in seen:
                continue
            seen.add(cn)
            cases.append(MontgomeryProbateCase(
                case_number=cn,
                raw_row_text=text[max(0, m.start()-50):m.end()+200].strip(),
            ))

    return cases


# ── The scraper ─────────────────────────────────────────────────────────

class MontgomeryProbateScraper:
    """Playwright scraper for Montgomery County Probate Court (ColdFusion)."""

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

    def _year_to_search(self) -> str:
        """Calendar year to enter in the search form. Per SOP, just enter
        a 4-digit year. If date_from given, use its year. Else current year."""
        if self.date_from and len(self.date_from) >= 4:
            return self.date_from[:4]
        return datetime.utcnow().strftime("%Y")

    async def run(self) -> None:
        year = self._year_to_search()
        self.log.info(
            f"MontgomeryProbateScraper start | mode={self.mode} | "
            f"year={year} | date filter {self.date_from or '-'} → {self.date_to or '-'} | "
            f"headless={self.headless} | capture_case_details={self.capture_case_details}"
        )
        async with async_playwright() as p:
            browser, ctx = await self._launch_browser(p)
            try:
                page = await ctx.new_page()
                await self._goto_portal(page)
                await self._fill_year_and_search(page, year)
                await self._capture_results(page)

                if self.capture_case_details > 0:
                    await self._capture_case_details_pages(page)
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

    async def _fill_year_and_search(self, page: Page, year: str) -> None:
        """Fill the Calendar Year field and click Search.

        ColdFusion forms typically use named inputs; common patterns:
          - input[name='caseyear'] or input[name='year']
          - input[name='calyear']
        Will refine after first recon.
        """
        self.log.info(f"Filling Calendar Year = {year}")
        for sel in [
            "input[name*='year' i]",
            "input[name*='calyear' i]",
            "input[id*='year' i]",
            "select[name*='year' i]",
        ]:
            try:
                if await page.locator(sel).count() > 0:
                    elem = page.locator(sel).first
                    tag = await elem.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        await elem.select_option(year)
                    else:
                        await elem.fill(year)
                    self._dlog("year_filled", selector=sel, value=year)
                    self.log.info(f"  year filled via {sel}")
                    break
            except Exception as e:
                self._dlog("year_fill_error", selector=sel, error=str(e))
                continue

        # Click the GO button (NOT "Search" — the SOP misnames it).
        # The portal has TWO forms on the page:
        #   1. Estate Type Search (Case Year + Case Number) → first GO button
        #   2. Name Search (Last + First Name) → second GO button
        # We want the FIRST GO button (scoped to the form that has Case Year).
        self.log.info("Submitting search via GO button ...")
        submitted = False
        for sel in [
            # Scoped: GO button inside the form containing the year input
            "xpath=//input[contains(@name,'year') or contains(@name,'Year') or contains(@id,'year')]/ancestor::form[1]//input[@type='submit'][contains(translate(@value,'go','GO'),'GO')]",
            # GO button (anywhere) — first one
            "input[type='submit'][value='GO']",
            "input[type='button'][value='GO']",
            "button:has-text('GO')",
            # Legacy "Search" fallback (in case portal changes)
            "input[type='submit'][value*='Search' i]",
            "button[type='submit']",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    self._dlog("search_submitted", selector=sel)
                    self.log.info(f"  GO clicked via: {sel[:80]}")
                    submitted = True
                    break
            except Exception as e:
                self._dlog("search_submit_error", selector=sel, error=str(e))
                continue
        if not submitted:
            self._dlog("search_submit_failed",
                       note="No GO/Search button matched any selector")
            self.log.warning("Could not find GO button — search did not submit")

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)

    # ── Capture ─────────────────────────────────────────────────────

    async def _capture_results(self, page: Page) -> None:
        self.log.info("Capturing results page HTML + screenshot")
        self.recon.results_html = await page.content()
        self.recon.results_screenshot = await page.screenshot(full_page=True)

        all_cases = parse_results_html(self.recon.results_html)

        # The portal returns ALL cases for the calendar year sorted by case
        # number ascending (00005 → 01137). Weekly runs only care about the
        # MOST RECENT filings (~ last 100-200 cases). If we cap from the
        # head, we'd lose exactly the rows we want. Keep the tail instead.
        if len(all_cases) > self.max_cases:
            self.log.warning(
                f"Capping {len(all_cases)} cases at max_cases={self.max_cases} "
                f"— keeping the most recent (highest case numbers)"
            )
            all_cases = all_cases[-self.max_cases:]
        self.recon.parsed_cases = all_cases

        self.log.info(
            f"Parsed {len(all_cases)} probate case numbers from results page."
        )
        self._dlog("results_parsed",
                   total_cases=len(all_cases),
                   html_bytes=len(self.recon.results_html))

    async def _capture_case_details_pages(self, page: Page) -> None:
        """Phase 2: navigate to N case detail pages, capture HTML.

        Iterates in REVERSE order (newest case numbers first). Montgomery's
        portal returns ALL cases for the year, so when the caller wants the
        latest week, fetching from the end first means the relevant cases
        are captured before hitting the per-run budget.
        """
        n = min(self.capture_case_details, len(self.recon.parsed_cases))
        if n == 0:
            return
        self.log.info(
            f"Phase 2: capturing {n} case detail page(s) "
            f"(newest first) ..."
        )

        # Capture base URL so we can build absolute URLs for the detail-page hrefs
        base_url = page.url
        base_prefix = base_url.rsplit("/", 1)[0] + "/"

        # Reverse the list so newest case # comes first
        ordered = list(reversed(self.recon.parsed_cases))[:n]
        for i, case in enumerate(ordered):
            if not case.detail_url:
                self.log.warning(
                    f"  Case {case.case_number}: no detail_url — skipping"
                )
                continue

            # Build absolute URL
            if case.detail_url.startswith("http"):
                full_url = case.detail_url
            else:
                full_url = base_prefix + case.detail_url

            self.log.info(
                f"  [{i+1}/{n}] {case.case_number} ({case.decedent_name}) ..."
            )
            cap = CaseDetailCapture(case_number=case.case_number,
                                    final_url=full_url)
            try:
                resp = await page.goto(
                    full_url, wait_until="domcontentloaded", timeout=20000
                )
                await page.wait_for_timeout(2000)
                cap.html = await page.content()
                cap.final_url = page.url
                self._dlog(
                    "case_detail_captured",
                    case_number=case.case_number,
                    status=resp.status if resp else 0,
                    html_bytes=len(cap.html),
                )
            except Exception as e:
                cap.error = str(e)
                self.log.warning(f"    failed: {e}")
                self._dlog("case_detail_error",
                           case_number=case.case_number, error=str(e))

            # Parse the captured HTML into a structured ProbateRecord.
            detail: MontgomeryProbateDetail | None = None
            if cap.html and not cap.error:
                try:
                    detail = parse_case_detail(cap.html)
                    self._dlog(
                        "case_detail_parsed",
                        case_number=cap.case_number,
                        decedent=detail.decedent_name,
                        fiduciary=detail.fiduciary_name,
                    )
                except Exception as e:
                    self.log.warning(
                        f"    parser failed for {case.case_number}: {e}"
                    )
                    self._dlog("case_detail_parse_error",
                               case_number=case.case_number, error=str(e))

            # Navigate to the docket page (click "Click here to view DOCKET").
            # The link is <a href="CASESEARCH_DOCKETx.cfm?<token>"> inside the
            # case detail HTML. We follow the href directly rather than DOM-click.
            if cap.html and not cap.error:
                docket_href = _extract_docket_href(cap.html)
                if docket_href:
                    docket_full_url = (
                        docket_href if docket_href.startswith("http")
                        else base_prefix + docket_href
                    )
                    try:
                        resp = await page.goto(
                            docket_full_url,
                            wait_until="domcontentloaded",
                            timeout=20000,
                        )
                        await page.wait_for_timeout(1500)
                        cap.docket_html = await page.content()
                        self._dlog(
                            "docket_captured",
                            case_number=case.case_number,
                            status=resp.status if resp else 0,
                            html_bytes=len(cap.docket_html),
                        )

                        # Parse docket entries + pick the best PDF
                        try:
                            cap.docket_entries = parse_docket(cap.docket_html)
                            best = select_application_pdf(cap.docket_entries)
                            if best:
                                cap.application_pdf_description = best.description
                                cap.application_pdf_url = best.pdf_url
                                self._dlog(
                                    "application_pdf_selected",
                                    case_number=case.case_number,
                                    description=best.description[:80],
                                    docket_entries=len(cap.docket_entries),
                                )
                                # Download the PDF using the browser's request
                                # context (preserves cookies/session).
                                if self.download_pdfs:
                                    try:
                                        req_ctx = page.context.request
                                        pdf_resp = await req_ctx.get(
                                            best.pdf_url, timeout=20000
                                        )
                                        if pdf_resp.ok:
                                            cap.application_pdf_bytes = (
                                                await pdf_resp.body()
                                            )
                                            self._dlog(
                                                "pdf_downloaded",
                                                case_number=case.case_number,
                                                bytes=len(cap.application_pdf_bytes),
                                            )
                                        else:
                                            self._dlog(
                                                "pdf_download_failed",
                                                case_number=case.case_number,
                                                status=pdf_resp.status,
                                            )
                                    except Exception as e:
                                        self.log.warning(
                                            f"    PDF download failed: {e}"
                                        )
                                        self._dlog(
                                            "pdf_download_error",
                                            case_number=case.case_number,
                                            error=str(e),
                                        )
                            else:
                                self._dlog(
                                    "no_application_pdf_found",
                                    case_number=case.case_number,
                                    docket_entries=len(cap.docket_entries),
                                )
                        except Exception as e:
                            self.log.warning(
                                f"    docket parse failed: {e}"
                            )
                            self._dlog(
                                "docket_parse_error",
                                case_number=case.case_number,
                                error=str(e),
                            )
                    except Exception as e:
                        self.log.warning(
                            f"    docket navigation failed: {e}"
                        )
                        self._dlog(
                            "docket_error",
                            case_number=case.case_number,
                            error=str(e),
                        )
                else:
                    self._dlog(
                        "docket_link_not_found",
                        case_number=case.case_number,
                    )

            self.recon.case_details.append(cap)

            # Build the structured ProbateRecord (with whatever fields we have).
            # Pass docket_entries so the earliest docket entry can drive the
            # filing-date field (more accurate than case_status_date, which
            # is closure-date for closed cases).
            if detail:
                rec = _detail_to_record(detail, cap.docket_entries)
                self.recon.probate_records.append(rec)

            # Be polite — small delay between case page hits
            await page.wait_for_timeout(500)

    # ── Internal ────────────────────────────────────────────────────

    def _dlog(self, event: str, **kwargs: Any) -> None:
        entry = {"event": event, "ts": datetime.utcnow().isoformat() + "Z", **kwargs}
        self.recon.debug_log.append(entry)


class _StdoutLog:
    def info(self, msg: str) -> None: print(f"[INFO] {msg}")
    def warning(self, msg: str) -> None: print(f"[WARN] {msg}")
    def error(self, msg: str) -> None: print(f"[ERROR] {msg}")
