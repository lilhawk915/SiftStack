"""Tests for src/ohio_probate_scrapers.py — the dispatcher contract.

Mirrors test_ohio_foreclosure_adapters.py: registry coverage,
Greene canary via override path, 6 stubs raising NotImplementedError,
dispatcher routing + error handling.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import ohio_probate_scrapers as mod
from ohio_probate_scrapers import (
    OHIO_PROBATE_ENDPOINTS,
    _DISPATCH,
    fetch_butler_probate,
    fetch_clark_probate,
    fetch_clermont_probate,
    fetch_greene_probate,
    fetch_miami_probate,
    fetch_montgomery_probate,
    fetch_ohio_probate,
    fetch_warren_probate,
)
from h3.output_writers.probate_format import ProbateRecord


# ── Endpoint registry ────────────────────────────────────────────────


def test_all_7_counties_in_endpoint_registry():
    expected = {"Butler", "Clark", "Clermont", "Greene", "Miami",
                "Montgomery", "Warren"}
    assert set(OHIO_PROBATE_ENDPOINTS) == expected


@pytest.mark.parametrize("county", [
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Montgomery", "Warren",
])
def test_endpoint_has_required_metadata(county):
    cfg = OHIO_PROBATE_ENDPOINTS[county]
    assert cfg["vendor"]
    assert cfg["portal"].startswith("https://")
    assert "status" in cfg


def test_greene_marked_live_other_six_marked_stub():
    assert "live" in OHIO_PROBATE_ENDPOINTS["Greene"]["status"]
    for c in ("Butler", "Clark", "Clermont", "Miami", "Montgomery", "Warren"):
        assert "stub" in OHIO_PROBATE_ENDPOINTS[c]["status"]


# ── Dispatcher coverage ──────────────────────────────────────────────


def test_dispatch_table_covers_all_7_counties():
    assert set(_DISPATCH) == {
        "butler", "clark", "clermont", "greene", "miami",
        "montgomery", "warren",
    }


def test_dispatcher_unknown_county_raises_with_supported_list():
    with pytest.raises(ValueError, match="Unknown Ohio probate"):
        fetch_ohio_probate("Hamilton")


@pytest.mark.parametrize("alias", ["Greene", "GREENE", "  greene  "])
def test_dispatcher_county_lookup_is_case_insensitive(alias, monkeypatch):
    calls = []

    def stub(ctx=None, **kw):
        calls.append(("called", kw))
        return []

    monkeypatch.setitem(_DISPATCH, "greene", stub)
    fetch_ohio_probate(alias)
    assert len(calls) == 1


# ── Greene canary — override path ────────────────────────────────────


def test_greene_override_returns_noticedata_list():
    """Override path: a populated ProbateRecord → one NoticeData row
    tagged probate + Greene."""
    rec = ProbateRecord(
        case_number="2025 PR 100",
        case_type="ESTATE",
        date_filed="2025-04-01",
        decedent_name="HENRY M. ROBERTS",
        date_of_death="2024-12-15",
        fiduciary_name="MARY ROBERTS",
        fiduciary_address="55 ELM ST, KETTERING, OH 45429",
        relationship="DAUGHTER",
        subject_property="888 OAK AVE, DAYTON, OH 45405",
    )
    result = fetch_greene_probate(override_probate_records=[rec])
    assert len(result) == 1
    n = result[0]
    assert n.notice_type == "probate"
    assert n.county == "Greene"
    assert n.state == "OH"
    assert n.decedent_name == "HENRY M. ROBERTS"
    assert n.owner_name == "MARY ROBERTS"
    assert n.address == "888 OAK AVE"
    assert n.owner_deceased == "yes"
    # Source URL threaded from registry
    assert n.source_url.startswith("https://probate.co.greene.oh.us")


def test_greene_override_empty_input_returns_empty_list():
    assert fetch_greene_probate(override_probate_records=[]) == []


def test_greene_override_threads_through_dispatcher():
    """fetch_ohio_probate('Greene', override=...) routes correctly."""
    rec = ProbateRecord(
        decedent_name="X",
        fiduciary_name="Y",
        fiduciary_address="1 A ST, DAYTON, OH 45403",
    )
    result = fetch_ohio_probate("Greene", override_probate_records=[rec])
    assert len(result) == 1
    assert result[0].decedent_name == "X"


def test_greene_override_populates_obituary_preset_dm_fields():
    """Bridge should pre-populate decision_maker_* so the obituary
    enricher's probate-preset path activates without searching."""
    rec = ProbateRecord(
        decedent_name="X",
        fiduciary_name="JANE EXECUTOR",
        fiduciary_address="55 ELM ST, KETTERING, OH 45429",
        relationship="SPOUSE",
    )
    result = fetch_greene_probate(override_probate_records=[rec])
    n = result[0]
    assert n.decision_maker_name == "JANE EXECUTOR"
    assert n.decision_maker_relationship == "SPOUSE"
    assert n.decision_maker_status == "verified_living"
    assert n.decision_maker_source == "probate_notice"


# ── 6 stubs — Phase 4 ────────────────────────────────────────────────


@pytest.mark.parametrize("stub_fn,county", [
    (fetch_butler_probate,     "Butler"),
    (fetch_clark_probate,      "Clark"),
    (fetch_clermont_probate,   "Clermont"),
    (fetch_miami_probate,      "Miami"),
    (fetch_montgomery_probate, "Montgomery"),
    (fetch_warren_probate,     "Warren"),
])
def test_stubs_raise_not_implemented(stub_fn, county):
    with pytest.raises(NotImplementedError, match=county):
        stub_fn()


def test_stubs_mention_phase_4_tracking_in_message():
    for stub in (fetch_butler_probate, fetch_clark_probate,
                 fetch_clermont_probate, fetch_miami_probate,
                 fetch_montgomery_probate, fetch_warren_probate):
        with pytest.raises(NotImplementedError) as excinfo:
            stub()
        assert "Phase 4" in str(excinfo.value)


def test_dispatcher_propagates_stub_exception():
    with pytest.raises(NotImplementedError, match="Butler"):
        fetch_ohio_probate("Butler")


# ── Date range helper ────────────────────────────────────────────────


def test_default_date_range_returns_iso_strings_with_weekly_lookback():
    from datetime import datetime
    df, dt = mod._default_date_range(today=datetime(2026, 6, 19))
    # Default weekly lookback = 7 days
    assert df == "2026-06-12"
    assert dt == "2026-06-19"
