"""Tests for the Greene County (OH) tax-delinquent adapter.

Greene partitions its delinquent list across ~30 townships. Each township
exposes an Export CSV. The adapter iterates all townships, downloads each
CSV, deduplicates by parcel_id (EXC variants overlap with their parent
townships), and emits NoticeData.

Schema (per-township CSV):
    Property Number | Owner Name | School District | Location Address | Amount | View Property

Greene is the ONLY county in this set that includes property address
inline in the delinquent feed — no separate parcel→address lookup needed.

Transport: Azure WAF (NOT Cloudflare) — Playwright required.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from ohio_tax_delinquent_scrapers import (
    OHIO_ENDPOINTS,
    _greene_emit_records,
    _greene_parse_csv,
    _parse_multiline_address,
    fetch_greene,
)


# Real-shape Greene CSV (one township's export). Note the multi-line
# Location Address cells with embedded newlines inside CSV-quoted values.
GREENE_BATH_CSV = '''Property Number,Owner Name,School District,Location Address,Amount,View Property
A01-00005,NICKELL SUE E,FAIRBORN CSD,1900 SPANGLER RD LOT 62,"1,283.06",https://example.com/x
A01-0001-0001-0-0065-00,GRAHAM PAUL W & AMIE ELAM,FAIRBORN CSD,"4903 BATH RD\nDAYTON OH 45424","5,456.47",https://example.com/y
A01-0001-0002-0-0019-00,LOVIN CLYDE,FAIRBORN CSD,E KITRIDGE RD,"262.74",https://example.com/z
'''

# Same parcel as above (dedup target)
GREENE_BATH_EXC_CSV = '''Property Number,Owner Name,School District,Location Address,Amount,View Property
A01-0001-0001-0-0065-00,GRAHAM PAUL W & AMIE ELAM,FAIRBORN CSD,"4903 BATH RD\nDAYTON OH 45424","5,456.47",https://example.com/y
B99-9999-9999-9-9999-99,UNIQUE TO EXC TWP,XENIA CSD,"100 EXC ST\nXENIA OH 45385","100.00",https://example.com/w
'''


def test_greene_parse_basic():
    records = list(_greene_parse_csv(GREENE_BATH_CSV, "BATH TWP"))
    assert len(records) == 3
    assert all(r.notice_type == "tax_delinquent" for r in records)
    assert all(r.county == "Greene" for r in records)


def test_greene_parse_multiline_address():
    """Location Address with embedded newline: '4903 BATH RD\\nDAYTON OH 45424'."""
    records = list(_greene_parse_csv(GREENE_BATH_CSV, "BATH TWP"))
    r = next(r for r in records if r.owner_name.startswith("GRAHAM"))
    assert r.address == "4903 BATH RD"
    assert r.city == "Dayton"
    assert r.state == "OH"
    assert r.zip == "45424"


def test_greene_parse_address_without_city_state_zip():
    """Some rows have street-only addresses (no city/zip) — don't crash."""
    records = list(_greene_parse_csv(GREENE_BATH_CSV, "BATH TWP"))
    nickell = next(r for r in records if r.owner_name == "NICKELL SUE E")
    assert nickell.address == "1900 SPANGLER RD LOT 62"
    # city/state/zip blank when not present in source
    assert nickell.city == ""
    assert nickell.zip == ""


def test_greene_parse_amount_strip():
    records = list(_greene_parse_csv(GREENE_BATH_CSV, "BATH TWP"))
    g = next(r for r in records if r.owner_name.startswith("GRAHAM"))
    assert g.tax_delinquent_amount == "5456.47"


def test_greene_parse_raw_text_carries_township():
    records = list(_greene_parse_csv(GREENE_BATH_CSV, "BATH TWP"))
    assert "township: BATH TWP" in records[0].raw_text
    assert "FAIRBORN CSD" in records[0].raw_text


def test_greene_emit_records_deduplicates_across_townships():
    """EXC variants of a township overlap — dedupe by parcel_id."""
    csv_map = {
        "BATH TWP": GREENE_BATH_CSV,
        "BATH TWP EXC FAIRBORN CITY": GREENE_BATH_EXC_CSV,
    }
    records = list(_greene_emit_records(csv_map))
    # BATH TWP has 3 rows, BATH TWP EXC has 2 (1 dupe + 1 unique) → 4 total after dedup
    assert len(records) == 4
    parcels = {r.parcel_id for r in records}
    assert "A01-0001-0001-0-0065-00" in parcels  # dupe survived once
    assert "B99-9999-9999-9-9999-99" in parcels  # unique to EXC


def test_greene_emit_records_handles_empty_csv():
    """Empty township CSV → no records, no crash."""
    records = list(_greene_emit_records({"BATH TWP": ""}))
    assert records == []


# Multi-line address parser


def test_parse_multiline_address_with_newline():
    s, c, st, z = _parse_multiline_address("4903 BATH RD\nDAYTON OH 45424")
    assert s == "4903 BATH RD"
    assert c == "Dayton"
    assert st == "OH"
    assert z == "45424"


def test_parse_multiline_address_house_number_first_line():
    """'4343\\nE KITRIDGE RD DAYTON OH 45424' — house# alone on line 1."""
    s, c, st, z = _parse_multiline_address("4343\nE KITRIDGE RD DAYTON OH 45424")
    assert s == "4343 E KITRIDGE RD"
    assert c == "Dayton"


def test_parse_multiline_address_no_zip_returns_street_only():
    s, c, st, z = _parse_multiline_address("1900 SPANGLER RD LOT 62")
    assert s == "1900 SPANGLER RD LOT 62"
    assert c == ""
    assert st == ""
    assert z == ""


def test_parse_multiline_address_one_line_city_state_zip():
    s, c, st, z = _parse_multiline_address("244 PA ST XENIA OH 45385")
    assert s == "244 PA ST"
    assert c == "Xenia"
    assert st == "OH"
    assert z == "45385"


# fetch_greene + dispatcher


def test_fetch_greene_via_override():
    """fetch_greene with township_csv_text_map = offline path."""
    records = fetch_greene(township_csv_text_map={"BATH TWP": GREENE_BATH_CSV})
    assert len(records) == 3
    assert all(r.notice_type == "tax_delinquent" for r in records)
    assert all(r.county == "Greene" for r in records)


def test_fetch_greene_requires_ctx_or_override():
    with pytest.raises(ValueError, match="ctx="):
        fetch_greene()


def test_greene_endpoint_metadata():
    cfg = OHIO_ENDPOINTS["Greene"]
    assert cfg["transport"] == "playwright"
    assert cfg["refresh_cadence"] == "weekly"
    # Verified-2026-06-16 URL (NOT the dead greeneauditor.org)
    assert "auditor.greenecountyohio.gov" in cfg["portal"]


def test_greene_in_dispatch():
    from ohio_tax_delinquent_scrapers import _DISPATCH
    assert _DISPATCH["greene"].__name__ == "fetch_greene"
