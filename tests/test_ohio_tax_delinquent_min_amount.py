"""Tests for the cross-county tax-delinquent filter.

Business rule (decided 2026-06-19, supersedes 2026-06-17 $8k-only):
keep records that owe at least ``MIN_TAX_DELINQUENT_AMOUNT`` ($3,000)
AND have been delinquent for at least ``MIN_TAX_DELINQUENT_YEARS``
(2 years). Amount-only fallback applies when the record's
``tax_delinquent_years`` field is empty — needed for counties whose
adapters don't yet emit the certified year (Butler/Greene/Montgomery/
Warren today vs Clark/Miami who do).

Applied uniformly via ``fetch_ohio_tax_delinquent()`` — individual
adapters return ALL parsed records; the dispatcher gates output.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from notice_parser import NoticeData
from ohio_tax_delinquent_scrapers import (
    MIN_TAX_DELINQUENT_AMOUNT,
    MIN_TAX_DELINQUENT_YEARS,
    _amount_meets_threshold,
    _meets_filter,
    _meets_min_amount,
    _years_delinquent_at_least,
    fetch_ohio_tax_delinquent,
)

# Pin "today" for years-rule tests so they don't drift year to year.
TODAY = datetime(2026, 6, 19)


def _rec(amount: str = "", years: str = "") -> NoticeData:
    return NoticeData(
        notice_type="tax_delinquent",
        county="TestCounty",
        tax_delinquent_amount=amount,
        tax_delinquent_years=years,
    )


# ── Amount-only predicate (preserves the original semantics) ──────────


def test_amount_above_threshold_keeps_record():
    assert _amount_meets_threshold(_rec("3000.00"), Decimal("3000")) is True
    assert _amount_meets_threshold(_rec("10000.00"), Decimal("3000")) is True
    assert _amount_meets_threshold(_rec("79839.05"), Decimal("3000")) is True


def test_amount_at_exact_threshold_keeps_record():
    """Inclusive boundary (≥, not >)."""
    assert _amount_meets_threshold(_rec("3000"), Decimal("3000")) is True
    assert _amount_meets_threshold(_rec("3000.00"), Decimal("3000")) is True


def test_amount_below_threshold_returns_false():
    assert _amount_meets_threshold(_rec("2999.99"), Decimal("3000")) is False
    assert _amount_meets_threshold(_rec("500.00"), Decimal("3000")) is False
    assert _amount_meets_threshold(_rec("0.00"), Decimal("3000")) is False


def test_amount_strips_currency_chars():
    assert _amount_meets_threshold(_rec("$10,000.00"), Decimal("3000")) is True
    assert _amount_meets_threshold(_rec("$1,500.00"), Decimal("3000")) is False


def test_amount_empty_returns_false():
    assert _amount_meets_threshold(_rec(""), Decimal("3000")) is False
    assert _amount_meets_threshold(_rec("   "), Decimal("3000")) is False


def test_amount_unparseable_returns_false():
    assert _amount_meets_threshold(_rec("nope"), Decimal("3000")) is False
    assert _amount_meets_threshold(_rec("invalid"), Decimal("3000")) is False


def test_legacy_meets_min_amount_alias_still_works():
    """``_meets_min_amount`` is kept as a back-compat alias for callers
    in orchestrate_upload.py and other modules."""
    assert _meets_min_amount(_rec("5000.00"), Decimal("3000")) is True
    assert _meets_min_amount is _amount_meets_threshold


# ── Years-delinquent predicate (certified-year semantics) ─────────────


def test_years_certified_year_2_years_ago_qualifies():
    """2024 certified, current year 2026 → 2 years delinquent → qualifies."""
    assert _years_delinquent_at_least(_rec(years="2024"), 2, TODAY) is True


def test_years_certified_year_1_year_ago_does_not_qualify():
    """2025 certified → 1 year delinquent → fails the 2-year rule."""
    assert _years_delinquent_at_least(_rec(years="2025"), 2, TODAY) is False


def test_years_old_certified_year_qualifies():
    """2016 certified → 10 years delinquent → easily qualifies."""
    assert _years_delinquent_at_least(_rec(years="2016"), 2, TODAY) is True


def test_years_current_year_does_not_qualify():
    """2026 certified → 0 years delinquent → fails."""
    assert _years_delinquent_at_least(_rec(years="2026"), 2, TODAY) is False


def test_years_pre_computed_count_handled():
    """If a future adapter emits ``"3"`` (count) instead of a 4-digit
    year, the predicate handles it as a count."""
    assert _years_delinquent_at_least(_rec(years="3"), 2, TODAY) is True
    assert _years_delinquent_at_least(_rec(years="1"), 2, TODAY) is False
    assert _years_delinquent_at_least(_rec(years="2"), 2, TODAY) is True


def test_years_empty_returns_false():
    """Empty years field can't satisfy the rule — fall through to OR."""
    assert _years_delinquent_at_least(_rec(years=""), 2, TODAY) is False
    assert _years_delinquent_at_least(_rec(years="   "), 2, TODAY) is False


def test_years_unparseable_returns_false():
    assert _years_delinquent_at_least(_rec(years="N/A"), 2, TODAY) is False
    assert _years_delinquent_at_least(_rec(years="never"), 2, TODAY) is False


def test_years_strips_non_digit_chars():
    """Adapters may emit '2024' or '2024-01' or 'TY2024' — first digits win."""
    assert _years_delinquent_at_least(_rec(years="TY2024"), 2, TODAY) is True
    assert _years_delinquent_at_least(_rec(years="2024-Q3"), 2, TODAY) is True


def test_years_zero_returns_false():
    """Bogus '0' year should not satisfy the rule."""
    assert _years_delinquent_at_least(_rec(years="0"), 2, TODAY) is False


def test_years_future_year_returns_false():
    """Cert year > current year is junk data → fail safely."""
    assert _years_delinquent_at_least(_rec(years="2030"), 2, TODAY) is False


# ── AND-with-amount-fallback semantics ────────────────────────────────


def test_meets_filter_keeps_when_both_pass():
    """Amount ≥ $3k AND years ≥ 2 — clear keep."""
    assert _meets_filter(
        _rec(amount="10000.00", years="2018"), Decimal("3000"), 2, TODAY,
    ) is True


def test_meets_filter_drops_when_amount_fails_even_if_chronic():
    """Years rule alone is NOT sufficient — sub-$3k chronic gets dropped.

    This is the explicit change from the OR rule. A $1,500 amount fails
    the AND even if certified back to 2018.
    """
    assert _meets_filter(
        _rec(amount="1500.00", years="2018"), Decimal("3000"), 2, TODAY,
    ) is False


def test_meets_filter_drops_when_years_fails_with_known_year():
    """Amount-only is NOT sufficient when years data IS present and fails.

    A $10k bill certified in 2025 fails: years data is present, so the
    full AND applies and the 1-year duration doesn't clear the bar.
    """
    assert _meets_filter(
        _rec(amount="10000.00", years="2025"), Decimal("3000"), 2, TODAY,
    ) is False


def test_meets_filter_amount_fallback_when_years_empty():
    """No years data → amount-only is the entire test.

    This is the Butler/Greene/Montgomery/Warren path. The years half of
    the AND can't be evaluated, so we keep the record on amount alone.
    """
    assert _meets_filter(
        _rec(amount="5000.00", years=""), Decimal("3000"), 2, TODAY,
    ) is True


def test_meets_filter_amount_fallback_does_not_save_sub_3k():
    """Even with the amount-fallback path, sub-$3k records still drop."""
    assert _meets_filter(
        _rec(amount="500.00", years=""), Decimal("3000"), 2, TODAY,
    ) is False


def test_meets_filter_drops_when_both_empty():
    """No signal at all → drop (no silent passes)."""
    assert _meets_filter(
        _rec(amount="", years=""), Decimal("3000"), 2, TODAY,
    ) is False


def test_meets_filter_treats_unparseable_years_as_missing():
    """Junk years → fall back to amount-only (don't drop on bad data)."""
    assert _meets_filter(
        _rec(amount="5000.00", years="N/A"), Decimal("3000"), 2, TODAY,
    ) is True


# ── Defaults reflect business rule ────────────────────────────────────


def test_default_amount_threshold_is_3000():
    assert MIN_TAX_DELINQUENT_AMOUNT == Decimal("3000.00")


def test_default_years_threshold_is_2():
    assert MIN_TAX_DELINQUENT_YEARS == 2


# ── Dispatcher applies the OR-rule ────────────────────────────────────


def test_dispatcher_amount_fallback_for_no_years_county(monkeypatch):
    """Montgomery-style stub (no years data) — amount-only filtering."""
    import ohio_tax_delinquent_scrapers as mod

    def stub(*args, **kwargs):
        return [
            _rec("100.00"),         # < $3k → drop
            _rec("2999.99"),        # < $3k by a cent → drop
            _rec("3000.00"),        # exactly at threshold → keep
            _rec("10000.00"),       # ≥ $3k → keep
            _rec("79839.05"),       # ≥ $3k → keep
        ]
    monkeypatch.setitem(mod._DISPATCH, "montgomery", stub)
    records = fetch_ohio_tax_delinquent("Montgomery", today=TODAY)
    assert len(records) == 3
    amounts = [r.tax_delinquent_amount for r in records]
    assert amounts == ["3000.00", "10000.00", "79839.05"]


def test_dispatcher_strict_and_for_county_with_years(monkeypatch):
    """Clark-style stub (years emitted) — full AND applies."""
    import ohio_tax_delinquent_scrapers as mod

    def stub(*args, **kwargs):
        return [
            _rec(amount="5000.00", years="2018"),   # ≥$3k + 8yr → KEEP
            _rec(amount="5000.00", years="2025"),   # ≥$3k + 1yr → DROP
            _rec(amount="1200.00", years="2018"),   # <$3k + 8yr → DROP
            _rec(amount="500.00",  years="2024"),   # <$3k + 2yr → DROP
            _rec(amount="10000.00", years="2023"),  # ≥$3k + 3yr → KEEP
        ]
    monkeypatch.setitem(mod._DISPATCH, "clark", stub)
    records = fetch_ohio_tax_delinquent("Clark", today=TODAY)
    assert len(records) == 2
    amounts = sorted(r.tax_delinquent_amount for r in records)
    assert amounts == ["10000.00", "5000.00"]


def test_dispatcher_overrides_for_qa_dry_run(monkeypatch):
    """Caller can disable filter or weaken either rule."""
    import ohio_tax_delinquent_scrapers as mod
    monkeypatch.setitem(mod._DISPATCH, "montgomery", lambda **kw: [
        _rec(amount="500.00"), _rec(amount="5000.00"), _rec(amount="50000.00"),
    ])
    # apply_filter=False keeps everything
    records = fetch_ohio_tax_delinquent("Montgomery", apply_filter=False)
    assert len(records) == 3
    # Weaken amount rule to $1k — keeps all 3
    records = fetch_ohio_tax_delinquent("Montgomery", min_amount=1000, today=TODAY)
    assert len(records) == 2  # 5000 + 50000


def test_dispatcher_drops_records_with_no_signal(monkeypatch):
    """Records that fail the amount rule are dropped — no silent passes."""
    import ohio_tax_delinquent_scrapers as mod
    monkeypatch.setitem(mod._DISPATCH, "montgomery", lambda **kw: [
        _rec(amount="", years=""),              # blank/blank — drop
        _rec(amount="invalid", years="never"),  # garbage amount — drop
        _rec(amount="9000.00"),                 # valid amount, no years → keep (fallback)
        _rec(amount="200.00", years="2010"),    # chronic but sub-$3k → drop under AND
    ])
    records = fetch_ohio_tax_delinquent("Montgomery", today=TODAY)
    assert len(records) == 1
    assert records[0].tax_delinquent_amount == "9000.00"


def test_filter_preserves_other_fields(monkeypatch):
    """Filter must not mutate the NoticeData fields it keeps."""
    import ohio_tax_delinquent_scrapers as mod

    rec = NoticeData(
        notice_type="tax_delinquent", county="TestCounty",
        owner_name="SMITH JOHN", parcel_id="P001",
        tax_delinquent_amount="10000.00", address="123 MAIN ST",
        tax_delinquent_years="2020",
    )
    monkeypatch.setitem(mod._DISPATCH, "montgomery", lambda **kw: [rec])
    records = fetch_ohio_tax_delinquent("Montgomery", today=TODAY)
    assert len(records) == 1
    out = records[0]
    assert out.owner_name == "SMITH JOHN"
    assert out.parcel_id == "P001"
    assert out.address == "123 MAIN ST"
    assert out.tax_delinquent_years == "2020"
