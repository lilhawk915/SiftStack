"""Ohio foreclosure adapters — 7 SW Ohio counties.

Mirrors the public contract of :mod:`ohio_tax_delinquent_scrapers` and
:mod:`ohio_sheriff_sale_scrapers`:

* Per-county adapter functions (``fetch_<county>_foreclosure``) each
  accept ``ctx=`` for a live Playwright context, override fixtures
  for sync test paths, and return ``list[NoticeData]`` (or an
  awaitable yielding the same in the live path).
* ``_DISPATCH`` maps lowercased county names to adapter functions.
* :func:`fetch_ohio_foreclosure` is the public dispatcher used by
  ``scraper.scrape_all()``.

CANARY STATUS (2026-06-19): Montgomery is the only fully-implemented
county. The other 6 (Butler, Clark, Clermont, Greene, Miami, Warren)
raise ``NotImplementedError`` until Phase 4. Callers should catch the
exception and continue rather than crashing the daily run; the
existing dispatcher pattern in ``scraper.scrape_all()`` already does.

NOTE on shared Playwright context: Montgomery's underlying
:class:`h3.scrapers.mcohio.MontgomeryScraper` creates its own browser
instance per run. The ``ctx`` parameter is accepted for signature
parity with the other ``fetch_ohio_*`` adapters but is currently
ignored. Refactoring the H3 scrapers to accept an external ctx is a
future optimisation (Phase 5 / orchestrator polish).
"""
from __future__ import annotations

import inspect
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from h3.integration import integrate_montgomery_foreclosure
from h3.notice_data_bridge import case_record_to_notice_data
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Endpoint registry ──────────────────────────────────────────────────


# Per-county foreclosure portal metadata. ``sale_day`` here means the
# **case-filing search cadence**, not sheriff-sale day — these portals
# are court-case search interfaces, not auction calendars. Source URLs
# verified against reference/ohio_counties/<County>.csv.
OHIO_FORECLOSURE_ENDPOINTS: dict[str, dict] = {
    "Butler": {
        "vendor": "CourtView (Equivant)",
        "portal": "https://courtsearch.bcohio.gov",
        "captcha": "reCAPTCHA v2",
        "status": "stub — Phase 4",
    },
    "Clark": {
        "vendor": "CourtView (Equivant)",
        "portal": "https://eservices.clarkcountyohio.gov",
        "captcha": None,
        "status": "stub — Phase 4",
    },
    "Clermont": {
        "vendor": "CourtView (Equivant)",
        "portal": "https://eservices.clermontclerk.org",
        "captcha": None,
        "status": "stub — Phase 4",
    },
    "Greene": {
        "vendor": "CourtView (Equivant)",
        "portal": "https://courts.greenecountyohio.gov",
        "captcha": None,
        "status": "stub — Phase 4",
    },
    "Miami": {
        "vendor": "CourtView (Equivant)",
        "portal": "https://courts.miamicountyohio.gov",
        "captcha": "image CAPTCHA",
        "status": "stub — Phase 4",
    },
    "Montgomery": {
        "vendor": "Custom ASP.NET (PROv3)",
        "portal": "https://pro.mcohio.org",
        "captcha": None,
        "status": "live — canary",
    },
    "Warren": {
        "vendor": "BenchmarkCP",
        "portal": "https://probatecasereport.warrencountyohio.gov",
        "captcha": None,
        "status": "stub — Phase 4 (also fix cap=15 bug)",
    },
}


# Default date window for daily runs — overrideable per call. The
# Montgomery scraper handles its own ``MM/DD/YYYY`` formatting from
# ISO input via ``_to_mco_date_input``.
DEFAULT_DAILY_LOOKBACK_DAYS = 1
DEFAULT_MAX_CASES = 200


def _default_date_range(
    today: datetime | None = None,
    lookback_days: int = DEFAULT_DAILY_LOOKBACK_DAYS,
) -> tuple[str, str]:
    """Return ``(date_from, date_to)`` as ``YYYY-MM-DD`` strings.

    Daily Montgomery default is yesterday → today. Caller can widen
    by passing ``lookback_days``.
    """
    end = today or datetime.now()
    start = end - timedelta(days=lookback_days)
    return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


# ── Montgomery — fully implemented (canary) ───────────────────────────


def fetch_montgomery_foreclosure(
    ctx=None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    max_cases: int = DEFAULT_MAX_CASES,
    proxy_url: str | None = None,
    headless: bool = True,
    override_case_details: list[Any] | None = None,
    today: datetime | None = None,
):
    """Fetch Montgomery County foreclosure cases.

    Two paths — same dual-return contract as
    :func:`ohio_sheriff_sale_scrapers.fetch_butler_sheriff_sale`:

    * **Override path** (sync, used by tests): pass
      ``override_case_details=[CaseDetailCapture, ...]`` to skip the
      live Playwright scrape entirely and run the integration layer
      directly over fixture captures. Returns ``list[NoticeData]``.
    * **Live path** (async, used by production): leave override blank.
      Returns the coroutine from :func:`_run_montgomery_live`; the
      caller must ``await`` it. The internal scraper creates its own
      Playwright browser — ``ctx`` is accepted for signature parity
      but ignored.
    """
    portal = OHIO_FORECLOSURE_ENDPOINTS["Montgomery"]["portal"]

    # ── Override (sync) ────────────────────────────────────────────
    if override_case_details is not None:
        records = integrate_montgomery_foreclosure(override_case_details)
        out: list[NoticeData] = []
        for r in records:
            out.extend(case_record_to_notice_data(
                r, "Montgomery", source_url=portal,
            ))
        return out

    # ── Live (returns coroutine) ───────────────────────────────────
    return _run_montgomery_live(
        date_from=date_from, date_to=date_to,
        max_cases=max_cases, proxy_url=proxy_url,
        headless=headless, today=today,
    )


async def _run_montgomery_live(
    *,
    date_from: str | None,
    date_to: str | None,
    max_cases: int,
    proxy_url: str | None,
    headless: bool,
    today: datetime | None,
) -> list[NoticeData]:
    """Live Playwright path. Separated from the sync ``fetch_*``
    function so the override path doesn't get wrapped in a coroutine."""
    from h3.scrapers.mcohio import MontgomeryScraper

    portal = OHIO_FORECLOSURE_ENDPOINTS["Montgomery"]["portal"]
    if date_from is None or date_to is None:
        df, dt = _default_date_range(today=today)
        date_from = date_from or df
        date_to = date_to or dt

    logger.info(
        "Montgomery foreclosure: %s → %s (max %d cases)",
        date_from, date_to, max_cases,
    )
    scraper = MontgomeryScraper(
        date_from=date_from,
        date_to=date_to,
        mode="case_details",
        max_cases=max_cases,
        # capture_case_details > 0 makes the scraper open each case detail
        # page and snapshot the AJAX tabs + download CIS PDF. Without this
        # we get recon-only data which the integration layer can't use.
        capture_case_details=max_cases,
        download_pdfs=True,
        proxy_config_url=proxy_url,
        headless=headless,
    )
    await scraper.run()
    captures = list(scraper.recon.case_details)
    logger.info(
        "Montgomery foreclosure: captured %d case-detail pages → integrating",
        len(captures),
    )
    records = integrate_montgomery_foreclosure(captures)
    out: list[NoticeData] = []
    for r in records:
        out.extend(case_record_to_notice_data(
            r, "Montgomery", source_url=portal,
        ))
    logger.info(
        "Montgomery foreclosure: emitted %d NoticeData rows from %d cases",
        len(out), len(records),
    )
    return out


# ── 6 stubs — populated in Phase 4 ────────────────────────────────────


def _not_implemented(county: str, reason: str = ""):
    """Build a stub adapter that raises NotImplementedError loudly."""
    def stub(*args, **kwargs):
        msg = (
            f"{county} foreclosure not yet ported to SiftStack-native. "
            f"Tracked under Phase 4."
        )
        if reason:
            msg += f" {reason}"
        raise NotImplementedError(msg)
    stub.__name__ = f"fetch_{county.lower()}_foreclosure"
    stub.__doc__ = (
        f"STUB — Phase 4. Raises NotImplementedError.\n\n"
        f"{county} foreclosure runs via H3_Scrapers Apify Actor for now. "
        f"See {OHIO_FORECLOSURE_ENDPOINTS.get(county, {}).get('portal', '')}.\n\n"
        f"{reason}".strip()
    )
    return stub


fetch_butler_foreclosure = _not_implemented(
    "Butler",
    "Vendor: CourtView (equivant); shares integration code path with "
    "Clark/Clermont/Greene/Miami — port these as a single batch.",
)
fetch_clark_foreclosure = _not_implemented(
    "Clark",
    "Vendor: CourtView (equivant); shared port batch.",
)
fetch_clermont_foreclosure = _not_implemented(
    "Clermont",
    "Vendor: CourtView (equivant); shared port batch + 'I Agree' "
    "disclaimer click.",
)
fetch_greene_foreclosure = _not_implemented(
    "Greene",
    "Vendor: CourtView (equivant); shared port batch. Greene uses "
    "<span class=pty-name> instead of <div class=ptyInfoLabel> — "
    "address parser already handles the variation.",
)
fetch_miami_foreclosure = _not_implemented(
    "Miami",
    "Vendor: CourtView (equivant); shared port batch + image-CAPTCHA "
    "via h3.captcha.twocaptcha.",
)
fetch_warren_foreclosure = _not_implemented(
    "Warren",
    "Vendor: BenchmarkCP. Separate integration path: parse_case_detail_html "
    "+ Warren Auditor parcel lookup + PJR/COMPLAINT PDF OCR fallback. "
    "Also fix the known cap=15 Apify timeout bug as part of this port.",
)


# ── Dispatcher ────────────────────────────────────────────────────────


_DISPATCH: dict[str, Callable[..., Any]] = {
    "butler":     fetch_butler_foreclosure,
    "clark":      fetch_clark_foreclosure,
    "clermont":   fetch_clermont_foreclosure,
    "greene":     fetch_greene_foreclosure,
    "miami":      fetch_miami_foreclosure,
    "montgomery": fetch_montgomery_foreclosure,
    "warren":     fetch_warren_foreclosure,
}


def fetch_ohio_foreclosure(
    county: str,
    *,
    ctx=None,
    **kwargs,
):
    """Dispatch a foreclosure fetch to the per-county adapter.

    The dispatcher is sync; per-county adapters may return either a
    ``list[NoticeData]`` (the override / sync test path) OR an
    awaitable yielding the same (live Playwright path). Callers in
    ``scraper.scrape_all`` already check ``inspect.isawaitable`` and
    await when needed — the same pattern as ``fetch_ohio_tax_delinquent``.

    Stubbed counties (Phase 4 work) raise ``NotImplementedError``;
    the caller should catch + log + continue, not crash the run.
    """
    fn = _DISPATCH.get(county.strip().lower())
    if fn is None:
        raise ValueError(
            f"Unknown Ohio foreclosure county: {county!r}. "
            f"Supported: {sorted(_DISPATCH)}"
        )
    return fn(ctx=ctx, **kwargs)
