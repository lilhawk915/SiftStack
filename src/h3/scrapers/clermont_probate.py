"""Clermont County (Ohio) Probate Court — eservices.clermontclerk.org/probate scraper.

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
from h3.parsers.clermont_probate_case_detail import (
    ClermontProbateDetail,
    parse_case_detail,
)


def _detail_to_record(detail: ClermontProbateDetail) -> ProbateRecord:
    """Convert Clermont case-detail → ProbateRecord.

    7 of 12 columns populated from the case-detail summary page. The other
    5 (DOD, Fiduciary Address, Phone, Email, Subject Property) live on
    party-detail subpages and require additional Wicket-Ajax clicks —
    deferred to a follow-up iteration.
    """
    return ProbateRecord(
        case_number=detail.case_number,
        case_type=detail.case_type,
        date_filed=detail.file_date,
        decedent_name=detail.decedent_name,
        date_of_death="",                       # TODO from party detail
        action=detail.action,
        relationship="",                        # TODO from party detail
        fiduciary_name=detail.fiduciary_name,
        fiduciary_address="",                   # TODO from party detail
        fiduciary_phone="",                     # TODO from party detail
        fiduciary_email="",                     # TODO from PDF
        subject_property="",                    # TODO from Decedent party detail
        notes=(
            f"Status: {detail.case_status}; Judge: {detail.case_judge}"
            + (f"; Atty: {detail.attorney_name}" if detail.attorney_name else "")
        ),
    )


PORTAL_URL = "https://eservices.clermontclerk.org/probate"
PORTAL_HOST = "https://eservices.clermontclerk.org"

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
class ClermontProbateCase:
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
    captcha_image_bytes: bytes = b""    # raw captcha image we sent to 2Captcha
    captcha_answer: str = ""            # what 2Captcha returned
    parsed_cases: list[ClermontProbateCase] = field(default_factory=list)
    case_details: list[CaseDetailCapture] = field(default_factory=list)
    debug_log: list[dict[str, Any]] = field(default_factory=list)
    probate_records: list[ProbateRecord] = field(default_factory=list)


# Clermont case number format: YYYY ES NNNNN (with spaces, e.g. "2026 ES 00304")
CLERMONT_CASE_NUMBER_RE = re.compile(r"\b(\d{4})\s+ES\s+(\d{1,5})\b")


def _parse_clermont_results(html: str) -> list[ClermontProbateCase]:
    """Parse equivant Clermont results table.

    Each case is rendered as an <a> inside `<td id="grid~row-N~cell-3">`
    containing the case number text. All rows share the same Wicket URL —
    actual navigation happens via JS event handlers on click. We rely on
    Playwright .click() in the scraper rather than direct URL navigation.

    For each case we record:
      - case_number (visible text inside the link)
      - detail_url (the shared Wicket ?x=<token> URL — for reference)
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    cases: list[ClermontProbateCase] = []
    seen: set[str] = set()

    # Each row's case-number link is inside <td id="grid~row-N~cell-3">
    for td in soup.find_all("td", id=re.compile(r"grid~row-\d+~cell-3$")):
        a = td.find("a")
        if not a:
            continue
        text = a.get_text(" ", strip=True)
        m = CLERMONT_CASE_NUMBER_RE.search(text)
        if not m:
            continue
        case_number = f"{m.group(1)} ES {m.group(2)}"
        if case_number in seen:
            continue
        seen.add(case_number)

        # File date is in cell-5 of the same row (best-guess from DOM order)
        tr = td.find_parent("tr")
        date_filed = ""
        if tr:
            row_text = tr.get_text(" ", strip=True)
            date_m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", row_text)
            if date_m:
                try:
                    date_filed = datetime.strptime(
                        date_m.group(1), "%m/%d/%Y"
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    pass

        cases.append(ClermontProbateCase(
            case_number=case_number,
            date_filed=date_filed,
            detail_url=a.get("href", ""),
        ))

    return cases


class ClermontProbateScraper:
    """Recon-mode scraper for Clermont Probate Court (CourtView family).

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
        captcha_api_key: str = "",
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
        self.captcha_api_key = captcha_api_key
        self.log = logger if logger else _StdoutLog()
        self.recon: ReconCapture = ReconCapture()

    async def run(self) -> None:
        self.log.info(
            f"ClermontProbateScraper start | mode={self.mode} | "
            f"dates {self.date_from or '-'} → {self.date_to or '-'}"
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
                await page.wait_for_timeout(3000)
                self.recon.landing_html = await page.content()
                self.recon.landing_screenshot = await page.screenshot(
                    full_page=True
                )
                self._dlog("landing_captured",
                           html_bytes=len(self.recon.landing_html),
                           final_url=page.url)

                # Solve the disclaimer-page CAPTCHA + click I Accept
                try:
                    accepted = await self._solve_disclaimer_captcha(page)
                    if accepted and self.date_from:
                        # Now on the search form — same equivant layout as Greene
                        await self._fill_and_search(page)
                    # Capture whichever page we ended up on (search results or
                    # disclaimer if captcha failed)
                    self.recon.results_html = await page.content()
                    self.recon.results_screenshot = await page.screenshot(
                        full_page=True
                    )
                    self.recon.parsed_cases = _parse_clermont_results(
                        self.recon.results_html
                    )
                    self.log.info(
                        f"Parsed {len(self.recon.parsed_cases)} Estate cases"
                    )
                    self._dlog("final_page_captured",
                               html_bytes=len(self.recon.results_html),
                               final_url=page.url,
                               cases_parsed=len(self.recon.parsed_cases))

                    # Capture first N case detail pages (Wicket Ajax click flow)
                    if self.capture_case_details > 0:
                        # Save the results-page URL so we can return between
                        # each case-detail capture
                        self._results_page_url = page.url
                        await self._capture_case_details(page)
                except Exception as e:
                    self.log.warning(f"Captcha/disclaimer flow failed: {e}")
                    self._dlog("disclaimer_flow_error", error=str(e))
            finally:
                await ctx.close()
                await browser.close()

    async def _fill_and_search(self, page: Page) -> None:
        """Fill equivant search form (same DOM as Greene). Switch to Case
        Type tab (bypasses required Last/First Name), select Estate, fill
        File Date range, click Search."""
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
            f"Filling File Date range {us_begin} → {us_end}, "
            f"Case Type = Estate"
        )

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

        # Select Estate (Clermont uses "Estate - ES" label; try both)
        case_type_filled = False
        for label in ["Estate - ES", "Estate"]:
            try:
                sel = "select[name='caseCd']"
                if await page.locator(sel).count() > 0:
                    await page.locator(sel).first.select_option(label=label)
                    self._dlog("case_type_selected", label=label)
                    case_type_filled = True
                    break
            except Exception:
                continue
        if not case_type_filled:
            self._dlog("case_type_not_found")

        # Fill file date range
        for field, value in [
            ("fileDateRange:dateInputBegin", us_begin),
            ("fileDateRange:dateInputEnd", us_end),
        ]:
            try:
                sel = f"input[name='{field}']"
                if await page.locator(sel).count() > 0:
                    await page.locator(sel).first.fill(value)
                    await page.locator(sel).first.blur()
                    self._dlog("date_filled", field=field, value=value)
                    await page.wait_for_timeout(500)
            except Exception as e:
                self._dlog("date_fill_error", field=field, error=str(e))

        # Submit
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

    async def _capture_case_details(self, page: Page) -> None:
        """For the first N Clermont cases, click into each case-detail page,
        capture HTML, then click the breadcrumb / back link to return.

        Wicket's URL pattern doesn't allow direct navigation (all rows share
        one href), so we use Playwright clicks. Each click triggers Wicket's
        Ajax handler which navigates to the specific case.
        """
        n = min(self.capture_case_details, len(self.recon.parsed_cases))
        if n == 0:
            return
        self.log.info(f"Phase 2: capturing {n} case detail pages ...")

        # Use the already-parsed case list (from parse_results_html) which
        # has unique case numbers in order. Click by case-number TEXT so we
        # don't depend on Wicket's volatile row IDs.
        targets = self.recon.parsed_cases[:n]
        for i, target_case in enumerate(targets):
            case_number = target_case.case_number
            try:
                # Find the link whose visible text contains this case number
                # (each case number is unique on the page).
                row_link = page.locator(
                    f"a:has-text('{case_number}')"
                ).first
                if await row_link.count() == 0:
                    self._dlog("case_link_not_found",
                               case_number=case_number)
                    continue
                self._dlog("row_count_before_click", iteration=i+1,
                           page_url=page.url)

                self.log.info(f"  [{i+1}/{n}] clicking {case_number} ...")
                await row_link.click()
                await page.wait_for_load_state("domcontentloaded",
                                                 timeout=20000)
                await page.wait_for_timeout(2000)

                cap = CaseDetailCapture(case_number=case_number)
                cap.html = await page.content()
                cap.final_url = page.url
                self._dlog("case_detail_captured",
                           case_number=case_number,
                           html_bytes=len(cap.html))
                self.recon.case_details.append(cap)

                # Parse into ProbateRecord (7/12 cols)
                try:
                    detail = parse_case_detail(cap.html)
                    if not detail.case_number:
                        detail.case_number = case_number
                    rec = _detail_to_record(detail)
                    self.recon.probate_records.append(rec)
                    self._dlog("case_detail_parsed",
                               case_number=case_number,
                               decedent=detail.decedent_name,
                               fiduciary=detail.fiduciary_name,
                               action=detail.action)
                except Exception as e:
                    self.log.warning(
                        f"    parser failed for {case_number}: {e}"
                    )
                    self._dlog("case_detail_parse_error",
                               case_number=case_number, error=str(e))

                # Return to the search results page. Wicket's row links all
                # share the same URL token, so the only reliable navigation
                # is to go back to the results page URL captured earlier.
                if i < n - 1:  # don't navigate back after the last case
                    try:
                        results_url = getattr(
                            self, "_results_page_url", None
                        )
                        if results_url:
                            await page.goto(
                                results_url,
                                wait_until="domcontentloaded",
                                timeout=20000,
                            )
                            await page.wait_for_timeout(2000)
                        else:
                            await page.go_back(
                                wait_until="domcontentloaded",
                                timeout=15000,
                            )
                            await page.wait_for_timeout(1500)
                    except Exception as e:
                        self.log.warning(f"  results-nav failed: {e}")
                        self._dlog("results_nav_failed",
                                   case_number=case_number, error=str(e))
                        break  # can't continue without results page
            except Exception as e:
                self.log.warning(f"  row {i+1} failed: {e}")
                self._dlog("case_iteration_error",
                           row=i+1, error=str(e))

    async def _solve_disclaimer_captcha(self, page: Page,
                                        max_attempts: int = 5) -> bool:
        """Solve Clermont's CourtView disclaimer CAPTCHA + click I Accept.

        The CourtView captcha is HEAVILY obscured (stroke-through lines, noise)
        and 2Captcha's standard OCR often returns partial answers (3 chars
        when 5-6 are needed). Retry up to `max_attempts` times — each retry
        reloads the page to get a fresh CAPTCHA challenge.

        Returns True if we got past the disclaimer page.
        """
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.log.info(
                    f"CAPTCHA attempt {attempt}/{max_attempts} — "
                    f"reloading page for fresh challenge"
                )
                await page.goto(PORTAL_URL, wait_until="domcontentloaded",
                                timeout=30000)
                await page.wait_for_timeout(2000)
            success = await self._try_solve_once(page)
            if success:
                # Verify we actually got past the disclaimer (URL changes
                # or the captcha image disappears from the page)
                await page.wait_for_timeout(1500)
                still_on_disclaimer = await page.locator(
                    "img.captchaImg"
                ).count() > 0
                if not still_on_disclaimer:
                    self.log.info(f"  Past disclaimer on attempt {attempt}")
                    return True
                # Try again if captcha is still there
                self._dlog("captcha_rejected", attempt=attempt)
                self.log.warning(
                    f"  CAPTCHA rejected on attempt {attempt}, retrying"
                )
        self.log.warning(
            f"Could not solve CAPTCHA after {max_attempts} attempts"
        )
        return False

    async def _try_solve_once(self, page: Page) -> bool:
        """Single attempt: fetch image, solve, fill, click. Returns True if
        all steps completed (caller verifies the result)."""
        from h3.captcha.twocaptcha import (
            get_api_key, solve_image_captcha, TwoCaptchaError,
        )
        api_key = get_api_key(self.captcha_api_key)
        if not api_key:
            self.log.warning(
                "No 2Captcha API key — Clermont disclaimer cannot be bypassed"
            )
            self._dlog("captcha_no_api_key")
            return False

        # Find the captcha image element
        try:
            img_loc = page.locator("img.captchaImg").first
            if await img_loc.count() == 0:
                self.log.warning("No captcha image on Clermont landing")
                self._dlog("captcha_image_not_found")
                return False
        except Exception as e:
            self._dlog("captcha_locate_error", error=str(e))
            return False

        # Grab the captcha image bytes. Prefer fetching the raw image URL
        # via the page's request context (cleaner image than element
        # screenshot, which may include surrounding pixels / scaling).
        image_bytes = b""
        try:
            self.log.info("Fetching Clermont CAPTCHA image ...")
            captcha_src = await img_loc.get_attribute("src")
            if captcha_src:
                # Resolve relative URL
                if captcha_src.startswith("?"):
                    full_url = PORTAL_URL + captcha_src
                elif captcha_src.startswith("/"):
                    full_url = PORTAL_HOST + captcha_src
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

        # Fallback to element screenshot
        if not image_bytes:
            try:
                image_bytes = await img_loc.screenshot()
                self._dlog("captcha_image_screenshot_fallback",
                           bytes=len(image_bytes))
            except Exception as e:
                self.log.warning(f"CAPTCHA capture failed: {e}")
                self._dlog("captcha_image_error", error=str(e))
                return False

        # Save the captcha image bytes for offline inspection
        self.recon.captcha_image_bytes = image_bytes

        # Solve via 2Captcha
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

        # Fill the response + click I Accept
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

        # Click I Accept / beginButton — try several strategies because the
        # button has a Wicket-style onclick wrapper that doesn't always fire
        # via Playwright's normal click.
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
                    # Last resort — submit the form directly
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
