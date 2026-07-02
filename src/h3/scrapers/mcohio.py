"""Montgomery County (Ohio) Common Pleas — pro.mcohio.org scraper.

Built for deployment as an Apify Actor (Python + Playwright). Three modes:

  - recon : dismiss disclaimer, fill the search form for the date range,
            click Search, parse the results table into structured rows,
            capture page HTML + screenshot. No per-case navigation.
            Safe for first-deploy validation. Cost: ~$0.008 per run.

  - case_details : recon + open the first N case detail pages (controlled
            by capture_case_details input). Captures each case-summary HTML
            to KV store. Used to develop the docket parser against real
            structure. Cost scales linearly with N.

  - full : (NOT YET BUILT) recon + every case detail + docket navigation +
            PDF download + complaint parsing → fully-populated CaseRecord
            objects. Gated on the docket-page parser being built from
            case_details-mode artifacts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
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

from h3.output_writers.h3_format import CaseRecord, Defendant
import config
from recaptcha_v3_solver import (
    RecaptchaV3SolveError,
    solve_recaptcha_v3,
)


# ── reCAPTCHA v3 block detection (BUG-04, deployed 2026-07-01) ──────────
#
# On 2026-07-01 pro.mcohio.org started serving a "reCAPTCHA score too low"
# block page instead of the results table when its invisible v3 detector
# flags the browser as bot-like. The scraper's Playwright form-fill +
# submit runs cleanly, but the returned page has no <tbody id='tblSearchResults'>
# rows — parse_results_table sees zero <tr>s and returns [] silently,
# which was misdiagnosed for a full morning as "no filings today".
#
# The guardrail below turns that silent failure into an explicit
# RecaptchaBlockedError so callers can log/alert/fall back instead of
# emitting a bogus 0-record CSV.

class RecaptchaBlockedError(RuntimeError):
    """Raised when the courthouse portal returns a reCAPTCHA block page."""

    def __init__(self, reason: str, url: str, html_bytes: int, *,
                 snippet: str = ""):
        self.reason = reason
        self.url = url
        self.html_bytes = html_bytes
        self.snippet = snippet
        super().__init__(
            f"reCAPTCHA blocked ({reason}) at {url} — "
            f"html={html_bytes} bytes"
        )


# Sentinel phrases lifted from the 2026-07-01 captured block page. Both
# are stable across the message body Google renders for score_too_low.
# Match case-insensitively so any capitalization drift still trips it.
RECAPTCHA_BLOCK_MARKERS: tuple[str, ...] = (
    "reCAPTCHA (a system for detecting whether you are a real "
    "user or a bot) has flagged you",
    "try the search again in 20 minutes",
)


def _detect_recaptcha_block(html: str) -> str | None:
    """Return "score_too_low" if the HTML looks like a v3 block page.

    Plain lowercase substring check — no regex. Cheap enough to run on
    every capture. Callers should treat non-None as a hard stop, not a
    retry signal (per the 2026-07-01 diagnosis, the "20 minutes" claim
    was optimistic).
    """
    if not html:
        return None
    lowered = html.lower()
    for marker in RECAPTCHA_BLOCK_MARKERS:
        if marker.lower() in lowered:
            return "score_too_low"
    return None


# ── Portal config (from recon tests #1 + #2 + Apify run #1) ─────────────

PORTAL_URL = "https://pro.mcohio.org"

SEL_AGREE_BUTTON = "button:has-text('I Agree')"
SEL_ACTION_TYPE = "select#gen_action_type"
SEL_CASE_TYPE = "select#gen_case_type"
SEL_DATE_FROM = "input#gen_begin_date"
SEL_DATE_TO = "input#gen_end_date"
SEL_SEARCH_BUTTON = "input[type='submit'][value='Search'], button:has-text('Search')"
SEL_RESULTS_TBODY = "#tblSearchResults"

ACTION_TYPE_VALUE = "MORTGAGE FORECLOSURE"

# onclick="openTab('caseInfo','case_id=62390491&amp;screen=summary',1,'2026 CV 03347');"
ROW_ONCLICK_RE = re.compile(
    r"openTab\('caseInfo',\s*'case_id=(\d+)[^']*',\s*\d+,\s*'([^']+)'\)"
)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ── Helpers ─────────────────────────────────────────────────────────────

def _parse_proxy_url(url: str) -> dict[str, str]:
    """Convert Apify proxy URL to Playwright's {server, username, password} form."""
    p = urlparse(url)
    return {
        "server": f"{p.scheme}://{p.hostname}:{p.port}",
        "username": p.username or "",
        "password": p.password or "",
    }


def _to_mco_date_input(iso_date: str) -> str:
    """Portal expects YYYY-MM-DD for its HTML5 date inputs."""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", iso_date):
        return iso_date
    try:
        return datetime.strptime(iso_date, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return iso_date


# ── Parsed data structures ──────────────────────────────────────────────

@dataclass
class ParsedRow:
    """One <tr> from the results table — one party per row."""
    case_number: str
    case_id: str
    action_type: str
    party_name: str
    status: str
    role: str  # DEFENDANT | PLAINTIFF


@dataclass
class ParsedCase:
    """One unique case_number, with all its parties grouped."""
    case_number: str
    case_id: str
    action_type: str
    status: str
    parties: list[ParsedRow] = field(default_factory=list)


@dataclass
class DocketEntry:
    """One row from the case's docket — possibly with a downloadable PDF."""
    docketid: str
    case_id: str
    date_filed: str          # MM/DD/YYYY
    document_type: str       # "COMPLAINT", "CIVIL SUMMONS ISSUED", etc.
    description: str         # full row text
    download_url: str = ""   # /Helpers/getDocumentFromOnBase.aspx?... when available


@dataclass
class CaseScreenCapture:
    """Captured HTML for one tab/screen of a case detail (summary, docket, etc)."""
    screen: str           # "summary" | "docket" | "party" | ...
    final_url: str = ""
    html: str = ""
    error: str = ""


@dataclass
class PdfDownload:
    """A downloaded docket document (PDF bytes + metadata)."""
    docketid: str
    document_type: str
    pdf_bytes: bytes = b""
    error: str = ""


@dataclass
class CaseDetailCapture:
    """All captured screens for one case (summary + docket + ...)."""
    case_number: str
    case_id: str
    screens: list[CaseScreenCapture] = field(default_factory=list)
    docket_entries: list[DocketEntry] = field(default_factory=list)
    pdfs: list[PdfDownload] = field(default_factory=list)

    @property
    def html(self) -> str:
        for s in self.screens:
            if s.screen == "summary" and s.html:
                return s.html
        return ""

    @property
    def error(self) -> str:
        errs = [s.error for s in self.screens if s.error]
        return "; ".join(errs)


@dataclass
class ReconCapture:
    """All artifacts the scraper produces; serialized into KV store by main.py."""
    results_html: str = ""
    results_screenshot: bytes = b""
    parsed_rows: list[ParsedRow] = field(default_factory=list)
    parsed_cases: list[ParsedCase] = field(default_factory=list)
    case_details: list[CaseDetailCapture] = field(default_factory=list)
    debug_log: list[dict[str, Any]] = field(default_factory=list)


# ── Parsers (pure, no Playwright needed — testable locally) ─────────────

def parse_results_table(html: str) -> list[ParsedRow]:
    """Extract one ParsedRow per defendant/plaintiff row in #tblSearchResults."""
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.select_one(SEL_RESULTS_TBODY)
    if tbody is None:
        return []

    rows: list[ParsedRow] = []
    for tr in tbody.select("tr"):
        onclick = tr.get("onclick") or ""
        m = ROW_ONCLICK_RE.search(onclick)
        if not m:
            continue
        case_id, _ = m.group(1), m.group(2)

        tds = tr.select("td")
        if len(tds) < 6:
            continue

        rows.append(ParsedRow(
            case_number=tds[0].get_text(strip=True),
            case_id=case_id,
            action_type=tds[1].get_text(strip=True),
            party_name=tds[2].get_text(strip=True),
            status=tds[4].get_text(strip=True),
            role=tds[5].get_text(strip=True),
        ))
    return rows


_DOCKET_ROW_ID_RE = re.compile(r"docket_row_(\d+)")
_DOWNLOAD_ONCLICK_RE = re.compile(
    r"window\.open\('([^']*getDocumentFromOnBase[^']*)'"
)
_DOCTYPE_RE = re.compile(r"Document Type:\s*([^<\n]+)", re.I)


def parse_docket_entries(html: str) -> list[DocketEntry]:
    """Extract docket entries from a case-detail docket-tab HTML."""
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.select_one("#tblDocketBody")
    if tbody is None:
        return []

    entries: list[DocketEntry] = []
    for tr in tbody.select("tr"):
        td = tr.select_one("td.docketRows, td")
        if not td:
            continue
        td_id = td.get("id", "")
        m = _DOCKET_ROW_ID_RE.search(td_id)
        if not m:
            continue
        docketid = m.group(1)

        # Date + description live in the .row > .col-md-* divs
        date_div = td.select_one(".col-md-2")
        body_div = td.select_one(".col-md-10")
        date_filed = date_div.get_text(strip=True) if date_div else ""

        # Document type from the small grey label
        doc_type = ""
        small = td.select_one("small")
        if small:
            m_dt = _DOCTYPE_RE.search(small.get_text())
            if m_dt:
                doc_type = m_dt.group(1).strip()

        description = body_div.get_text(" ", strip=True) if body_div else ""

        # Download URL from the DOWNLOAD button's onclick
        download_url = ""
        case_id = ""
        for btn in td.select("button"):
            onclick = btn.get("onclick", "")
            m_dl = _DOWNLOAD_ONCLICK_RE.search(onclick)
            if m_dl:
                download_url = m_dl.group(1).replace("&amp;", "&")
                cid_m = re.search(r"caseid=(\d+)", download_url)
                if cid_m:
                    case_id = cid_m.group(1)
                break

        entries.append(DocketEntry(
            docketid=docketid,
            case_id=case_id,
            date_filed=date_filed,
            document_type=doc_type,
            description=description,
            download_url=download_url,
        ))
    return entries


def group_rows_into_cases(rows: list[ParsedRow]) -> list[ParsedCase]:
    """Collapse per-defendant rows into one ParsedCase per unique case_number."""
    by_case: dict[str, ParsedCase] = {}
    order: list[str] = []
    for r in rows:
        if r.case_number not in by_case:
            by_case[r.case_number] = ParsedCase(
                case_number=r.case_number,
                case_id=r.case_id,
                action_type=r.action_type,
                status=r.status,
            )
            order.append(r.case_number)
        by_case[r.case_number].parties.append(r)
    return [by_case[k] for k in order]


# ── The scraper ─────────────────────────────────────────────────────────

class MontgomeryScraper:
    """Playwright-based scraper for pro.mcohio.org Mortgage Foreclosure dockets."""

    def __init__(
        self,
        *,
        date_from: str,
        date_to: str,
        proxy_config_url: str | None = None,
        headless: bool = True,
        mode: str = "recon",
        max_cases: int = 200,
        capture_case_details: int = 0,
        download_pdfs: bool = False,
        logger: Any = None,
    ):
        self.date_from = _to_mco_date_input(date_from)
        self.date_to = _to_mco_date_input(date_to)
        self.proxy_url = proxy_config_url
        self.headless = headless
        self.mode = mode
        self.max_cases = max_cases
        self.capture_case_details = capture_case_details
        self.download_pdfs = download_pdfs
        self.log = logger if logger else _StdoutLog()
        self.recon: ReconCapture = ReconCapture()

    # ── Public ──────────────────────────────────────────────────────

    async def run(self) -> list[CaseRecord]:
        self.log.info(f"MontgomeryScraper start | mode={self.mode} | "
                      f"{self.date_from} → {self.date_to} | "
                      f"headless={self.headless} | "
                      f"capture_case_details={self.capture_case_details}")

        async with async_playwright() as p:
            browser, ctx = await self._launch_browser(p)
            try:
                page = await ctx.new_page()
                await self._goto_portal(page)
                await self._dismiss_disclaimer(page)
                await self._fill_search_form(page)
                await self._solve_and_inject_recaptcha_v3(page)
                await self._submit_search(page)
                await self._capture_results(page)

                # Phase 2 — capture first N case detail pages
                if self.capture_case_details > 0:
                    await self._capture_case_details_pages(page)

                if self.mode == "full":
                    self.log.warning(
                        "mode=full requested but docket/PDF parsing not yet "
                        "implemented. Use case_details-mode capture to develop the "
                        "parser, then set mode=full once selectors are in place."
                    )

                return [
                    CaseRecord(
                        case_number=c.case_number,
                        defendants=[Defendant(name=p.party_name) for p in c.parties],
                    )
                    for c in self.recon.parsed_cases
                ]
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

    # ── Search flow ─────────────────────────────────────────────────

    async def _goto_portal(self, page: Page) -> None:
        self.log.info(f"GET {PORTAL_URL}")
        resp = await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
        status = resp.status if resp else 0
        self._dlog("goto", url=PORTAL_URL, status=status, final_url=page.url)
        if status >= 400:
            raise RuntimeError(f"Portal returned HTTP {status}")
        await page.wait_for_timeout(2000)

    async def _dismiss_disclaimer(self, page: Page) -> None:
        try:
            btn = page.locator(SEL_AGREE_BUTTON).first
            if await btn.count() > 0 and await btn.is_visible():
                self.log.info("Clicking 'I Agree' on disclaimer modal")
                await btn.click()
                await page.wait_for_timeout(2500)
                self._dlog("disclaimer_dismissed", url=page.url)
            else:
                self._dlog("disclaimer_not_found", note="Modal may have been pre-dismissed")
        except Exception as e:
            self._dlog("disclaimer_error", error=str(e))
            raise

    async def _fill_search_form(self, page: Page) -> None:
        self.log.info(
            f"Setting Action Type = {ACTION_TYPE_VALUE!r}, "
            f"dates {self.date_from} → {self.date_to}"
        )
        await page.locator(SEL_ACTION_TYPE).select_option(label=ACTION_TYPE_VALUE)
        await page.locator(SEL_DATE_FROM).fill(self.date_from)
        await page.locator(SEL_DATE_TO).fill(self.date_to)
        await page.wait_for_timeout(500)
        self._dlog("form_filled",
                   action_type=ACTION_TYPE_VALUE,
                   date_from=self.date_from,
                   date_to=self.date_to)

    async def _solve_and_inject_recaptcha_v3(self, page: Page) -> None:
        """Solve pro.mcohio.org's reCAPTCHA v3 via 2Captcha, inject the
        token into the form's hidden textarea, and trigger any registered
        callbacks so the site's JS treats the token as user-generated.

        Runs BETWEEN form-fill and search-submit. If PRO_MCOHIO_RECAPTCHA_V3_SITEKEY
        is unset or 2Captcha fails, raises RecaptchaV3SolveError which
        propagates up through run() — the D.1 block-page detector in
        _capture_results is the final safety net if the token is rejected
        by Google's server-side scoring.
        """
        if not config.PRO_MCOHIO_RECAPTCHA_V3_SITEKEY:
            self.log.error(
                "PRO_MCOHIO_RECAPTCHA_V3_SITEKEY not configured — "
                "cannot proceed against post-2026-07-01 portal"
            )
            raise RecaptchaV3SolveError(
                "PRO_MCOHIO_RECAPTCHA_V3_SITEKEY not set"
            )
        self.log.info(
            f"Solving reCAPTCHA v3 for {PORTAL_URL} "
            f"(action={config.PRO_MCOHIO_RECAPTCHA_V3_ACTION}, "
            f"min_score={config.PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE})"
        )
        token = await solve_recaptcha_v3(
            url=PORTAL_URL,
            sitekey=config.PRO_MCOHIO_RECAPTCHA_V3_SITEKEY,
            action=config.PRO_MCOHIO_RECAPTCHA_V3_ACTION,
            min_score=config.PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE,
            logger=self.log,
        )
        # Injection: same battle-tested pattern as captcha_solver.py v2.
        # Sets the token into every g-recaptcha-response element AND walks
        # ___grecaptcha_cfg.clients to invoke any callback(token) hooks.
        # v3 sites usually register a callback in grecaptcha.execute().then()
        # — invoking it makes the site treat the token as user-generated.
        await page.evaluate(
            """(token) => {
                const el = document.getElementById('g-recaptcha-response');
                if (el) { el.value = token; el.style.display = 'block'; }
                const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                if (ta) { ta.value = token; ta.style.display = 'block'; }
                if (typeof ___grecaptcha_cfg !== 'undefined') {
                    const clients = ___grecaptcha_cfg.clients;
                    if (clients) {
                        Object.keys(clients).forEach(key => {
                            const client = clients[key];
                            const findCallback = (obj) => {
                                if (!obj || typeof obj !== 'object') return;
                                Object.values(obj).forEach(v => {
                                    if (typeof v === 'object' && v !== null) {
                                        if (typeof v.callback === 'function') {
                                            v.callback(token);
                                        }
                                        findCallback(v);
                                    }
                                });
                            };
                            findCallback(client);
                        });
                    }
                }
            }""",
            token,
        )
        self._dlog("recaptcha_v3_injected", token_len=len(token))
        await page.wait_for_timeout(500)

    async def _submit_search(self, page: Page) -> None:
        self.log.info("Clicking Search")
        await page.locator(SEL_SEARCH_BUTTON).first.click()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        self._dlog("search_submitted", url=page.url)

    # ── Results capture + parsing ───────────────────────────────────

    async def _capture_results(self, page: Page) -> None:
        self.log.info("Capturing results page HTML + screenshot")
        self.recon.results_html = await page.content()

        # BUG-04 guardrail: detect reCAPTCHA v3 block page before parsing.
        # A block page has no <tbody id='tblSearchResults'> rows, so the
        # legacy code path was returning 0 records silently (interpreted
        # downstream as "no filings"). Capture the screenshot first so we
        # have forensic evidence, then raise before parse_results_table
        # can produce a false-empty result set.
        block_reason = _detect_recaptcha_block(self.recon.results_html)
        if block_reason is not None:
            self.recon.results_screenshot = await page.screenshot(
                full_page=True,
            )
            self._dlog(
                "recaptcha_blocked",
                url=page.url,
                reason=block_reason,
                html_bytes=len(self.recon.results_html),
            )
            self.log.error(
                f"reCAPTCHA blocked at {page.url} — reason={block_reason} — "
                f"html={len(self.recon.results_html)} bytes — see debug_log "
                f"for full trail"
            )
            raise RecaptchaBlockedError(
                reason=block_reason,
                url=page.url,
                html_bytes=len(self.recon.results_html),
                snippet=self.recon.results_html[:500],
            )

        self.recon.results_screenshot = await page.screenshot(full_page=True)

        # Parse the table — proper HTML parsing now, not regex on body text
        rows = parse_results_table(self.recon.results_html)
        self.recon.parsed_rows = rows
        cases = group_rows_into_cases(rows)
        if len(cases) > self.max_cases:
            self.log.warning(
                f"Capping {len(cases)} cases at max_cases={self.max_cases}"
            )
            cases = cases[: self.max_cases]
        self.recon.parsed_cases = cases

        n_defendants = sum(1 for r in rows if r.role == "DEFENDANT")
        n_plaintiffs = sum(1 for r in rows if r.role == "PLAINTIFF")
        self.log.info(
            f"Parsed {len(rows)} rows → {len(cases)} unique cases "
            f"(defendants={n_defendants}, plaintiffs={n_plaintiffs})"
        )
        self._dlog("results_parsed",
                   row_count=len(rows),
                   case_count=len(cases),
                   defendants=n_defendants,
                   plaintiffs=n_plaintiffs)

    # ── Phase 2: per-case detail pages ──────────────────────────────

    # Screens we need per case for full-mode extraction. Trimmed from the
    # initial 6-screen recon to the 4 that actually carry data we use:
    #   summary  → parcel number (for verification + auditor lookup)
    #   docket   → filing date, filing type, PDF links
    #   party    → owner names + mailing addresses + attorneys
    #   service  → summons status (basis for narrative notes)
    # qfile + financial are skipped (no new fields, saves ~5s/case).
    SCREENS_TO_CAPTURE = ["summary", "docket", "party", "service"]

    async def _capture_case_details_pages(self, page: Page) -> None:
        """For the first N unique cases, navigate through each tab/screen
        (summary, docket, party, ...) and capture the HTML at each step.
        Tabs load via AJAX — the openTab(...screen=X) call triggers the load.
        """
        n = min(self.capture_case_details, len(self.recon.parsed_cases))
        if n == 0:
            return
        self.log.info(
            f"Phase 2: capturing case-detail tabs for first {n} cases "
            f"(screens per case: {self.SCREENS_TO_CAPTURE})"
        )

        for i, case in enumerate(self.recon.parsed_cases[:n], start=1):
            self.log.info(f"  [{i}/{n}] case {case.case_number} (id={case.case_id})")
            cap = CaseDetailCapture(
                case_number=case.case_number,
                case_id=case.case_id,
            )

            for screen in self.SCREENS_TO_CAPTURE:
                screen_cap = CaseScreenCapture(screen=screen)
                try:
                    await page.evaluate(
                        "(args) => openTab('caseInfo', "
                        "`case_id=${args.cid}&screen=${args.screen}`, 1, args.cnum)",
                        {"cid": case.case_id,
                         "cnum": case.case_number,
                         "screen": screen},
                    )
                    await page.wait_for_timeout(2500)
                    screen_cap.final_url = page.url
                    screen_cap.html = await page.content()
                    self._dlog("screen_captured",
                               case_number=case.case_number,
                               screen=screen,
                               html_bytes=len(screen_cap.html))
                except Exception as e:
                    screen_cap.error = str(e)
                    self.log.warning(f"      screen={screen} failed: {e}")
                    self._dlog("screen_failed",
                               case_number=case.case_number,
                               screen=screen,
                               error=str(e))
                cap.screens.append(screen_cap)
                await page.wait_for_timeout(500)

            # Parse docket entries from the docket-tab HTML
            docket_html = next(
                (s.html for s in cap.screens if s.screen == "docket" and s.html),
                ""
            )
            if docket_html:
                cap.docket_entries = parse_docket_entries(docket_html)
                self.log.info(
                    f"    docket entries: {len(cap.docket_entries)} "
                    f"({sum(1 for d in cap.docket_entries if d.download_url)} downloadable)"
                )

            # Download Complaint PDF if requested
            if self.download_pdfs and cap.docket_entries:
                await self._download_complaint_pdfs(page, cap)

            self.recon.case_details.append(cap)
            await page.wait_for_timeout(1500)

    async def _download_complaint_pdfs(self, page: Page, cap: CaseDetailCapture) -> None:
        """Download the COMPLAINT PDF (and any related primary docs) for one case.

        Uses Playwright's request context — inherits the browser session's
        cookies, so the GET to /Helpers/getDocumentFromOnBase.aspx works."""
        # Pick docket entries that are likely to contain property/owner info
        priority_types = {"COMPLAINT", "CASE INFORMATION SHEET",
                          "PRELIMINARY JUDICIAL REPORT"}
        targets = [
            d for d in cap.docket_entries
            if d.download_url and (
                any(p in d.document_type.upper() for p in priority_types)
                or any(p in d.description.upper() for p in priority_types)
            )
        ]
        if not targets:
            self.log.info("    no priority docket entries to download")
            return

        for entry in targets[:5]:   # safety cap: max 5 PDFs per case
            pdf = PdfDownload(
                docketid=entry.docketid,
                document_type=entry.document_type or "(unknown)",
            )
            url = entry.download_url
            if url.startswith("/"):
                url = f"https://pro.mcohio.org{url}"
            try:
                self.log.info(f"    GET {url[:90]}...")
                resp = await page.context.request.get(url, timeout=30000)
                if resp.status >= 400:
                    pdf.error = f"HTTP {resp.status}"
                    self.log.warning(f"      → HTTP {resp.status}")
                else:
                    pdf.pdf_bytes = await resp.body()
                    ct = resp.headers.get("content-type", "")
                    self.log.info(
                        f"      → {len(pdf.pdf_bytes)} bytes, content-type={ct}"
                    )
                    self._dlog("pdf_downloaded",
                               case_number=cap.case_number,
                               docketid=entry.docketid,
                               document_type=entry.document_type,
                               bytes=len(pdf.pdf_bytes))
            except Exception as e:
                pdf.error = str(e)
                self.log.warning(f"      → failed: {e}")
                self._dlog("pdf_failed",
                           case_number=cap.case_number,
                           docketid=entry.docketid,
                           error=str(e))
            cap.pdfs.append(pdf)
            await page.wait_for_timeout(800)   # polite spacing

    # ── Internal logging ────────────────────────────────────────────

    def _dlog(self, event: str, **kwargs: Any) -> None:
        entry = {"event": event, "ts": datetime.utcnow().isoformat() + "Z", **kwargs}
        self.recon.debug_log.append(entry)


class _StdoutLog:
    """Fallback when Apify's Actor.log isn't available (local dev)."""
    def info(self, msg: str) -> None: print(f"[INFO] {msg}")
    def warning(self, msg: str) -> None: print(f"[WARN] {msg}")
    def error(self, msg: str) -> None: print(f"[ERROR] {msg}")
