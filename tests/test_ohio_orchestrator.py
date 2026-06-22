"""Tests for ohio_orchestrator — daily + weekly cron entry points.

Drives the orchestrator with mocked dispatchers + monkeypatched
uploader so we can verify the routing + bucketing logic without
touching Playwright or DataSift.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from notice_parser import NoticeData
import ohio_orchestrator as orch
from ohio_destination_lists import (
    LIST_MONTGOMERY_DAILY,
    LIST_SW_OHIO_WEEKLY,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _n(county: str, notice_type: str = "foreclosure") -> NoticeData:
    return NoticeData(notice_type=notice_type, county=county, state="OH")


def _patch_dispatchers(monkeypatch, *, per_county_per_type_records: int = 2):
    """Make each fetch_ohio_* dispatcher return a small canned list
    instead of running a live scraper."""
    def fake(county: str, **kwargs):
        # Return ``per_county_per_type_records`` records per call —
        # source_type is implicit from which dispatcher was patched.
        # We don't know the source_type here, so we tag them all with
        # the dispatcher's name via the closure.
        async def _wrap():
            return [
                _n(county, dispatcher_label)
                for _ in range(per_county_per_type_records)
            ]
        return _wrap()

    # Each dispatcher gets a slightly-different label so we can tell
    # them apart in the assertions
    for label, mod_path, attr in [
        ("foreclosure",    "ohio_foreclosure_scrapers",   "fetch_ohio_foreclosure"),
        ("probate",        "ohio_probate_scrapers",       "fetch_ohio_probate"),
        ("tax_delinquent", "ohio_tax_delinquent_scrapers","fetch_ohio_tax_delinquent"),
        ("sheriff_sale",   "ohio_sheriff_sale_scrapers",  "fetch_ohio_sheriff_sale"),
    ]:
        dispatcher_label = label  # closure binding
        def make_fake(_label=label):
            def fake_dispatcher(county, **kwargs):
                async def _wrap():
                    return [
                        _n(county, _label)
                        for _ in range(per_county_per_type_records)
                    ]
                return _wrap()
            return fake_dispatcher
        mod = __import__(mod_path)
        monkeypatch.setattr(mod, attr, make_fake())


# ── Source-type configuration ───────────────────────────────────────


def test_source_types_splits_by_cadence():
    """Daily/weekly cover the 3 fresh-court-activity source types;
    tax_delinquent moved to QUARTERLY_SOURCE_TYPES (a 3-month
    refresh catches new delinquencies + amount updates without
    daily wasted scrape time). Order in daily/weekly is fixed:
    foreclosure first so DataSift merge-by-address shows the
    freshest court action at the top."""
    assert orch.SOURCE_TYPES == (
        "foreclosure", "probate", "sheriff_sale",
    )
    assert orch.QUARTERLY_SOURCE_TYPES == ("tax_delinquent",)
    # No overlap — a source type belongs to exactly one cadence
    assert not set(orch.SOURCE_TYPES) & set(orch.QUARTERLY_SOURCE_TYPES)


def test_quarterly_counties_covers_all_7():
    """Quarterly tax_delinquent runs across every county (Mont + 6
    weekly). Each still routes to its own DataSift list per the
    destination_list_for_county rule."""
    assert orch.QUARTERLY_COUNTIES == (
        "Montgomery", "Butler", "Clark", "Clermont", "Greene",
        "Miami", "Warren",
    )


def test_daily_counties_is_only_montgomery():
    assert orch.DAILY_COUNTIES == ("Montgomery",)


def test_weekly_counties_is_exactly_the_6_non_montgomery():
    assert orch.WEEKLY_COUNTIES_ORDERED == (
        "Butler", "Clark", "Clermont", "Greene", "Miami", "Warren",
    )


# ── Dispatcher resolution ───────────────────────────────────────────


@pytest.mark.parametrize("source_type,expected_attr", [
    ("foreclosure",    "fetch_ohio_foreclosure"),
    ("probate",        "fetch_ohio_probate"),
    ("tax_delinquent", "fetch_ohio_tax_delinquent"),
    ("sheriff_sale",   "fetch_ohio_sheriff_sale"),
])
def test_dispatcher_for_resolves_each_source_type(source_type, expected_attr):
    fn = orch._dispatcher_for(source_type)
    assert fn.__name__ == expected_attr


def test_dispatcher_for_unknown_source_type_raises():
    with pytest.raises(ValueError, match="Unknown source_type"):
        orch._dispatcher_for("eviction")


# ── Dry-run plan generation ─────────────────────────────────────────


def test_daily_dry_run_plans_montgomery_only():
    """Dry-run prints destination plan + exits — no scraping, no uploads."""
    result = asyncio.run(orch.run_daily(dry_run=True))
    assert result["dry_run"] is True
    assert set(result["plan"]) == {LIST_MONTGOMERY_DAILY}
    assert result["plan"][LIST_MONTGOMERY_DAILY] == ["Montgomery"]


def test_weekly_dry_run_plans_sw_ohio_only():
    result = asyncio.run(orch.run_weekly(dry_run=True))
    assert result["dry_run"] is True
    assert set(result["plan"]) == {LIST_SW_OHIO_WEEKLY}
    assert sorted(result["plan"][LIST_SW_OHIO_WEEKLY]) == sorted([
        "Butler", "Clark", "Clermont", "Greene", "Miami", "Warren",
    ])


def test_dry_run_never_lets_montgomery_into_the_weekly_bucket():
    """Cross-contamination guard at the orchestrator boundary."""
    result = asyncio.run(orch.run_daily(dry_run=True))
    # No SW Ohio list at all in a daily run
    assert LIST_SW_OHIO_WEEKLY not in result["plan"]

    result = asyncio.run(orch.run_weekly(dry_run=True))
    # No Montgomery list in a weekly run
    assert LIST_MONTGOMERY_DAILY not in result["plan"]


# ── Scrape-only path (no upload) ────────────────────────────────────


def test_run_daily_no_upload_writes_csv_then_stops(monkeypatch, tmp_path):
    """With upload=False we still scrape + write CSV but skip DataSift."""
    _patch_dispatchers(monkeypatch, per_county_per_type_records=1)
    # Redirect CSV output to tmp_path
    monkeypatch.chdir(tmp_path)
    # The DataSift uploader must NEVER be called in no-upload mode
    def explode(*a, **kw):
        raise AssertionError(
            "upload_to_datasift was called in --no-upload mode")
    import datasift_uploader as du
    monkeypatch.setattr(du, "upload_to_datasift", explode, raising=False)

    result = asyncio.run(orch.run_daily(upload=False, headless=True))
    assert result["records"] == 3   # 1 county × 3 source types × 1 rec each
    assert LIST_MONTGOMERY_DAILY in result["upload_summary"]
    bucket = result["upload_summary"][LIST_MONTGOMERY_DAILY]
    assert bucket["uploaded"] is False
    assert bucket["records"] == 3
    csv_path = Path(bucket["csv_path"])
    assert csv_path.exists()


def test_run_weekly_no_upload_buckets_all_6_into_sw_ohio(monkeypatch, tmp_path):
    _patch_dispatchers(monkeypatch, per_county_per_type_records=1)
    monkeypatch.chdir(tmp_path)
    result = asyncio.run(orch.run_weekly(upload=False, headless=True))
    # 6 counties × 3 source types × 1 = 18 records, all → SW Ohio list
    assert result["records"] == 18
    assert set(result["upload_summary"]) == {LIST_SW_OHIO_WEEKLY}
    assert result["upload_summary"][LIST_SW_OHIO_WEEKLY]["records"] == 18


def test_run_daily_never_writes_sw_ohio_csv(monkeypatch, tmp_path):
    """Cross-contamination guard at the WRITE boundary, not just the
    plan boundary."""
    _patch_dispatchers(monkeypatch, per_county_per_type_records=1)
    monkeypatch.chdir(tmp_path)
    result = asyncio.run(orch.run_daily(upload=False, headless=True))
    assert LIST_SW_OHIO_WEEKLY not in result["upload_summary"]


# ── Failure handling per county×source_type ─────────────────────────


def test_one_county_failure_does_not_kill_the_run(monkeypatch, tmp_path):
    """If one adapter raises, the orchestrator logs + continues with
    the rest — daily breakage isolated to one county."""
    _patch_dispatchers(monkeypatch, per_county_per_type_records=1)
    # Make Butler foreclosure blow up
    import ohio_foreclosure_scrapers as fc
    def explode_butler(county, **kw):
        if county.lower() == "butler":
            raise RuntimeError("Butler portal returned 500")
        # Fall back to the rest of the fakes (Clark, Greene, etc.)
        async def _wrap():
            return [_n(county, "foreclosure")]
        return _wrap()
    monkeypatch.setattr(fc, "fetch_ohio_foreclosure", explode_butler)
    monkeypatch.chdir(tmp_path)

    result = asyncio.run(orch.run_weekly(upload=False, headless=True))
    # 6 counties × 3 source types × 1 = 18 expected.
    # Butler foreclosure failed → 17.
    assert result["records"] == 17


def test_stub_county_skipped_with_warning_does_not_kill_run(monkeypatch, tmp_path):
    """NotImplementedError → log + skip (mirrors scraper.scrape_all
    NotImplementedError-handling pattern)."""
    _patch_dispatchers(monkeypatch, per_county_per_type_records=1)
    # Make probate for Clermont raise NotImplementedError (probate is
    # in the weekly source types; tax_delinquent is now yearly-only)
    import ohio_probate_scrapers as pr
    def stub_clermont(county, **kw):
        if county.lower() == "clermont":
            raise NotImplementedError("Clermont probate not online")
        async def _wrap():
            return [_n(county, "probate")]
        return _wrap()
    monkeypatch.setattr(pr, "fetch_ohio_probate", stub_clermont)
    monkeypatch.chdir(tmp_path)

    result = asyncio.run(orch.run_weekly(upload=False, headless=True))
    # 6×3 = 18, minus Clermont's 1 probate = 17
    assert result["records"] == 17


# ── upload_by_destination unit ──────────────────────────────────────


def test_upload_by_destination_no_upload_writes_one_csv_per_bucket(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    notices = [
        _n("Montgomery", "foreclosure"),
        _n("Butler",     "probate"),
        _n("Greene",     "tax_delinquent"),
        _n("Montgomery", "sheriff_sale"),
    ]
    summary = asyncio.run(orch.upload_by_destination(notices, upload=False))
    assert set(summary) == {LIST_MONTGOMERY_DAILY, LIST_SW_OHIO_WEEKLY}
    assert summary[LIST_MONTGOMERY_DAILY]["records"] == 2
    assert summary[LIST_SW_OHIO_WEEKLY]["records"] == 2
    for bucket in summary.values():
        assert bucket["uploaded"] is False
        assert Path(bucket["csv_path"]).exists()


def test_upload_by_destination_skips_when_no_records():
    """Empty input → empty summary, no CSV written."""
    summary = asyncio.run(orch.upload_by_destination([], upload=False))
    assert summary == {}
