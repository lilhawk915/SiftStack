"""Tests for src/ohio_foreclosure_scrapers.py — the dispatcher contract.

Verifies:
* All 7 counties registered in OHIO_FORECLOSURE_ENDPOINTS + _DISPATCH
* Montgomery (canary) end-to-end through the OVERRIDE path
* Six stubs raise NotImplementedError loudly with informative messages
* fetch_ohio_foreclosure(county, ...) routes correctly + raises on
  unknown county
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import ohio_foreclosure_scrapers as mod
from ohio_foreclosure_scrapers import (
    OHIO_FORECLOSURE_ENDPOINTS,
    _DISPATCH,
    fetch_butler_foreclosure,
    fetch_clark_foreclosure,
    fetch_clermont_foreclosure,
    fetch_greene_foreclosure,
    fetch_miami_foreclosure,
    fetch_montgomery_foreclosure,
    fetch_ohio_foreclosure,
    fetch_warren_foreclosure,
)
from h3.output_writers.h3_format import CaseRecord, Defendant
from h3.scrapers.mcohio import (
    CaseDetailCapture,
    CaseScreenCapture,
    DocketEntry,
)
from h3.parsers.party_tab import PartyEntry


# ── Endpoint registry ────────────────────────────────────────────────


def test_all_7_counties_in_endpoint_registry():
    expected = {"Butler", "Clark", "Clermont", "Greene", "Miami",
                "Montgomery", "Warren"}
    assert set(OHIO_FORECLOSURE_ENDPOINTS) == expected


@pytest.mark.parametrize("county", [
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Montgomery", "Warren",
])
def test_endpoint_has_required_metadata(county):
    cfg = OHIO_FORECLOSURE_ENDPOINTS[county]
    assert cfg["vendor"]
    assert cfg["portal"].startswith("https://")
    assert cfg["status"] in {
        "live — canary", "live — equivant",
        "stub — Phase 4B (also fix cap=15 bug)",
    }


def test_montgomery_and_equivant_5_marked_live_warren_marked_stub():
    """After Phase 4A: Montgomery + 5 equivant counties live; Warren
    remains stubbed pending Phase 4B (BenchmarkCP + Auditor + PJR OCR)."""
    for c in ("Montgomery", "Butler", "Clark", "Clermont", "Greene", "Miami"):
        assert "live" in OHIO_FORECLOSURE_ENDPOINTS[c]["status"], c
    assert "stub" in OHIO_FORECLOSURE_ENDPOINTS["Warren"]["status"]


# ── Dispatcher coverage ──────────────────────────────────────────────


def test_dispatch_table_covers_all_7_counties():
    assert set(_DISPATCH) == {
        "butler", "clark", "clermont", "greene", "miami",
        "montgomery", "warren",
    }


def test_dispatcher_unknown_county_raises_with_supported_list():
    with pytest.raises(ValueError, match="Unknown Ohio foreclosure"):
        fetch_ohio_foreclosure("Hamilton")  # not in scope


@pytest.mark.parametrize("alias", ["Montgomery", "MONTGOMERY", "  montgomery  "])
def test_dispatcher_county_lookup_is_case_insensitive(alias, monkeypatch):
    """Dispatcher must accept any case + whitespace — record source
    sometimes uses 'Montgomery', sometimes 'montgomery'."""
    calls = []

    def stub(ctx=None, **kw):
        calls.append(("called", kw))
        return []

    monkeypatch.setitem(_DISPATCH, "montgomery", stub)
    fetch_ohio_foreclosure(alias)
    assert len(calls) == 1


# ── Montgomery canary — override path (sync, no Playwright) ──────────


def _make_capture(case_number="2025 CV 1", party_html="<x/>"):
    """Build a minimal CaseDetailCapture for the integration to chew on."""
    return CaseDetailCapture(
        case_number=case_number,
        case_id="x",
        screens=[CaseScreenCapture(screen="party", final_url="", html=party_html)],
        docket_entries=[DocketEntry(
            docketid="d1", case_id="c1",
            date_filed="03/15/2025",
            document_type="COMPLAINT FOR FORECLOSURE",
            description="",
        )],
        pdfs=[],
    )


def _patch_montgomery_parsers(monkeypatch, parties):
    """Inject canned PartyEntry list bypassing the bs4 parsers."""
    import h3.integration as integ
    monkeypatch.setattr(integ, "parse_party_tab", lambda html: list(parties))
    monkeypatch.setattr(integ, "parse_service_tab", lambda html: [])
    monkeypatch.setattr(integ, "parse_cis", lambda _b: None)
    # Mark first party as primary so the address-resolution helper picks it.
    for p in parties:
        if not hasattr(p, "is_primary"):
            p.is_primary = False
    if parties:
        parties[0].is_primary = True
    monkeypatch.setattr(
        integ, "filter_defendants",
        lambda ps, main_defendant_name="": list(parties),
    )


def test_montgomery_override_returns_noticedata_list(monkeypatch):
    """Override path: in-county Smith family → 2 NoticeData rows tagged
    foreclosure + Montgomery."""
    smith = PartyEntry(name="SMITH JOHN", street="123 OAK ST",
                       city="DAYTON", state="OH", zip="45403",
                       role="DEFENDANT")
    spouse = PartyEntry(name="SMITH JANE", street="123 OAK ST",
                        city="DAYTON", state="OH", zip="45403",
                        role="DEFENDANT")
    _patch_montgomery_parsers(monkeypatch, [smith, spouse])

    result = fetch_montgomery_foreclosure(
        override_case_details=[_make_capture()],
    )
    assert len(result) == 2
    assert all(n.notice_type == "foreclosure" for n in result)
    assert all(n.county == "Montgomery" for n in result)
    assert all(n.state == "OH" for n in result)
    assert result[0].owner_name == "SMITH JOHN"
    assert result[1].owner_name == "SMITH JANE"
    # source_url should thread through from the portal registry
    assert result[0].source_url.startswith("https://pro.mcohio.org")


def test_montgomery_override_empty_input_returns_empty_list():
    """No captures → no NoticeData rows. Must not crash."""
    result = fetch_montgomery_foreclosure(override_case_details=[])
    assert result == []


def test_montgomery_override_threads_through_dispatcher(monkeypatch):
    """fetch_ohio_foreclosure('Montgomery', override_case_details=...) →
    same result as calling fetch_montgomery_foreclosure directly."""
    smith = PartyEntry(name="SMITH JOHN", street="1 ELM",
                       city="DAYTON", state="OH", zip="45403",
                       role="DEFENDANT")
    _patch_montgomery_parsers(monkeypatch, [smith])

    awaitable = fetch_ohio_foreclosure(
        "Montgomery",
        override_case_details=[_make_capture(case_number="2025 CV 42")],
    )
    result = awaitable
    assert len(result) == 1
    assert result[0].owner_name == "SMITH JOHN"


# ── 6 stubs — Phase 4 ────────────────────────────────────────────────


def test_warren_stub_raises_not_implemented():
    """Only remaining stub (post Phase 4A) — Warren."""
    with pytest.raises(NotImplementedError, match="Warren"):
        fetch_warren_foreclosure()


def test_warren_stub_mentions_phase_4b_tracking_in_message():
    """The exception message has to point at the work item so
    production logs can be triaged."""
    with pytest.raises(NotImplementedError) as excinfo:
        fetch_warren_foreclosure()
    assert "Phase 4B" in str(excinfo.value)


def test_dispatcher_propagates_stub_exception():
    """Caller of fetch_ohio_foreclosure('Warren') sees the same
    NotImplementedError. Production code in scraper.scrape_all should
    catch this and continue with the other counties."""
    with pytest.raises(NotImplementedError, match="Warren"):
        fetch_ohio_foreclosure("Warren")


# ── Date range helper ────────────────────────────────────────────────


def test_default_date_range_returns_iso_strings():
    from datetime import datetime
    df, dt = mod._default_date_range(today=datetime(2026, 6, 19))
    assert df == "2026-06-18"
    assert dt == "2026-06-19"


def test_default_date_range_respects_lookback():
    from datetime import datetime
    df, dt = mod._default_date_range(
        today=datetime(2026, 6, 19), lookback_days=7,
    )
    assert df == "2026-06-12"
    assert dt == "2026-06-19"
