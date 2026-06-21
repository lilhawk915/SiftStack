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
        "status": "stub — Phase 4",
    },
    "Clark": {
        "vendor": "Custom PHP (Caselook)",
        "portal": "https://probate.clarkcountyohio.gov",
        "captcha": "image CAPTCHA",
        "status": "stub — Phase 4",
    },
    "Clermont": {
        "vendor": "CourtView JWorks",
        "portal": "https://eservices.clermontclerk.org/probate",
        "captcha": None,
        "status": "stub — Phase 4 (has TODOs for DOD + fiduciary addr)",
    },
    "Greene": {
        "vendor": "CourtView JWorks",
        "portal": "https://probate.co.greene.oh.us",
        "captcha": None,
        "status": "live — canary",
    },
    "Miami": {
        "vendor": "Custom PHP (Caselook)",
        "portal": "https://miami.probate.casefilexpress.com",
        "captcha": "image CAPTCHA",
        "status": "stub — Phase 4",
    },
    "Montgomery": {
        "vendor": "ColdFusion (go.mcohio.org)",
        "portal": "https://go.mcohio.org/probate",
        "captcha": None,
        "status": "stub — Phase 4",
    },
    "Warren": {
        "vendor": "Custom PHP",
        "portal": "https://probate.co.warren.oh.us",
        "captcha": None,
        "status": "stub — Phase 4",
    },
}


# Probate runs are weekly across all 7 counties — default lookback is
# the past 7 days from "today". Callers can override per call.
DEFAULT_WEEKLY_LOOKBACK_DAYS = 7
DEFAULT_MAX_CASES = 500


def _default_date_range(
    today: datetime | None = None,
    lookback_days: int = DEFAULT_WEEKLY_LOOKBACK_DAYS,
) -> tuple[str, str]:
    """Return ``(date_from, date_to)`` as ``YYYY-MM-DD`` strings for the
    standard weekly probate window."""
    end = today or datetime.now()
    start = end - timedelta(days=lookback_days)
    return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


# ── Greene — fully implemented (canary) ───────────────────────────────


def fetch_greene_probate(
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
    """Fetch Greene County probate cases.

    Same dual-return contract as other Ohio adapters:

    * **Override path** (sync): pass
      ``override_probate_records=[ProbateRecord, ...]`` to skip the
      live scrape and run the bridge directly. Returns
      ``list[NoticeData]``.
    * **Live path** (async): returns the coroutine from
      :func:`_run_greene_probate_live`; the caller must ``await``.
    """
    portal = OHIO_PROBATE_ENDPOINTS["Greene"]["portal"]

    # ── Override (sync) ────────────────────────────────────────────
    if override_probate_records is not None:
        return [
            probate_record_to_notice_data(r, "Greene", source_url=portal)
            for r in override_probate_records
        ]

    # ── Live (returns coroutine) ───────────────────────────────────
    return _run_greene_probate_live(
        date_from=date_from, date_to=date_to,
        max_cases=max_cases, proxy_url=proxy_url,
        headless=headless, today=today,
    )


async def _run_greene_probate_live(
    *,
    date_from: str | None,
    date_to: str | None,
    max_cases: int,
    proxy_url: str | None,
    headless: bool,
    today: datetime | None,
) -> list[NoticeData]:
    """Live Playwright path for Greene probate. Separated so the
    override path stays synchronous."""
    from h3.scrapers.greene_probate import GreeneProbateScraper

    portal = OHIO_PROBATE_ENDPOINTS["Greene"]["portal"]
    if date_from is None or date_to is None:
        df, dt = _default_date_range(today=today)
        date_from = date_from or df
        date_to = date_to or dt

    logger.info(
        "Greene probate: %s → %s (max %d cases)",
        date_from, date_to, max_cases,
    )
    scraper = GreeneProbateScraper(
        date_from=date_from,
        date_to=date_to,
        mode="case_details",
        max_cases=max_cases,
        capture_case_details=max_cases,
        proxy_config_url=proxy_url,
        headless=headless,
    )
    await scraper.run()
    records = extract_probate_records(scraper.recon)
    logger.info("Greene probate: %d ProbateRecords from recon", len(records))
    out = [
        probate_record_to_notice_data(r, "Greene", source_url=portal)
        for r in records
    ]
    logger.info("Greene probate: emitted %d NoticeData rows", len(out))
    return out


# ── 6 stubs — populated in Phase 4 ────────────────────────────────────


def _not_implemented(county: str, reason: str = ""):
    def stub(*args, **kwargs):
        msg = (
            f"{county} probate not yet ported to SiftStack-native. "
            f"Tracked under Phase 4."
        )
        if reason:
            msg += f" {reason}"
        raise NotImplementedError(msg)
    stub.__name__ = f"fetch_{county.lower()}_probate"
    stub.__doc__ = (
        f"STUB — Phase 4. Raises NotImplementedError.\n\n"
        f"{county} probate runs via H3_Scrapers Apify Actor for now. "
        f"See {OHIO_PROBATE_ENDPOINTS.get(county, {}).get('portal', '')}.\n\n"
        f"{reason}".strip()
    )
    return stub


fetch_butler_probate = _not_implemented(
    "Butler",
    "Vendor: Custom PHP. Daily-only search constraint and "
    "session-token-in-URL pattern require special handling.",
)
fetch_clark_probate = _not_implemented(
    "Clark",
    "Vendor: Custom PHP (Caselook). Image CAPTCHA via "
    "h3.captcha.twocaptcha required.",
)
fetch_clermont_probate = _not_implemented(
    "Clermont",
    "Vendor: CourtView JWorks. ProbateRecord has explicit TODOs for "
    "date_of_death and fiduciary_address — extend the parser before "
    "wiring this as production.",
)
fetch_miami_probate = _not_implemented(
    "Miami",
    "Vendor: Custom PHP (Caselook). Image CAPTCHA required.",
)
fetch_montgomery_probate = _not_implemented(
    "Montgomery",
    "Vendor: ColdFusion (go.mcohio.org). Year-based calendar search; "
    "weekly run schedule.",
)
fetch_warren_probate = _not_implemented(
    "Warren",
    "Vendor: Custom PHP. Has TODOs for action-from-docket and "
    "fiduciary_email-from-PDF.",
)


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
