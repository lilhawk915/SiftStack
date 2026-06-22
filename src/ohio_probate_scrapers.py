"""Ohio probate adapters — 7 SW Ohio counties.

Mirrors the public contract of :mod:`ohio_tax_delinquent_scrapers`,
:mod:`ohio_sheriff_sale_scrapers`, and
:mod:`ohio_foreclosure_scrapers`. Per-county adapters accept ``ctx=``
for live mode, override fixtures for sync tests, and return
``list[NoticeData]`` (or an awaitable yielding the same).

Probate scrapers differ from foreclosure scrapers in two ways:

1. **No integration layer needed.** Each probate scraper populates
   ``scraper.recon.probate_records`` directly during ``run()`` — the
   ProbateRecord objects come out essentially finished. We just read
   the list and run it through :func:`h3.notice_data_bridge.probate_record_to_notice_data`.

2. **Cadence is uniform.** All 7 counties run on the weekly cycle —
   there's no daily-vs-weekly split like foreclosure has between
   Montgomery and the other 6. (Foreclosure's split is driven by
   Montgomery being the active-calling list; probate volume is too
   low per-day to need a daily cadence.)

CANARY STATUS (2026-06-19): Greene is the only fully-implemented
county. The other 6 raise ``NotImplementedError`` until Phase 4.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from h3.integration import extract_probate_records
from h3.notice_data_bridge import probate_record_to_notice_data
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Endpoint registry ──────────────────────────────────────────────────


OHIO_PROBATE_ENDPOINTS: dict[str, dict] = {
    "Butler": {
        "vendor": "Custom PHP",
        "portal": "https://probate-clerk.butlercountyohio.org",
        "captcha": None,
        "status": "live",
    },
    "Clark": {
        "vendor": "Custom PHP (Caselook)",
        "portal": "https://probate.clarkcountyohio.gov",
        "captcha": "image CAPTCHA",
        "status": "live",
    },
    "Clermont": {
        "vendor": "CourtView JWorks",
        "portal": "https://eservices.clermontclerk.org/probate",
        "captcha": None,
        "status": "live (parser has TODOs for DOD + fiduciary addr)",
    },
    "Greene": {
        "vendor": "CourtView JWorks",
        "portal": "https://probate.co.greene.oh.us",
        "captcha": None,
        "status": "live (parser is best-effort first-pass — see Phase 3D)",
    },
    "Miami": {
        "vendor": "Custom PHP (Caselook)",
        "portal": "https://miami.probate.casefilexpress.com",
        "captcha": "image CAPTCHA",
        "status": "live",
    },
    "Montgomery": {
        "vendor": "ColdFusion (go.mcohio.org)",
        "portal": "https://go.mcohio.org/probate",
        "captcha": None,
        "status": "live",
    },
    "Warren": {
        "vendor": "Custom PHP",
        "portal": "https://probate.co.warren.oh.us",
        "captcha": None,
        "status": "live",
    },
}


# Probate runs are weekly across all 7 counties — default lookback is
# the past 7 days from "today". Callers can override per call.
DEFAULT_WEEKLY_LOOKBACK_DAYS = 7
# Cap on case-detail captures per county per run. Montgomery (the
# slowest) takes ~4 sec per case-detail page (case detail + docket
# + PDF). Typical weekly volume is ~60-100 new cases per county.
# 100 gives a ~7 min runtime budget per county — fits comfortably
# inside the 25-min Monitor / cron timeout. Earlier default 500
# blew past the timeout (~33 min); the orchestrator hung waiting
# for probate to complete before moving to the next source type.
DEFAULT_MAX_CASES = 100


def _default_date_range(
    today: datetime | None = None,
    lookback_days: int = DEFAULT_WEEKLY_LOOKBACK_DAYS,
) -> tuple[str, str]:
    """Return ``(date_from, date_to)`` as ``YYYY-MM-DD`` strings for the
    standard weekly probate window."""
    end = today or datetime.now()
    start = end - timedelta(days=lookback_days)
    return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


# ── All 7 probate adapters — factory pattern ─────────────────────────


# Per-county scraper-class lookup. Each scraper module exposes a
# ``<County>ProbateScraper`` class that populates
# ``scraper.recon.probate_records`` directly during run() — no
# integration layer needed.
_PROBATE_SCRAPER_CLASSES = {
    "butler":     ("h3.scrapers.butler_probate",     "ButlerProbateScraper"),
    "clark":      ("h3.scrapers.clark_probate",      "ClarkProbateScraper"),
    "clermont":   ("h3.scrapers.clermont_probate",   "ClermontProbateScraper"),
    "greene":     ("h3.scrapers.greene_probate",     "GreeneProbateScraper"),
    "miami":      ("h3.scrapers.miami_probate",      "MiamiProbateScraper"),
    "montgomery": ("h3.scrapers.mcohio_probate",     "MontgomeryProbateScraper"),
    "warren":     ("h3.scrapers.warren_probate",     "WarrenProbateScraper"),
}


def _make_probate_fetcher(county: str):
    """Build a per-county probate adapter. Same dual-return contract as
    ``fetch_greene_probate``.

    The probate side is uniform across all 7 counties: the scraper
    class populates ``scraper.recon.probate_records`` during run(),
    so the adapter is a thin wrapper that:

      1. (override) bridges fixture ProbateRecord list → NoticeData
      2. (live)    instantiates the scraper, awaits run(), extracts
                   recon.probate_records, bridges through.

    Per-county quirks (CAPTCHA, session tokens, etc.) live inside the
    scraper class — the adapter doesn't need to know.
    """
    county_title = county.capitalize()
    portal = OHIO_PROBATE_ENDPOINTS[county_title]["portal"]

    def fetch(
        ctx=None,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        max_cases: int = DEFAULT_MAX_CASES,
        proxy_url: str | None = None,
        headless: bool = True,
        override_probate_records: list[Any] | None = None,
        today: datetime | None = None,
    ):
        # ── Override (sync) ───────────────────────────────────────
        if override_probate_records is not None:
            return [
                probate_record_to_notice_data(
                    r, county_title, source_url=portal,
                )
                for r in override_probate_records
            ]

        # ── Live (returns coroutine) ──────────────────────────────
        return _run_probate_live(
            county=county,
            date_from=date_from, date_to=date_to,
            max_cases=max_cases, proxy_url=proxy_url,
            headless=headless, today=today,
        )

    fetch.__name__ = f"fetch_{county}_probate"
    fetch.__doc__ = (
        f"Fetch {county_title} County probate cases.\n\n"
        f"Vendor: {OHIO_PROBATE_ENDPOINTS[county_title]['vendor']}.\n\n"
        f"Dual-return contract: sync ``list[NoticeData]`` when "
        f"``override_probate_records=`` is passed; coroutine otherwise. "
        f"Per-county scraping quirks (CAPTCHA, session tokens, etc.) "
        f"are handled inside the underlying scraper class."
    )
    return fetch


def _record_in_window(rec, date_from: str, date_to: str) -> bool:
    """True when a ProbateRecord's file_date sits within [date_from, date_to].

    Montgomery + other probate scrapers always do a calendar-year
    portal search — date filtering has to happen AFTER case-detail
    capture (the results listing doesn't carry a date column). H3's
    main.py used to apply this filter post-scrape; the SiftStack port
    moves it into the adapter so callers get a date-bounded result
    matching the orchestrator's contract.

    Records with no file_date (parser couldn't extract) are KEPT —
    we don't want to silently drop them. The downstream enrichment +
    upload pipeline can still process them; the operator just won't
    have a date tag.
    """
    raw = (getattr(rec, "date_filed", "") or "").strip()
    if not raw:
        return True   # don't drop unknown-date records
    # ProbateRecord.date_filed is ISO YYYY-MM-DD per the dataclass.
    return date_from <= raw <= date_to


async def _run_probate_live(
    *,
    county: str,
    date_from: str | None,
    date_to: str | None,
    max_cases: int,
    proxy_url: str | None,
    headless: bool,
    today: datetime | None,
) -> list[NoticeData]:
    """Shared live Playwright path for all 7 probate counties."""
    import importlib

    county_title = county.capitalize()
    portal = OHIO_PROBATE_ENDPOINTS[county_title]["portal"]
    mod_path, cls_name = _PROBATE_SCRAPER_CLASSES[county]
    scraper_cls = getattr(importlib.import_module(mod_path), cls_name)

    if date_from is None or date_to is None:
        df, dt = _default_date_range(today=today)
        date_from = date_from or df
        date_to = date_to or dt

    logger.info(
        "%s probate: %s → %s (max %d cases)",
        county_title, date_from, date_to, max_cases,
    )
    scraper = scraper_cls(
        date_from=date_from,
        date_to=date_to,
        mode="case_details",
        max_cases=max_cases,
        capture_case_details=max_cases,
        proxy_config_url=proxy_url,
        headless=headless,
    )
    await scraper.run()
    all_records = extract_probate_records(scraper.recon)
    logger.info("%s probate: %d ProbateRecords from recon",
                county_title, len(all_records))

    # Post-scrape date-window filter — the portal returns the whole
    # calendar year. Default behaviour: keep only records whose
    # file_date sits within the requested window.
    records = [r for r in all_records
               if _record_in_window(r, date_from, date_to)]
    dropped = len(all_records) - len(records)
    if dropped:
        logger.info("%s probate: filtered out %d records outside "
                    "[%s, %s] (date-window match)",
                    county_title, dropped, date_from, date_to)

    out = [
        probate_record_to_notice_data(r, county_title, source_url=portal)
        for r in records
    ]
    logger.info("%s probate: emitted %d NoticeData rows",
                county_title, len(out))
    return out


# All 7 county probate adapters built from the same factory.
fetch_butler_probate     = _make_probate_fetcher("butler")
fetch_clark_probate      = _make_probate_fetcher("clark")
fetch_clermont_probate   = _make_probate_fetcher("clermont")
fetch_greene_probate     = _make_probate_fetcher("greene")
fetch_miami_probate      = _make_probate_fetcher("miami")
fetch_montgomery_probate = _make_probate_fetcher("montgomery")
fetch_warren_probate     = _make_probate_fetcher("warren")


# ── Dispatcher ────────────────────────────────────────────────────────


_DISPATCH: dict[str, Callable[..., Any]] = {
    "butler":     fetch_butler_probate,
    "clark":      fetch_clark_probate,
    "clermont":   fetch_clermont_probate,
    "greene":     fetch_greene_probate,
    "miami":      fetch_miami_probate,
    "montgomery": fetch_montgomery_probate,
    "warren":     fetch_warren_probate,
}


def fetch_ohio_probate(
    county: str,
    *,
    ctx=None,
    **kwargs,
):
    """Dispatch a probate fetch to the per-county adapter.

    Same await semantics as ``fetch_ohio_foreclosure``. Stubbed
    counties raise ``NotImplementedError``.
    """
    fn = _DISPATCH.get(county.strip().lower())
    if fn is None:
        raise ValueError(
            f"Unknown Ohio probate county: {county!r}. "
            f"Supported: {sorted(_DISPATCH)}"
        )
    return fn(ctx=ctx, **kwargs)
