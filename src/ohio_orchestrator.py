"""Ohio orchestrator — daily + weekly cron entry points.

Two production runs:

* **daily**  — Montgomery only, all 4 source types (foreclosure,
  probate, tax_delinquent, sheriff_sale). Feeds the
  ``H3 Montgomery Courthouse Data`` DataSift list — the active
  calling list, dialled every day.
* **weekly** — Butler + Clark + Clermont + Greene + Miami + Warren,
  all 4 source types. Feeds the ``H3 SW Ohio Courthouse Data``
  list — secondary inventory.

Records from the two flows NEVER mix into the wrong list. The
:mod:`ohio_destination_lists` module enforces the routing rule;
this module wires the scrapers + bucketing + per-list upload.

CLI:

    python src/ohio_orchestrator.py daily         # Montgomery, all 4 sources
    python src/ohio_orchestrator.py weekly        # other 6, all 4 sources
    python src/ohio_orchestrator.py daily --no-upload   # scrape only, no DataSift
    python src/ohio_orchestrator.py daily --dry-run     # plan + counts, no scrape

Cron wiring (production):
    daily   — 6:00 AM ET, every day:
        0 6 * * *   cd /opt/siftstack && /opt/siftstack/.venv/bin/python \\
                    src/ohio_orchestrator.py daily

    weekly  — 6:00 AM ET, Monday only:
        0 6 * * 1   cd /opt/siftstack && /opt/siftstack/.venv/bin/python \\
                    src/ohio_orchestrator.py weekly

Adjust the cron timezone via the system's TZ env var or use a cron
implementation that honours ``TZ=America/New_York`` per-line.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import inspect
import logging
import sys
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from notice_parser import NoticeData
from ohio_destination_lists import (
    LIST_MONTGOMERY_DAILY,
    LIST_SW_OHIO_WEEKLY,
    WEEKLY_COUNTIES,
    destination_list_for_county,
    split_by_destination_list,
)

logger = logging.getLogger(__name__)


# ── Run configuration ────────────────────────────────────────────────


# All 4 OH source types. Order is intentional — foreclosure first so
# the merged-by-address DataSift list shows the freshest court
# action at the top.
SOURCE_TYPES: tuple[str, ...] = (
    "foreclosure",
    "probate",
    "tax_delinquent",
    "sheriff_sale",
)


DAILY_COUNTIES: tuple[str, ...] = ("Montgomery",)
WEEKLY_COUNTIES_ORDERED: tuple[str, ...] = (
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Warren",
)


# Per source-type adapter dispatcher. The 4 dispatchers all share the
# same call shape: ``fn(county, ctx=None, **kwargs) -> list[NoticeData]
# or awaitable[list[NoticeData]]``.
def _dispatcher_for(source_type: str):
    """Return the right ``fetch_ohio_*`` dispatcher for a source type."""
    if source_type == "foreclosure":
        from ohio_foreclosure_scrapers import fetch_ohio_foreclosure
        return fetch_ohio_foreclosure
    if source_type == "probate":
        from ohio_probate_scrapers import fetch_ohio_probate
        return fetch_ohio_probate
    if source_type == "tax_delinquent":
        from ohio_tax_delinquent_scrapers import fetch_ohio_tax_delinquent
        return fetch_ohio_tax_delinquent
    if source_type == "sheriff_sale":
        from ohio_sheriff_sale_scrapers import fetch_ohio_sheriff_sale
        return fetch_ohio_sheriff_sale
    raise ValueError(f"Unknown source_type: {source_type!r}")


# ── Scrape orchestration ─────────────────────────────────────────────


async def _scrape_one(county: str, source_type: str,
                       *, date_from: str | None = None,
                       date_to: str | None = None) -> list[NoticeData]:
    """Run a single county × source_type combination.

    Optional ``date_from`` / ``date_to`` override the per-adapter
    default date window (which is yesterday → today for foreclosure
    and a 7-day lookback for probate). They thread through to the
    dispatcher only for the source types that accept them —
    ``tax_delinquent`` and ``sheriff_sale`` use other criteria
    (current-snapshot + sale-day calendar respectively).
    """
    dispatcher = _dispatcher_for(source_type)
    # Build the kwargs forwarded to the dispatcher. Only foreclosure
    # + probate accept date_from/date_to; tax_delinquent /
    # sheriff_sale ignore them, so don't pass at all.
    kw = {}
    if source_type in ("foreclosure", "probate"):
        if date_from is not None: kw["date_from"] = date_from
        if date_to is not None:   kw["date_to"] = date_to
    try:
        result = dispatcher(county, **kw)
        if inspect.isawaitable(result):
            result = await result
        n = len(result) if result else 0
        logger.info("  %-12s %-15s → %d records", county, source_type, n)
        return list(result) if result else []
    except NotImplementedError as e:
        logger.warning("  %-12s %-15s skipped (stub): %s",
                       county, source_type, e)
        return []
    except Exception:
        logger.exception("  %-12s %-15s FAILED — continuing",
                         county, source_type)
        return []


async def scrape_all(counties: list[str],
                     source_types: list[str],
                     *, date_from: str | None = None,
                     date_to: str | None = None) -> list[NoticeData]:
    """Run every county × source_type in the matrix sequentially.

    Sequential rather than concurrent because (1) each underlying
    scraper creates its own browser (concurrent would 8x our memory
    footprint) and (2) some county portals throttle aggressively.

    The scrape pattern is shared with TN's ``scraper.scrape_all`` —
    individual failures don't kill the run.
    """
    out: list[NoticeData] = []
    for county in counties:
        for source_type in source_types:
            recs = await _scrape_one(
                county, source_type,
                date_from=date_from, date_to=date_to,
            )
            out.extend(recs)
    return out


# ── Group + upload ───────────────────────────────────────────────────


def _write_batch_csv(notices: list[NoticeData], label: str,
                     out_dir: Path) -> Path:
    """Write a NoticeData list to a DataSift-shaped CSV.

    The DataSift uploader handles the canonical 41-column schema +
    Tags + Lists columns via :func:`datasift_formatter.write_datasift_csv`.
    """
    from datasift_formatter import write_datasift_csv
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"OH_{label}_{timestamp}.csv"
    return write_datasift_csv(notices, filename=filename)


async def upload_by_destination(notices: list[NoticeData], *,
                                  enrich: bool = True,
                                  skip_trace: bool = True,
                                  headless: bool = True,
                                  upload: bool = True) -> dict:
    """Bucket notices by destination list + upload each separately.

    Two completely separate ``upload_to_datasift`` calls — different
    ``list_name`` per bucket. Each list has its own enrichment +
    skip-trace.

    Returns a summary dict keyed by list_name with per-list upload
    outcome.
    """
    buckets = split_by_destination_list(notices)
    out_dir = Path("output"); out_dir.mkdir(exist_ok=True)
    summary: dict[str, dict] = {}

    for list_name, batch in buckets.items():
        # Stable per-list label for the CSV filename
        if list_name == LIST_MONTGOMERY_DAILY:
            label = "Montgomery_daily"
        elif list_name == LIST_SW_OHIO_WEEKLY:
            label = "SW_Ohio_weekly"
        else:
            label = list_name.replace(" ", "_")

        csv_path = _write_batch_csv(batch, label, out_dir)
        logger.info("[%s] wrote %d records → %s",
                    list_name, len(batch), csv_path)

        if not upload:
            summary[list_name] = {
                "records": len(batch),
                "csv_path": str(csv_path),
                "uploaded": False,
                "note": "upload=False (scrape-only mode)",
            }
            continue

        # Per-list upload. The DataSift uploader's upload_to_datasift
        # uses upload_csv() under the hood — pass list_name= to route
        # each batch into the right list without cross-contamination.
        from datasift_uploader import upload_to_datasift_with_list

        try:
            result = await upload_to_datasift_with_list(
                csv_path,
                list_name=list_name,
                enrich=enrich,
                skip_trace=skip_trace,
                headless=headless,
            )
        except NameError:
            # Backward compat: if the helper doesn't exist yet, fall
            # back to upload_to_datasift (which doesn't accept
            # list_name — DataSift falls back to its CSV's Lists
            # column for routing).
            from datasift_uploader import upload_to_datasift
            result = await upload_to_datasift(
                csv_path,
                enrich=enrich,
                skip_trace=skip_trace,
                headless=headless,
            )
            result["list_name_was_threaded"] = False
        summary[list_name] = {
            "records": len(batch),
            "csv_path": str(csv_path),
            "uploaded": result.get("success", False),
            "upload_result": result,
        }
        logger.info("[%s] upload: %s (records=%d)",
                    list_name, result.get("message", "?"), len(batch))

    return summary


# ── Public entry points (also used by main.py + cron) ───────────────


async def run_daily(*, upload: bool = True, headless: bool = True,
                     dry_run: bool = False,
                     date_from: str | None = None,
                     date_to: str | None = None) -> dict:
    """Daily Montgomery run — 4 source types → Montgomery DataSift list."""
    logger.info("=" * 70)
    logger.info("OH ORCHESTRATOR — DAILY (Montgomery)")
    logger.info("=" * 70)
    return await _run(DAILY_COUNTIES, upload=upload, headless=headless,
                      dry_run=dry_run,
                      date_from=date_from, date_to=date_to)


async def run_weekly(*, upload: bool = True, headless: bool = True,
                      dry_run: bool = False,
                      date_from: str | None = None,
                      date_to: str | None = None) -> dict:
    """Weekly run — 6 counties × 4 source types → SW Ohio DataSift list."""
    logger.info("=" * 70)
    logger.info("OH ORCHESTRATOR — WEEKLY (Butler/Clark/Clermont/Greene/Miami/Warren)")
    logger.info("=" * 70)
    return await _run(WEEKLY_COUNTIES_ORDERED, upload=upload,
                      headless=headless, dry_run=dry_run,
                      date_from=date_from, date_to=date_to)


async def _run(counties: tuple[str, ...], *, upload: bool, headless: bool,
                dry_run: bool,
                date_from: str | None = None,
                date_to: str | None = None) -> dict:
    """Shared body of daily/weekly. Returns a summary."""
    start = time.monotonic()
    logger.info("Counties: %s", ", ".join(counties))
    logger.info("Source types: %s", ", ".join(SOURCE_TYPES))
    if date_from or date_to:
        logger.info("Date window override: %s → %s",
                    date_from or "<default>", date_to or "<default>")

    if dry_run:
        # Confirm routing without doing any work
        plan = {}
        for c in counties:
            list_name = destination_list_for_county(c)
            plan.setdefault(list_name, []).append(c)
        for list_name, cts in plan.items():
            logger.info("  PLAN: %d counties → %s — %s",
                        len(cts), list_name, ", ".join(cts))
        return {"dry_run": True, "plan": plan}

    notices = await scrape_all(list(counties), list(SOURCE_TYPES),
                                date_from=date_from, date_to=date_to)
    elapsed = time.monotonic() - start
    logger.info("Scrape phase done in %.1fs — %d total records",
                elapsed, len(notices))

    if not notices:
        logger.warning("No records scraped — nothing to upload.")
        return {"records": 0, "elapsed_s": elapsed, "upload_summary": {}}

    upload_summary = await upload_by_destination(
        notices, headless=headless, upload=upload,
    )

    return {
        "records": len(notices),
        "elapsed_s": elapsed,
        "upload_summary": upload_summary,
    }


# ── CLI ──────────────────────────────────────────────────────────────


def _cli():
    parser = argparse.ArgumentParser(
        description="Ohio data orchestrator — daily / weekly cron entry.",
    )
    parser.add_argument("mode", choices=("daily", "weekly"),
                        help="Which run to execute. 'daily' = Montgomery; "
                             "'weekly' = the other 6 counties.")
    parser.add_argument("--no-upload", action="store_true",
                        help="Scrape + write CSV but skip DataSift upload.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the per-list destination plan + exit. "
                             "No scraping, no uploads.")
    parser.add_argument("--headed", action="store_true",
                        help="Run browser headed (default: headless).")
    parser.add_argument("--date-from", default=None,
                        help="Override the start of the scrape window "
                             "(YYYY-MM-DD or MM/DD/YYYY). Applies to "
                             "foreclosure + probate only; tax_delinquent "
                             "always pulls current state and sheriff_sale "
                             "always pulls the upcoming-90-day calendar.")
    parser.add_argument("--date-to", default=None,
                        help="Override the end of the scrape window. "
                             "Same source-type semantics as --date-from.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG-level logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.mode == "daily":
        result = asyncio.run(run_daily(
            upload=not args.no_upload, headless=not args.headed,
            dry_run=args.dry_run,
            date_from=args.date_from, date_to=args.date_to,
        ))
    else:
        result = asyncio.run(run_weekly(
            upload=not args.no_upload, headless=not args.headed,
            dry_run=args.dry_run,
            date_from=args.date_from, date_to=args.date_to,
        ))

    logger.info("=" * 70)
    logger.info("FINAL: %s", result)
    return 0 if result.get("records", 0) >= 0 else 1


if __name__ == "__main__":
    sys.exit(_cli())
