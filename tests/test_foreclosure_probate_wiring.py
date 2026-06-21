"""End-to-end wiring tests for the new foreclosure + probate notice_types.

Mirrors :mod:`tests.test_sheriff_sale_wiring`. Verifies the wiring
between layers without exercising live scrapers:

1. ``config.SAVED_SEARCHES`` has 7 foreclosure + 7 probate entries
2. ``scraper.scrape_all`` imports the new sentinel constants
3. ``datasift_formatter._build_tags`` already emits the right tags
   (per-county lowercase + notice_type + Courthouse Data + has_auction
   for foreclosure)
4. ``NOTICE_TYPE_TO_LIST`` routes foreclosure and probate correctly
   (these were already in the map; this test pins they didn't drift)
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


# ── config.SAVED_SEARCHES — 7 foreclosure + 7 probate entries ────────


def test_saved_searches_has_7_foreclosure_entries():
    fc = [s for s in config.SAVED_SEARCHES if s.notice_type == "foreclosure"
          and s.saved_search_name.startswith("ohio_foreclosure:")]
    assert len(fc) == 7


def test_saved_searches_has_7_probate_entries():
    pb = [s for s in config.SAVED_SEARCHES if s.notice_type == "probate"
          and s.saved_search_name.startswith("ohio_probate:")]
    assert len(pb) == 7


@pytest.mark.parametrize("county", [
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Montgomery", "Warren",
])
def test_saved_searches_covers_all_7_oh_counties_foreclosure(county):
    matches = [s for s in config.SAVED_SEARCHES
               if s.notice_type == "foreclosure"
               and s.county == county
               and s.saved_search_name.startswith("ohio_foreclosure:")]
    assert len(matches) == 1, f"Missing OH foreclosure for {county}"
    assert matches[0].saved_search_name == f"ohio_foreclosure:{county.lower()}"


@pytest.mark.parametrize("county", [
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Montgomery", "Warren",
])
def test_saved_searches_covers_all_7_oh_counties_probate(county):
    matches = [s for s in config.SAVED_SEARCHES
               if s.notice_type == "probate"
               and s.county == county
               and s.saved_search_name.startswith("ohio_probate:")]
    assert len(matches) == 1, f"Missing OH probate for {county}"
    assert matches[0].saved_search_name == f"ohio_probate:{county.lower()}"


def test_ohio_foreclosure_sentinel_prefix_exported():
    assert hasattr(config, "OHIO_FORECLOSURE_SENTINEL_PREFIX")
    assert config.OHIO_FORECLOSURE_SENTINEL_PREFIX == "ohio_foreclosure:"


def test_ohio_probate_sentinel_prefix_exported():
    assert hasattr(config, "OHIO_PROBATE_SENTINEL_PREFIX")
    assert config.OHIO_PROBATE_SENTINEL_PREFIX == "ohio_probate:"


def test_foreclosure_county_list_matches_saved_searches():
    from_const = set(config.OHIO_FORECLOSURE_COUNTIES)
    from_searches = {s.county for s in config.SAVED_SEARCHES
                     if s.saved_search_name.startswith("ohio_foreclosure:")}
    assert from_const == from_searches


def test_probate_county_list_matches_saved_searches():
    from_const = set(config.OHIO_PROBATE_COUNTIES)
    from_searches = {s.county for s in config.SAVED_SEARCHES
                     if s.saved_search_name.startswith("ohio_probate:")}
    assert from_const == from_searches


def test_all_four_oh_sentinels_distinct():
    """Each sentinel routes to a different adapter — they MUST be
    different prefixes so the dispatcher's startswith() check is
    unambiguous."""
    sentinels = {
        config.OHIO_AUDITOR_SENTINEL_PREFIX,
        config.OHIO_SHERIFF_SENTINEL_PREFIX,
        config.OHIO_FORECLOSURE_SENTINEL_PREFIX,
        config.OHIO_PROBATE_SENTINEL_PREFIX,
    }
    assert len(sentinels) == 4   # all distinct


# ── scraper imports the new sentinels ────────────────────────────────


def test_scraper_imports_new_sentinels():
    """scraper.py must import both new sentinels — caught at module load.
    If the import fails this test errors out at collection time."""
    import scraper as _scraper  # noqa: F401
    # Reaching this line proves the import chain resolved.
    assert True


# ── Tag pipeline — foreclosure side ──────────────────────────────────


def _foreclosure_notice(**overrides) -> NoticeData:
    base = dict(
        notice_type="foreclosure",
        county="Montgomery",
        state="OH",
        address="123 OAK ST",
        city="DAYTON",
        zip="45403",
        owner_name="SMITH JOHN",
        auction_date=(datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"),
        source_url="https://pro.mcohio.org/...",
    )
    base.update(overrides)
    return NoticeData(**base)


def test_foreclosure_tags_include_universal_courthouse_data():
    tags = [t.strip() for t in df._build_tags(_foreclosure_notice()).split(",")]
    assert "Courthouse Data" in tags


def test_foreclosure_tags_include_notice_type_and_county():
    tags = [t.strip() for t in df._build_tags(_foreclosure_notice()).split(",")]
    assert "foreclosure" in tags
    assert "montgomery" in tags


def test_foreclosure_tags_include_has_auction_for_future_date():
    tags = [t.strip() for t in df._build_tags(_foreclosure_notice()).split(",")]
    assert "has_auction" in tags


def test_foreclosure_tags_per_county_label_propagates_for_all_7():
    """Every county must produce its own lowercase tag — confirms the
    per-county-filtering contract."""
    for c in ("Butler", "Clark", "Clermont", "Greene", "Miami",
              "Montgomery", "Warren"):
        n = _foreclosure_notice(county=c)
        tags = [t.strip() for t in df._build_tags(n).split(",")]
        assert c.lower() in tags, f"Missing per-county tag for {c}"


# ── Tag pipeline — probate side ──────────────────────────────────────


def _probate_notice(**overrides) -> NoticeData:
    base = dict(
        notice_type="probate",
        county="Greene",
        state="OH",
        address="888 OAK AVE",
        city="DAYTON",
        zip="45405",
        owner_name="MARY ROBERTS",    # the PR/executor
        decedent_name="HENRY M. ROBERTS",
        owner_deceased="yes",
        date_added="2025-04-01",
        source_url="https://courts.greenecountyohio.gov/...",
    )
    base.update(overrides)
    return NoticeData(**base)


def test_probate_tags_include_probate_notice_type():
    tags = [t.strip() for t in df._build_tags(_probate_notice()).split(",")]
    assert "probate" in tags
    assert "greene" in tags
    assert "Courthouse Data" in tags


def test_probate_tags_flip_to_deceased_via_owner_deceased():
    """Probate records all have owner_deceased=yes by definition —
    so they all carry the 'deceased' tag, not 'living'."""
    tags = [t.strip() for t in df._build_tags(_probate_notice()).split(",")]
    assert "deceased" in tags
    assert "living" not in tags


# ── Lists routing — foreclosure + probate (existing entries) ─────────


def test_notice_type_to_list_routes_foreclosure_to_foreclosure_list():
    """Already in the map — pin it so future edits don't break the
    foreclosure pipeline."""
    assert df.NOTICE_TYPE_TO_LIST["foreclosure"] == "Foreclosure"


def test_notice_type_to_list_routes_probate_to_probate_list():
    assert df.NOTICE_TYPE_TO_LIST["probate"] == "Probate"


# ── Build a row + confirm Lists + Tags + Foreclosure-Date columns ────


def test_foreclosure_row_has_lists_tags_and_foreclosure_date_columns():
    n = _foreclosure_notice()
    row = df._build_row(n)
    assert row["Lists"] == "Foreclosure"
    assert "foreclosure" in row["Tags"]
    assert "montgomery" in row["Tags"]
    assert "Courthouse Data" in row["Tags"]
    # foreclosure has auction_date → Foreclosure Date column populated
    assert row["Foreclosure Date"] != ""


def test_probate_row_has_lists_tags_and_probate_open_date_columns():
    n = _probate_notice()
    row = df._build_row(n)
    assert row["Lists"] == "Probate"
    assert "probate" in row["Tags"]
    assert "greene" in row["Tags"]
    # probate's date_added → Probate Open Date column
    assert row["Probate Open Date"] != ""
