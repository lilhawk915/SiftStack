"""Warren County (Ohio) Clerk of Courts — clerkofcourt.co.warren.oh.us scraper.

Sixth (and final) county in the H3 stack. **Vendor: Benchmark Case Processing**
— different from the CourtView vendor that powers Greene/Clark/Butler/Miami.

Warren's filter UI is actually the most explicit of any of the six counties:

  - Court Type dropdown: deselect all, select CIVIL only
  - Cause of Action dropdown: deselect all, select FORECLOSURES only  ← direct!
  - Date Opened range: required
  - Party Types: leave at "5 selected" default
  - Division: leave at "6 selected" default

Because the search itself filters directly to FORECLOSURES, there's no
post-search initiating-action regex needed — every result is a foreclosure.

Standard 6 extracted fields per SOP: Owner Name, Property Address, Mailing
Address, Summons Status, Competing Liens, Prayer Amount.
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

from h3.output_writers.h3_format import CaseRecord, Defendant


# ── Portal config (per H3 SOP H3-SOP-WCO-001) ───────────────────────────

PORTAL_URL = (
    "https://clerkofcourt.co.warren.oh.us/BenchmarkCP/Home.aspx/Search"
)
COURT_TYPE_VALUE = "CIVIL"
CAUSE_OF_ACTION_VALUE = "FORECLOSURES"

# Warren case numbers may follow a different format than CV; using a broader
# pattern to be safe — refine after first recon HTML lands.
# Warren format: "26CV100563" — 2-digit year + CV + 6-digit seq, no spaces
CASE_NUMBER_RE = re.compile(r"\b\d{2}CV\d{4,7}\b")

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
    if not iso_date:
        return ""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return iso_date


# ── Parsed data structures ──────────────────────────────────────────────

@dataclass
class WarrenParsedCase:
    case_number: str
    cause_of_action: str = ""
    court_type: str = ""
    status: str = ""
    detail_url: str = ""        # /BenchmarkCP/CourtCase.aspx/Details/...
    raw_row_text: str = ""


@dataclass
class CaseDetailCapture:
    case_number: str
    final_url: str = ""
    html: str = ""
    error: str = ""
    complaint_pdf_bytes: bytes = b""
    complaint_pdf_error: str = ""
    pjr_pdf_bytes: bytes = b""
    pjr_pdf_error: str = ""
    # Which docket entry the address-PDF came from. "PJR" when the title
    # company filed a Preliminary Judicial Report; "COMPLAINT" when no
    # PJR exists (tax-foreclosure cases) and we fell back to the
    # complaint itself. Empty string when neither was captured.
    pjr_pdf_source: str = ""


@dataclass
class ReconCapture:
    results_html: str = ""
    results_screenshot: bytes = b""
    parsed_cases: list[WarrenParsedCase] = field(default_factory=list)
    case_details: list[CaseDetailCapture] = field(default_factory=list)
    debug_log: list[dict[str, Any]] = field(default_factory=list)


# ── Parsers ─────────────────────────────────────────────────────────────

_CORPORATE_HINTS = re.compile(
    r"\b(LLC|L\.L\.C|INC|INCORPORATED|CORP|CORPORATION|CO\.|COMPANY|"
    r"BANK|N\.A\.|N\.A\b|ASSOCIATION|ASSN|TRUST|TREASURER|UNITED STATES|"
    r"DEPARTMENT|STATE OF|UNKNOWN|HEIRS|SPOUSE|ESTATE OF|UNIVERSITY|"
    r"PROPERTY OWNERS|HOMEOWNERS|CREDIT UNION|SECRETARY OF|"
    r"BOARD|COMMERCE|FIRE MARSHAL|ATTORNEY GENERAL|CITY OF|"
    r"DIVISION|COMPENSATION|TENANT|MUNICIPAL|VILLAGE OF|TOWNSHIP|"
    r"COUNTY|AUDITOR|OFFICE OF|CASE MANAGER)\b",
    re.IGNORECASE,
)

# Real people in court records look like "LAST, FIRST" or "LAST, FIRST M.".
# Match a comma followed by a single given name (no commas, no boards/INCs).
_PERSON_NAME_RE = re.compile(
    r"^[A-Z][A-Z\-'\.]+(?:\s[A-Z][A-Z\-'\.]+)?\s*,\s*[A-Z][A-Z\-'\.]+",
    re.IGNORECASE,
)

_PLACEHOLDER_HINTS = re.compile(
    r"^(DOE,?\s*(JANE|JOHN)|JANE\s+OR\s+JAMES\s+DOE|JOHN\s+DOE|"
    r"JANE\s+DOE|JOHN\s+OR\s+JANE\s+DOE)",
    re.IGNORECASE,
)


def _looks_like_person(name: str) -> bool:
    """Heuristic: does this party name look like a real individual?"""
    if not name:
        return False
    if _CORPORATE_HINTS.search(name):
        return False
    if _PLACEHOLDER_HINTS.search(name.strip()):
        return False
    return True


@dataclass
class WarrenParty:
    party_type: str           # PRIMARY DEF., ADD. DEFENDANT, PLAINTIFF, etc.
    name: str
    attorney: str = ""


@dataclass
class WarrenCaseDetail:
    case_number: str
    judge: str = ""
    filing_date: str = ""       # MM/DD/YYYY as shown by Benchmark
    case_type: str = ""         # FORECLOSURES
    status: str = ""            # OPEN/CLOSED
    uniform_case_number: str = ""
    parties: list[WarrenParty] = field(default_factory=list)
    parcel_number: str = ""
    docket_entries: list[str] = field(default_factory=list)
    property_street: str = ""
    property_city: str = ""
    property_state: str = ""
    property_zip: str = ""

    @property
    def plaintiffs(self) -> list[WarrenParty]:
        return [p for p in self.parties if "PLAINTIFF" in p.party_type.upper()]

    @property
    def defendants(self) -> list[WarrenParty]:
        return [p for p in self.parties
                if "DEF" in p.party_type.upper()
                and "UNKNOWN" not in p.party_type.upper()]

    @property
    def primary_owner(self) -> WarrenParty | None:
        """First defendant that looks like the actual property owner.

        H3's DM uses OWNER NAME = the first natural-person defendant.
        Priority order:
          1. PRIMARY DEF. that matches the "LAST, FIRST" pattern
          2. Any PRIMARY DEF. that looks like a real person
          3. Any defendant matching "LAST, FIRST" (most reliable signal)
          4. Any defendant that looks like a real person
          5. First defendant of any kind (last resort)
        Benchmark sometimes files spouses as ADD. DEFENDANT and the
        actual borrower as PRIMARY DEF.; rarely the corporate entity is
        listed as PRIMARY DEF (as in commercial foreclosure cases).
        """
        primary = [d for d in self.defendants
                   if "PRIMARY" in d.party_type.upper()]
        for d in primary:
            if (_looks_like_person(d.name)
                    and _PERSON_NAME_RE.search(d.name)):
                return d
        for d in primary:
            if _looks_like_person(d.name):
                return d
        for d in self.defendants:
            if (_looks_like_person(d.name)
                    and _PERSON_NAME_RE.search(d.name)):
                return d
        for d in self.defendants:
            if _looks_like_person(d.name):
                return d
        return self.defendants[0] if self.defendants else None


def parse_case_detail_html(case_number: str, html: str) -> WarrenCaseDetail:
    """Parse a Warren BenchmarkCP CourtCase Details page.

    The page exposes 4 accordion sections — Summary, Parties, Events, and
    Case Dockets — all server-rendered (no AJAX wait needed once the page
    finishes loading). We pull the structured fields we need; property
    street address is only in the Complaint PDF, not in this HTML.
    """
    detail = WarrenCaseDetail(case_number=case_number)
    if not html:
        return detail
    soup = BeautifulSoup(html, "html.parser")

    # ── Summary section: judge, filing date, case type, status ──────
    # Benchmark renders Summary as <dl class="dl-horizontal"> with
    # <dt>label</dt><dd>value</dd> pairs (NOT a 2-col table).
    summary = soup.find(id="summaryAccordionCollapse")
    if summary:
        for dt in summary.find_all("dt"):
            label = dt.get_text(" ", strip=True).rstrip(":").upper()
            dd = dt.find_next_sibling("dd")
            value = dd.get_text(" ", strip=True) if dd else ""
            if label == "JUDGE":
                detail.judge = value
            elif label == "CLERK FILE DATE":
                detail.filing_date = value
            elif label == "CASE TYPE":
                detail.case_type = value
            elif label == "STATUS":
                detail.status = value
            elif label == "UNIFORM CASE NUMBER":
                detail.uniform_case_number = value

    # ── Parties section: type | name | attorney ─────────────────────
    parties = soup.find(id="partyAccordionCollapse")
    if parties:
        for row in parties.select("tr"):
            cols = [c.get_text(" ", strip=True)
                    for c in row.find_all(["td", "th"])]
            if len(cols) < 2:
                continue
            ptype = cols[0]
            if ptype.upper() == "TYPE":  # header row
                continue
            name = cols[1]
            attorney = cols[2] if len(cols) > 2 else ""
            if ptype.upper() == "PROPERTY ADDRESS":
                # The "name" here is "PARCEL NO: NNN-NN-NNN-NNN"
                m = re.search(r"PARCEL\s*NO[:\s]+([\w\-]+)", name, re.I)
                if m:
                    detail.parcel_number = m.group(1)
                continue
            detail.parties.append(
                WarrenParty(party_type=ptype, name=name, attorney=attorney)
            )

    # ── Dockets section: list of filed documents ────────────────────
    dockets = soup.find(id="caseDocketsAccordionCollapse")
    if dockets:
        for row in dockets.select("tr"):
            cols = [c.get_text(" ", strip=True)
                    for c in row.find_all(["td", "th"])]
            if len(cols) < 2:
                continue
            text = " | ".join(c for c in cols if c)
            if "Date" in cols[0] and "Entry" in " ".join(cols):  # header
                continue
            detail.docket_entries.append(text)

    return detail


def parse_results_html(html: str) -> list[WarrenParsedCase]:
    """Parse Warren Benchmark CP DataTables results.

    Each visible case-row contains an <a href="/BenchmarkCP/CourtCase.aspx/
    Details/<caseId>?digest=..."> link wrapping the case number. We capture
    the URL so we can navigate to the per-case detail page later.
    """
    soup = BeautifulSoup(html, "html.parser")
    cases: list[WarrenParsedCase] = []
    seen: set[str] = set()

    # First pass: walk anchor tags pointing at CourtCase Details
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/BenchmarkCP/CourtCase.aspx/Details/" not in href:
            continue
        text = a.get_text(" ", strip=True)
        m = CASE_NUMBER_RE.search(text)
        if not m:
            continue
        cn = re.sub(r"\s+", " ", m.group(0))
        if cn in seen:
            continue
        seen.add(cn)
        cases.append(WarrenParsedCase(
            case_number=cn,
            cause_of_action=CAUSE_OF_ACTION_VALUE,
            detail_url=href,
            raw_row_text=text,
        ))

    # Fallback: regex over text if no anchors matched
    if not cases:
        text_all = soup.get_text(" ", strip=True)
        for m in CASE_NUMBER_RE.finditer(text_all):
            cn = re.sub(r"\s+", " ", m.group(0))
            if cn in seen:
                continue
            seen.add(cn)
            cases.append(WarrenParsedCase(
                case_number=cn,
                cause_of_action=CAUSE_OF_ACTION_VALUE,
                raw_row_text=text_all[max(0,m.start()-50):m.end()+200].strip(),
            ))
    return cases


# ── The scraper ─────────────────────────────────────────────────────────

class WarrenScraper:
    """Playwright scraper for Warren County Benchmark CP foreclosures."""

    def __init__(
        self,
        *,
        date_from: str = "",
        date_to: str = "",
        proxy_config_url: str | None = None,
        headless: bool = True,
        mode: str = "recon",
        max_cases: int = 200,
        capture_case_details: int = 0,
        download_pdfs: bool = False,
        logger: Any = None,
    ):
        self.date_from = _to_us_date(date_from)
        self.date_to = _to_us_date(date_to)
        self.proxy_url = proxy_config_url
        self.headless = headless
        self.mode = mode
        self.max_cases = max_cases
        self.capture_case_details = capture_case_details
        self.log = logger if logger else _StdoutLog()
        self.recon: ReconCapture = ReconCapture()

    async def run(self) -> list[CaseRecord]:
        self.log.info(
            f"WarrenScraper start | mode={self.mode} | "
            f"dates {self.date_from or '-'} → {self.date_to or '-'} | "
            f"headless={self.headless} | "
            f"capture_case_details={self.capture_case_details}"
        )
        async with async_playwright() as p:
            browser, ctx = await self._launch_browser(p)
            try:
                page = await ctx.new_page()
                await self._goto_portal(page)
                await self._set_date_range(page)
                await self._set_court_type(page)
                await self._set_cause_of_action(page)
                await self._submit_search(page)
                await self._capture_results(page)

                if self.capture_case_details > 0:
                    await self._capture_case_details_pages(page)

                return [
                    CaseRecord(
                        case_number=c.case_number,
                        defendants=[Defendant(name="(recon)")],
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
        await page.wait_for_timeout(4000)

    async def _set_date_range(self, page: Page) -> None:
        """Benchmark CP uses #openedFrom + #openedTo text inputs (MM/DD/YYYY).

        The visible date pickers populate hidden form fields with the same
        name on blur. We set BOTH the visible field (so the UI looks right)
        AND the hidden form field (so submission carries the value) — the
        visible-only approach didn't reliably propagate.
        """
        from datetime import datetime
        # Convert ISO YYYY-MM-DD → MM/DD/YYYY (Benchmark expects US format)
        def to_us(iso):
            try:
                return datetime.strptime(iso, "%Y-%m-%d").strftime("%m/%d/%Y")
            except (ValueError, TypeError):
                return iso

        if self.date_from and self.date_to:
            us_from = to_us(self.date_from)
            us_to = to_us(self.date_to)
            try:
                # Fill visible pickers
                loc_from = page.locator("#openedFrom").first
                if await loc_from.count() > 0:
                    await loc_from.fill(us_from)
                    await loc_from.blur()
                loc_to = page.locator("#openedTo").first
                if await loc_to.count() > 0:
                    await loc_to.fill(us_to)
                    await loc_to.blur()

                # Force-set the hidden form fields via JS in case the
                # visible picker doesn't propagate to the form on blur
                await page.evaluate(
                    """([from_val, to_val]) => {
                        const setHidden = (name, val) => {
                            for (const el of document.querySelectorAll(
                                `input[name='${name}']`
                            )) {
                                if (el.type === 'hidden') {
                                    el.value = val;
                                    el.dispatchEvent(
                                        new Event('change', {bubbles: true})
                                    );
                                }
                            }
                        };
                        setHidden('openedFrom', from_val);
                        setHidden('openedTo', to_val);
                    }""",
                    [us_from, us_to],
                )
                self._dlog("date_range_set",
                           from_us=us_from, to_us=us_to)
                self.log.info(f"Date range = {us_from} → {us_to}")
            except Exception as e:
                self._dlog("date_range_error", error=str(e))

    async def _set_court_type(self, page: Page) -> None:
        """Set Court Type to CIVIL.

        Benchmark CP wraps the <select id="courTypes"> in a Bootstrap
        Multiselect widget that hides the underlying <select> via CSS. The
        standard select_option() can't interact with display:none elements,
        so we manipulate the select's options via JS and dispatch a change
        event for the multiselect plugin to pick up.
        """
        try:
            result = await page.evaluate(
                """(targetLabel) => {
                    const sel = document.getElementById('courTypes');
                    if (!sel) return 'select-not-found';
                    let matched = null;
                    for (const opt of sel.options) {
                        if (opt.text.trim().toUpperCase() === targetLabel.toUpperCase()) {
                            opt.selected = true;
                            matched = opt.value;
                        } else {
                            opt.selected = false;
                        }
                    }
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    if (window.jQuery && window.jQuery(sel).multiselect) {
                        window.jQuery(sel).multiselect('refresh');
                    }
                    return matched || 'no-match';
                }""",
                COURT_TYPE_VALUE,
            )
            if result and result not in ("select-not-found", "no-match"):
                self._dlog("court_type_set",
                           value=COURT_TYPE_VALUE, option_value=result)
                self.log.info(f"Court Type = {COURT_TYPE_VALUE} (val={result})")
                return
            self._dlog("court_type_no_match", result=result)
        except Exception as e:
            self._dlog("court_type_error", error=str(e))
        self._dlog("court_type_skipped")

    async def _set_cause_of_action(self, page: Page) -> None:
        """Set Cause of Action to FORECLOSURES via JS-manipulated multiselect."""
        try:
            result = await page.evaluate(
                """(targetLabel) => {
                    const sel = document.getElementById('caseTypes');
                    if (!sel) return 'select-not-found';
                    let matched = null;
                    const tgt = targetLabel.toUpperCase();
                    for (const opt of sel.options) {
                        if (opt.text.trim().toUpperCase() === tgt) {
                            opt.selected = true;
                            matched = opt.value;
                        } else {
                            opt.selected = false;
                        }
                    }
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    if (window.jQuery && window.jQuery(sel).multiselect) {
                        window.jQuery(sel).multiselect('refresh');
                    }
                    return matched || 'no-match';
                }""",
                CAUSE_OF_ACTION_VALUE,
            )
            if result and result not in ("select-not-found", "no-match"):
                self._dlog("cause_of_action_set",
                           value=CAUSE_OF_ACTION_VALUE, option_value=result)
                self.log.info(
                    f"Cause of Action = {CAUSE_OF_ACTION_VALUE} (val={result})"
                )
                return
            self._dlog("cause_of_action_no_match", result=result)
        except Exception as e:
            self._dlog("cause_of_action_error", error=str(e))
        self._dlog("cause_of_action_skipped")

    async def _submit_search(self, page: Page) -> None:
        # BenchmarkCP's submit handler reads visible inputs and overwrites
        # the hidden form fields just before posting. The visible date
        # picker plugin sometimes wipes the visible value after blur if it
        # can't parse the format we typed. So immediately before submit,
        # forcibly re-set BOTH visible and hidden date fields via JS — this
        # makes the search deterministic instead of racing the datepicker.
        if self.date_from and self.date_to:
            try:
                # self.date_from/date_to are already in US MM/DD/YYYY
                # format (the constructor calls _to_us_date on input)
                us_from = self.date_from
                us_to = self.date_to
                await page.evaluate(
                    """([from_val, to_val]) => {
                        // Set visible inputs and fire jQuery change so the
                        // datepicker doesn't decide it's stale and wipe them
                        const vFrom = document.getElementById('openedFrom');
                        const vTo = document.getElementById('openedTo');
                        if (vFrom) vFrom.value = from_val;
                        if (vTo) vTo.value = to_val;
                        if (window.jQuery) {
                            if (vFrom) window.jQuery(vFrom).trigger('change');
                            if (vTo) window.jQuery(vTo).trigger('change');
                        }
                        // Set hidden form fields too
                        document.querySelectorAll(
                            "input[name='openedFrom'][type='hidden']"
                        ).forEach(el => el.value = from_val);
                        document.querySelectorAll(
                            "input[name='openedTo'][type='hidden']"
                        ).forEach(el => el.value = to_val);
                    }""",
                    [us_from, us_to],
                )
                self._dlog("dates_reapplied_pre_submit",
                           from_us=us_from, to_us=us_to)
            except Exception as e:
                self._dlog("dates_reapply_error", error=str(e))

        # Pre-submit diagnostic: capture actual form field values so we can
        # tell whether the visible-input → hidden-field sync ever happened.
        try:
            form_state = await page.evaluate(
                """() => {
                    const get = name => {
                        const el = document.querySelector(
                            `input[name='${name}']`
                        );
                        return el ? el.value : null;
                    };
                    const visible = id => {
                        const el = document.getElementById(id);
                        return el ? el.value : null;
                    };
                    const selOpts = id => {
                        const sel = document.getElementById(id);
                        if (!sel) return null;
                        return Array.from(sel.selectedOptions).map(
                            o => o.value + ':' + o.text
                        );
                    };
                    return {
                        visible_openedFrom: visible('openedFrom'),
                        visible_openedTo:   visible('openedTo'),
                        hidden_openedFrom:  get('openedFrom'),
                        hidden_openedTo:    get('openedTo'),
                        courtTypes_sel:     selOpts('courTypes'),
                        caseTypes_sel:      selOpts('caseTypes'),
                        hidden_courtTypes:  get('courtTypes'),
                        hidden_caseTypes:   get('caseTypes'),
                    };
                }"""
            )
            self._dlog("pre_submit_form_state", **form_state)
            self.log.info(f"Pre-submit form state: {form_state}")
        except Exception as e:
            self._dlog("pre_submit_diagnostic_error", error=str(e))

        # The jQuery datepicker plugin owns #openedFrom and #openedTo and
        # wipes any value it didn't set via its own API. Use the proper
        # datepicker API to set both dates, then submit. Falls back to
        # direct value assignment if the picker isn't initialized.
        self.log.info("Submitting search ...")
        clicked = False
        if self.date_from and self.date_to:
            try:
                # self.date_from/date_to are already in US MM/DD/YYYY
                # format (the constructor calls _to_us_date on input)
                us_from = self.date_from
                us_to = self.date_to
                clicked = await page.evaluate(
                    """([from_val, to_val]) => {
                        const $ = window.jQuery;
                        const setVia = (id, val) => {
                            const el = document.getElementById(id);
                            if (!el) return 'no-el';
                            // Try jQuery datepicker API first (the picker
                            // wipes raw .value sets but accepts setDate)
                            try {
                                if ($ && $(el).data('datepicker')) {
                                    $(el).datepicker('setDate', val);
                                    return 'datepicker-api';
                                }
                            } catch (e) {}
                            // Fallback: set value + trigger jQuery change
                            el.value = val;
                            if ($) $(el).trigger('change');
                            return 'raw';
                        };
                        const fromMode = setVia('openedFrom', from_val);
                        const toMode = setVia('openedTo', to_val);
                        // Belt-and-suspenders: also set the hidden fields
                        // directly in case the picker didn't propagate
                        document.querySelectorAll(
                            "input[name='openedFrom'][type='hidden']"
                        ).forEach(el => el.value = from_val);
                        document.querySelectorAll(
                            "input[name='openedTo'][type='hidden']"
                        ).forEach(el => el.value = to_val);
                        // Submit via form.submit() to bypass any click
                        // handler that might re-read visible inputs
                        const form = document.querySelector('.searchform');
                        if (form) {
                            form.submit();
                            return 'form.submit ' + fromMode + '/' + toMode;
                        }
                        const btn = document.getElementById('searchButton');
                        if (btn) {
                            btn.click();
                            return 'btn.click ' + fromMode + '/' + toMode;
                        }
                        return 'no-submit';
                    }""",
                    [us_from, us_to],
                )
                if clicked and clicked != "no-submit":
                    self._dlog("search_submitted", selector=clicked)
                    self.log.info(f"Submitted via: {clicked}")
            except Exception as e:
                self._dlog("js_submit_error", error=str(e))
                clicked = False

        if not clicked:
            # Fallback to locator-based click (for searches without dates)
            for sel in ["input[type='submit'][value*='Search' i]",
                        "button:has-text('Search')",
                        "button[type='submit']"]:
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

    # ── Capture ─────────────────────────────────────────────────────

    async def _capture_results(self, page: Page) -> None:
        """Capture all pages of the DataTables-driven results grid.

        We iterate pages with the default page size (DataTables defaults to
        10 entries). Changing page size triggers a refresh that has bitten
        us before — wiped the date filter and returned ancient cases.
        """
        # Iterate pages — collect HTML from each, then aggregate parsing
        all_cases = []
        page_num = 1
        while True:
            html = await page.content()
            page_cases = parse_results_html(html)
            existing_nums = {c.case_number for c in all_cases}
            fresh = [c for c in page_cases if c.case_number not in existing_nums]
            all_cases.extend(fresh)
            self.log.info(
                f"  Page {page_num}: {len(page_cases)} on page, "
                f"{len(fresh)} new. Total: {len(all_cases)}"
            )
            self._dlog("page_captured", page=page_num,
                       on_page=len(page_cases), total=len(all_cases))

            # Try to advance to next page
            next_btn = page.locator(
                "#gridSearchResults_next:not(.disabled)"
            ).first
            if await next_btn.count() == 0:
                break
            try:
                await next_btn.click(timeout=5000)
                await page.wait_for_timeout(2000)
            except Exception:
                break
            page_num += 1
            if page_num > 20:  # safety stop
                self.log.warning("Pagination safety stop at page 20")
                break

        # Store the LAST page's HTML (most pages have similar structure)
        self.recon.results_html = await page.content()
        self.recon.results_screenshot = await page.screenshot(full_page=True)

        if len(all_cases) > self.max_cases:
            self.log.warning(
                f"Capping {len(all_cases)} cases at max_cases={self.max_cases}"
            )
            all_cases = all_cases[: self.max_cases]
        self.recon.parsed_cases = all_cases

        self.log.info(
            f"Parsed {len(all_cases)} foreclosure case numbers "
            f"across {page_num} page(s)."
        )
        self._dlog("results_parsed",
                   total_cases=len(all_cases),
                   pages=page_num)

    async def _capture_case_details_pages(self, page: Page) -> None:
        """Navigate to each case detail page and capture HTML.

        Benchmark CP case-detail URLs require an active search session (we
        get "Access Denied" from direct curl). We use the Playwright page
        (which has the session cookies from the search) to navigate.
        """
        n = min(self.capture_case_details, len(self.recon.parsed_cases))
        if n == 0:
            return
        self.log.info(f"Phase 2: capturing {n} case detail page(s) ...")

        base_url = "https://clerkofcourt.co.warren.oh.us"
        for i, case in enumerate(self.recon.parsed_cases[:n]):
            if not case.detail_url:
                self._dlog("case_detail_no_url", case_number=case.case_number)
                continue
            url = (case.detail_url if case.detail_url.startswith("http")
                   else base_url + case.detail_url)
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
            except Exception as e:
                cap.error = str(e)
                self._dlog("case_detail_error",
                           case_number=case.case_number, error=str(e))

            # Try to grab the Preliminary Judicial Report PDF — title
            # companies file this within days of the complaint and the
            # PDF includes the property's street address. We use this
            # as a fallback when the auditor parcel-lookup yields
            # nothing (no parcel in the case detail, or parcel not
            # registered yet). If no PJR exists (e.g. county-treasurer
            # tax foreclosures don't get one), fall back to the
            # COMPLAINT itself — the body usually states the property
            # address even in tax cases.
            if cap.html:
                try:
                    from h3.parsers.warren_complaint_pdf import (
                        find_pjr_docket_link, find_complaint_docket_link,
                        download_complaint_pdf,
                    )
                    link = find_pjr_docket_link(cap.html)
                    src = "PJR"
                    if not link:
                        # Final fallback: complaint PDF
                        link = find_complaint_docket_link(cap.html)
                        src = "COMPLAINT"
                    if link:
                        cid, digest = link
                        pdf_bytes, pdf_diag = await download_complaint_pdf(
                            page, cid, digest,
                        )
                        if pdf_bytes:
                            cap.pjr_pdf_bytes = pdf_bytes
                            cap.pjr_pdf_source = src
                            self._dlog("pjr_pdf_captured",
                                       case_number=case.case_number,
                                       cid=cid, pdf_bytes=len(pdf_bytes),
                                       source=src)
                        else:
                            cap.pjr_pdf_error = "empty_pdf"
                            self._dlog("pjr_pdf_empty",
                                       case_number=case.case_number,
                                       cid=cid, source=src, **pdf_diag)
                    else:
                        cap.pjr_pdf_error = "no_pjr_or_complaint_link"
                        self._dlog("pjr_pdf_no_link",
                                   case_number=case.case_number)
                except Exception as e:
                    cap.pjr_pdf_error = str(e)
                    self._dlog("pjr_pdf_error",
                               case_number=case.case_number, error=str(e))

            self.recon.case_details.append(cap)
            await page.wait_for_timeout(500)

    # ── Internal ────────────────────────────────────────────────────

    def _dlog(self, event: str, **kwargs: Any) -> None:
        entry = {"event": event, "ts": datetime.utcnow().isoformat() + "Z", **kwargs}
        self.recon.debug_log.append(entry)


class _StdoutLog:
    def info(self, msg: str) -> None: print(f"[INFO] {msg}")
    def warning(self, msg: str) -> None: print(f"[WARN] {msg}")
    def error(self, msg: str) -> None: print(f"[ERROR] {msg}")
