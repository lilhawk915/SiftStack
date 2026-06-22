"""Tests for the unified Ohio sheriff-sale adapter.

All 7 counties share the same RealForeclose DOM. These tests use raw
``.AUCTION_DETAILS`` innerText blocks captured live (June 2026) from
multiple counties to verify the parser handles every case-number
format observed in production.
"""
from __future__ import annotations

import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from datetime import datetime

from ohio_sheriff_sale_scrapers import (
    DEFAULT_HORIZON_DAYS,
    OHIO_SHERIFF_ENDPOINTS,
    _DISPATCH,
    _auction_date_iso,
    _block_to_notice,
    _crawl_from_override_blocks,
    _next_sale_day,
    _parse_auction_block,
    _split_property_address,
    fetch_butler_sheriff_sale,
    fetch_clark_sheriff_sale,
    fetch_clermont_sheriff_sale,
    fetch_greene_sheriff_sale,
    fetch_miami_sheriff_sale,
    fetch_montgomery_sheriff_sale,
    fetch_ohio_sheriff_sale,
    fetch_warren_sheriff_sale,
)


# ── Real fixtures captured from live crawl (June 2026) ─────────────────


# Montgomery — `2024 CV 00233 (0)` format, 9am Friday sales
MONTGOMERY_BLOCK = """\
Auction Starts
06/26/2026 09:00 AM ET
Case Status:\tACTIVE
Case #:\t2024 CV 00233 (0)
Parcel ID:\tR72 04805A0027
Property Address:\t39 NORTH QUENTIN AVENUE
\tDAYTON , 45403
Appraised Value:\t$54,000.00
Opening Bid:\t$36,000.00
Deposit Requirement:\t$5,000.00"""

# Miami — `25CV00068 (0)` format, 10am Wednesday sales
MIAMI_BLOCK = """\
Auction Starts
07/01/2026 10:00 AM ET
Case Status:\tACTIVE
Case #:\t25CV00068 (0)
Parcel ID:\tG15-004630
Property Address:\t117 S 7th St
\tTipp City , 45371
Appraised Value:\t$249,000.00
Opening Bid:\t$166,000.00
Deposit Requirement:\t$10,000.00"""

# Butler — `CV23122477 (5756)` format + 9-digit padded zip
BUTLER_BLOCK = """\
Auction Starts
06/18/2026 09:00 AM ET
Case Status:\tACTIVE
Case #:\tCV23122477 (5756)
Parcel ID:\tB1010 013 000 021
Property Address:\t2016 GARDNER ROAD
\tHAMILTON , 450130000
Appraised Value:\t$330,000.00
Opening Bid:\t$220,000.00
Deposit Requirement:\t$10,000.00"""

# Warren — `24CV098101` format (no trailing parens)
WARREN_BLOCK = """\
Auction Starts
07/28/2026 09:00 AM ET
Case Status:\tACTIVE
Case #:\t24CV098101
Parcel ID:\t1616384037
Property Address:\t7766 HACKNEY CIR
\tMAINEVILLE , 45039
Appraised Value:\t$210,000.00
Opening Bid:\t$140,000.00
Deposit Requirement:\t$10,000.00"""

# Clermont — `2025-CVE-1628 (0)` (hyphenated CVE form)
CLERMONT_BLOCK = """\
Auction Starts
06/30/2026 10:00 AM ET
Case Status:\tACTIVE
Case #:\t2025-CVE-1628 (0)
Parcel ID:\tMULTIPLE
Property Address:\t2377 GINN RD
\tNEW RICHMOND , 45157
Appraised Value:\t$180,000.00
Opening Bid:\t$120,000.00
Deposit Requirement:\t$5,000.00"""

# Greene — `2025CV0151 (241)` format
GREENE_BLOCK = """\
Auction Starts
07/07/2026 09:00 AM ET
Case Status:\tACTIVE
Case #:\t2025CV0151 (241)
Parcel ID:\tM40000100030004100
Property Address:\t107 ROSELAWN DRIVE
\tXENIA , 45385
Appraised Value:\t$180,000.00
Opening Bid:\t$120,000.00
Deposit Requirement:\t$5,000.00"""

# Clark — `24CV0839 (0)` format
CLARK_BLOCK = """\
Auction Starts
07/10/2026 09:00 AM ET
Case Status:\tACTIVE
Case #:\t24CV0839 (0)
Parcel ID:\t1010000036402010
Property Address:\t11571 WILTS LN
\tMEDWAY , 45341
Appraised Value:\t$175,000.00
Opening Bid:\t$116,667.00
Deposit Requirement:\t$5,000.00"""


# ── Block parser — handles all 7 case-number formats ──────────────────


@pytest.mark.parametrize("block,expected_case,expected_parcel", [
    (MONTGOMERY_BLOCK, "2024 CV 00233 (0)", "R72 04805A0027"),
    (MIAMI_BLOCK,      "25CV00068 (0)",     "G15-004630"),
    (BUTLER_BLOCK,     "CV23122477 (5756)", "B1010 013 000 021"),
    (WARREN_BLOCK,     "24CV098101",        "1616384037"),
    (CLERMONT_BLOCK,   "2025-CVE-1628 (0)", "MULTIPLE"),
    (GREENE_BLOCK,     "2025CV0151 (241)",  "M40000100030004100"),
    (CLARK_BLOCK,      "24CV0839 (0)",      "1010000036402010"),
])
def test_parse_case_number_format_variants(block, expected_case, expected_parcel):
    """All 7 county case-number formats parse cleanly."""
    p = _parse_auction_block(block)
    assert p["case_number"] == expected_case
    assert p["parcel_id"] == expected_parcel


def test_parse_extracts_all_money_fields():
    p = _parse_auction_block(MONTGOMERY_BLOCK)
    assert p["appraised_value"] == "$54,000.00"
    assert p["opening_bid"] == "$36,000.00"
    assert p["deposit"] == "$5,000.00"


def test_parse_extracts_auction_starts_with_timezone():
    p = _parse_auction_block(MONTGOMERY_BLOCK)
    assert p["auction_starts"] == "06/26/2026 09:00 AM ET"


def test_parse_extracts_case_status():
    p = _parse_auction_block(MONTGOMERY_BLOCK)
    assert p["case_status"] == "ACTIVE"


def test_parse_property_address_raw_includes_city_and_zip():
    p = _parse_auction_block(MONTGOMERY_BLOCK)
    assert p["property_address_raw"] == "39 NORTH QUENTIN AVENUE, DAYTON , 45403"


def test_parse_empty_block_returns_blank_fields():
    p = _parse_auction_block("")
    assert all(v == "" for v in p.values())


def test_parse_partial_block_does_not_crash():
    """Garbage input → empty fields, no exception."""
    p = _parse_auction_block("not a real auction block")
    assert p["case_number"] == ""
    assert p["parcel_id"] == ""


# ── Address splitter — handles county-specific quirks ─────────────────


def test_split_address_normal_5digit_zip():
    s, c, z = _split_property_address("39 NORTH QUENTIN AVENUE, DAYTON, 45403")
    assert s == "39 NORTH QUENTIN AVENUE"
    assert c == "DAYTON"
    assert z == "45403"


def test_split_address_butler_9digit_padded_zip():
    """Butler emits 9-digit zip with trailing zeros (450130000 → 45013)."""
    s, c, z = _split_property_address("2016 GARDNER ROAD, HAMILTON, 450130000")
    assert s == "2016 GARDNER ROAD"
    assert c == "HAMILTON"
    assert z == "45013"


def test_split_address_space_before_zip_comma():
    """Some counties stray a space before the zip comma — strip handles it."""
    s, c, z = _split_property_address("107 ROSELAWN DRIVE, XENIA , 45385")
    assert s == "107 ROSELAWN DRIVE"
    assert c == "XENIA"
    assert z == "45385"


def test_split_address_multi_part_street():
    """Apartment/unit suffix stays with street, not city.

    Real-world Butler example: ``7516 SHAWNEE LANE, UNIT 165, WEST CHESTER, 450690000``.
    The unit-line belongs with the street; the city is the last token
    before the zip. Treating UNIT-165 as the city would route mail
    to the wrong place.
    """
    s, c, z = _split_property_address(
        "7516 SHAWNEE LANE, UNIT 165, WEST CHESTER, 450690000"
    )
    assert s == "7516 SHAWNEE LANE, UNIT 165"
    assert c == "WEST CHESTER"
    assert z == "45069"


def test_split_address_missing_zip_returns_partial():
    """If only 2 parts, treat as 'unparseable' — keep raw in street slot."""
    s, c, z = _split_property_address("just a street name")
    assert s == "just a street name"
    assert c == "" and z == ""


def test_split_address_empty_returns_blank_triple():
    assert _split_property_address("") == ("", "", "")
    assert _split_property_address("   ") == ("", "", "")


# ── Block → NoticeData conversion ──────────────────────────────────────


def test_block_to_notice_basic_fields():
    p = _parse_auction_block(MONTGOMERY_BLOCK)
    n = _block_to_notice(
        "Montgomery", p,
        scraped_date_us="06/26/2026",
        source_url="https://montgomery.sheriffsaleauction.ohio.gov/",
    )
    assert n.notice_type == "sheriff_sale"
    assert n.county == "Montgomery"
    assert n.state == "OH"
    assert n.address == "39 NORTH QUENTIN AVENUE"
    assert n.city == "DAYTON"
    assert n.zip == "45403"
    assert n.parcel_id == "R72 04805A0027"
    assert n.auction_date == "2026-06-26"  # ISO format
    assert n.source_url.startswith("https://montgomery.sheriffsaleauction.ohio.gov/")


def test_block_to_notice_stashes_extra_in_raw_text():
    """case_number, opening_bid, etc. land in raw_text as JSON for downstream."""
    p = _parse_auction_block(BUTLER_BLOCK)
    n = _block_to_notice(
        "Butler", p,
        scraped_date_us="06/18/2026",
        source_url="https://butler.sheriffsaleauction.ohio.gov/",
    )
    extra = json.loads(n.raw_text)
    assert extra["case_number"] == "CV23122477 (5756)"
    assert extra["opening_bid"] == "$220,000.00"
    assert extra["deposit"] == "$10,000.00"
    assert extra["appraised_value"] == "$330,000.00"
    assert extra["case_status"] == "ACTIVE"


def test_auction_date_iso_conversion():
    assert _auction_date_iso("06/26/2026") == "2026-06-26"
    assert _auction_date_iso("01/01/2026") == "2026-01-01"
    # Bad input returns empty rather than crashing.
    assert _auction_date_iso("not a date") == ""
    assert _auction_date_iso("") == ""


# ── Sync override path (used by tests + offline fixture pipelines) ─────


def test_crawl_from_override_blocks_emits_records():
    notices = _crawl_from_override_blocks(
        "Montgomery", [MONTGOMERY_BLOCK, MIAMI_BLOCK]
    )
    # Both blocks have valid case # + parcel, so both survive.
    assert len(notices) == 2
    assert notices[0].notice_type == "sheriff_sale"
    assert notices[0].county == "Montgomery"
    # Auction date pulled from each block's own "Auction Starts" line
    # when the caller didn't pass scraped_date_us.
    assert notices[0].auction_date == "2026-06-26"
    assert notices[1].auction_date == "2026-07-01"


def test_crawl_from_override_blocks_drops_noise():
    """A block with no case # AND no parcel is junk — drop it."""
    notices = _crawl_from_override_blocks(
        "Greene", [GREENE_BLOCK, "noise block with nothing useful"]
    )
    assert len(notices) == 1
    assert notices[0].county == "Greene"


def test_crawl_from_override_blocks_respects_explicit_scraped_date():
    """If the caller passes scraped_date_us, use it over the block's own date."""
    notices = _crawl_from_override_blocks(
        "Miami", [MIAMI_BLOCK],
        scraped_date_us="07/15/2026",
    )
    assert notices[0].auction_date == "2026-07-15"


# ── Per-county adapter contracts ───────────────────────────────────────


@pytest.mark.parametrize("fetcher,county,block", [
    (fetch_butler_sheriff_sale,     "Butler",     BUTLER_BLOCK),
    (fetch_clark_sheriff_sale,      "Clark",      CLARK_BLOCK),
    (fetch_clermont_sheriff_sale,   "Clermont",   CLERMONT_BLOCK),
    (fetch_greene_sheriff_sale,     "Greene",     GREENE_BLOCK),
    (fetch_miami_sheriff_sale,      "Miami",      MIAMI_BLOCK),
    (fetch_montgomery_sheriff_sale, "Montgomery", MONTGOMERY_BLOCK),
    (fetch_warren_sheriff_sale,     "Warren",     WARREN_BLOCK),
])
def test_each_county_fetcher_via_override(fetcher, county, block):
    """Override-text path returns NoticeData with the right county label."""
    recs = fetcher(override_blocks=[block])
    assert len(recs) == 1
    assert recs[0].county == county
    assert recs[0].notice_type == "sheriff_sale"
    assert recs[0].state == "OH"


def test_fetcher_without_ctx_or_override_raises():
    """Catches the common misuse — passing nothing to a live adapter."""
    with pytest.raises(ValueError, match="requires either"):
        fetch_butler_sheriff_sale()


# ── Endpoint registry ──────────────────────────────────────────────────


def test_all_7_counties_in_endpoint_registry():
    expected = {"Butler", "Clark", "Clermont", "Greene",
                "Miami", "Montgomery", "Warren"}
    assert set(OHIO_SHERIFF_ENDPOINTS) == expected


@pytest.mark.parametrize("county", [
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Montgomery", "Warren",
])
def test_endpoint_has_required_metadata(county):
    cfg = OHIO_SHERIFF_ENDPOINTS[county]
    assert cfg["subdomain"] == county.lower()
    assert cfg["sale_day"] in (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
    )
    assert cfg["portal"].startswith(f"https://{county.lower()}.sheriffsaleauction.ohio.gov")


def test_default_horizon_is_90_days():
    """User-stated horizon (2026-06-18 conversation): 90-day forward window."""
    assert DEFAULT_HORIZON_DAYS == 90


# ── Next-sale-day seed helper ─────────────────────────────────────────


def test_next_sale_day_same_day_returns_today():
    """Friday 06/19/2026 + 'Friday' → same day (no advance)."""
    today = datetime(2026, 6, 19)  # Friday
    assert _next_sale_day(today, "Friday").date() == today.date()


def test_next_sale_day_advances_to_target_weekday():
    """Thursday 06/18/2026 + 'Friday' → 06/19/2026."""
    today = datetime(2026, 6, 18)  # Thursday
    nxt = _next_sale_day(today, "Friday")
    assert nxt.date() == datetime(2026, 6, 19).date()


def test_next_sale_day_wraps_through_week():
    """Saturday + 'Tuesday' → next Tuesday (3 days later)."""
    today = datetime(2026, 6, 20)  # Saturday
    nxt = _next_sale_day(today, "Tuesday")
    assert nxt.date() == datetime(2026, 6, 23).date()


def test_next_sale_day_handles_all_county_configs():
    """Every county's sale_day name maps to a real weekday."""
    today = datetime(2026, 6, 18)
    for county, cfg in OHIO_SHERIFF_ENDPOINTS.items():
        result = _next_sale_day(today, cfg["sale_day"])
        assert result.weekday() < 5, f"{county} sale_day produced weekend"


# ── Dispatcher ─────────────────────────────────────────────────────────


def test_dispatch_table_covers_all_7_counties():
    expected = {"butler", "clark", "clermont", "greene",
                "miami", "montgomery", "warren"}
    assert set(_DISPATCH) == expected


def test_dispatcher_routes_case_insensitively(monkeypatch):
    """fetch_ohio_sheriff_sale('Butler') and 'BUTLER' both hit fetch_butler."""
    calls = []
    def stub(ctx=None, **kw):
        calls.append(("called", kw))
        return []
    monkeypatch.setitem(_DISPATCH, "butler", stub)
    # Pass a sentinel ctx so the dispatcher routes straight to the
    # stub instead of going through the self-managed-browser path
    # (which would return an un-awaited coroutine in the test).
    fetch_ohio_sheriff_sale("Butler", ctx="STUB")
    fetch_ohio_sheriff_sale("BUTLER", ctx="STUB")
    fetch_ohio_sheriff_sale("  butler  ", ctx="STUB")
    assert len(calls) == 3


def test_dispatcher_unknown_county_raises():
    with pytest.raises(ValueError, match="Unknown Ohio sheriff-sale county"):
        fetch_ohio_sheriff_sale("Hamilton")  # Hamilton isn't in scope


def test_dispatcher_passes_horizon_through(monkeypatch):
    """horizon_days/start_date/today reach the underlying adapter."""
    captured = {}
    def stub(ctx=None, **kw):
        captured.update(kw)
        return []
    monkeypatch.setitem(_DISPATCH, "miami", stub)
    fetch_ohio_sheriff_sale("Miami", horizon_days=30,
                             start_date="07/01/2026", ctx="STUB")
    assert captured["horizon_days"] == 30
    assert captured["start_date"] == "07/01/2026"


def test_dispatcher_without_ctx_returns_coroutine():
    """The new self-managed-browser path: no ctx → returns awaitable.

    Locks the orchestrator integration contract — the orchestrator
    calls fetch_ohio_sheriff_sale(county) with no ctx, expects to
    receive a coroutine, and awaits it. Previously the dispatcher
    raised ValueError immediately, which the orchestrator swallowed
    as a silent failure (sheriff_sale = 0 records every run).
    """
    import inspect as _inspect
    result = fetch_ohio_sheriff_sale("Montgomery")
    assert _inspect.isawaitable(result), \
        "fetch_ohio_sheriff_sale without ctx must return a coroutine"
    # Clean up — close the coroutine without awaiting to avoid the
    # 'never awaited' warning
    result.close()
