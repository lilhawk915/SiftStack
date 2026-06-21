"""Tests for ohio_destination_lists — county → DataSift list routing.

Pins the business rule: Montgomery → daily list; other 6 →
weekly list; no cross-contamination ever; unknown county loud-fails.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from notice_parser import NoticeData
from ohio_destination_lists import (
    LIST_MONTGOMERY_DAILY,
    LIST_SW_OHIO_WEEKLY,
    WEEKLY_COUNTIES,
    destination_list_for_county,
    split_by_destination_list,
)


# ── List-name constants ─────────────────────────────────────────────


def test_list_constants_are_exact_strings():
    """List names are case-sensitive in DataSift — these strings MUST
    match what's configured there. Pin them so a typo is caught."""
    assert LIST_MONTGOMERY_DAILY == "H3 Montgomery Courthouse Data"
    assert LIST_SW_OHIO_WEEKLY   == "H3 SW Ohio Courthouse Data"


def test_lists_are_distinct():
    """Cross-contamination guard — different lists ALWAYS."""
    assert LIST_MONTGOMERY_DAILY != LIST_SW_OHIO_WEEKLY


def test_weekly_counties_are_exactly_the_6_non_montgomery():
    """The non-Montgomery 6 — verify the set is exactly these."""
    assert WEEKLY_COUNTIES == {
        "Butler", "Clark", "Clermont", "Greene", "Miami", "Warren",
    }


# ── destination_list_for_county ─────────────────────────────────────


def test_montgomery_routes_to_daily_list():
    assert destination_list_for_county("Montgomery") == LIST_MONTGOMERY_DAILY


@pytest.mark.parametrize("variant", [
    "Montgomery", "montgomery", "MONTGOMERY", "  Montgomery  ",
    "  montgomery", "MONTGOMERY  ",
])
def test_montgomery_lookup_is_case_and_whitespace_insensitive(variant):
    """Cron args, config drift, and operator typing all produce these
    variants — every one must route to the same list."""
    assert destination_list_for_county(variant) == LIST_MONTGOMERY_DAILY


@pytest.mark.parametrize("county", [
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Warren",
])
def test_each_of_the_6_routes_to_weekly_list(county):
    assert destination_list_for_county(county) == LIST_SW_OHIO_WEEKLY


@pytest.mark.parametrize("variant", [
    "BUTLER", "butler", "  butler  ", "Butler", "ButLer",
])
def test_weekly_county_lookup_is_case_and_whitespace_insensitive(variant):
    assert destination_list_for_county(variant) == LIST_SW_OHIO_WEEKLY


def test_unknown_county_raises_value_error():
    """Hamilton isn't in scope — fail loudly so the operator catches
    the typo rather than silently dropping records."""
    with pytest.raises(ValueError, match="unknown OH county"):
        destination_list_for_county("Hamilton")


def test_empty_county_raises_value_error():
    """A record with no county can't be routed. The pipeline should
    never produce such records; surface it as an error."""
    with pytest.raises(ValueError, match="empty county"):
        destination_list_for_county("")


def test_unknown_county_error_message_lists_expected_options():
    """The error message must enumerate the 7 supported counties so
    the operator can self-correct without reading docs."""
    with pytest.raises(ValueError) as excinfo:
        destination_list_for_county("Cuyahoga")
    msg = str(excinfo.value)
    for c in ("Butler", "Clark", "Clermont", "Greene", "Miami",
              "Montgomery", "Warren"):
        assert c in msg


# ── split_by_destination_list ───────────────────────────────────────


def _n(county: str, notice_type: str = "foreclosure") -> NoticeData:
    return NoticeData(notice_type=notice_type, county=county, state="OH")


def test_split_buckets_montgomery_alone():
    notices = [_n("Montgomery") for _ in range(3)]
    out = split_by_destination_list(notices)
    assert set(out) == {LIST_MONTGOMERY_DAILY}
    assert len(out[LIST_MONTGOMERY_DAILY]) == 3


def test_split_buckets_weekly_only_when_no_montgomery():
    notices = [_n("Butler"), _n("Clark"), _n("Warren")]
    out = split_by_destination_list(notices)
    assert set(out) == {LIST_SW_OHIO_WEEKLY}
    assert len(out[LIST_SW_OHIO_WEEKLY]) == 3


def test_split_returns_two_buckets_for_mixed_input():
    """Confirms no cross-contamination — Montgomery records NEVER end
    up in the SW Ohio bucket and vice versa."""
    notices = [
        _n("Montgomery", "foreclosure"),
        _n("Butler",     "foreclosure"),
        _n("Montgomery", "probate"),
        _n("Greene",     "tax_delinquent"),
        _n("Warren",     "sheriff_sale"),
        _n("Montgomery", "sheriff_sale"),
    ]
    out = split_by_destination_list(notices)
    assert set(out) == {LIST_MONTGOMERY_DAILY, LIST_SW_OHIO_WEEKLY}
    assert len(out[LIST_MONTGOMERY_DAILY]) == 3
    assert len(out[LIST_SW_OHIO_WEEKLY]) == 3
    # All Montgomery records must end up in the Montgomery bucket
    assert all(n.county == "Montgomery"
               for n in out[LIST_MONTGOMERY_DAILY])
    # And vice versa
    assert all(n.county in WEEKLY_COUNTIES
               for n in out[LIST_SW_OHIO_WEEKLY])


def test_split_preserves_record_order_within_each_bucket():
    """Stable bucketing — preserves input order, so an operator
    inspecting an upload CSV can trace it back to scrape order."""
    notices = [
        _n("Butler"), _n("Clark"), _n("Greene"), _n("Miami"),
    ]
    out = split_by_destination_list(notices)
    counties_in_order = [n.county for n in out[LIST_SW_OHIO_WEEKLY]]
    assert counties_in_order == ["Butler", "Clark", "Greene", "Miami"]


def test_split_empty_input_returns_empty_dict():
    assert split_by_destination_list([]) == {}


def test_split_propagates_unknown_county_error():
    """One bad record in the batch raises — caller sees the error
    rather than silently dropping it into the wrong bucket."""
    notices = [_n("Montgomery"), _n("Cuyahoga"), _n("Butler")]
    with pytest.raises(ValueError, match="unknown OH county"):
        split_by_destination_list(notices)
