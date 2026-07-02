"""BUG-04 guardrail — reCAPTCHA v3 block-page detection.

pro.mcohio.org deployed reCAPTCHA v3 invisible bot-scoring on 2026-07-01,
returning a "score too low" block page instead of the results table. The
legacy _capture_results path parsed 0 <tr> rows silently and downstream
reported "0 records" as if the courthouse had no filings.

These tests lock the guardrail: block page raises RecaptchaBlockedError,
healthy pages parse normally, quiet weekdays don't trip a false positive.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from h3.scrapers.mcohio import (
    MontgomeryScraper,
    RecaptchaBlockedError,
    _detect_recaptcha_block,
)


# ── Fixtures ────────────────────────────────────────────────────────────


BLOCK_HTML = """<!DOCTYPE html>
<html><body>
  <h1>reCAPTCHA score too low</h1>
  <p>reCAPTCHA (a system for detecting whether you are a real user or a
     bot) has flagged you as likely being a bot or automated browser
     instead of a real human.</p>
  <p>We suggest you leave PRO and try the search again in 20 minutes...</p>
</body></html>
"""


# One DEFENDANT + one PLAINTIFF row (parser needs both). Real pro.mcohio.org
# markup uses openTab('caseInfo', ...) onclick and 6 <td> cells per row.
HEALTHY_HTML = """<!DOCTYPE html>
<html><body>
  <table><tbody id="tblSearchResults">
    <tr onclick="openTab('caseInfo', 'case_id=62390491&screen=summary',1,'2026 CV 03347');">
      <td>2026 CV 03347</td>
      <td>MORTGAGE FORECLOSURE</td>
      <td>SMITH, JOHN</td>
      <td>&nbsp;</td>
      <td>OPEN</td>
      <td>DEFENDANT</td>
    </tr>
    <tr onclick="openTab('caseInfo', 'case_id=62390491&screen=summary',1,'2026 CV 03347');">
      <td>2026 CV 03347</td>
      <td>MORTGAGE FORECLOSURE</td>
      <td>WELLS FARGO BANK NA</td>
      <td>&nbsp;</td>
      <td>OPEN</td>
      <td>PLAINTIFF</td>
    </tr>
    <tr onclick="openTab('caseInfo', 'case_id=62390492&screen=summary',1,'2026 CV 03348');">
      <td>2026 CV 03348</td>
      <td>MORTGAGE FORECLOSURE</td>
      <td>DOE, JANE</td>
      <td>&nbsp;</td>
      <td>OPEN</td>
      <td>DEFENDANT</td>
    </tr>
  </tbody></table>
</body></html>
"""


QUIET_HTML = """<!DOCTYPE html>
<html><body>
  <table><tbody id="tblSearchResults"></tbody></table>
</body></html>
"""


# Marker phrase injected into an HTML comment above a healthy tbody.
# Should still trip — "loud > quiet" (fail closed on ambiguous portal state).
DEFENSIVE_HTML = HEALTHY_HTML.replace(
    "<table>",
    "<!-- reCAPTCHA (a system for detecting whether you are a real user "
    "or a bot) has flagged you --><table>",
)


class _FakePage:
    """Minimal stand-in for playwright.async_api.Page used by _capture_results."""

    def __init__(self, html: str, url: str = "https://pro.mcohio.org/results"):
        self._html = html
        self.url = url

    async def content(self) -> str:
        return self._html

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        return b"\x89PNG\r\n\x1a\n"  # placeholder magic bytes; not parsed


def _make_scraper() -> MontgomeryScraper:
    return MontgomeryScraper(
        date_from="2026-06-29",
        date_to="2026-06-30",
        headless=True,
        mode="recon",
        max_cases=50,
        capture_case_details=0,
    )


# ── Pure helper tests ───────────────────────────────────────────────────


def test_detect_block_on_block_page():
    assert _detect_recaptcha_block(BLOCK_HTML) == "score_too_low"


def test_detect_block_none_on_healthy_page():
    assert _detect_recaptcha_block(HEALTHY_HTML) is None


def test_detect_block_none_on_quiet_page():
    """Genuine quiet-Sunday result: empty tbody, no reCAPTCHA marker."""
    assert _detect_recaptcha_block(QUIET_HTML) is None


def test_detect_block_none_on_empty_html():
    assert _detect_recaptcha_block("") is None
    assert _detect_recaptcha_block(None) is None  # type: ignore[arg-type]


def test_detect_block_case_insensitive():
    """Marker phrasing must match even if Google changes capitalization."""
    weird = BLOCK_HTML.upper()
    assert _detect_recaptcha_block(weird) == "score_too_low"


def test_detect_block_secondary_marker():
    """The '20 minutes' phrase alone should also trip the detector."""
    partial = "<html><body><p>try the search again in 20 minutes</p></body></html>"
    assert _detect_recaptcha_block(partial) == "score_too_low"


# ── Integration tests via _capture_results with a fake Page ─────────────


@pytest.mark.asyncio
async def test_capture_results_raises_on_block():
    scraper = _make_scraper()
    page = _FakePage(BLOCK_HTML)
    with pytest.raises(RecaptchaBlockedError) as exc:
        await scraper._capture_results(page)
    assert exc.value.reason == "score_too_low"
    assert "pro.mcohio.org" in str(exc.value)
    assert exc.value.html_bytes == len(BLOCK_HTML)
    # Forensic evidence captured before the raise
    assert scraper.recon.results_html == BLOCK_HTML
    assert scraper.recon.results_screenshot is not None


@pytest.mark.asyncio
async def test_capture_results_ok_on_healthy_page():
    scraper = _make_scraper()
    page = _FakePage(HEALTHY_HTML)
    await scraper._capture_results(page)
    assert len(scraper.recon.parsed_rows) >= 2
    assert len(scraper.recon.parsed_cases) >= 1


@pytest.mark.asyncio
async def test_capture_results_ok_on_quiet_page():
    """Zero filings for a date range is NOT a block — must not raise."""
    scraper = _make_scraper()
    page = _FakePage(QUIET_HTML)
    await scraper._capture_results(page)
    assert scraper.recon.parsed_rows == []
    assert scraper.recon.parsed_cases == []


@pytest.mark.asyncio
async def test_capture_results_defensive_block_wins():
    """If the marker appears anywhere in the HTML, block-detection wins.

    Enforces D-02 "loud > quiet" — fail closed on ambiguous portal state
    rather than silently returning maybe-real rows from a marked page.
    """
    scraper = _make_scraper()
    page = _FakePage(DEFENSIVE_HTML)
    with pytest.raises(RecaptchaBlockedError):
        await scraper._capture_results(page)
