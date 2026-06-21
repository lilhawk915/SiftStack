"""Butler County (Ohio) Common Pleas — eservices.clermontclerk.org scraper.

Fifth CourtView county in the H3 stack (Greene + Butler + Butler + Butler +
Butler). Butler's filter UI is the cleanest of the CourtView five —
Case Type dropdown has a "CV-FORECLOSURES" value that surfaces foreclosures
directly, no post-search regex needed.

Butler-specific notes:

  1. Entry: "I Agree to terms of use" button on portal load — single click,
     no reCAPTCHA. Simpler than Butler's disclaimer or Butler's reCAPTCHA.

  2. Case Type = "CV-FORECLOSURES" — direct filter (like Butler's Action
     Code approach, unlike Greene/Butler's "Civil + Initiating Action regex").

  3. Date range REQUIRED.

  4. SOP copy-paste artifacts: footer says CCO-001 (Butler's ID), comparison
     table on p6 lists wrong /probate/ portal URL and contradicts the body's
     Ctrl+A workflow. Body + QC checklist are authoritative.

Once we have recon HTML for all 5 CourtView counties, refactor to
scrapers/courtview_base.py.
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


# ── Portal config (per H3 SOP H3-SOP-CLCO-001) ──────────────────────────

PORTAL_URL = "https://clerkservices.bcohio.gov/eservices/"
CASE_TYPE_VALUE = "Foreclosure"

# Butler case format: "2026 CV F 01462" — 4-digit year + CV + 1-letter
# action prefix (F=foreclosure, C=contracts, etc.) + 5-digit seq.
# Butler format: "CV 2026 05 1282" — CV + 4-digit year + 2-digit month
# + 4-digit sequence.
CASE_NUMBER_RE = re.compile(r"\bCV\s+20\d{2}\s+\d{2}\s+\d{4}\b")

TERMS_BUTTONS = [
    "button:has-text('I Agree')",
    "button:has-text('I AGREE')",
    "input[type='submit'][value*='Agree' i]",
    "a:has-text('I Agree')",
]

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
class ButlerParsedCase:
    case_number: str
    case_type: str = ""
    status: str = ""
    raw_row_text: str = ""


@dataclass
class CaseDetailCapture:
    case_number: str
    final_url: str = ""
    html: str = ""
    error: str = ""


@dataclass
class ReconCapture:
    results_html: str = ""
    results_screenshot: bytes = b""
    parsed_cases: list[ButlerParsedCase] = field(default_factory=list)
    case_details: list[CaseDetailCapture] = field(default_factory=list)
    debug_log: list[dict[str, Any]] = field(default_factory=list)
    captcha_image_bytes: bytes = b""
    captcha_answer: str = ""


# ── Parser ──────────────────────────────────────────────────────────────

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

_PLACEHOLDER_HINTS = re.compile(
    r"^(DOE,?\s*(JANE|JOHN)|JANE\s+OR\s+JAMES\s+DOE|JOHN\s+DOE|"
    r"JANE\s+DOE|JOHN\s+OR\s+JANE\s+DOE)",
    re.IGNORECASE,
)

_PERSON_NAME_RE = re.compile(
    r"^[A-Z][A-Z\-'\.]+(?:\s[A-Z][A-Z\-'\.]+)?\s*,\s*[A-Z][A-Z\-'\.]+",
    re.IGNORECASE,
)


def _looks_like_person(name: str) -> bool:
    if not name:
        return False
    if _CORPORATE_HINTS.search(name):
        return False
    if _PLACEHOLDER_HINTS.search(name.strip()):
        return False
    return True


@dataclass
class ButlerCaseDetail:
    case_number: str = ""
    case_type: str = ""
    file_date: str = ""
    plaintiff: str = ""
    defendants: list[str] = field(default_factory=list)
    attorney: str = ""
    action: str = ""  # Complaint action (e.g. "Foreclosures", "DELINQUENT TAX FORECLOSURE")
    # When the named borrower has died, the docket lists "UNKNOWN HEIRS
    # OF <DECEDENT> DECEASED" as a placeholder defendant. We extract the
    # decedent name so the DM row can be flagged Unknown Heirs = Y AND
    # so primary_owner skips the deceased defendant in favor of the
    # surviving spouse / heir.
    decedent: str = ""

    @property
    def primary_owner(self) -> str:
        """First defendant that's a real person, prefer LAST, FIRST.

        Skips defendants whose name matches the decedent (when present)
        so the surviving spouse / heir is picked instead.
        """
        from parsers.owner_refinements import (
            strip_role_middle, is_decedent_match,
        )
        candidates: list[str] = []
        for d in self.defendants:
            cleaned = strip_role_middle(d)
            if self.decedent and is_decedent_match(cleaned, self.decedent):
                continue
            candidates.append(cleaned)
        for d in candidates:
            if _looks_like_person(d) and _PERSON_NAME_RE.search(d):
                return d
        for d in candidates:
            if _looks_like_person(d):
                return d
        return candidates[0] if candidates else (
            self.defendants[0] if self.defendants else ""
        )


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _normalize_date(s: str) -> str:
    """Convert MM/DD/YYYY → YYYY-MM-DD."""
    s = _clean(s)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mo, da, yr = m.groups()
        return f"{yr}-{int(mo):02d}-{int(da):02d}"
    return s


def parse_case_detail_html(html: str) -> ButlerCaseDetail:
    """Parse Butler equivant CourtView case-detail page.

    Same DOM idioms as clermont_probate's parse_case_detail, just with
    foreclosure roles: Plaintiff, Defendant, Attorney for Plaintiff.
    """
    detail = ButlerCaseDetail()
    soup = BeautifulSoup(html, "html.parser")

    # Case number from h1/heading text — Butler format: "CV 2026 05 1282"
    m = re.search(
        r"\b(CV\s+20\d{2}\s+\d{2}\s+\d{4})\b",
        html,
    )
    if m:
        detail.case_number = m.group(1)

    fields: dict[str, str] = {}
    for li_label in soup.find_all("li", class_="caseHdrLabel"):
        li_value = li_label.find_next_sibling("li", class_="caseHdrInfo")
        if not li_value:
            continue
        label = _clean(li_label.get_text()).rstrip(":")
        value = _clean(li_value.get_text(" "))
        if label and label not in fields:
            fields[label] = value
    detail.case_type = fields.get("Case Type", "")
    detail.file_date = _normalize_date(fields.get("File Date", ""))
    detail.action = fields.get("Action", "")

    # Butler's case-detail uses <div class="ptyInfoLabel"> + ptyType
    # (same as Clermont/Clark), not Greene's <span class="pty-name">.
    role_re = re.compile(
        r"\s*-\s*(Plaintiff|Defendant|Attorney(?:\s+for\s+\w+)?)",
        re.I,
    )
    for label_div in soup.find_all("div", class_="ptyInfoLabel"):
        name = _clean(label_div.get_text(" "))
        if not name:
            continue
        name = re.sub(r"\s+,\s+", ", ", name)
        name = re.sub(r"\s{2,}", " ", name)
        type_div = label_div.find_next("div", class_="ptyType")
        if not type_div:
            continue
        role_text = _clean(type_div.get_text(" "))
        m = role_re.search(role_text)
        if not m:
            continue
        role = m.group(1).title()
        if role == "Plaintiff" and not detail.plaintiff:
            detail.plaintiff = name
        elif role == "Defendant":
            if name not in detail.defendants:
                detail.defendants.append(name)
        elif role.startswith("Attorney") and not detail.attorney:
            detail.attorney = name

    # Detect "UNKNOWN HEIRS OF <DECEDENT> DECEASED" placeholder so
    # primary_owner skips the deceased defendant and the H3 row can be
    # flagged Unknown Heirs = Y.
    from parsers.owner_refinements import extract_decedent
    detail.decedent = extract_decedent(detail.defendants)

    return detail


def parse_results_html(html: str) -> list[ButlerParsedCase]:
    """Parse equivant results grid.

    CourtView renders each row as <td id="grid~row-N~cell-3"> with a
    case-number link. Same DOM as clermont_probate. We also fall back
    to a regex scan over page text for any cases that don't fit the
    expected DOM pattern.
    """
    soup = BeautifulSoup(html, "html.parser")
    cases: list[ButlerParsedCase] = []
    seen: set[str] = set()

    # Butler's grid puts party name in cell-3 and the CASE NUMBER in
    # cell-6 (with date/action in 7/8/9). Walk rows by their cell-6.
    # Filter to "Foreclosure" action via row text scan.
    fc_re = re.compile(r"\bForeclosure\b", re.I)
    for td in soup.find_all("td", id=re.compile(r"grid~row-\d+~cell-6$")):
        text = td.get_text(" ", strip=True)
        m = CASE_NUMBER_RE.search(text)
        if not m:
            continue
        cn = re.sub(r"\s+", " ", m.group(0))
        if cn in seen:
            continue
        tr = td.find_parent("tr")
        row_text = tr.get_text(" ", strip=True) if tr else text
        if not fc_re.search(row_text):
            continue
        seen.add(cn)
        cases.append(ButlerParsedCase(
            case_number=cn,
            case_type=CASE_TYPE_VALUE,
            raw_row_text=row_text,
        ))

    # Fallback: regex scan over text
    if not cases:
        text = soup.get_text(" ", strip=True)
        for m in CASE_NUMBER_RE.finditer(text):
            cn = re.sub(r"\s+", " ", m.group(0))
            if cn in seen:
                continue
            seen.add(cn)
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 200)
            cases.append(ButlerParsedCase(
                case_number=cn,
                case_type=CASE_TYPE_VALUE,
                raw_row_text=text[start:end].strip(),
            ))
    return cases


# ── The scraper ─────────────────────────────────────────────────────────

class ButlerScraper:
    """Playwright scraper for Butler County CourtView CV-FORECLOSURES."""

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
        captcha_api_key: str = "",
        logger: Any = None,
    ):
        self.date_from = _to_us_date(date_from)
        self.date_to = _to_us_date(date_to)
        self.proxy_url = proxy_config_url
        self.headless = headless
        self.mode = mode
        self.max_cases = max_cases
        self.capture_case_details = capture_case_details
        self.captcha_api_key = captcha_api_key
        self.log = logger if logger else _StdoutLog()
        self.recon: ReconCapture = ReconCapture()

    async def run(self) -> list[CaseRecord]:
        self.log.info(
            f"ButlerScraper start | mode={self.mode} | "
            f"dates {self.date_from or '-'} → {self.date_to or '-'} | "
            f"headless={self.headless} | "
            f"capture_case_details={self.capture_case_details}"
        )
        async with async_playwright() as p:
            browser, ctx = await self._launch_browser(p)
            try:
                page = await ctx.new_page()
                await self._goto_portal(page)
                # Butler's portal: lands directly on disclaimer page with
                # reCAPTCHA v2. Solve, then click I Accept (linkFrag:beginButton).
                solved = await self._solve_recaptcha_v2(page)
                if not solved:
                    self.log.warning("Could not solve reCAPTCHA")
                    self._dlog("recaptcha_failed")
                    return []
                # Click I Accept to leave the disclaimer
                clicked_accept = await self._click_accept_button(page)
                if not clicked_accept:
                    self.log.warning("Could not click I Accept")
                    self._dlog("accept_button_not_found")
                    return []
                await self._set_case_type_filter(page)
                await self._set_date_range(page)
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
        await page.wait_for_timeout(5000)

    async def _click_accept_button(self, page) -> bool:
        """Click the disclaimer I Accept button (linkFrag:beginButton)."""
        for strategy in ["force_click", "js_click", "js_submit"]:
            try:
                if strategy == "force_click":
                    await page.locator(
                        "input[type='submit'][name='linkFrag:beginButton']"
                    ).first.click(force=True, timeout=8000)
                elif strategy == "js_click":
                    await page.evaluate(
                        """() => {
                            const btn = document.querySelector(
                                "input[type='submit'][name='linkFrag:beginButton']"
                            );
                            if (btn) btn.click();
                        }"""
                    )
                else:
                    await page.evaluate(
                        """() => {
                            const btn = document.querySelector(
                                "input[type='submit'][name='linkFrag:beginButton']"
                            );
                            if (btn && btn.form) btn.form.submit();
                        }"""
                    )
                self._dlog("accept_clicked", strategy=strategy)
                self.log.info(f"Clicked I Accept via {strategy}")
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=20000
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(2500)
                return True
            except Exception as e:
                self._dlog("accept_click_failed",
                           strategy=strategy, error=str(e)[:100])
                continue
        return False

    async def _click_case_search_entry(self, page) -> bool:
        """Click the 'Click Here' button inside the Case Search card."""
        # Try common selectors used by equivant landing pages
        selectors = [
            "xpath=//*[normalize-space(.)='Case Search']/ancestor::*[self::div or self::section][1]//*[self::a or self::button][contains(translate(normalize-space(.), 'CH', 'ch'), 'click here')]",
            "a:has-text('Click Here')",
            "button:has-text('Click Here')",
            "a:has-text('Case Search')",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click()
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=15000
                    )
                    await page.wait_for_timeout(3000)
                    self._dlog("case_search_entry_clicked", selector=sel)
                    self.log.info(f"Clicked Case Search via {sel[:60]}")
                    return True
            except Exception as e:
                self._dlog("entry_click_failed",
                           selector=sel[:60], error=str(e))
                continue
        return False

    async def _solve_recaptcha_v2(self, page) -> bool:
        """Find reCAPTCHA sitekey, solve via 2Captcha, inject token."""
        from captcha.twocaptcha import (
            get_api_key, solve_recaptcha_v2, TwoCaptchaError,
        )
        api_key = get_api_key(self.captcha_api_key)
        if not api_key:
            self._dlog("recaptcha_no_api_key")
            return False

        # Look for the reCAPTCHA sitekey — in DOM, iframe src, or HTML.
        import re as _re
        try:
            sitekey = await page.evaluate(
                """() => {
                    const el = document.querySelector('.g-recaptcha')
                        || document.querySelector('[data-sitekey]');
                    if (el) return el.getAttribute('data-sitekey');
                    const iframes = document.querySelectorAll(
                        'iframe[src*="recaptcha"]'
                    );
                    for (const f of iframes) {
                        const m = f.src.match(/[?&]k=([^&]+)/);
                        if (m) return m[1];
                    }
                    return null;
                }"""
            )
        except Exception as e:
            self._dlog("recaptcha_sitekey_lookup_failed", error=str(e))
            sitekey = None
        if not sitekey:
            # Fallback: search raw HTML for sitekey
            try:
                html = await page.content()
                m = _re.search(
                    r'(?:data-sitekey|sitekey|[\'"]k[\'"])\s*[=:]\s*[\'"]([\w-]{30,})[\'"]',
                    html,
                )
                if m:
                    sitekey = m.group(1)
            except Exception:
                pass
        if not sitekey:
            self._dlog("recaptcha_no_sitekey",
                       note="page may not have reCAPTCHA")
            return True  # nothing to solve, continue

        self.log.info(
            f"Found reCAPTCHA sitekey {sitekey[:20]}..., solving"
        )
        pageurl = page.url
        try:
            token = await solve_recaptcha_v2(
                api_key=api_key,
                sitekey=sitekey,
                pageurl=pageurl,
                logger=self.log,
            )
        except TwoCaptchaError as e:
            self.log.warning(f"reCAPTCHA solve failed: {e}")
            self._dlog("recaptcha_solve_failed", error=str(e))
            return False

        # Inject token into the page
        try:
            await page.evaluate(
                """(token) => {
                    const el = document.getElementById('g-recaptcha-response')
                        || document.querySelector('[name="g-recaptcha-response"]');
                    if (el) {
                        el.style.display = 'block';
                        el.innerHTML = token;
                        el.value = token;
                    }
                    // Some sites use a callback — trigger it if present
                    if (typeof window.___grecaptcha_cfg !== 'undefined') {
                        try {
                            const clients = window.___grecaptcha_cfg.clients;
                            for (const k in clients) {
                                const c = clients[k];
                                for (const k2 in c) {
                                    const obj = c[k2];
                                    for (const k3 in obj) {
                                        const inner = obj[k3];
                                        if (inner && inner.callback) {
                                            inner.callback(token);
                                        }
                                    }
                                }
                            }
                        } catch (e) {}
                    }
                }""",
                token,
            )
            self._dlog("recaptcha_injected", token_len=len(token))
            self.log.info("  reCAPTCHA token injected")
        except Exception as e:
            self._dlog("recaptcha_inject_failed", error=str(e))
            return False

        # Wait for the page to react / submit-button to enable
        await page.wait_for_timeout(3000)
        return True

    async def _set_case_type_filter(self, page: Page) -> None:
        """Set Case Type tab + dropdown.

        Mirrors clermont_probate's _fill_and_search but selects a foreclosure
        case type instead of Estate. CourtView's form uses tabs; the Case
        Type tab bypasses the required Last/First Name fields.
        """
        # Switch to Case Type tab
        for sel in [
            "a:has-text('Case Type')",
            "li:has-text('Case Type') a",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    self._dlog("case_type_tab_clicked", selector=sel)
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        # Butler caseCd has only broad case types (CIVIL, CRIMINAL, etc.) —
        # no per-action-code foreclosure entry. Select CIVIL and rely on
        # post-search initiating-action filtering (same as Greene).
        case_type_set = False
        # Butler uses "CIVIL - GENERAL" / value="CV"
        sel = "select[name='caseCd']"
        try:
            if await page.locator(sel).count() > 0:
                # Try by value first (most reliable)
                await page.locator(sel).first.select_option(value="CV")
                self._dlog("case_type_selected", value="CV")
                self.log.info("Case Type = CV (CIVIL - GENERAL)")
                case_type_set = True
        except Exception:
            pass
        if not case_type_set:
            for label in [
                "CIVIL - GENERAL",
                "CIVIL",
                "Civil",
            ]:
                try:
                    if await page.locator(sel).count() > 0:
                        await page.locator(sel).first.select_option(label=label)
                        self._dlog("case_type_selected", label=label)
                        self.log.info(f"Case Type = {label}")
                        case_type_set = True
                        break
                except Exception:
                    continue
        if not case_type_set:
            self._dlog("case_type_not_found",
                       note="will rely on date-range only")

    async def _set_date_range(self, page: Page) -> None:
        # Butler uses Wicket-style nested field names same as probate
        for field, value in [
            ("fileDateRange:dateInputBegin", self.date_from),
            ("fileDateRange:dateInputEnd", self.date_to),
        ]:
            if not value:
                continue
            try:
                sel = f"input[name='{field}']"
                if await page.locator(sel).count() > 0:
                    await page.locator(sel).first.fill(value)
                    await page.locator(sel).first.blur()
                    self._dlog("date_filled", field=field, value=value)
                    self.log.info(f"  {field} = {value}")
                    await page.wait_for_timeout(500)
            except Exception as e:
                self._dlog("date_fill_error", field=field, error=str(e))

    async def _submit_search(self, page: Page) -> None:
        self.log.info("Submitting search ...")
        for sel in [
            "input[type='submit'][value='Search']",
            "input[name='submitLink']",
            "button:has-text('Search')",
            "input[type='submit'][value*='Search' i]",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    self._dlog("search_submitted", selector=sel)
                    self.log.info(f"  Clicked Search via {sel}")
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
        """Capture page 1 HTML + screenshot, then paginate via numeric
        page links ('2', '3', ...) until exhausted or the page count
        stops growing. Each page's parse adds unique case numbers.
        """
        self.log.info("Capturing results page 1 HTML + screenshot")
        self.recon.results_html = await page.content()
        self.recon.results_screenshot = await page.screenshot(full_page=True)
        self._results_page_url = page.url

        all_cases = parse_results_html(self.recon.results_html)
        self.log.info(
            f"  Page 1: {len(all_cases)} unique cases"
        )

        # Butler uses equivant pagination: prefer #nextPaginationLink /
        # "Next »" link, fall back to numeric page links. Don't stop
        # early on 0-fresh — non-foreclosure rows can appear between
        # foreclosure pages.
        page_num = 2
        while page_num < 30:  # safety
            next_link = None
            for sel in [
                "a#nextPaginationLink",
                "a:has-text('Next »')",
                "a:has-text('Next')",
                f"a:has-text('{page_num}')",
            ]:
                try:
                    cand = page.locator(sel).first
                    if (await cand.count()) > 0 and (await cand.is_visible()):
                        next_link = cand
                        break
                except Exception:
                    continue
            if next_link is None:
                break
            try:
                await next_link.click()
                await page.wait_for_load_state("domcontentloaded",
                                                timeout=15000)
                await page.wait_for_timeout(2000)
            except Exception as e:
                self._dlog("paginate_error",
                           page_num=page_num, error=str(e))
                break
            html = await page.content()
            page_cases = parse_results_html(html)
            existing = {c.case_number for c in all_cases}
            fresh = [c for c in page_cases if c.case_number not in existing]
            all_cases.extend(fresh)
            self.log.info(
                f"  Page {page_num}: {len(page_cases)} on page, "
                f"{len(fresh)} new. Total: {len(all_cases)}"
            )
            self._dlog("page_captured",
                       page=page_num,
                       on_page=len(page_cases),
                       total=len(all_cases))
            page_num += 1

        if len(all_cases) > self.max_cases:
            self.log.warning(
                f"Capping {len(all_cases)} cases at max_cases={self.max_cases}"
            )
            all_cases = all_cases[: self.max_cases]
        self.recon.parsed_cases = all_cases

        self.log.info(
            f"Parsed {len(all_cases)} CV-FORECLOSURE case numbers "
            f"across {page_num - 1} page(s)."
        )
        self._dlog("results_parsed",
                   total_cases=len(all_cases),
                   pages=page_num - 1)

    async def _capture_case_details_pages(self, page: Page) -> None:
        """Click into each case in the results grid, capture HTML, return.

        Same Wicket-AJAX pattern as clermont_probate — all row links share
        a single URL token so we navigate by clicking the visible case-#
        text. After each case detail, we navigate back to results.
        """
        n = min(self.capture_case_details, len(self.recon.parsed_cases))
        if n == 0:
            return
        self.log.info(f"Phase 2: capturing {n} case detail page(s) ...")
        results_url = getattr(self, "_results_page_url", page.url)

        # Start on results page 1
        try:
            await page.goto(results_url, wait_until="domcontentloaded",
                             timeout=20000)
            await page.wait_for_timeout(1500)
        except Exception as e:
            self._dlog("initial_results_nav_failed", error=str(e))

        current_results_page = 1
        for i, target in enumerate(self.recon.parsed_cases[:n]):
            cn = target.case_number
            try:
                row_link = page.locator(f"a:has-text('{cn}')").first
                while await row_link.count() == 0:
                    next_p = current_results_page + 1
                    next_link = None
                    for sel in [
                        "a#nextPaginationLink",
                        "a:has-text('Next »')",
                        "a:has-text('Next')",
                        f"a:has-text('{next_p}')",
                    ]:
                        try:
                            c = page.locator(sel).first
                            if (await c.count()) > 0 and (await c.is_visible()):
                                next_link = c
                                break
                        except Exception:
                            continue
                    if next_link is None:
                        break
                    try:
                        await next_link.click()
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=15000
                        )
                        await page.wait_for_timeout(1500)
                        current_results_page = next_p
                    except Exception:
                        break
                    row_link = page.locator(f"a:has-text('{cn}')").first

                if await row_link.count() == 0:
                    self._dlog("case_link_not_found", case_number=cn,
                               results_page=current_results_page)
                    continue

                self.log.info(
                    f"  [{i+1}/{n}] clicking {cn} "
                    f"(results page {current_results_page}) ..."
                )
                await row_link.click()
                await page.wait_for_load_state("domcontentloaded",
                                                 timeout=20000)
                await page.wait_for_timeout(2000)

                cap = CaseDetailCapture(case_number=cn)
                cap.html = await page.content()
                cap.final_url = page.url
                self._dlog("case_detail_captured",
                           case_number=cn,
                           html_bytes=len(cap.html))
                self.recon.case_details.append(cap)

                if i < n - 1:
                    # Butler has many results pages (~58 cases across 8).
                    # go_back proved unreliable here (lands on stale page
                    # state). Re-navigate to results_url and paginate
                    # forward to current_results_page each time — slower
                    # (~8s per case at page 8) but reliable.
                    target_page = current_results_page
                    try:
                        await page.goto(results_url,
                                         wait_until="domcontentloaded",
                                         timeout=20000)
                        await page.wait_for_timeout(1500)
                    except Exception:
                        break
                    current_results_page = 1
                    while current_results_page < target_page:
                        next_p = current_results_page + 1
                        adv_link = None
                        for sel in [
                            "a#nextPaginationLink",
                            "a:has-text('Next »')",
                            "a:has-text('Next')",
                            f"a:has-text('{next_p}')",
                        ]:
                            try:
                                c = page.locator(sel).first
                                if (await c.count()) > 0 and (
                                        await c.is_visible()):
                                    adv_link = c
                                    break
                            except Exception:
                                continue
                        if adv_link is None:
                            break
                        try:
                            await adv_link.click()
                            await page.wait_for_load_state(
                                "domcontentloaded", timeout=15000
                            )
                            await page.wait_for_timeout(1500)
                            current_results_page = next_p
                        except Exception:
                            break
            except Exception as e:
                self.log.warning(f"  {cn} failed: {e}")
                self._dlog("case_iteration_error",
                           case_number=cn, error=str(e))

    # ── Internal ────────────────────────────────────────────────────

    def _dlog(self, event: str, **kwargs: Any) -> None:
        entry = {"event": event, "ts": datetime.utcnow().isoformat() + "Z", **kwargs}
        self.recon.debug_log.append(entry)


class _StdoutLog:
    def info(self, msg: str) -> None: print(f"[INFO] {msg}")
    def warning(self, msg: str) -> None: print(f"[WARN] {msg}")
    def error(self, msg: str) -> None: print(f"[ERROR] {msg}")
