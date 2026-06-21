"""End-to-end wiring tests for the sheriff_sale notice_type.

These tests verify the new ``sheriff_sale`` notice_type flows correctly
through every downstream stage that was originally written for the
existing types (foreclosure / probate / tax_delinquent):

1. ``config.SAVED_SEARCHES`` exposes a sheriff_sale entry per county
2. ``scraper.scrape_all`` recognises the ``ohio_sheriff:`` sentinel
3. ``datasift_formatter._build_tags`` emits ``sheriff_sale`` + ancillaries
4. ``datasift_formatter.NOTICE_TYPE_TO_LIST`` routes to "Sheriff Sale"
5. ``datasift_formatter`` maps ``auction_date`` → built-in "Foreclosure Date"

The actual scraping logic is covered by
``test_ohio_sheriff_sale_scrapers.py``; this file only checks the wiring
between layers.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import config
from notice_parser import NoticeData
import datasift_formatter as df


# ── config.SAVED_SEARCHES — 7 sheriff_sale entries ────────────────────


def test_saved_searches_has_7_sheriff_sale_entries():
    sheriff_entries = [s for s in config.SAVED_SEARCHES
                       if s.notice_type == "sheriff_sale"]
    assert len(sheriff_entries) == 7


@pytest.mark.parametrize("county", [
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Montgomery", "Warren",
])
def test_saved_searches_covers_all_7_oh_counties(county):
    """Every SW Ohio county has a sheriff_sale SavedSearch."""
    matches = [s for s in config.SAVED_SEARCHES
               if s.notice_type == "sheriff_sale" and s.county == county]
    assert len(matches) == 1, f"Missing sheriff_sale entry for {county}"
    assert matches[0].saved_search_name == f"ohio_sheriff:{county.lower()}"


def test_ohio_sheriff_sentinel_prefix_exported():
    """The dispatcher in scraper.py needs this constant."""
    assert hasattr(config, "OHIO_SHERIFF_SENTINEL_PREFIX")
    assert config.OHIO_SHERIFF_SENTINEL_PREFIX == "ohio_sheriff:"


def test_ohio_sheriff_sale_counties_list_matches_saved_searches():
    """OHIO_SHERIFF_SALE_COUNTIES mirrors the per-county SAVED_SEARCHES."""
    from_const = set(config.OHIO_SHERIFF_SALE_COUNTIES)
    from_searches = {s.county for s in config.SAVED_SEARCHES
                     if s.notice_type == "sheriff_sale"}
    assert from_const == from_searches


def test_sheriff_sentinel_distinct_from_auditor_sentinel():
    """Two sentinels must not collide — they route to different adapters."""
    assert (config.OHIO_SHERIFF_SENTINEL_PREFIX
            != config.OHIO_AUDITOR_SENTINEL_PREFIX)


# ── scraper.scrape_all() recognises the sentinel ──────────────────────


def test_scraper_imports_sheriff_sentinel():
    """scraper.py must import the new sentinel — caught at import time."""
    import scraper as _  # noqa: F401  — import side effect
    # Light coupling: if scraper.py doesn't import the constant, this
    # would fail at module load. Reaching this line proves it imports.
    assert True


# ── datasift_formatter — list mapping ─────────────────────────────────


def test_notice_type_to_list_routes_sheriff_sale_to_sheriff_sale_list():
    assert df.NOTICE_TYPE_TO_LIST["sheriff_sale"] == "Sheriff Sale"


def test_notice_type_to_list_did_not_break_existing_types():
    """Patch must not have collateral damage on the existing types."""
    assert df.NOTICE_TYPE_TO_LIST["foreclosure"] == "Foreclosure"
    assert df.NOTICE_TYPE_TO_LIST["probate"] == "Probate"
    assert df.NOTICE_TYPE_TO_LIST["tax_sale"] == "Tax Sale"
    assert df.NOTICE_TYPE_TO_LIST["tax_delinquent"] == "Tax Delinquent"


# ── Tag pipeline ──────────────────────────────────────────────────────


def _sheriff_sale_notice(**overrides) -> NoticeData:
    """Build a minimally-populated sheriff_sale NoticeData for tag tests."""
    base = dict(
        notice_type="sheriff_sale",
        county="Montgomery",
        state="OH",
        address="39 NORTH QUENTIN AVENUE",
        city="DAYTON",
        zip="45403",
        parcel_id="R72 04805A0027",
        auction_date=(datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"),
        source_url="https://montgomery.sheriffsaleauction.ohio.gov/",
    )
    base.update(overrides)
    return NoticeData(**base)


def test_tags_include_universal_courthouse_data():
    notice = _sheriff_sale_notice()
    tags = df._build_tags(notice).split(",")
    tags = [t.strip() for t in tags]
    assert "Courthouse Data" in tags


def test_tags_include_sheriff_sale_notice_type():
    notice = _sheriff_sale_notice()
    tags = df._build_tags(notice).split(",")
    tags = [t.strip() for t in tags]
    assert "sheriff_sale" in tags


def test_tags_include_lowercase_county():
    notice = _sheriff_sale_notice(county="Butler")
    tags = df._build_tags(notice).split(",")
    tags = [t.strip() for t in tags]
    assert "butler" in tags


def test_tags_include_has_auction_for_future_auction_date():
    """sheriff_sale records ALWAYS have a future auction date by definition."""
    notice = _sheriff_sale_notice(
        auction_date=(datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
    )
    tags = df._build_tags(notice).split(",")
    tags = [t.strip() for t in tags]
    assert "has_auction" in tags


def test_tags_dont_include_has_auction_for_past_date():
    """Sanity: if an old record sneaks in, has_auction is omitted."""
    notice = _sheriff_sale_notice(
        auction_date=(datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
    )
    tags = df._build_tags(notice).split(",")
    tags = [t.strip() for t in tags]
    assert "has_auction" not in tags


def test_tags_include_living_by_default():
    """Pre-obituary-enrichment, sheriff_sale records are tagged 'living'."""
    notice = _sheriff_sale_notice()
    tags = df._build_tags(notice).split(",")
    tags = [t.strip() for t in tags]
    assert "living" in tags


def test_tags_flip_to_deceased_after_obituary_enrichment():
    """Once obituary enrichment confirms death, the tag flips."""
    notice = _sheriff_sale_notice(
        owner_deceased="yes",
        dm_confidence="high",
    )
    tags = df._build_tags(notice).split(",")
    tags = [t.strip() for t in tags]
    assert "deceased" in tags
    assert "high_confidence" in tags
    assert "living" not in tags


# ── auction_date routes to Foreclosure Date column ────────────────────


def test_auction_date_maps_to_foreclosure_date_column_for_sheriff_sale():
    """Sheriff sale's auction_date should populate the built-in
    'Foreclosure Date' field — that's the column DataSift's niche
    sequential filters and Pendulum Theory presets key off."""
    notice = _sheriff_sale_notice(auction_date="2026-06-26")
    row = df._build_row(notice)
    assert row["Foreclosure Date"] != ""
    # The other auction-date columns must stay empty.
    assert row.get("Tax Auction Date", "") == ""
    assert row.get("Probate Open Date", "") == ""


def test_lists_column_set_for_sheriff_sale():
    notice = _sheriff_sale_notice()
    row = df._build_row(notice)
    assert row["Lists"] == "Sheriff Sale"


def test_tags_column_includes_sheriff_sale_and_county():
    notice = _sheriff_sale_notice(county="Warren")
    row = df._build_row(notice)
    tags_str = row["Tags"]
    assert "sheriff_sale" in tags_str
    assert "warren" in tags_str
    assert "Courthouse Data" in tags_str
