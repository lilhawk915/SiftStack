"""Tests for the Warren County (OH) tax-delinquent adapter.

Warren's Auditor publishes a born-digital text PDF (no OCR needed,
verified live 2026-06-16) once per tax cycle. Schema per row:

    <7-digit account>  <OWNER NAME>  <legal desc>  <$ amount>

The owner / legal-description boundary has no clean delimiter — we
anchor on account-number digits + trailing currency value and split
the middle blob heuristically. Tests pin both the easy and the
ugly cases.

Address resolution uses Warren Auditor's Account-Number search
(plain HTTP, no Cloudflare). Tests mock the lookup so we don't hit
the network.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from ohio_tax_delinquent_scrapers import (
    OHIO_ENDPOINTS,
    _warren_parse_rows,
    fetch_warren,
)


# ── Fixture text mirroring real Warren PDF content ────────────────────

WARREN_PDF_TEXT_SAMPLE = """\
WARREN COUNTY DELINQUENT TAX LIST
The lands, lots and part of lots returned delinquent by the Treasurer
of Warren County, with the taxes, assessments, interest and penalties,
charged against them agreeably to law, are contained and described in
the following list:
------------------------------------------------------------------------
OWNER AS OF 11/6/2025 PROPERTY DESCRIPTION TOTAL DUE
------------------------------------------------------------------------
0101974 BLUBAUGH, LILLIAN MILDRED 5-3-32 2.4031 AC. 3,200.69
0102792 GREER, DONALD M. & BRENDA CENTERVILLE FOREST LOT: 62 505.54
0105929 JACKSON, KAREN L. ; STEGE* 4-4-21 14.1010 AC. 8,467.50
0107069 HAINES, FRANCES 5-3-26 0.5000 AC. 2,140.72
0113565 GHP INVESTMENTS LLC HORIZON HILLS LOT: 21 2,129.79
0118311 & ERIN C.; OBERER, CHARLE* 4-4-35 113.5670 AC. 17,717.34
0501409 MILNE, PATRICIA J. & TAMARACK HILLS LOT: 347-46 PT 1,754.48
9703921 DEANNA M JOHNSON NKA DEAN* MS-5990 5,836.38
"""


# ── Row parsing ───────────────────────────────────────────────────────


def test_warren_parse_row_count():
    rows = list(_warren_parse_rows(WARREN_PDF_TEXT_SAMPLE))
    assert len(rows) == 8


def test_warren_parse_clean_row():
    """Plain BLUBAUGH row — clean account + owner + legal + amount."""
    rows = list(_warren_parse_rows(WARREN_PDF_TEXT_SAMPLE))
    r = rows[0]
    assert r["account"] == "0101974"
    assert r["owner"] == "BLUBAUGH, LILLIAN MILDRED"
    assert r["amount"] == "3200.69"
    # Legal description follows the section-township-range token
    assert "5-3-32" in r["legal"]
    assert "2.4031 AC" in r["legal"]


def test_warren_parse_amount_strips_comma():
    """Amounts like '8,467.50' must come back as '8467.50' (Decimal-ready)."""
    rows = list(_warren_parse_rows(WARREN_PDF_TEXT_SAMPLE))
    r = next(r for r in rows if r["account"] == "0105929")
    assert r["amount"] == "8467.50"


def test_warren_parse_strips_continuation_asterisk():
    """Owner names ending in '*' (continuation marker) must be stripped."""
    rows = list(_warren_parse_rows(WARREN_PDF_TEXT_SAMPLE))
    r = next(r for r in rows if r["account"] == "0105929")
    assert not r["owner"].endswith("*")
    assert r["owner"] == "JACKSON, KAREN L. ; STEGE"


def test_warren_parse_lot_subdivision_legal_desc():
    """Subdivision-name + LOT format separates into owner + legal."""
    rows = list(_warren_parse_rows(WARREN_PDF_TEXT_SAMPLE))
    r = next(r for r in rows if r["account"] == "0113565")
    # The owner name comes through. Legal description includes LOT info.
    assert "GHP INVESTMENTS" in r["owner"]
    assert "LOT" in r["legal"]


def test_warren_parse_skips_header_block():
    """Header rows ('WARREN COUNTY DELINQUENT TAX LIST', etc.) must not match."""
    rows = list(_warren_parse_rows(WARREN_PDF_TEXT_SAMPLE))
    # Header text never starts with a 7-digit account number — verify
    # none of the row owners contain "WARREN COUNTY" or "OWNER AS OF"
    for r in rows:
        assert "WARREN COUNTY" not in r["owner"]
        assert "OWNER AS OF" not in r["owner"]


def test_warren_parse_skips_blank_lines():
    """Blank / whitespace-only lines must not produce rows."""
    text = "\n\n   \n0101974 SMITH JOHN 5-3-32 100.00\n\n"
    rows = list(_warren_parse_rows(text))
    assert len(rows) == 1


def test_warren_parse_no_match_on_non_account_lines():
    """Lines without a 7-digit prefix must not produce rows."""
    text = "Some random text\n12345 NOT A REAL ROW $50.00\n"
    rows = list(_warren_parse_rows(text))
    assert rows == []


def test_warren_parse_handles_high_amount():
    """Large delinquent amounts (5+ digit dollar values) parse cleanly."""
    rows = list(_warren_parse_rows(WARREN_PDF_TEXT_SAMPLE))
    r = next(r for r in rows if r["account"] == "0118311")
    assert r["amount"] == "17717.34"


def test_warren_parse_handles_lot_with_dash_and_pt():
    """Legal description 'LOT: 347-46 PT' must be captured (not split mid-token)."""
    rows = list(_warren_parse_rows(WARREN_PDF_TEXT_SAMPLE))
    r = next(r for r in rows if r["account"] == "0501409")
    assert "TAMARACK HILLS" in r["legal"] or "TAMARACK HILLS" in r["owner"]
    assert "1754.48" == r["amount"]


# ── fetch_warren via override (no network) ────────────────────────────


def test_fetch_warren_via_override_text():
    """fetch_warren with override text + lookup_addresses=False = pure parsing."""
    records = fetch_warren(
        pdf_override_text=WARREN_PDF_TEXT_SAMPLE,
        lookup_addresses=False,
    )
    assert len(records) == 8
    assert all(r.notice_type == "tax_delinquent" for r in records)
    assert all(r.county == "Warren" for r in records)
    assert all(r.state == "OH" for r in records)
    # No address lookup → address fields stay empty
    assert all(r.address == "" for r in records)
    # Account → parcel_id
    assert records[0].parcel_id == "0101974"
    # Owner name maps through
    assert records[0].owner_name == "BLUBAUGH, LILLIAN MILDRED"
    # Tax amount string preserved
    assert records[0].tax_delinquent_amount == "3200.69"


def test_fetch_warren_address_lookup_uses_mock(monkeypatch):
    """When lookup_addresses=True we hit warren_auditor for each row.

    Mock the lookup so the test stays offline. Verify each emitted
    NoticeData gets an address field populated from the mock.
    """
    import warren_auditor
    calls = []

    def mock_lookup(account, client=None):
        calls.append(account)
        # Return a fake address that includes the account number so
        # we can assert join-correctness per row.
        addr = warren_auditor.ParcelAddress(
            street=f"{account} TEST ST",
            city="MOCKVILLE",
            state="OH",
            zip="45000",
        )
        return addr

    monkeypatch.setattr(
        warren_auditor, "lookup_property_by_account", mock_lookup,
    )

    records = fetch_warren(
        pdf_override_text=WARREN_PDF_TEXT_SAMPLE,
        lookup_addresses=True,
    )
    assert len(records) == 8
    # Every row got an address from the mock
    assert all(r.address.endswith("TEST ST") for r in records)
    # The mock saw exactly one call per row
    assert len(calls) == 8
    # Account → address binding is right (no off-by-one)
    smith = next(r for r in records if r.parcel_id == "0107069")
    assert smith.address == "0107069 TEST ST"
    assert smith.city == "Mockville" or smith.city == "MOCKVILLE"


def test_fetch_warren_max_address_lookups_caps_lookup_pass():
    """max_address_lookups caps lookup count but still emits all rows."""
    import warren_auditor
    calls = []

    def mock_lookup(account, client=None):
        calls.append(account)
        return warren_auditor.ParcelAddress(
            street="123 MOCK", city="MOCK", state="OH", zip="45000",
        )

    import ohio_tax_delinquent_scrapers as mod
    # Patch where the adapter looks it up
    mod.warren_auditor = warren_auditor  # ensure ref
    import pytest as _pt
    with _pt.MonkeyPatch.context() as mp:
        mp.setattr(warren_auditor, "lookup_property_by_account", mock_lookup)
        records = fetch_warren(
            pdf_override_text=WARREN_PDF_TEXT_SAMPLE,
            lookup_addresses=True,
            max_address_lookups=3,
        )
    # All 8 rows still emitted; only 3 got address lookups
    assert len(records) == 8
    assert len(calls) == 3
    # First 3 records have addresses; last 5 don't
    assert sum(1 for r in records if r.address) == 3


# ── Config wiring ─────────────────────────────────────────────────────


def test_warren_endpoint_metadata():
    """OHIO_ENDPOINTS['Warren'] must declare yearly cadence + plain HTTP."""
    cfg = OHIO_ENDPOINTS["Warren"]
    assert cfg["refresh_cadence"] == "yearly"
    assert cfg["transport"] == "http"
    assert cfg["method"] == "pdf_text"
    assert cfg["portal"].endswith("DelqTax.pdf")


def test_warren_in_dispatch():
    """fetch_warren must be routed by the dispatcher for 'Warren'."""
    from ohio_tax_delinquent_scrapers import _DISPATCH
    assert _DISPATCH["warren"].__name__ == "fetch_warren"
