"""Ohio sheriff-sale adapters — 7 SW Ohio counties.

All 7 counties (Butler, Clark, Clermont, Greene, Miami, Montgomery, Warren)
publish their sheriff sales on the same Realauction.com "RealForeclose"
platform, each at ``https://{county}.sheriffsaleauction.ohio.gov``. The
auction-preview URL pattern is identical across counties:

    /index.cfm?zaction=AUCTION&zmethod=PREVIEW&AuctionDate=MM/DD/YYYY

That URL is **publicly accessible without login** — the credential wall
only gates bidding/registration. Each PREVIEW page renders the day's
auctions as ``.AUCTION_DETAILS`` blocks, each carrying:

    Case Status   ACTIVE
    Case #        2024 CV 00233 (0)     (format varies by county)
    Parcel ID     R72 04805A0027
    Property Addr 39 NORTH QUENTIN AVENUE, DAYTON, 45403
    Appraised     $54,000.00
    Opening Bid   $36,000.00
    Deposit       $5,000.00
    Auction Starts 06/26/2026 09:00 AM ET

A single shared async crawler (``_crawl_realforeclose_forward``) walks
each county's calendar via the page's "Next Auction" link until a date
beyond the configured forward horizon (default 90 days from today). Per-
county wrappers (``fetch_butler_sheriff_sale`` etc.) exist for dispatch
parity with ``ohio_tax_delinquent_scrapers`` and to allow per-county
override paths from tests.

Transport: every county sits behind Cloudflare via the realauction.com
edge. Playwright is required for live runs; ``override_blocks=`` lets
unit tests parse fixture HTML synchronously without a browser.

Case-number formats observed (all handled by the generic regex):

    Butler      CV23122477 (5756)
    Montgomery  2024 CV 00233 (0)
    Miami       25CV00068 (0)
    Warren      24CV098101
    Clark       24CV0839 (0)
    Clermont    2025-CVE-1628 (0)
    Greene      2025CV0151 (241)

Sale-day cadence observed (used by orchestrator to schedule retries):

    Butler      Thursdays      ~weekly, irregular
    Montgomery  Fridays        ~weekly
    Miami       Wednesdays     ~weekly
    Warren      Tuesdays       monthly
    Clark       Fridays        sparse
    Clermont    Tuesdays       biweekly, sparse
    Greene      Tuesdays       biweekly, sparse
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Callable, TYPE_CHECKING

from notice_parser import NoticeData

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

logger = logging.getLogger(__name__)


# ── Endpoint registry ──────────────────────────────────────────────────


# Mirrors ``OHIO_ENDPOINTS`` in ohio_tax_delinquent_scrapers.py but
# narrower — every county uses the same transport (RealForeclose +
# Cloudflare), so the per-county metadata is mostly the subdomain and
# the observed sale day. Source-of-truth for portal URLs:
# reference/ohio_counties/<County>.csv row "Tax Sale / Sheriff Sale Auction".
OHIO_SHERIFF_ENDPOINTS: dict[str, dict] = {
    "Butler": {
        "subdomain": "butler",
        "sale_day": "Thursday",
        "cadence_observed": "weekly (irregular gaps)",
        "portal": "https://butler.sheriffsaleauction.ohio.gov/",
    },
    "Clark": {
        "subdomain": "clark",
        "sale_day": "Friday",
        "cadence_observed": "sparse",
        "portal": "https://clark.sheriffsaleauction.ohio.gov/",
    },
    "Clermont": {
        "subdomain": "clermont",
        "sale_day": "Tuesday",
        "cadence_observed": "biweekly, sparse",
        "portal": "https://clermont.sheriffsaleauction.ohio.gov/",
    },
    "Greene": {
        "subdomain": "greene",
        "sale_day": "Tuesday",
        "cadence_observed": "biweekly, sparse",
        "portal": "https://greene.sheriffsaleauction.ohio.gov/",
    },
    "Miami": {
        "subdomain": "miami",
        "sale_day": "Wednesday",
        "cadence_observed": "weekly",
        "portal": "https://miami.sheriffsaleauction.ohio.gov/",
    },
    "Montgomery": {
        "subdomain": "montgomery",
        "sale_day": "Friday",
        "cadence_observed": "weekly",
        "portal": "https://montgomery.sheriffsaleauction.ohio.gov/",
    },
    "Warren": {
        "subdomain": "warren",
        "sale_day": "Tuesday",
        "cadence_observed": "monthly",
        "portal": "https://warren.sheriffsaleauction.ohio.gov/",
    },
}


# Forward window — only emit sales whose AuctionDate is within this many
# days of today. The user's stated horizon (2026-06-18) was 90 days;
# orchestrator can override per-tick if needed.
DEFAULT_HORIZON_DAYS = 90

# Per-page wait after navigation — RealForeclose pages render auction
# blocks via JS shortly after DOMContentLoaded. Warren and Miami need
# the longer end of this range; Montgomery happy at 3-4s.
_RENDER_WAIT_MS = 8000

# Safety cap on Next-link hops per county per run. Real auction
# calendars span 4-6 dates in a typical 90-day window. 40 is generous
# enough to traverse low-traffic counties (Warren, Greene) where the
# crawl may walk past 10+ historical sale dates before reaching the
# first future sale.
_MAX_HOPS = 40


_SALE_DAY_WEEKDAY = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2,
    "Thursday": 3, "Friday": 4,
}


def _next_sale_day(today_dt: datetime, sale_day_name: str) -> datetime:
    """Return the first date ≥ ``today_dt`` whose weekday matches ``sale_day_name``.

    Used to seed the calendar walk on a date the RealForeclose database
    recognises as an auction day. Starting on a non-sale-day weekday
    causes the page's Next-link to bounce backward into deep history
    rather than forward to the next sale — burning through the hop
    budget for nothing.
    """
    target = _SALE_DAY_WEEKDAY.get(sale_day_name, today_dt.weekday())
    diff = (target - today_dt.weekday()) % 7
    return today_dt + timedelta(days=diff)


# ── Block parser (shared across all counties) ──────────────────────────


def _parse_auction_block(text: str) -> dict:
    """Extract fields from a single ``.AUCTION_DETAILS`` innerText.

    The format is tab-separated key/value pairs over multiple lines.
    All seven counties emit the same labels; only case-number formatting
    differs (handled by the generic regex below).

    Returns a dict with empty strings for missing fields — callers
    decide whether to drop or keep partial records.
    """
    rec = {
        "auction_starts": "",
        "case_status": "",
        "case_number": "",
        "parcel_id": "",
        "property_address_raw": "",
        "appraised_value": "",
        "opening_bid": "",
        "deposit": "",
    }
    m = re.search(r"Auction Starts\s*\n?\s*([\d/]+\s+[\d:]+\s+[AP]M\s+ET)", text)
    if m:
        rec["auction_starts"] = m.group(1).strip()
    m = re.search(r"Case Status:\s*\n?\s*(\w+)", text)
    if m:
        rec["case_status"] = m.group(1).strip()
    # Handles `CV23122477 (5756)`, `2024 CV 00233 (0)`, `25CV00068 (0)`,
    # `2025-CVE-1628 (0)`, `2025CV0151 (241)`. Greedy on the first token,
    # optionally followed by ` CV NNNNN` (Montgomery's spaced form) and
    # an optional `(N)` suffix.
    m = re.search(r"Case #:\s*\n?\s*(\S+(?:\s+CV\s+\d+)?(?:\s*\(\d+\))?)", text)
    if m:
        rec["case_number"] = m.group(1).strip()
    m = re.search(r"Parcel ID:\s*\n?\s*([^\n]+)", text)
    if m:
        rec["parcel_id"] = m.group(1).strip()
    # Property Address spans 2 tab/newline-separated lines: street, then
    # ``CITY , ZIP`` (with the space before the comma in some counties).
    m = re.search(r"Property Address:\s*\n?\s*([^\n]+)\n\s*([^\n]+)", text)
    if m:
        street = m.group(1).strip()
        city_zip = m.group(2).strip()
        rec["property_address_raw"] = f"{street}, {city_zip}"
    m = re.search(r"Appraised Value:\s*\n?\s*(\$[\d,.]+)", text)
    if m:
        rec["appraised_value"] = m.group(1).strip()
    m = re.search(r"Opening Bid:\s*\n?\s*(\$[\d,.]+)", text)
    if m:
        rec["opening_bid"] = m.group(1).strip()
    m = re.search(r"Deposit Requirement:\s*\n?\s*(\$[\d,.]+)", text)
    if m:
        rec["deposit"] = m.group(1).strip()
    return rec


def _split_property_address(raw: str) -> tuple[str, str, str]:
    """Split ``'STREET, CITY, ZIP'`` into (street, city, zip5).

    Butler sometimes emits 9-digit zips with trailing zeros (e.g.
    ``450130000`` for 45013); we normalise to the first 5 digits since
    the trailing four are garbage padding rather than real ZIP+4.
    Some counties stray a space before the comma (``DAYTON , 45403``);
    strip() handles that.

    Returns ("", "", "") if the input can't be split cleanly.
    """
    if not raw:
        return ("", "", "")
    parts = [p.strip() for p in raw.split(",")]
    # Minimum: street, city, zip. If a comma is missing we get fewer.
    if len(parts) < 3:
        return (raw.strip(), "", "")
    # Last part is the zip; second-to-last is the city. Everything
    # before that gets re-joined back into street — this preserves
    # apartment/unit lines like "7516 SHAWNEE LANE, UNIT 165, WEST
    # CHESTER, 450690000" where the unit belongs to the street, not
    # the city.
    zip_raw = parts[-1].strip()
    city = parts[-2].strip()
    street = ", ".join(parts[:-2]).strip()
    # Take first 5 digits — Butler pads with zeros, others give clean 5.
    zip_digits = re.sub(r"[^\d]", "", zip_raw)
    zip5 = zip_digits[:5] if zip_digits else ""
    return (street, city, zip5)


def _auction_date_iso(scraped_date_us: str) -> str:
    """Convert ``'06/26/2026'`` → ``'2026-06-26'``. Empty string on parse error."""
    try:
        return datetime.strptime(scraped_date_us, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _block_to_notice(
    county: str,
    block: dict,
    scraped_date_us: str,
    source_url: str,
) -> NoticeData:
    """Convert a parsed block + context to a ``NoticeData`` record."""
    street, city, zip5 = _split_property_address(block.get("property_address_raw", ""))
    extra = {
        k: v for k, v in {
            "case_number": block.get("case_number"),
            "appraised_value": block.get("appraised_value"),
            "opening_bid": block.get("opening_bid"),
            "deposit": block.get("deposit"),
            "case_status": block.get("case_status"),
            "auction_starts": block.get("auction_starts"),
        }.items() if v
    }
    return NoticeData(
        notice_type="sheriff_sale",
        county=county,
        state="OH",
        address=street,
        city=city,
        zip=zip5,
        parcel_id=block.get("parcel_id", ""),
        auction_date=_auction_date_iso(scraped_date_us),
        source_url=source_url,
        raw_text=json.dumps(extra, ensure_ascii=False) if extra else "",
    )


# ── Override-text path (synchronous, used in tests) ────────────────────


def _crawl_from_override_blocks(
    county: str,
    blocks: list[str],
    *,
    scraped_date_us: str = "",
    source_url: str = "",
) -> list[NoticeData]:
    """Parse a fixture list of raw ``.AUCTION_DETAILS`` innerTexts.

    Used by tests to exercise the parser and the NoticeData builder
    without spinning up Playwright. Each item in ``blocks`` should be
    the raw innerText of one AUCTION_ITEM (e.g. as captured by the live
    probe and saved to JSON).

    Records inherit ``scraped_date_us`` for ``auction_date`` and the
    same ``source_url`` if those came from the same PREVIEW page.
    Pass empty strings to leave them blank.
    """
    out: list[NoticeData] = []
    for raw in blocks:
        parsed = _parse_auction_block(raw)
        # Auctions without a case # or parcel are noise — drop them.
        if not parsed.get("case_number") and not parsed.get("parcel_id"):
            continue
        # If the block carries an "Auction Starts" date and the caller
        # didn't supply one, use the block's own date.
        block_date_us = scraped_date_us
        if not block_date_us and parsed.get("auction_starts"):
            m = re.match(r"(\d{2}/\d{2}/\d{4})", parsed["auction_starts"])
            if m:
                block_date_us = m.group(1)
        out.append(_block_to_notice(county, parsed, block_date_us, source_url))
    return out


# ── Live async crawler (Playwright) ────────────────────────────────────


async def _fetch_one_date(
    ctx: "BrowserContext",
    subdomain: str,
    date_us: str,
) -> tuple[list[str], str | None]:
    """Load one PREVIEW page; return (auction-block innerTexts, next_url).

    The ``next_url`` is the href of the "Next Auction" link if present —
    used by the calendar walker to traverse forward through sale dates.
    """
    url = (
        f"https://{subdomain}.sheriffsaleauction.ohio.gov/"
        f"index.cfm?zaction=AUCTION&zmethod=PREVIEW&AuctionDate={date_us}"
    )
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(_RENDER_WAIT_MS)
        blocks: list[str] = await page.evaluate(
            """() => [...document.querySelectorAll('.AUCTION_DETAILS')]
                .map(el => {
                    const p = el.closest('.AUCTION_ITEM') || el.parentElement;
                    return (p && p.innerText) || el.innerText;
                })"""
        )
        next_url: str | None = await page.evaluate(
            """() => {
                const a = [...document.querySelectorAll('a')]
                    .find(a => /next\\s*auction/i.test(a.textContent || ''));
                return a ? a.href : null;
            }"""
        )
    finally:
        await page.close()
    return blocks, next_url


async def _crawl_realforeclose_forward(
    county: str,
    ctx: "BrowserContext",
    *,
    start_date: str | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    today: datetime | None = None,
) -> list[NoticeData]:
    """Walk a county's auction calendar forward via "Next Auction" links.

    Starting URL strategy: today's date as ``AuctionDate=``. If today
    is a non-sale day, RealForeclose may bounce backward to the most
    recent past auction — that's fine, we skip past records (only emit
    future-dated) and keep following Next-links until we reach the
    first future sale.

    Args:
        county: Title-cased county name (e.g. "Butler"). Must be in
            ``OHIO_SHERIFF_ENDPOINTS``.
        ctx: Playwright ``BrowserContext`` for the live crawl. Caller
            owns lifecycle (open + close).
        start_date: Optional ``MM/DD/YYYY`` override. Defaults to today.
        horizon_days: Stop crawling when an AuctionDate is more than
            this many days past ``today``. Default 90.
        today: Override "now" — used by tests. Defaults to ``datetime.now()``.

    Returns:
        ``list[NoticeData]`` for every auction with an AuctionDate
        between today and ``today + horizon_days``, inclusive.
    """
    cfg = OHIO_SHERIFF_ENDPOINTS.get(county)
    if cfg is None:
        raise ValueError(
            f"Unknown Ohio sheriff-sale county: {county!r}. "
            f"Supported: {sorted(OHIO_SHERIFF_ENDPOINTS)}"
        )
    subdomain = cfg["subdomain"]
    today_dt = today or datetime.now()
    horizon = today_dt + timedelta(days=horizon_days)
    # Seed the walk on the county's observed sale-day weekday so the
    # first Next-link bounces forward (toward the next real auction)
    # rather than backward into deep historical sales.
    if start_date:
        cur_date_us = start_date
    else:
        cur_date_us = _next_sale_day(today_dt, cfg["sale_day"]).strftime("%m/%d/%Y")

    out: list[NoticeData] = []
    seen_dates: set[str] = set()
    for hop in range(_MAX_HOPS):
        if cur_date_us in seen_dates:
            break
        seen_dates.add(cur_date_us)
        try:
            cur_dt = datetime.strptime(cur_date_us, "%m/%d/%Y")
        except ValueError:
            logger.warning("[%s] Unparseable auction date %r — stopping",
                           county, cur_date_us)
            break
        if cur_dt > horizon:
            break

        blocks, next_url = await _fetch_one_date(ctx, subdomain, cur_date_us)
        cur_today = cur_dt.date() >= today_dt.date()
        if cur_today:
            source_url = (
                f"https://{subdomain}.sheriffsaleauction.ohio.gov/"
                f"index.cfm?zaction=AUCTION&zmethod=PREVIEW"
                f"&AuctionDate={cur_date_us}"
            )
            page_records = _crawl_from_override_blocks(
                county, blocks,
                scraped_date_us=cur_date_us,
                source_url=source_url,
            )
            out.extend(page_records)
            logger.info("[%s] %s — %d auctions emitted",
                        county, cur_date_us, len(page_records))
        else:
            logger.debug("[%s] %s is past — walking forward via Next link",
                         county, cur_date_us)

        if not next_url:
            break
        m = re.search(r"AuctionDate=([\d/]+)", next_url)
        if not m:
            break
        cur_date_us = m.group(1)

    logger.info("[%s] Total sheriff-sale records: %d (across %d dates)",
                county, len(out), len(seen_dates))
    return out


# ── Per-county adapters ────────────────────────────────────────────────


def _county_fetcher(county_title: str) -> Callable[..., list[NoticeData]]:
    """Return a per-county adapter that defers to the shared crawler.

    Pattern mirrors ``ohio_tax_delinquent_scrapers.fetch_<county>`` so
    the dispatcher contract is uniform. Each adapter accepts:

        ctx              Playwright BrowserContext (required for live)
        override_blocks  list[str] of fixture innerTexts (sync test path)
        start_date       MM/DD/YYYY override (live mode)
        horizon_days     forward-window override (live mode, default 90)
        today            datetime override (tests)

    Returns ``list[NoticeData]`` synchronously when ``override_blocks=``
    is provided; otherwise returns the awaitable from
    ``_crawl_realforeclose_forward``.
    """
    def fetch(
        ctx=None,
        *,
        override_blocks: list[str] | None = None,
        start_date: str | None = None,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        today: datetime | None = None,
    ):
        if override_blocks is not None:
            return _crawl_from_override_blocks(county_title, override_blocks)
        if ctx is None:
            raise ValueError(
                f"fetch_{county_title.lower()}_sheriff_sale() requires either "
                f"ctx= (Playwright context) or override_blocks= (fixture list)."
            )
        return _crawl_realforeclose_forward(
            county_title, ctx,
            start_date=start_date,
            horizon_days=horizon_days,
            today=today,
        )
    fetch.__name__ = f"fetch_{county_title.lower()}_sheriff_sale"
    fetch.__doc__ = (
        f"Fetch sheriff-sale records for {county_title} County (OH).\n\n"
        f"See module docstring + ``_crawl_realforeclose_forward`` for the "
        f"shared semantics. {county_title} sale day: "
        f"{OHIO_SHERIFF_ENDPOINTS[county_title]['sale_day']}."
    )
    return fetch


fetch_butler_sheriff_sale = _county_fetcher("Butler")
fetch_clark_sheriff_sale = _county_fetcher("Clark")
fetch_clermont_sheriff_sale = _county_fetcher("Clermont")
fetch_greene_sheriff_sale = _county_fetcher("Greene")
fetch_miami_sheriff_sale = _county_fetcher("Miami")
fetch_montgomery_sheriff_sale = _county_fetcher("Montgomery")
fetch_warren_sheriff_sale = _county_fetcher("Warren")


_DISPATCH: dict[str, Callable[..., list[NoticeData]]] = {
    "butler": fetch_butler_sheriff_sale,
    "clark": fetch_clark_sheriff_sale,
    "clermont": fetch_clermont_sheriff_sale,
    "greene": fetch_greene_sheriff_sale,
    "miami": fetch_miami_sheriff_sale,
    "montgomery": fetch_montgomery_sheriff_sale,
    "warren": fetch_warren_sheriff_sale,
}


def fetch_ohio_sheriff_sale(
    county: str,
    *,
    ctx=None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    start_date: str | None = None,
    today: datetime | None = None,
):
    """Dispatch sheriff-sale fetch to the per-county adapter.

    Adapters may return either ``list[NoticeData]`` (sync — when caller
    uses ``override_blocks=``) or an awaitable (async — live Playwright
    path). Callers in the production orchestrator check
    ``inspect.isawaitable()`` and ``await`` when needed, mirroring
    ``fetch_ohio_tax_delinquent``.
    """
    fn = _DISPATCH.get(county.strip().lower())
    if fn is None:
        raise ValueError(
            f"Unknown Ohio sheriff-sale county: {county!r}. "
            f"Supported: {sorted(_DISPATCH)}"
        )
    return fn(
        ctx=ctx,
        horizon_days=horizon_days,
        start_date=start_date,
        today=today,
    )
