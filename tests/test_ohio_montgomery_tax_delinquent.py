"""Tests for the Montgomery County (OH) tax-delinquent adapter.

Montgomery publishes an inline HTML table at
``mcohio.org/1521/Delinquent-List`` (~3,547 rows live 2026-06-16).
Schema: ``DistCode | District Name | Owner Name | Parcel ID | Delq Amount``.
No property address — that's a deliberate gap; downstream enrichment
fills it via parcel→address lookup at mcrealestate.org (Playwright).

Transport: plain HTTP (no Cloudflare/WAF).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from ohio_tax_delinquent_scrapers import (
    OHIO_ENDPOINTS,
    _montgomery_parse_rows,
    fetch_montgomery,
    fetch_ohio_tax_delinquent,
)


# ── Fixture mirroring the live HTML ───────────────────────────────────

MONTGOMERY_HTML_SAMPLE = """\
<html><body>
<table>
<tr><td>DistCode</td><td>District Name</td><td>Owner Name</td><td>Parcel ID</td><td>Delq Amount</td></tr>
<tr><td>A01</td><td>BUTLER TWP-VAN BUTLER CSD</td><td>KOHLRIESER PATRICK D</td><td>A01 00104 0086</td><td>3,641.05</td></tr>
<tr><td>A01</td><td>BUTLER TWP-VAN BUTLER CSD</td><td>CAMPBELL PATRICIA L TR</td><td>A01 00109 0070</td><td>182.27</td></tr>
<tr><td>A01</td><td>BUTLER TWP-VAN BUTLER CSD</td><td>WESTFALL-STILLWATER PROPERTIES II LLC</td><td>A01 00110 0021</td><td>246.21</td></tr>
<tr><td>D02</td><td>DAYTON CSD</td><td>SMITH JOHN E</td><td>D02 12345 6789</td><td>$1,000.00</td></tr>
<tr><td>K01</td><td>KETTERING CSD</td><td>DOE JANE</td><td>K01 99999 0001</td><td>500.00</td></tr>
</table>
<table><tr><td>Some other table</td><td>not the delinquent list</td></tr></table>
</body></html>
"""


# ── Row parsing ───────────────────────────────────────────────────────


def test_montgomery_parse_row_count():
    rows = list(_montgomery_parse_rows(MONTGOMERY_HTML_SAMPLE))
    assert len(rows) == 5


def test_montgomery_parse_skips_header_row():
    """Header row with 'DistCode'/'Owner Name' must not produce a data row."""
    rows = list(_montgomery_parse_rows(MONTGOMERY_HTML_SAMPLE))
    for r in rows:
        assert r["dist_code"] != "DistCode"
        assert r["owner"] != "Owner Name"


def test_montgomery_parse_field_mapping():
    rows = list(_montgomery_parse_rows(MONTGOMERY_HTML_SAMPLE))
    first = rows[0]
    assert first["dist_code"] == "A01"
    assert first["district"] == "BUTLER TWP-VAN BUTLER CSD"
    assert first["owner"] == "KOHLRIESER PATRICK D"
    assert first["parcel"] == "A01 00104 0086"


def test_montgomery_parse_strips_amount_commas_and_dollar():
    """Amount cells like '3,641.05' or '$1,000.00' come through as '3641.05'/'1000.00'."""
    rows = list(_montgomery_parse_rows(MONTGOMERY_HTML_SAMPLE))
    assert rows[0]["amount"] == "3641.05"   # 3,641.05 → 3641.05
    smith = next(r for r in rows if r["owner"] == "SMITH JOHN E")
    assert smith["amount"] == "1000.00"     # $1,000.00 → 1000.00


def test_montgomery_parse_ignores_unrelated_tables():
    """Only the table with the 5-col header schema produces rows."""
    rows = list(_montgomery_parse_rows(MONTGOMERY_HTML_SAMPLE))
    # All 5 rows came from the FIRST table; the "Some other table" is skipped
    assert len(rows) == 5
    # All have full 5-field shape
    assert all("owner" in r and "parcel" in r and "amount" in r for r in rows)


def test_montgomery_parse_handles_empty_html():
    assert list(_montgomery_parse_rows("")) == []
    assert list(_montgomery_parse_rows("<html></html>")) == []


def test_montgomery_parse_handles_missing_table():
    """When the data table isn't present, parser yields nothing (doesn't crash)."""
    other_html = "<html><body><table><tr><td>foo</td><td>bar</td></tr></table></body></html>"
    assert list(_montgomery_parse_rows(other_html)) == []


# ── fetch_montgomery via override ─────────────────────────────────────


def test_fetch_montgomery_via_override():
    """fetch_montgomery with override text — no network."""
    records = fetch_montgomery(html_override_text=MONTGOMERY_HTML_SAMPLE)
    assert len(records) == 5
    assert all(r.notice_type == "tax_delinquent" for r in records)
    assert all(r.county == "Montgomery" for r in records)
    assert all(r.state == "OH" for r in records)
    # No address fields — they're a deliberate gap
    assert all(r.address == "" for r in records)
    # Parcel + owner + amount populate
    assert records[0].parcel_id == "A01 00104 0086"
    assert records[0].owner_name == "KOHLRIESER PATRICK D"
    assert records[0].tax_delinquent_amount == "3641.05"


def test_fetch_montgomery_stashes_district_in_raw_text():
    """DistCode + district name preserved in raw_text for downstream context."""
    records = fetch_montgomery(html_override_text=MONTGOMERY_HTML_SAMPLE)
    r = records[0]
    assert "DistCode: A01" in r.raw_text
    assert "BUTLER TWP-VAN BUTLER CSD" in r.raw_text


def test_fetch_montgomery_empty_input_returns_empty_list():
    """Empty HTML input → empty record list, no crash."""
    assert fetch_montgomery(html_override_text="") == []
    assert fetch_montgomery(html_override_text="<html></html>") == []


# ── Config wiring ─────────────────────────────────────────────────────


def test_montgomery_endpoint_metadata():
    """OHIO_ENDPOINTS['Montgomery'] declares plain HTTP + weekly cadence."""
    cfg = OHIO_ENDPOINTS["Montgomery"]
    assert cfg["transport"] == "http"
    assert cfg["refresh_cadence"] == "weekly"
    assert cfg["method"] == "scrape"
    assert "mcohio.org" in cfg["portal"]


def test_montgomery_in_dispatch():
    """fetch_montgomery routed by the dispatcher for 'Montgomery'."""
    from ohio_tax_delinquent_scrapers import _DISPATCH
    assert _DISPATCH["montgomery"].__name__ == "fetch_montgomery"


def test_dispatcher_montgomery_no_longer_raises_not_implemented(monkeypatch):
    """Earlier stub raised NotImplementedError; now we ship real records.

    Use ``apply_filter=False`` so this test sees the raw 5 fixture rows
    independent of the $8k production filter (which is exercised in
    ``tests/test_ohio_tax_delinquent_min_amount.py``).
    """
    import ohio_tax_delinquent_scrapers as mod
    monkeypatch.setattr(mod, "_montgomery_fetch_html",
                        lambda c=None: MONTGOMERY_HTML_SAMPLE)
    records = fetch_ohio_tax_delinquent("Montgomery", apply_filter=False)
    assert len(records) == 5
    assert all(r.notice_type == "tax_delinquent" for r in records)
