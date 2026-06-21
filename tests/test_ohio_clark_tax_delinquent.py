"""Tests for the Clark County (OH) tax-delinquent adapter.

Clark publishes a UTF-16-encoded CSV (Excel-style ``sep=,`` directive
on line 1) accessible via an "Export CSV" button on the Auditor's
DelinquencyReport page. Live 2026-06-16: ~2,582 delinquent parcels.

Schema: ``Parcel Number | Tax Payer | Certified Year | Vacant | Amount``.

Transport: Cloudflare-protected portal — requires Playwright. Tests
bypass via ``csv_override_text``.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from ohio_tax_delinquent_scrapers import (
    OHIO_ENDPOINTS,
    _clark_parse_csv,
    fetch_clark,
    fetch_ohio_tax_delinquent,
)


# Fixture mirroring real Clark CSV (with sep=, directive)
CLARK_CSV_SAMPLE = '''sep=,
"Parcel Number","Tax Payer","Certified Year","Vacant","Amount"
"0100500003103022","JONES MICHAEL J & KARLA D","2023","False","$2,159.28"
"0100500004400031","EBLIN JULIE M","2023","False","$1,465.06"
"0100500005100003","BROOKS PAUL R & REBECCA","2024","True","$33.75"
"0100500005300015","S & B CONTRACTORS LLC","2023","False","$2,096.85"
'''

# Without sep= directive (just header + rows)
CLARK_CSV_NO_DIRECTIVE = '''"Parcel Number","Tax Payer","Certified Year","Vacant","Amount"
"P001","SMITH JOHN","2023","False","$500.00"
'''


def test_clark_parse_skips_sep_directive():
    """The 'sep=,' line on row 1 must not break parsing."""
    records = list(_clark_parse_csv(CLARK_CSV_SAMPLE))
    assert len(records) == 4


def test_clark_parse_no_directive_also_works():
    """CSV without the sep= line still parses (defensive)."""
    records = list(_clark_parse_csv(CLARK_CSV_NO_DIRECTIVE))
    assert len(records) == 1
    assert records[0].owner_name == "SMITH JOHN"


def test_clark_parse_field_mapping():
    records = list(_clark_parse_csv(CLARK_CSV_SAMPLE))
    r = records[0]
    assert r.parcel_id == "0100500003103022"
    assert r.owner_name == "JONES MICHAEL J & KARLA D"
    assert r.tax_delinquent_years == "2023"
    assert r.notice_type == "tax_delinquent"
    assert r.county == "Clark"
    assert r.state == "OH"
    # No address — same gap as Montgomery
    assert r.address == ""


def test_clark_parse_strips_dollar_and_commas():
    """'$2,159.28' → '2159.28'."""
    records = list(_clark_parse_csv(CLARK_CSV_SAMPLE))
    assert records[0].tax_delinquent_amount == "2159.28"
    assert records[1].tax_delinquent_amount == "1465.06"


def test_clark_parse_stashes_vacant_flag():
    """Vacant True/False preserved in raw_text for downstream filtering."""
    records = list(_clark_parse_csv(CLARK_CSV_SAMPLE))
    vacant_row = next(r for r in records if r.parcel_id == "0100500005100003")
    assert "vacant: True" in vacant_row.raw_text
    occupied_row = next(r for r in records if r.parcel_id == "0100500003103022")
    assert "vacant: False" in occupied_row.raw_text


def test_clark_parse_skips_rows_missing_parcel():
    """Rows without a parcel number must be skipped (irrecoverable)."""
    csv_with_bad_row = '''sep=,
"Parcel Number","Tax Payer","Certified Year","Vacant","Amount"
"","UNKNOWN OWNER","2023","False","$100.00"
"P001","SMITH JOHN","2023","False","$500.00"
'''
    records = list(_clark_parse_csv(csv_with_bad_row))
    assert len(records) == 1
    assert records[0].parcel_id == "P001"


def test_clark_parse_empty_text():
    assert list(_clark_parse_csv("")) == []


def test_clark_parse_handles_only_directive():
    """Just the sep= directive with no data rows → no records."""
    records = list(_clark_parse_csv("sep=,\n"))
    assert records == []


# fetch_clark via override


def test_fetch_clark_via_override():
    records = fetch_clark(csv_override_text=CLARK_CSV_SAMPLE)
    assert len(records) == 4
    assert all(r.notice_type == "tax_delinquent" for r in records)
    assert all(r.county == "Clark" for r in records)


def test_fetch_clark_requires_ctx_or_override():
    """Without ctx AND without override, fetch_clark raises ValueError."""
    with pytest.raises(ValueError, match="ctx="):
        fetch_clark()


def test_fetch_clark_amount_no_dollar_sign():
    records = fetch_clark(csv_override_text=CLARK_CSV_SAMPLE)
    # Amounts come out as bare decimal strings, ready for Decimal()
    for r in records:
        assert "$" not in r.tax_delinquent_amount
        assert "," not in r.tax_delinquent_amount


def test_clark_endpoint_metadata():
    cfg = OHIO_ENDPOINTS["Clark"]
    assert cfg["transport"] == "playwright"
    assert cfg["refresh_cadence"] == "weekly"
    assert "clarkcountyauditor" in cfg["portal"]


def test_clark_in_dispatch():
    from ohio_tax_delinquent_scrapers import _DISPATCH
    assert _DISPATCH["clark"].__name__ == "fetch_clark"
