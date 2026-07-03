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
import os
import sys
import time
from dataclasses import fields
from datetime import datetime, timedelta, timezone
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


# Source types split by upload cadence:
#
#   daily/weekly:  foreclosure + probate + sheriff_sale — fresh court
#                  activity, highest signal-to-noise. Each county's
#                  feed is small enough to scrape under 10 min.
#   quarterly:     tax_delinquent — county treasurer's delinquent
#                  property list. The feed is a 3000+ row snapshot
#                  that changes slowly. A 3-month cadence catches
#                  new delinquencies, paid-off properties, and
#                  amount updates while affording the slow
#                  parcel→address enrichment (~15 min for Mont,
#                  ~2 hr across all 7 counties).
#
# Order within each tuple is intentional — foreclosure first in
# daily/weekly so the merged-by-address DataSift list shows the
# freshest court action at the top.
SOURCE_TYPES: tuple[str, ...] = (
    "foreclosure",
    "probate",
    "sheriff_sale",
)

QUARTERLY_SOURCE_TYPES: tuple[str, ...] = (
    "tax_delinquent",
)


DAILY_COUNTIES: tuple[str, ...] = ("Montgomery",)
WEEKLY_COUNTIES_ORDERED: tuple[str, ...] = (
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Warren",
)
# Quarterly tax_delinquent is Montgomery-only. The other 6 SW Ohio
# counties' tax_delinquent feeds aren't currently enriched (the
# iasWorld parcel→address lookup is Montgomery-specific to
# mcrealestate.org), so scraping them in the quarterly pass would
# just produce records without addresses — wasteful. If we ever
# add per-county auditor adapters for the others, bump this back
# to include them.
QUARTERLY_COUNTIES: tuple[str, ...] = ("Montgomery",)


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
                       date_to: str | None = None,
                       max_cases: int | None = None) -> list[NoticeData]:
    """Run a single county × source_type combination.

    Optional ``date_from`` / ``date_to`` override the per-adapter
    default date window (which is yesterday → today for foreclosure
    and a 7-day lookback for probate). They thread through to the
    dispatcher only for the source types that accept them —
    ``tax_delinquent`` and ``sheriff_sale`` use other criteria
    (current-snapshot + sale-day calendar respectively).

    Optional ``max_cases`` overrides the per-source default cap
    (probate=100, foreclosure=200). Same source-type gating —
    tax_delinquent + sheriff_sale ignore.
    """
    dispatcher = _dispatcher_for(source_type)
    # Build the kwargs forwarded to the dispatcher. Only foreclosure
    # + probate accept date_from/date_to/max_cases; tax_delinquent /
    # sheriff_sale ignore them, so don't pass at all.
    kw = {}
    if source_type in ("foreclosure", "probate"):
        if date_from is not None: kw["date_from"] = date_from
        if date_to is not None:   kw["date_to"] = date_to
        if max_cases is not None: kw["max_cases"] = max_cases
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
                     date_to: str | None = None,
                     max_cases: int | None = None) -> list[NoticeData]:
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
                max_cases=max_cases,
            )
            out.extend(recs)
    return out


# ── Group + upload ───────────────────────────────────────────────────


def _dedupe_by_mailing(notices: list[NoticeData]) -> list[NoticeData]:
    """Drop later occurrences of records that share a mailing address.

    Why we need this: DataSift's "Add Data" upload mode merges
    duplicates by Property Address (Street + City + ZIP composite).
    But probate records frequently share a fiduciary mailing address
    (a single attorney handling multiple estates, a child handling
    both parents' estates, etc.), so the same mailing target can
    appear twice in one daily run. Without pre-dedup we'd upload
    the same physical-mail target twice and (depending on DataSift's
    merge behaviour) ship duplicate mail.

    Dedup key is the executor / owner mailing tuple, NOT the property
    address — property address might legitimately differ between two
    estates that share an executor, and we still want a single mail
    target per unique mailing.

    Records with an empty mailing address (sheriff_sale rows are
    owner-less by design) are ALWAYS kept — there's no key to dedup
    against, and they represent legitimately distinct properties.

    First occurrence wins. Scrape order is foreclosure → probate →
    sheriff_sale per ``SOURCE_TYPES``, so a record from the richer
    source (foreclosure has owner first/last name, probate has
    executor + decedent) survives over a thinner overlap.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[NoticeData] = []
    duplicates_by_type: dict[str, int] = {}
    for n in notices:
        street = (n.owner_street or "").strip().lower()
        if not street:
            # No mailing — can't dedup. Always keep (sheriff_sale,
            # foreclosure where defendant address blank, etc.)
            out.append(n)
            continue
        key = (
            street,
            (n.owner_city or "").strip().lower(),
            (n.owner_zip or "").strip(),
        )
        if key in seen:
            duplicates_by_type[n.notice_type or "?"] = (
                duplicates_by_type.get(n.notice_type or "?", 0) + 1
            )
            continue
        seen.add(key)
        out.append(n)
    if duplicates_by_type:
        logger.info(
            "Dedup by mailing address: dropped %d duplicate(s) "
            "[%s]; kept %d/%d records",
            sum(duplicates_by_type.values()),
            ", ".join(f"{k}={v}" for k, v in duplicates_by_type.items()),
            len(out), len(notices),
        )
    return out


def _write_batch_csv(notices: list[NoticeData], label: str,
                     out_dir: Path,
                     list_name: str | None = None) -> Path:
    """Write a NoticeData list to a DataSift-shaped CSV.

    ``list_name`` overrides the per-row ``Lists`` column for the
    entire CSV — required so the bucket lands in the right DataSift
    destination ("H3 Montgomery Courthouse Data" or "H3 SW Ohio
    Courthouse Data") instead of the legacy per-notice-type lists.
    """
    from datasift_formatter import write_datasift_csv
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"OH_{label}_{timestamp}.csv"
    return write_datasift_csv(notices, filename=filename,
                                list_name=list_name)


# Notice types that benefit from Tracerfy skip-trace + Trestle phone
# scoring. Probate is excluded because its fiduciary contact already
# comes from court-record HTML / OnBase PDFs (no point paying
# Tracerfy to re-find the executor we already have).
_SKIP_TRACE_NOTICE_TYPES = frozenset({
    "foreclosure", "sheriff_sale", "tax_delinquent",
})


def _enrich_with_skip_trace_and_scoring(
    notices: list[NoticeData],
) -> dict:
    """Run Tracerfy + Trestle enrichment on non-probate notices.

    Both phases are env-var gated and cost-capped:
      * TRACERFY_ENABLED=1 + TRACERFY_DAILY_COST_CAP_USD (default $5)
      * TRESTLE_ENABLED=1  + TRESTLE_DAILY_COST_CAP_USD  (default $3)

    Mutates ``notices`` in place. Phones land on
    ``primary_phone / mobile_1 / mobile_2``; emails on ``email_1``;
    tiers on ``primary_phone_tier / mobile_1_tier / mobile_2_tier``.

    Cost capping is preflight-only — Tracerfy's batch endpoint
    submits the whole batch at once with no streaming, so we trim
    the input list to a count whose worst-case cost (records × $0.02)
    fits in the cap. Trestle uses the same trim approach by predicting
    Σ(unique-phones-per-record) × $0.015.

    Returns ``{tracerfy_*: ..., trestle_*: ...}`` stats for the Slack
    summary. Empty dict if both phases are disabled or no eligible
    notices exist.
    """
    tracerfy_on = os.environ.get("TRACERFY_ENABLED") == "1"
    trestle_on  = os.environ.get("TRESTLE_ENABLED")  == "1"
    if not (tracerfy_on or trestle_on):
        return {}

    eligible = [n for n in notices
                if n.notice_type in _SKIP_TRACE_NOTICE_TYPES]
    if not eligible:
        logger.info("Enrichment: no foreclosure/sheriff_sale/"
                    "tax_delinquent records to skip-trace")
        return {}

    stats: dict = {}

    # ── Tracerfy phase ──
    if tracerfy_on:
        cap_usd = float(os.environ.get("TRACERFY_DAILY_COST_CAP_USD",
                                          "5.0"))
        # Cost per record submitted is ~$0.02 (per
        # tracerfy_skip_tracer.py: stats["cost"] = submitted * 0.02).
        # Trim eligible list to fit the cap.
        per_record_cost = 0.02
        max_records = int(cap_usd / per_record_cost)
        to_trace = eligible[:max_records]
        skipped = len(eligible) - len(to_trace)
        if skipped:
            logger.warning("Tracerfy cap $%.2f → tracing first %d of "
                           "%d records (%d skipped to stay in budget)",
                           cap_usd, len(to_trace), len(eligible),
                           skipped)
        try:
            from tracerfy_skip_tracer import batch_skip_trace
            t_stats = batch_skip_trace(to_trace)
            phones_added = sum(1 for n in to_trace if n.primary_phone)
            logger.info("Tracerfy skip-traced %d/%d records "
                        "(phones+%d, cost $%.4f / $%.2f cap)",
                        t_stats.get("matched", 0), len(to_trace),
                        phones_added, t_stats.get("cost", 0.0),
                        cap_usd)
            stats.update({
                "tracerfy_records_traced": len(to_trace),
                "tracerfy_records_matched": t_stats.get("matched", 0),
                "tracerfy_phones_added": phones_added,
                "tracerfy_cost_usd": t_stats.get("cost", 0.0),
                "tracerfy_cap_usd": cap_usd,
            })
        except Exception:
            logger.exception("Tracerfy phase FAILED — continuing "
                             "pipeline without skip-trace")

    # ── Trestle phase ──
    if trestle_on:
        cap_usd = float(os.environ.get("TRESTLE_DAILY_COST_CAP_USD",
                                          "3.0"))
        # Only score notices that have at least one phone to score.
        # If Tracerfy didn't run, that's whatever's already on them
        # from prior enrichment passes (typically empty for fresh
        # foreclosure data, so this is effectively gated on Tracerfy).
        from phone_validator import (
            score_record_phones, COST_PER_PHONE,
            _collect_phones_from_notice,
        )
        to_score: list[NoticeData] = []
        unique_phones: set[str] = set()
        for n in eligible:
            n_phones = _collect_phones_from_notice(n)
            if not n_phones:
                continue
            # Predict total unique-phone count if we include n.
            predicted = unique_phones | set(n_phones)
            if len(predicted) * COST_PER_PHONE > cap_usd:
                break
            unique_phones = predicted
            to_score.append(n)
        skipped = sum(1 for n in eligible
                      if _collect_phones_from_notice(n)) - len(to_score)
        if skipped:
            logger.warning("Trestle cap $%.2f → scoring first %d of "
                           "eligible records (%d skipped to stay "
                           "in budget)",
                           cap_usd, len(to_score), skipped)
        if to_score:
            try:
                results = score_record_phones(to_score)
                # Mirror the Trestle tier strings onto our 3 NoticeData
                # tier fields so datasift_formatter can emit them as
                # CSV columns without parsing heir_map_json.
                from phone_validator import clean_phone
                tier_applied = 0
                for n in to_score:
                    for src_field, tier_field in (
                        ("primary_phone", "primary_phone_tier"),
                        ("mobile_1",      "mobile_1_tier"),
                        ("mobile_2",      "mobile_2_tier"),
                    ):
                        phone = getattr(n, src_field, "") or ""
                        if not phone:
                            continue
                        cleaned = clean_phone(phone)
                        score = results.get(cleaned)
                        if score:
                            setattr(n, tier_field, score["tier"])
                            tier_applied += 1
                cost = len(results) * COST_PER_PHONE
                logger.info("Trestle scored %d phones across %d "
                            "records (cost $%.4f / $%.2f cap, "
                            "%d tier tags applied)",
                            len(results), len(to_score), cost,
                            cap_usd, tier_applied)
                stats.update({
                    "trestle_phones_scored": len(results),
                    "trestle_records_scored": len(to_score),
                    "trestle_tier_tags_applied": tier_applied,
                    "trestle_cost_usd": cost,
                    "trestle_cap_usd": cap_usd,
                })
            except Exception:
                logger.exception("Trestle phase FAILED — continuing "
                                 "pipeline without phone scoring")

    return stats


def _run_enrichers(notices: list[NoticeData]) -> dict:
    """Run Smarty + Zillow + Obituary on notices in place.

    Runs between the sheriff-new-only filter and dedup/junk-filter/
    Tracerfy. Order matters:

      * **Smarty first** — normalizes addresses so downstream Zillow
        lookups hit and the dedup mailing-address key is USPS-canonical.
        The upstream ``address_standardizer`` guard rejecting non-``TN``
        matches was loosened to compare against each notice's own
        ``state`` field — Ohio addresses now standardize; a bad
        cross-state match is still caught for TN and OH alike.
      * **Zillow on blank-owner rows only** — per operator directive
        (2026-07-02) after the DataSift Pass-2 measurement was deferred.
        Note: the OpenWebNinja Zillow endpoint returns property signals
        (Zestimate, sqft, equity) but does NOT return owner-of-record,
        so this is a cost-saving cut rather than an owner-recovery step.
        Owner-name backfill for Montgomery FC/sheriff is handled
        upstream by the auditor parcel lookup.
      * **Obituary last** — needs a populated owner name to search.
        Uses the same enricher as the TN pipeline (probate preset sets
        DM directly from decedent; regular records search obit archives
        + heir chain). Setting ``owner_deceased=yes`` +
        ``decision_maker_name`` feeds the Tracerfy pre-flight guards
        (commit d6af715) — deceased owners with no DM get skipped
        rather than wasting an API call on a dead person.

    Each phase is independently gated on credential presence in the
    process env. Failures in one phase never break the next.

    Returns a stats dict for the Slack summary.
    """
    import config
    stats: dict = {}

    # ── Smarty ──
    if config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN:
        try:
            from address_standardizer import standardize_addresses
            standardize_addresses(
                notices, config.SMARTY_AUTH_ID, config.SMARTY_AUTH_TOKEN,
            )
            confirmed = sum(1 for n in notices if n.dpv_match_code == "Y")
            logger.info("Smarty USPS-confirmed: %d/%d",
                         confirmed, len(notices))
            stats["smarty_confirmed"] = confirmed
            stats["smarty_targets"] = len(notices)
        except Exception:
            logger.exception("Smarty phase failed — continuing")
    else:
        logger.info("Smarty: skipped (no SMARTY_AUTH_ID/TOKEN in env)")

    # ── Entity research (resolve the person behind LLC/HOA/Trust) ──
    # Runs BEFORE Zillow + Obituary so the resolved person's name feeds
    # both. Without this step, entity-owned rows (~10% of a typical
    # Montgomery day — LLC-owned rentals, HOA-owned commons, family
    # trusts) come through with owner_name populated but
    # datasift_formatter._clean_and_split_name blanks the CSV First/Last
    # name columns (entity names → "" per the DataSift contract). The
    # resolved entity_person_name is used by _get_contact_info as a
    # fallback when owner_name is an entity, so a resolved LLC member
    # or registered agent shows up as First/Last in the CSV and can be
    # skip-traced by Tracerfy.
    #
    # Cost: ~$0.01 per entity via Anthropic Haiku + Serper/Firecrawl
    # web search. Uses the same rate-limit-prone infra as obituary
    # (Google/Brave/DDG for name lookups) — may partially fail if
    # search engines are throttling. Silent fallback per module design.
    if config.ANTHROPIC_API_KEY:
        try:
            from entity_researcher import enrich_entity_data
            enrich_entity_data(notices, config.ANTHROPIC_API_KEY)
            resolved = sum(1 for n in notices
                            if (getattr(n, "entity_person_name", "") or "").strip())
            logger.info("Entity research: resolved %d person(s) behind entities",
                         resolved)
            stats["entity_resolved"] = resolved
        except Exception:
            logger.exception("Entity research phase failed — continuing")
    else:
        logger.info("Entity research: skipped (no ANTHROPIC_API_KEY in env)")

    # ── Property-state validation (catch cross-state bad rows) ──
    # For records whose county is Montgomery (or any of the SW OH
    # counties in the weekly slate) but whose Smarty-normalized state
    # came back as something other than OH, the scraper misparsed a
    # defendant/party mailing address as the property address. These
    # rows can't be marketed to (wrong state, wrong address) and any
    # Zillow/Obituary/Tracerfy spend on them is wasted. Drop.
    #
    # We check state AFTER Smarty because Smarty is authoritative:
    # if a row entered with state="OH" (scraper default) but Smarty's
    # validated candidate returned "TN"/"GA"/"KY", the address is
    # actually in that state. Note: some bad-data rows never reach
    # Smarty (dpv_match_code stays blank) — those slip through this
    # filter and only get caught downstream by validation.
    OH_COUNTIES = {"Montgomery", "Butler", "Clark", "Clermont",
                   "Greene", "Miami", "Warren"}
    cross_state_dropped: list = []
    kept: list = []
    for n in notices:
        if (getattr(n, "county", "") in OH_COUNTIES
                and (n.state or "").strip().upper() not in ("OH", "")):
            cross_state_dropped.append(n)
        else:
            kept.append(n)
    if cross_state_dropped:
        logger.warning(
            "Property-state filter: dropped %d cross-state row(s) — "
            "county=Montgomery-slate but Smarty state=%s",
            len(cross_state_dropped),
            ",".join(sorted({(n.state or "?").upper() for n in cross_state_dropped})),
        )
        for n in cross_state_dropped:
            logger.warning(
                "  dropped %s | %s, %s %s | %s",
                getattr(n, "case_number", "?"),
                getattr(n, "address", ""),
                getattr(n, "city", ""),
                n.state,
                getattr(n, "notice_type", "?"),
            )
        # In-place list mutation so callers see the filtered set
        notices[:] = kept
        stats["cross_state_dropped"] = len(cross_state_dropped)

    # ── Zillow (blank-owner rows only) ──
    blank_owner = [n for n in notices if not (n.owner_name or "").strip()]
    if blank_owner and config.OPENWEBNINJA_API_KEY:
        try:
            from property_enricher import enrich_properties
            enrich_properties(blank_owner, config.OPENWEBNINJA_API_KEY)
            enriched = sum(1 for n in blank_owner if n.estimated_value)
            logger.info("Zillow enriched %d/%d blank-owner rows",
                         enriched, len(blank_owner))
            stats["zillow_enriched"] = enriched
            stats["zillow_targets"] = len(blank_owner)
        except Exception:
            logger.exception("Zillow phase failed — continuing")
    elif not blank_owner:
        logger.info("Zillow: skipped (no blank-owner rows)")
    else:
        logger.info("Zillow: skipped (no OPENWEBNINJA_API_KEY in env)")

    # ── Obituary ──
    if config.ANTHROPIC_API_KEY:
        try:
            from obituary_enricher import enrich_obituary_data
            enrich_obituary_data(notices, config.ANTHROPIC_API_KEY)
            deceased = sum(1 for n in notices
                            if (n.owner_deceased or "").lower() == "yes")
            with_dm = sum(1 for n in notices
                            if (n.decision_maker_name or "").strip())
            logger.info("Obituary: %d confirmed deceased, %d DM identified",
                         deceased, with_dm)
            stats["obituary_deceased"] = deceased
            stats["obituary_dm_identified"] = with_dm
        except Exception:
            logger.exception("Obituary phase failed — continuing")
    else:
        logger.info("Obituary: skipped (no ANTHROPIC_API_KEY in env)")

    return stats


async def upload_by_destination(notices: list[NoticeData], *,
                                  enrich: bool = True,
                                  skip_trace: bool = True,
                                  headless: bool = True,
                                  upload: bool = True,
                                  post_to_slack: bool = False,
                                  auditor_stats: dict | None = None) -> dict:
    """Bucket notices by destination list + upload each separately.

    Two completely separate ``upload_to_datasift`` calls — different
    ``list_name`` per bucket. Each list has its own enrichment +
    skip-trace.

    Args:
        post_to_slack: When True, post the bucket's CSV (with a stats
            summary) to #h3-homebuyers-ftm after writing each CSV.
            Daily mode passes this through; other modes don't.
        auditor_stats: Optional dict of {auditor_enriched, auditor_targets}
            captured by the caller during the enrichment phase, threaded
            here so the Slack summary message has the right numbers.

    Returns a summary dict keyed by list_name with per-list upload
    outcome.
    """
    # Junk-owner filter (pre-Tracerfy). Runs here — BEFORE dedup and
    # skip-trace — so we don't waste $0.02/row tracing rows that would
    # be dropped anyway, and so dedup's mailing-key comparison operates
    # on the reduced set. The CSV writer still has an idempotent junk
    # check as defense-in-depth, so double-dropping is impossible.
    # Dropped rows are logged to output/filtered_junk.csv for audit.
    from datasift_formatter import filter_junk_owners
    notices, _junk_dropped_pre = filter_junk_owners(notices)
    junk_dropped_count = len(_junk_dropped_pre)

    # Dedup BEFORE bucketing — keeps the first occurrence of each
    # unique mailing address across all source types. See
    # _dedupe_by_mailing() for the full rationale.
    notices_pre_dedup = len(notices)
    notices = _dedupe_by_mailing(notices)
    dedup_dropped = notices_pre_dedup - len(notices)

    # Skip-trace + phone scoring for non-probate records, post-dedup
    # so we don't pay Tracerfy/Trestle twice for the same mailing
    # target. Each phase is independently env-gated and cost-capped;
    # both no-op when their *_ENABLED flag is unset. Mutates notices
    # in place — phones land on primary_phone/mobile_1/mobile_2, tiers
    # on primary_phone_tier/mobile_1_tier/mobile_2_tier.
    enrich_stats = _enrich_with_skip_trace_and_scoring(notices)

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

        csv_path = _write_batch_csv(batch, label, out_dir,
                                      list_name=list_name)
        logger.info("[%s] wrote %d records → %s",
                    list_name, len(batch), csv_path)

        # Post to #h3-homebuyers-ftm (daily mode only). Stats are
        # auto-computed from the CSV in slack_poster; we pass through
        # the state-bearing numbers (dedup count, auditor enrichment)
        # that aren't recoverable post-write.
        if post_to_slack:
            try:
                from slack_poster import post_csv_to_ftm
                slack_summary: dict = {"dedup_dropped": dedup_dropped}
                if auditor_stats:
                    slack_summary.update(auditor_stats)
                if enrich_stats:
                    slack_summary.update(enrich_stats)
                post_csv_to_ftm(csv_path, slack_summary)
            except Exception:
                # Slack post failure must never break the run — the
                # CSV is already on disk and that's the load-bearing
                # delivery in upload=False mode.
                logger.exception("Slack post failed; CSV still at %s",
                                 csv_path)

        if not upload:
            summary[list_name] = {
                "records": len(batch),
                "csv_path": str(csv_path),
                "uploaded": False,
                "note": "upload=False (scrape-only mode)",
            }
            continue

        # Pass list_name through so the wizard's Setup step routes
        # records into the EXISTING destination list (existing_list=
        # True path inside upload_csv). The wizard's per-row Lists
        # column-mapping is unreliable in practice — confirmed by
        # records landing only under the wizard's default list name
        # despite the CSV having "Lists=H3 Montgomery Courthouse
        # Data" on every row. Setting the destination at the Setup
        # step is the only path that actually puts records in the
        # right list.
        from datasift_uploader import upload_to_datasift
        result = await upload_to_datasift(
            csv_path,
            list_name=list_name,
            enrich=enrich,
            skip_trace=skip_trace,
            headless=headless,
        )
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


async def run_daily(*, upload: bool = False, headless: bool = True,
                     dry_run: bool = False,
                     date_from: str | None = None,
                     date_to: str | None = None,
                     post_to_slack: bool = True,
                     max_cases: int | None = None) -> dict:
    """Daily Montgomery run — 3 source types → Slack file-drop.

    DEFAULTS CHANGED (2026-06-24):
      - ``upload`` defaults to **False**. The daily delivery model is
        Slack file-drop to #h3-homebuyers-ftm, not DataSift web-wizard
        upload. Operators pull the CSV from Slack into whatever
        downstream destination they want. Pass ``upload=True`` (or
        ``--upload`` on the CLI) to re-enable the DataSift wizard.
      - ``post_to_slack`` defaults to **True**. The orchestrator posts
        the CSV + summary stats to #h3-homebuyers-ftm after CSV write.
        Requires SLACK_BOT_TOKEN in the env (handled by the launchd
        plist's EnvironmentVariables). Without the token, the post is
        silently skipped — daily run still produces the CSV in output/.
    """
    logger.info("=" * 70)
    logger.info("OH ORCHESTRATOR — DAILY (Montgomery)")
    logger.info("=" * 70)
    return await _run(DAILY_COUNTIES, upload=upload, headless=headless,
                      dry_run=dry_run,
                      date_from=date_from, date_to=date_to,
                      post_to_slack=post_to_slack,
                      max_cases=max_cases)


async def run_weekly(*, upload: bool = True, headless: bool = True,
                      dry_run: bool = False,
                      date_from: str | None = None,
                      date_to: str | None = None,
                      max_cases: int | None = None) -> dict:
    """Weekly run — 6 counties × 3 source types → SW Ohio DataSift list."""
    logger.info("=" * 70)
    logger.info("OH ORCHESTRATOR — WEEKLY (Butler/Clark/Clermont/Greene/Miami/Warren)")
    logger.info("=" * 70)
    return await _run(WEEKLY_COUNTIES_ORDERED, upload=upload,
                      headless=headless, dry_run=dry_run,
                      date_from=date_from, date_to=date_to,
                      max_cases=max_cases)


async def run_quarterly(*, upload: bool = True, headless: bool = True,
                         dry_run: bool = False,
                         enrich_addresses: bool = True) -> dict:
    """Quarterly run — Montgomery tax_delinquent with auditor enrichment.

    A 3-month cadence catches new delinquencies, paid-off properties
    (records dropping off the list), and amount changes. Splitting
    this out from daily/weekly skips wasted scrape time on every
    daily firing and lets us afford expensive parcel→address
    enrichment here (Montgomery's feed has parcel# but no address;
    the iasWorld auditor lookup is ~10 sec/parcel × concurrency=5
    → ~15 min for a typical 451-record post-filter list).

    **Montgomery only.** Other SW Ohio counties don't currently have
    iasWorld parcel→address adapters wired up; running their
    tax_delinquent feeds in the quarterly pass would produce records
    without addresses, defeating the point. Add per-county adapters
    + bump QUARTERLY_COUNTIES if/when that changes.

    Routes to the **H3 Montgomery Courthouse Data** DataSift list
    via the existing ``destination_list_for_county()`` rule.
    DataSift's "Add Data" mode merges by property address — so
    quarterly re-runs ADD new delinquent properties, UPDATE the
    delinquency amount on existing rows, and leave paid-off
    properties in place (they just stop appearing in fresh feeds).
    """
    logger.info("=" * 70)
    logger.info("OH ORCHESTRATOR — QUARTERLY (Montgomery tax_delinquent + auditor)")
    logger.info("=" * 70)
    return await _run(QUARTERLY_COUNTIES, upload=upload,
                      headless=headless, dry_run=dry_run,
                      source_types=QUARTERLY_SOURCE_TYPES,
                      enrich_addresses=enrich_addresses)


def _days_ago_iso(n: int, today_iso: str) -> str:
    """ISO date N days before ``today_iso``. Used for backfill detection."""
    today = datetime.fromisoformat(today_iso).date()
    return (today - timedelta(days=n)).isoformat()


async def _run(counties: tuple[str, ...], *, upload: bool, headless: bool,
                dry_run: bool,
                date_from: str | None = None,
                date_to: str | None = None,
                source_types: tuple[str, ...] = SOURCE_TYPES,
                enrich_addresses: bool = False,
                post_to_slack: bool = False,
                max_cases: int | None = None) -> dict:
    """Shared body of daily/weekly/yearly. Returns a summary."""
    start = time.monotonic()
    logger.info("Counties: %s", ", ".join(counties))
    logger.info("Source types: %s", ", ".join(source_types))
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

    notices = await scrape_all(list(counties), list(source_types),
                                date_from=date_from, date_to=date_to,
                                max_cases=max_cases)

    # Yearly mode: enrich Montgomery tax_delinquent records with
    # property addresses via the iasWorld parcel→address lookup.
    # The Montgomery feed (mcohio.org/1521/Delinquent-List) exposes
    # parcel + owner + amount but no address — the auditor lookup
    # is the only path. ~10 sec/parcel * 5 concurrent contexts =
    # ~15 min for a typical 451-record post-filter list.
    auditor_stats: dict | None = None
    if enrich_addresses and notices:
        from h3.scrapers.mc_auditor import enrich_tax_delinquent_with_auditor
        mont_td = [
            n for n in notices
            if n.county == "Montgomery"
            and n.notice_type == "tax_delinquent"
            and not n.address
            and n.parcel_id
        ]
        if mont_td:
            logger.info("Enriching %d Montgomery tax_delinquent "
                        "records with parcel→address (concurrent) ...",
                        len(mont_td))
            n_enriched = await enrich_tax_delinquent_with_auditor(
                mont_td, headless=headless,
            )
            logger.info("Auditor enriched %d/%d tax_delinquent "
                        "records with property addresses",
                        n_enriched, len(mont_td))
            auditor_stats = {
                "auditor_enriched": n_enriched,
                "auditor_targets": len(mont_td),
            }
    # Owner-name backfill for records the source scraper couldn't
    # populate (sheriff sale rows in particular — RealForeclose PREVIEW
    # URLs don't expose owner names; some foreclosure rows too, when
    # the case detail parser can't extract a defendant name). Uses the
    # Montgomery Auditor parcel lookup. Runs BEFORE dedup so that
    # records without owner mailing addresses acquire them and can
    # dedup against sibling rows. Also runs BEFORE the junk filter
    # + Tracerfy so we're not wasting API calls on rows we'll enrich
    # into deliverable records here.
    mont_needs_owner = [
        n for n in notices
        if getattr(n, "county", "") == "Montgomery"
        and n.notice_type in ("sheriff_sale", "foreclosure")
        and (n.parcel_id or "").strip()
        and not (n.owner_name or "").strip()
    ]
    # Deliberately NOT gated on `enrich_addresses`. That flag controls
    # the property-address backfill (used by quarterly tax_delinquent);
    # the owner-name backfill here is essential to daily
    # sheriff/foreclosure records — without it, Tracerfy has no name
    # to trace and downstream skip-trace value collapses.
    if mont_needs_owner:
        from h3.scrapers.mc_auditor import enrich_records_owner_by_parcel
        logger.info(
            "Enriching %d Montgomery %s records with owner names "
            "via auditor parcel lookup ...",
            len(mont_needs_owner),
            "/".join(sorted({n.notice_type for n in mont_needs_owner})),
        )
        owner_stats = await enrich_records_owner_by_parcel(
            mont_needs_owner, headless=headless,
        )
        logger.info(
            "Auditor owner-enriched %d/%d records (%d entity, %d failed)",
            owner_stats["enriched"], owner_stats["targets"],
            owner_stats["entity_count"], owner_stats["failed"],
        )
        if auditor_stats is None:
            auditor_stats = {}
        auditor_stats["owner_enriched"] = owner_stats["enriched"]
        auditor_stats["owner_targets"] = owner_stats["targets"]
        auditor_stats["owner_entity_count"] = owner_stats["entity_count"]
        auditor_stats["owner_failed"] = owner_stats["failed"]

    # Address-based owner backfill — fallback for records the parcel
    # pass couldn't help (owner blank AND parcel_id blank AND address
    # populated). Common cause: mcohio case-detail parser failed to
    # extract a defendant name and the docket didn't expose a parcel.
    # Verified 2026-07-02 with 4/14 FC rows on that day's Montgomery
    # daily. Runs AFTER the parcel pass so records that just got their
    # owner via parcel don't waste an address lookup.
    mont_needs_owner_by_addr = [
        n for n in notices
        if getattr(n, "county", "") == "Montgomery"
        and n.notice_type in ("sheriff_sale", "foreclosure")
        and not (n.owner_name or "").strip()
        and not (n.parcel_id or "").strip()
        and (n.address or "").strip()
    ]
    if mont_needs_owner_by_addr:
        from h3.scrapers.mc_auditor import enrich_records_owner_by_address
        logger.info(
            "Enriching %d Montgomery %s records with owner names "
            "via auditor address lookup ...",
            len(mont_needs_owner_by_addr),
            "/".join(sorted({n.notice_type for n in mont_needs_owner_by_addr})),
        )
        addr_stats = await enrich_records_owner_by_address(
            mont_needs_owner_by_addr, headless=headless,
        )
        logger.info(
            "Auditor address-enriched %d/%d records "
            "(%d entity, %d failed)",
            addr_stats["enriched"], addr_stats["targets"],
            addr_stats["entity_count"], addr_stats["failed"],
        )
        if auditor_stats is None:
            auditor_stats = {}
        auditor_stats["owner_by_addr_enriched"] = addr_stats["enriched"]
        auditor_stats["owner_by_addr_targets"] = addr_stats["targets"]
        auditor_stats["owner_by_addr_entity_count"] = addr_stats["entity_count"]
        auditor_stats["owner_by_addr_failed"] = addr_stats["failed"]

    elapsed = time.monotonic() - start
    logger.info("Scrape phase done in %.1fs — %d total records",
                elapsed, len(notices))

    if not notices:
        logger.warning("No records scraped — nothing to upload.")
        return {"records": 0, "elapsed_s": elapsed, "upload_summary": {}}

    # Sheriff-sale "new-only" filter. The cron pulls the entire
    # upcoming-auction calendar every morning (next ~8 weeks); without
    # this, the dial team gets the same cases re-emitted day after
    # day until each auction date passes. The filter tracks every
    # case# we've ever shipped in a small JSON state file and drops
    # already-seen ones BEFORE dedup + Tracerfy + Trestle (so we
    # don't pay enrichment cost on suppressed records).
    #
    # Gated on SHERIFF_NEW_ONLY=1 (the default for daily cron) and
    # auto-disabled for any run that looks like a historical backfill:
    # date_from != date_to (multi-day window) or date_from older than
    # 7 days. Set SHERIFF_NEW_ONLY=0 to force-disable.
    if os.environ.get("SHERIFF_NEW_ONLY", "1") != "0":
        today_iso = datetime.now(timezone.utc).astimezone().date().isoformat()
        is_backfill = False
        if date_from and date_to and date_from != date_to:
            is_backfill = True
        if date_from and date_from < _days_ago_iso(7, today_iso):
            is_backfill = True
        if is_backfill:
            logger.info(
                "Sheriff sale: filter skipped (backfill window "
                "%s → %s)", date_from or "<default>",
                date_to or "<default>",
            )
        else:
            from h3.sheriff_sale_state import filter_to_new_sheriff_sale
            notices = filter_to_new_sheriff_sale(
                notices, today_iso=today_iso,
            )

    # Smarty + Zillow + Obituary run AFTER the sheriff-new-only filter
    # (so we don't pay Zillow/Obituary cost on suppressed records) and
    # BEFORE upload_by_destination's junk filter → dedup → Tracerfy chain
    # (so Smarty-normalized addresses feed the dedup mailing-address key
    # and Obituary-set owner_deceased/decision_maker_name feed Tracerfy's
    # pre-flight deceased-owner guard).
    enricher_stats = _run_enrichers(notices)
    if enricher_stats:
        if auditor_stats is None:
            auditor_stats = {}
        auditor_stats.update(enricher_stats)

    upload_summary = await upload_by_destination(
        notices, headless=headless, upload=upload,
        post_to_slack=post_to_slack,
        auditor_stats=auditor_stats,
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
    parser.add_argument("mode", choices=("daily", "weekly", "quarterly"),
                        help="Which run to execute. 'daily' = Montgomery "
                             "(foreclosure/probate/sheriff_sale); "
                             "'weekly' = the other 6 counties "
                             "(same 3 source types); "
                             "'quarterly' = all 7 counties tax_delinquent "
                             "with parcel→address enrichment (~15 min "
                             "for Mont, ~2 hr for the full 7).")
    parser.add_argument("--no-upload", action="store_true",
                        help="Scrape + write CSV but skip DataSift upload. "
                             "Default for daily mode (which delivers via "
                             "Slack file-drop instead).")
    parser.add_argument("--upload", action="store_true",
                        help="Force DataSift upload for the daily mode "
                             "(which defaults to Slack-only delivery). "
                             "No effect on weekly/quarterly modes — they "
                             "still default to uploading unless "
                             "--no-upload is passed.")
    parser.add_argument("--no-slack", action="store_true",
                        help="Skip the Slack post even on daily mode. "
                             "CSV still lands in output/. Useful for "
                             "spot-check runs that shouldn't broadcast.")
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
    parser.add_argument("--max-cases", type=int, default=None,
                        help="Override the per-source max-case cap "
                             "(probate default 100, foreclosure default "
                             "200). Increase for multi-day backfills "
                             "where the default truncates older cases "
                             "(observed during the 2026-06-26 backtest: "
                             "5/11 probate dropped 2 of 7 cases because "
                             "the 100 cap ran out before reaching the "
                             "target date). Tax_delinquent and "
                             "sheriff_sale ignore.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG-level logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.mode == "daily":
        # Daily defaults to NO DataSift upload (Slack-only delivery).
        # --upload opts back in to the DataSift web-wizard path.
        # --no-upload is redundant for daily but accepted for symmetry.
        daily_upload = args.upload and not args.no_upload
        result = asyncio.run(run_daily(
            upload=daily_upload, headless=not args.headed,
            dry_run=args.dry_run,
            date_from=args.date_from, date_to=args.date_to,
            post_to_slack=not args.no_slack,
            max_cases=args.max_cases,
        ))
    elif args.mode == "weekly":
        result = asyncio.run(run_weekly(
            upload=not args.no_upload, headless=not args.headed,
            dry_run=args.dry_run,
            date_from=args.date_from, date_to=args.date_to,
            max_cases=args.max_cases,
        ))
    else:  # quarterly
        result = asyncio.run(run_quarterly(
            upload=not args.no_upload, headless=not args.headed,
            dry_run=args.dry_run,
        ))

    logger.info("=" * 70)
    logger.info("FINAL: %s", result)
    return 0 if result.get("records", 0) >= 0 else 1


if __name__ == "__main__":
    sys.exit(_cli())
