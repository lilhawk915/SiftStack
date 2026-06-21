"""Tests for the Miami County (OH) tax-delinquent adapter.

Miami uses the same Auditor software as Clark — same Export CSV
button, same UTF-16 CSV, same 5-column schema. The adapters share
``_clark_or_miami_parse_csv``; these tests pin Miami's specific
county-naming + endpoint wiring.

Live 2026-06-16: ~658 delinquent parcels.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from ohio_tax_delinquent_scrapers import (
    OHIO_ENDPOINTS,
    _clark_or_miami_parse_csv,
    fetch_miami,
)


# Real-shape Miami CSV fixture (mirrors live data)
MIAMI_CSV_SAMPLE = '''sep=,
"Parcel Number","Tax Payer","Certified Year","Vacant","Amount"
"A01-008360","WILEY TERESA","2024","False","$1,270.31"
"A01-009359","WILKSON JAMES P","1998","False","$899.40"
"A01-020270","SERRANO FELIPE LINORES","2024","True","$2,730.58"
'''


def test_miami_parse_row_count():
    records = list(_clark_or_miami_parse_csv(MIAMI_CSV_SAMPLE, "Miami"))
    assert len(records) == 3


def test_miami_parse_county_assignment():
    """county=Miami, source_url points to Miami's portal (NOT Clark's)."""
    records = list(_clark_or_miami_parse_csv(MIAMI_CSV_SAMPLE, "Miami"))
    for r in records:
        assert r.county == "Miami"
        assert "miamicountyohioauditor" in r.source_url


def test_miami_parse_amount_cleanup():
    records = list(_clark_or_miami_parse_csv(MIAMI_CSV_SAMPLE, "Miami"))
    assert records[0].tax_delinquent_amount == "1270.31"
    assert records[2].tax_delinquent_amount == "2730.58"


def test_miami_parse_handles_old_year():
    """Miami still has 1998 delinquencies — long-tail year values must parse."""
    records = list(_clark_or_miami_parse_csv(MIAMI_CSV_SAMPLE, "Miami"))
    old = next(r for r in records if r.owner_name == "WILKSON JAMES P")
    assert old.tax_delinquent_years == "1998"


def test_miami_parse_vacant_flag_in_raw_text():
    records = list(_clark_or_miami_parse_csv(MIAMI_CSV_SAMPLE, "Miami"))
    serrano = next(r for r in records if r.parcel_id == "A01-020270")
    assert "vacant: True" in serrano.raw_text


def test_fetch_miami_via_override():
    records = fetch_miami(csv_override_text=MIAMI_CSV_SAMPLE)
    assert len(records) == 3
    assert all(r.county == "Miami" for r in records)
    assert all(r.notice_type == "tax_delinquent" for r in records)


def test_fetch_miami_requires_ctx_or_override():
    with pytest.raises(ValueError, match="ctx="):
        fetch_miami()


def test_miami_endpoint_metadata():
    cfg = OHIO_ENDPOINTS["Miami"]
    assert cfg["transport"] == "playwright"
    assert cfg["refresh_cadence"] == "weekly"
    assert "miamicountyohioauditor" in cfg["portal"]


def test_miami_in_dispatch():
    from ohio_tax_delinquent_scrapers import _DISPATCH
    assert _DISPATCH["miami"].__name__ == "fetch_miami"


def test_miami_uses_shared_parser_with_clark():
    """Both fetch_miami and fetch_clark route to _clark_or_miami_parse_csv."""
    # Same fixture parses identically under both county names except for
    # county/source_url fields
    miami_records = list(_clark_or_miami_parse_csv(MIAMI_CSV_SAMPLE, "Miami"))
    clark_records = list(_clark_or_miami_parse_csv(MIAMI_CSV_SAMPLE, "Clark"))
    assert len(miami_records) == len(clark_records)
    assert miami_records[0].parcel_id == clark_records[0].parcel_id
    # Only the county + source_url differ
    assert miami_records[0].county == "Miami"
    assert clark_records[0].county == "Clark"
