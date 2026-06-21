"""Tests for the Butler County (OH) tax-delinquent adapter.

Butler is the cleanest source of the 7 SW Ohio counties — but "cleanest"
came with two surprises from the live probe (2026-06-16):

  1. Cloudflare on the revize.com CDN blocks plain HTTP. The adapter
     uses Playwright click-download in production. Tests bypass the
     network with override-text kwargs.
  2. The Delinquent Tax List CSV is narrow (PARID, CURRENTYEARDUE,
     LUC, PRIORYEARDUE) — owner + address come from a sibling Owners
     Report CSV. The adapter does an in-memory join on PARID==PARCEL.

These tests pin the wiring end-to-end:

  - Delinquent CSV parsing
  - Owners CSV → dict[PARID, row] streaming join
  - Tax-amount Decimal sum (CURRENTYEARDUE + PRIORYEARDUE)
  - Absentee detection (LOCATION street vs MAILADR1, normalized)
  - Dispatcher routing
  - Config wiring
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from ohio_tax_delinquent_scrapers import (
    OHIO_ENDPOINTS,
    _is_absentee,
    _normalize_street,
    _parse_owners_csv,
    _split_mail_lines,
    _sum_delinquent,
    fetch_butler,
    fetch_clermont,
    fetch_ohio_tax_delinquent,
)


# ── Fixtures — mirror real Butler schemas verified 2026-06-16 ─────────

# Real Butler Delinquent Tax List schema. Three rows: one with current
# year + prior year, one with prior only, one with junk in amounts.
DELINQUENT_CSV = """\
PARID,CURRENTYEARDUE,LUC,PRIORYEARDUE
"M5400-013-000-118","1245.67","500","2400.50"
"M5400-013-000-119","0","510","12890.50"
"M5400-013-000-120","invalid","521","0.00"
"""

# Real Butler Owners Report schema (subset of ~28 columns). Three rows
# with full mailing addresses — two in-county, one out-of-state.
OWNERS_CSV = """\
PARCEL,LOCATION,ACRES,LUC,OWNER1,OWNER2,MAILNAME1,MAILNAME2,MAILADR1,MAILADR2,MAILADR3
"M5400-013-000-118","123  MAIN ST","0.25","500","SMITH JOHN E","","SMITH JOHN E","","123 MAIN ST","","HAMILTON OH 45011"
"M5400-013-000-119","456 OAK AVE","0.30","510","DOE JANE","","DOE JANE","","PO BOX 1234","","CHICAGO IL 60601"
"M5400-013-000-120","789 ELM DR","0.40","521","JONES BOB","","JONES BOB","","789 ELM DR","","HAMILTON OH 45011"
"""


# ── Tax amount Decimal sum ────────────────────────────────────────────


def test_butler_tax_amount_sums():
    """CURRENTYEARDUE + PRIORYEARDUE summed precisely as Decimal."""
    assert _sum_delinquent("1000.50", "500.25") == "1500.75"


def test_butler_tax_amount_sum_with_dollar_and_commas():
    """Source data sometimes has $ and commas — Decimal handles them."""
    assert _sum_delinquent("$1,000.50", "$500.25") == "1500.75"


def test_butler_tax_amount_sum_with_only_current():
    """Prior year blank — sum is just the current year."""
    assert _sum_delinquent("1245.67", "") == "1245.67"


def test_butler_tax_amount_sum_with_only_prior():
    """Current year blank — sum is just the prior year."""
    assert _sum_delinquent("", "2400.50") == "2400.50"


def test_butler_tax_amount_sum_both_blank():
    """Both blank → empty string (not '0.00')."""
    assert _sum_delinquent("", "") == ""


def test_butler_tax_amount_sum_invalid_input():
    """Bad input → fall back to '' rather than crash."""
    assert _sum_delinquent("nope", "nope") == ""


def test_butler_tax_amount_sum_one_invalid_one_valid():
    """When one side is junk and the other is real, use the real one."""
    assert _sum_delinquent("invalid", "100.00") == "100.00"


# ── Owners CSV parsing + join ─────────────────────────────────────────


def test_butler_owners_parse_into_dict():
    """Owners CSV → dict keyed by PARID with the 9 fields we care about."""
    owners = _parse_owners_csv(OWNERS_CSV)
    assert len(owners) == 3
    row = owners["M5400-013-000-118"]
    assert row["location"] == "123  MAIN ST"  # whitespace not yet collapsed
    assert row["owner1"] == "SMITH JOHN E"
    assert row["mailadr1"] == "123 MAIN ST"
    assert row["mailadr3"] == "HAMILTON OH 45011"


def test_butler_owners_parse_empty_text():
    """Missing / empty Owners CSV returns an empty dict, not a crash."""
    assert _parse_owners_csv("") == {}


def test_butler_owners_join_keeps_only_delinquent_rows():
    """5 delinquent + 8 owners → 5 records (with 3 unmatched parcels)."""
    delinquent = """\
PARID,CURRENTYEARDUE,LUC,PRIORYEARDUE
"P001","100.00","500","50.00"
"P002","200.00","510","0"
"P003","300.00","521","0"
"P004","400.00","500","0"
"P005","500.00","500","0"
"""
    owners = """\
PARCEL,LOCATION,OWNER1,MAILADR1,MAILADR2,MAILADR3
"P001","1 ALPHA ST","ALPHA OWNER","1 ALPHA ST","","HAMILTON OH 45011"
"P002","2 BETA AVE","BETA OWNER","2 BETA AVE","","HAMILTON OH 45011"
"P003","3 GAMMA DR","GAMMA OWNER","3 GAMMA DR","","HAMILTON OH 45011"
"P004","4 DELTA LN","DELTA OWNER","4 DELTA LN","","HAMILTON OH 45011"
"P005","5 EPSILON CT","EPSILON OWNER","5 EPSILON CT","","HAMILTON OH 45011"
"P999","999 UNRELATED","NOT DELINQUENT","999 UNRELATED","","HAMILTON OH 45011"
"P998","998 UNRELATED","NOT DELINQUENT","998 UNRELATED","","HAMILTON OH 45011"
"P997","997 UNRELATED","NOT DELINQUENT","997 UNRELATED","","HAMILTON OH 45011"
"""
    records = fetch_butler(
        csv_override_text=delinquent, owners_override_text=owners,
    )
    assert len(records) == 5
    # Every emitted record has owner + address populated
    assert all(r.address for r in records)
    assert all(r.owner_name for r in records)
    assert all(r.owner_street for r in records)
    # Parcel IDs come back in delinquent-CSV order
    assert [r.parcel_id for r in records] == [
        "P001", "P002", "P003", "P004", "P005",
    ]


def test_butler_owners_join_emits_minimal_record_for_unmatched_parcels():
    """A delinquent parcel with no owners-row match still emits a record.

    Downstream enrichment can attempt its own address lookup — silent
    drops would hide real revenue.
    """
    delinquent = """\
PARID,CURRENTYEARDUE,LUC,PRIORYEARDUE
"GHOST-PARCEL","100.00","500","50.00"
"""
    records = fetch_butler(
        csv_override_text=delinquent, owners_override_text="",
    )
    assert len(records) == 1
    r = records[0]
    assert r.parcel_id == "GHOST-PARCEL"
    assert r.tax_delinquent_amount == "150.00"
    assert r.address == ""  # no owners-row match
    assert r.notice_type == "tax_delinquent"
    assert r.county == "Butler"


def test_butler_owners_join_populates_mailing_address():
    """MAILADR1 / MAILADR2 / MAILADR3 → owner_street / owner_city / owner_state / owner_zip."""
    records = fetch_butler(
        csv_override_text=DELINQUENT_CSV,
        owners_override_text=OWNERS_CSV,
    )
    smith = next(r for r in records if r.parcel_id == "M5400-013-000-118")
    assert smith.owner_street == "123 MAIN ST"
    assert smith.owner_city == "Hamilton"
    assert smith.owner_state == "OH"
    assert smith.owner_zip == "45011"


def test_butler_join_collapses_location_whitespace():
    """Butler Owners CSV emits "123  MAIN ST" (double space) — fix it."""
    records = fetch_butler(
        csv_override_text=DELINQUENT_CSV,
        owners_override_text=OWNERS_CSV,
    )
    smith = next(r for r in records if r.parcel_id == "M5400-013-000-118")
    assert smith.address == "123 MAIN ST"  # collapsed


# ── Absentee detection ────────────────────────────────────────────────


def test_butler_absentee_owner_occupied():
    """MAILADR1 matches LOCATION street → owner-occupied → absentee_owner blank."""
    records = fetch_butler(
        csv_override_text=DELINQUENT_CSV,
        owners_override_text=OWNERS_CSV,
    )
    # M5400-013-000-118: property at 123 MAIN ST, mail at 123 MAIN ST
    smith = next(r for r in records if r.parcel_id == "M5400-013-000-118")
    assert smith.absentee_owner == ""


def test_butler_absentee_owner_po_box():
    """PO Box mailing → always absentee (an owner can't live in a PO box)."""
    records = fetch_butler(
        csv_override_text=DELINQUENT_CSV,
        owners_override_text=OWNERS_CSV,
    )
    # M5400-013-000-119: property at 456 OAK AVE, mail at PO BOX 1234
    doe = next(r for r in records if r.parcel_id == "M5400-013-000-119")
    assert doe.absentee_owner == "Y"


def test_butler_absentee_different_street():
    """Mailing street ≠ property street → absentee."""
    delinquent = """\
PARID,CURRENTYEARDUE,LUC,PRIORYEARDUE
"P100","100.00","500","0"
"""
    owners = """\
PARCEL,LOCATION,OWNER1,MAILADR1,MAILADR2,MAILADR3
"P100","123 MAIN ST","ABSENT OWNER","999 ELSEWHERE BLVD","","CINCINNATI OH 45202"
"""
    records = fetch_butler(
        csv_override_text=delinquent, owners_override_text=owners,
    )
    assert records[0].absentee_owner == "Y"


def test_butler_absentee_field_flows_to_datasift_tag():
    """A record with absentee_owner='Y' must produce the absentee_owner Tag.

    Verifies the field crosses the boundary from the adapter into the
    DataSift Tags column. End-to-end check that the wiring isn't
    silently dropped.
    """
    from datasift_formatter import _build_tags
    delinquent = """\
PARID,CURRENTYEARDUE,LUC,PRIORYEARDUE
"P200","100.00","500","0"
"""
    owners = """\
PARCEL,LOCATION,OWNER1,MAILADR1,MAILADR2,MAILADR3
"P200","456 OAK AVE","ABSENT OWNER","999 ELSEWHERE BLVD","","CINCINNATI OH 45202"
"""
    records = fetch_butler(
        csv_override_text=delinquent, owners_override_text=owners,
    )
    tags = _build_tags(records[0])
    assert "absentee_owner" in tags
    assert "tax_delinquent" in tags
    assert "butler" in tags


def test_butler_absentee_normalizes_road_vs_rd():
    """``MAIN ROAD`` and ``MAIN RD`` are the same — not absentee."""
    assert _normalize_street("123 MAIN ROAD") == _normalize_street("123 MAIN RD")
    assert _is_absentee("123 MAIN ROAD", "123 MAIN RD") == ""


def test_butler_absentee_normalizes_apt_suffix():
    """Apartment numbers shouldn't trigger absentee."""
    assert _is_absentee("123 MAIN ST", "123 MAIN ST APT 5B") == ""


def test_butler_absentee_blank_when_one_side_missing():
    """If we lack either side, we don't guess."""
    assert _is_absentee("", "123 MAIN ST") == ""
    assert _is_absentee("123 MAIN ST", "") == ""


# ── Mail-line splitter ────────────────────────────────────────────────


def test_butler_split_mail_lines_three_lines():
    """Standard Butler shape: street / blank / city-state-zip."""
    street, city, state, zip_ = _split_mail_lines([
        "123 MAIN ST", "", "HAMILTON OH 45011-2403",
    ])
    assert street == "123 MAIN ST"
    assert city == "Hamilton"
    assert state == "OH"
    assert zip_ == "45011-2403"


def test_butler_split_mail_lines_two_lines():
    """Shorter shape: street / city-state-zip (no blank middle line)."""
    street, city, state, zip_ = _split_mail_lines([
        "456 OAK AVE", "MIDDLETOWN OH 45044",
    ])
    assert street == "456 OAK AVE"
    assert city == "Middletown"
    assert zip_ == "45044"


def test_butler_split_mail_lines_po_box():
    """PO Box on line 1; city-state-zip on line 3."""
    street, city, state, zip_ = _split_mail_lines([
        "PO BOX 1234", "", "CHICAGO IL 60601",
    ])
    assert street == "PO BOX 1234"
    assert city == "Chicago"
    assert state == "IL"
    assert zip_ == "60601"


# ── Dispatcher routing ────────────────────────────────────────────────


def test_dispatcher_butler_resolves():
    """fetch_ohio_tax_delinquent('Butler') must route to fetch_butler.

    Without ctx or override text, fetch_butler raises ValueError —
    proves the dispatch reached the right function.
    """
    with pytest.raises(ValueError, match="ctx="):
        fetch_ohio_tax_delinquent("Butler")


def test_dispatcher_unknown_county_raises():
    with pytest.raises(ValueError, match="Unknown Ohio"):
        fetch_ohio_tax_delinquent("Hamilton")


# ── Clermont deliberate stub ──────────────────────────────────────────


def test_clermont_raises_not_implemented():
    """Clermont's adapter must raise NotImplementedError — not a silent empty list.

    The full real-estate delinquent list isn't online (newspaper only),
    so we deliberately refuse to ship a half-implementation. Callers
    catch this exception and log; the run continues with other counties.
    """
    with pytest.raises(NotImplementedError, match=r"Clermont"):
        fetch_clermont()


# ── Config wiring ─────────────────────────────────────────────────────


def test_butler_is_in_saved_searches():
    """Butler must appear in SAVED_SEARCHES with the Ohio sentinel.

    This is the contract between config.py and the scrape_all
    dispatcher — if Butler is missing here, the daily run won't pick
    it up.
    """
    from config import (
        OHIO_AUDITOR_SENTINEL_PREFIX,
        OHIO_TAX_DELINQUENT_COUNTIES,
        SAVED_SEARCHES,
    )
    butler_entries = [
        s for s in SAVED_SEARCHES
        if s.county == "Butler" and s.notice_type == "tax_delinquent"
    ]
    assert len(butler_entries) == 1
    s = butler_entries[0]
    assert s.saved_search_name.startswith(OHIO_AUDITOR_SENTINEL_PREFIX)
    assert s.saved_search_name == "ohio_auditor:butler"
    assert "Butler" in OHIO_TAX_DELINQUENT_COUNTIES


def test_all_7_ohio_counties_are_in_saved_searches():
    """The full set must be wired up — including Clermont (stub)."""
    from config import OHIO_TAX_DELINQUENT_COUNTIES, SAVED_SEARCHES
    ohio_counties_in_searches = {
        s.county for s in SAVED_SEARCHES
        if s.notice_type == "tax_delinquent"
    }
    assert set(OHIO_TAX_DELINQUENT_COUNTIES) == ohio_counties_in_searches
    assert len(OHIO_TAX_DELINQUENT_COUNTIES) == 7


# ── Full-flow override path ───────────────────────────────────────────


def test_fetch_butler_full_flow_via_overrides():
    """End-to-end via override text — no Playwright, no network.

    The sync path is what the test suite exercises in CI. The async
    path is exercised only by live dry-runs.
    """
    records = fetch_butler(
        csv_override_text=DELINQUENT_CSV,
        owners_override_text=OWNERS_CSV,
    )
    assert len(records) == 3
    assert all(r.notice_type == "tax_delinquent" for r in records)
    assert all(r.county == "Butler" for r in records)
    assert all(r.state == "OH" for r in records)
    # All records have parcel_id
    assert all(r.parcel_id for r in records)
    # First two have full address + owner; third has $0 due (still emitted)
    smith = records[0]
    assert smith.address == "123 MAIN ST"
    assert smith.owner_name == "SMITH JOHN E"
    assert smith.tax_delinquent_amount == "3646.17"  # 1245.67 + 2400.50
