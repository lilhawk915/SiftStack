"""Clermont County (Ohio) Common Pleas — eservices.clermontclerk.org scraper.

Fifth CourtView county in the H3 stack (Greene + Clark + Butler + Miami +
Clermont). Clermont's filter UI is the cleanest of the CourtView five —
Case Type dropdown has a "CVE-FORECLOSURES" value that surfaces foreclosures
directly, no post-search regex needed.

Clermont-specific notes:

  1. Entry: "I Agree to terms of use" button on portal load — single click,
     no reCAPTCHA. Simpler than Clark's disclaimer or Butler's reCAPTCHA.

  2. Case Type = "CVE-FORECLOSURES" — direct filter (like Miami's Action
     Code approach, unlike Greene/Clark's "Civil + Initiating Action regex").

  3. Date range REQUIRED.

  4. SOP copy-paste artifacts: footer says CCO-001 (Clark's ID), comparison
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

PORTAL_URL = "https://eservices.clermontclerk.org/commonpleas/home.page"
CASE_TYPE_VALUE = "CVE-FORECLOSURES"

# Clermont format: "2026 CVE 00729" — 4-digit year + CVE + 5-digit seq
CASE_NUMBER_RE = re.compile(r"\b20\d{2}\s+CVE\s+\d{4,6}\b")

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
class ClermontParsedCase:
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
    parsed_cases: list[ClermontParsedCase] = field(default_factory=list)
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
class ClermontCaseDetail:
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


def parse_case_detail_html(html: str) -> ClermontCaseDetail:
    """Parse Clermont equivant CourtView case-detail page.

    Same DOM idioms as clermont_probate's parse_case_detail, just with
    foreclosure roles: Plaintiff, Defendant, Attorney for Plaintiff.
    """
    detail = ClermontCaseDetail()
    soup = BeautifulSoup(html, "html.parser")

    # Case number from h1/heading text
    m = re.search(r"\b(\d{4}\s+CVE\s+\d{1,6})\b", html)
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

    # Party section: ptyInfoLabel (name) + ptyType (role)
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


def parse_results_html(html: str) -> list[ClermontParsedCase]:
    """Parse equivant results grid.

    CourtView renders each row as <td id="grid~row-N~cell-3"> with a
    case-number link. Same DOM as clermont_probate. We also fall back
    to a regex scan over page text for any cases that don't fit the
    expected DOM pattern.
    """
    soup = BeautifulSoup(html, "html.parser")
    cases: list[ClermontParsedCase] = []
    seen: set[str] = set()

    # Primary path: equivant grid cells
    for td in soup.find_all("td", id=re.compile(r"grid~row-\d+~cell-3$")):
        a = td.find("a")
        if not a:
            continue
        text = a.get_text(" ", strip=True)
        m = CASE_NUMBER_RE.search(text)
        if not m:
            continue
        cn = re.sub(r"\s+", " ", m.group(0))
        if cn in seen:
            continue
        seen.add(cn)
        cases.append(ClermontParsedCase(
            case_number=cn,
            case_type=CASE_TYPE_VALUE,
            raw_row_text=text,
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
            cases.append(ClermontParsedCase(
                case_number=cn,
                case_type=CASE_TYPE_VALUE,
                raw_row_text=text[start:end].strip(),
            ))
    return cases


# ── The scraper ─────────────────────────────────────────────────────────

class ClermontScraper:
    """Playwright scraper for Clermont County CourtView CVE-FORECLOSURES."""

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
            f"ClermontScraper start | mode={self.mode} | "
            f"dates {self.date_from or '-'} → {self.date_to or '-'} | "
            f"headless={self.headless} | "
            f"capture_case_details={self.capture_case_details}"
        )
        async with async_playwright() as p:
            browser, ctx = await self._launch_browser(p)
            try:
                page = await ctx.new_page()
                await self._goto_portal(page)
                # Disclaimer page has an image captcha — must solve before
                # the "I Agree" click does anything. clermont_probate has
                # the same gate, we use the same 2Captcha-based solver.
                accepted = await self._solve_disclaimer_captcha(page)
                if not accepted:
                    self.log.warning("Could not get past disclaimer captcha")
                    self._dlog("disclaimer_failed")
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

    async def _solve_disclaimer_captcha(self, page: Page,
                                        max_attempts: int = 5) -> bool:
        """Solve Clermont's CourtView disclaimer CAPTCHA + click I Accept.

        Ported from clermont_probate.py — same disclaimer page, same
        captcha format. The CourtView captcha is HEAVILY obscured and
        2Captcha's standard OCR often returns partial answers; retry up
        to `max_attempts` times — each retry reloads for a fresh image.
        Returns True if we got past the disclaimer.
        """
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.log.info(
                    f"CAPTCHA attempt {attempt}/{max_attempts} — reloading"
                )
                await page.goto(PORTAL_URL, wait_until="domcontentloaded",
                                timeout=30000)
                await page.wait_for_timeout(2000)
            success = await self._try_solve_once(page)
            if success:
                await page.wait_for_timeout(1500)
                still_on_disclaimer = await page.locator(
                    "img.captchaImg"
                ).count() > 0
                if not still_on_disclaimer:
                    self.log.info(f"  Past disclaimer on attempt {attempt}")
                    return True
                self._dlog("captcha_rejected", attempt=attempt)
                self.log.warning(
                    f"  CAPTCHA rejected on attempt {attempt}, retrying"
                )
        self.log.warning(
            f"Could not solve CAPTCHA after {max_attempts} attempts"
        )
        return False

    async def _try_solve_once(self, page: Page) -> bool:
        from captcha.twocaptcha import (
            get_api_key, solve_image_captcha, TwoCaptchaError,
        )
        api_key = get_api_key(self.captcha_api_key)
        if not api_key:
            self.log.warning(
                "No 2Captcha API key — Clermont disclaimer cannot be bypassed"
            )
            self._dlog("captcha_no_api_key")
            return False

        try:
            img_loc = page.locator("img.captchaImg").first
            if await img_loc.count() == 0:
                self.log.warning("No captcha image on Clermont landing")
                self._dlog("captcha_image_not_found")
                return False
        except Exception as e:
            self._dlog("captcha_locate_error", error=str(e))
            return False

        image_bytes = b""
        try:
            self.log.info("Fetching Clermont CAPTCHA image ...")
            captcha_src = await img_loc.get_attribute("src")
            if captcha_src:
                if captcha_src.startswith("?"):
                    full_url = PORTAL_URL + captcha_src
                elif captcha_src.startswith("/"):
                    portal_host = "/".join(PORTAL_URL.split("/")[:3])
                    full_url = portal_host + captcha_src
                elif captcha_src.startswith("http"):
                    full_url = captcha_src
                else:
                    base = page.url.rsplit("?", 1)[0]
                    full_url = base + "?" + captcha_src.lstrip("?&")
                resp = await page.context.request.get(
                    full_url, timeout=15000
                )
                if resp.ok:
                    image_bytes = await resp.body()
                    self._dlog("captcha_image_fetched_url",
                               bytes=len(image_bytes))
        except Exception as e:
            self._dlog("captcha_url_fetch_failed", error=str(e))

        if not image_bytes:
            try:
                image_bytes = await img_loc.screenshot()
                self._dlog("captcha_image_screenshot_fallback",
                           bytes=len(image_bytes))
            except Exception as e:
                self.log.warning(f"CAPTCHA capture failed: {e}")
                self._dlog("captcha_image_error", error=str(e))
                return False
        self.recon.captcha_image_bytes = image_bytes

        try:
            self.log.info("Submitting CAPTCHA to 2Captcha ...")
            answer = await solve_image_captcha(
                image_bytes,
                api_key=api_key,
                case_sensitive=False,
                min_length=4,
                max_length=8,
                logger=self.log,
            )
            self._dlog("captcha_solved", answer_length=len(answer))
            self.recon.captcha_answer = answer
        except TwoCaptchaError as e:
            self.log.warning(f"2Captcha failed: {e}")
            self._dlog("captcha_solve_failed", error=str(e))
            return False

        try:
            await page.locator(
                "input[name='captchaPanel:challengePassword']"
            ).first.fill(answer)
            self._dlog("captcha_response_filled")
            self.log.info(f"  CAPTCHA answer filled: {answer}")
        except Exception as e:
            self.log.warning(f"CAPTCHA fill error: {e}")
            self._dlog("captcha_fill_error", error=str(e))
            return False

        self.log.info("Clicking I Accept ...")
        clicked = False
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
                elif strategy == "js_submit":
                    await page.evaluate(
                        """() => {
                            const btn = document.querySelector(
                                "input[type='submit'][name='linkFrag:beginButton']"
                            );
                            if (btn && btn.form) btn.form.submit();
                        }"""
                    )
                self._dlog("disclaimer_button_clicked", strategy=strategy)
                self.log.info(f"  Click strategy: {strategy}")
                clicked = True
                break
            except Exception as e:
                self._dlog("disclaimer_button_strategy_failed",
                           strategy=strategy, error=str(e))
                continue
        if not clicked:
            self.log.warning("All I-Accept click strategies failed")
            return False

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
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

        # Select the foreclosure case type. Confirmed via v3 recon: the
        # dropdown shows "CVE - Foreclosure" (value=4843, exact label).
        case_type_set = False
        for label in [
            "CVE - Foreclosure",
            "CVE-Foreclosure",
            "CVE - FORECLOSURE",
            "Foreclosure",
        ]:
            try:
                sel = "select[name='caseCd']"
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
        # Clermont uses Wicket-style nested field names same as probate
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

        # Paginate via numeric page links if present
        page_num = 2
        while page_num < 20:  # safety
            try:
                next_link = page.locator(
                    f"a:has-text('{page_num}')"
                ).first
                if await next_link.count() == 0:
                    break
                if not await next_link.is_visible():
                    break
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
            if not fresh:
                break
            page_num += 1

        if len(all_cases) > self.max_cases:
            self.log.warning(
                f"Capping {len(all_cases)} cases at max_cases={self.max_cases}"
            )
            all_cases = all_cases[: self.max_cases]
        self.recon.parsed_cases = all_cases

        self.log.info(
            f"Parsed {len(all_cases)} CVE-FORECLOSURE case numbers "
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

        # After pagination we may be on a later results page that doesn't
        # contain page-1 cases. Always navigate back to the original
        # results URL first so case-number links resolve.
        try:
            await page.goto(results_url, wait_until="domcontentloaded",
                             timeout=20000)
            await page.wait_for_timeout(1500)
        except Exception as e:
            self._dlog("initial_results_nav_failed", error=str(e))

        for i, target in enumerate(self.recon.parsed_cases[:n]):
            cn = target.case_number
            try:
                row_link = page.locator(f"a:has-text('{cn}')").first
                if await row_link.count() == 0:
                    # Try paginating forward to find the link
                    found = False
                    for p_num in range(2, 6):
                        try:
                            next_link = page.locator(
                                f"a:has-text('{p_num}')"
                            ).first
                            if await next_link.count() == 0:
                                break
                            if not await next_link.is_visible():
                                break
                            await next_link.click()
                            await page.wait_for_load_state(
                                "domcontentloaded", timeout=15000
                            )
                            await page.wait_for_timeout(1500)
                            row_link = page.locator(
                                f"a:has-text('{cn}')"
                            ).first
                            if await row_link.count() > 0:
                                found = True
                                break
                        except Exception:
                            continue
                    if not found:
                        self._dlog("case_link_not_found", case_number=cn)
                        continue

                self.log.info(f"  [{i+1}/{n}] clicking {cn} ...")
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
                    try:
                        await page.goto(results_url,
                                         wait_until="domcontentloaded",
                                         timeout=20000)
                        await page.wait_for_timeout(1500)
                    except Exception as e:
                        self._dlog("results_nav_failed",
                                   case_number=cn, error=str(e))
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
