"""Tests for h3.notice_data_bridge — CaseRecord/ProbateRecord →
NoticeData converters."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from h3.notice_data_bridge import (
    _parse_combined_address,
    _to_iso_date,
    case_record_to_notice_data,
    probate_record_to_notice_data,
)
from h3.output_writers.h3_format import CaseRecord, Defendant
from h3.output_writers.probate_format import ProbateRecord


# ── _to_iso_date ──────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("03/15/2025",   "2025-03-15"),
    ("12/31/2024",   "2024-12-31"),
    ("2025-03-15",   "2025-03-15"),   # already ISO
    ("3-15-2025",    "2025-03-15"),   # M-D-YYYY variant
    ("",             ""),
    ("   ",          ""),
    ("not a date",   ""),
    ("13/45/2025",   ""),             # invalid components → empty
])
def test_to_iso_date_handles_formats_and_junk(raw, expected):
    assert _to_iso_date(raw) == expected


# ── _parse_combined_address ───────────────────────────────────────────


def test_parse_combined_address_standard_format():
    out = _parse_combined_address("123 OAK ST, DAYTON, OH 45403")
    assert out == ("123 OAK ST", "DAYTON", "OH", "45403")


def test_parse_combined_address_with_zip_plus_4():
    out = _parse_combined_address("123 OAK ST, DAYTON, OH 45403-1234")
    assert out == ("123 OAK ST", "DAYTON", "OH", "45403-1234")


def test_parse_combined_address_with_apartment_in_street():
    out = _parse_combined_address("123 OAK ST APT 5, DAYTON, OH 45403")
    assert out == ("123 OAK ST APT 5", "DAYTON", "OH", "45403")


def test_parse_combined_address_unparseable_keeps_raw_in_street():
    """If the address can't be split, keep the raw string in the
    street slot so downstream Smarty has something to standardize."""
    raw = "incomplete address with no zip"
    out = _parse_combined_address(raw)
    assert out == (raw, "", "", "")


def test_parse_combined_address_empty_returns_blanks():
    assert _parse_combined_address("") == ("", "", "", "")
    assert _parse_combined_address("   ") == ("", "", "", "")


# ── case_record_to_notice_data — foreclosure ──────────────────────────


def test_case_record_emits_one_notice_per_defendant():
    rec = CaseRecord(
        case_number="2025 CV 12345",
        filing_type="COMPLAINT FOR FORECLOSURE",
        date_filed="03/15/2025",
        defendants=[
            Defendant(name="SMITH JOHN", street="123 OAK ST",
                      city="DAYTON", state="OH", zip="45403"),
            Defendant(name="SMITH JANE", street="123 OAK ST",
                      city="DAYTON", state="OH", zip="45403"),
        ],
        property_street="123 OAK ST", property_city="DAYTON",
        property_state="OH", property_zip="45403",
    )
    notices = case_record_to_notice_data(rec, "Montgomery")
    assert len(notices) == 2
    assert notices[0].owner_name == "SMITH JOHN"
    assert notices[1].owner_name == "SMITH JANE"


def test_case_record_carries_property_block_to_every_row():
    rec = CaseRecord(
        case_number="2025 CV 1",
        defendants=[Defendant(name=f"D{i}") for i in range(3)],
        property_street="42 MAIN ST", property_city="KETTERING",
        property_state="OH", property_zip="45429",
    )
    notices = case_record_to_notice_data(rec, "Montgomery")
    assert len(notices) == 3
    for n in notices:
        assert n.address == "42 MAIN ST"
        assert n.city == "KETTERING"
        assert n.zip == "45429"
        assert n.county == "Montgomery"
        assert n.state == "OH"
        assert n.notice_type == "foreclosure"


def test_case_record_owner_mailing_distinct_from_property_for_absentee():
    """Absentee owner: mailing addr (defendant.street etc.) ≠ property."""
    rec = CaseRecord(
        case_number="2025 CV 1",
        defendants=[Defendant(
            name="OWNER OUT", street="999 N AVE",
            city="LOS ANGELES", state="CA", zip="90001",
        )],
        property_street="42 MAIN ST", property_city="KETTERING",
        property_state="OH", property_zip="45429",
        absentee_owner="Y",
    )
    n = case_record_to_notice_data(rec, "Montgomery")[0]
    # Property block — Kettering
    assert n.address == "42 MAIN ST"
    assert n.city == "KETTERING"
    # Owner mailing block — LA
    assert n.owner_street == "999 N AVE"
    assert n.owner_city == "LOS ANGELES"
    assert n.owner_state == "CA"
    assert n.absentee_owner == "Y"


def test_case_record_propagates_unknown_heirs_to_decedent_name():
    """heirs_unknown=Y → owner_deceased='yes' + decedent_name carried."""
    rec = CaseRecord(
        case_number="2025 CV 1",
        defendants=[Defendant(name="UNKNOWN HEIRS OF MICHAEL D. JOHNSON DECEASED")],
        property_street="1 ELM", property_city="DAYTON",
        property_state="OH", property_zip="45403",
        heirs_unknown="Y",
        heirs_unknown_decedent="MICHAEL D. JOHNSON",
    )
    n = case_record_to_notice_data(rec, "Montgomery")[0]
    assert n.owner_deceased == "yes"
    assert n.decedent_name == "MICHAEL D. JOHNSON"


def test_case_record_with_no_defendants_emits_property_only_row():
    """Edge case: stub CaseRecord with no parties → still emit one row
    (property-only) so downstream pipeline sees something."""
    rec = CaseRecord(
        case_number="2025 CV 999",
        property_street="42 NOWHERE", property_city="DAYTON",
        property_state="OH", property_zip="45403",
        notes="Defendants unrecoverable",
    )
    notices = case_record_to_notice_data(rec, "Montgomery")
    assert len(notices) == 1
    assert notices[0].owner_name == ""
    assert notices[0].address == "42 NOWHERE"
    assert notices[0].raw_text == "Defendants unrecoverable"


def test_case_record_normalises_date_to_iso():
    rec = CaseRecord(
        case_number="X",
        date_filed="03/15/2025",
        defendants=[Defendant(name="X")],
    )
    n = case_record_to_notice_data(rec, "Montgomery")[0]
    assert n.date_added == "2025-03-15"


def test_case_record_threads_source_url():
    rec = CaseRecord(case_number="X", defendants=[Defendant(name="X")])
    n = case_record_to_notice_data(
        rec, "Montgomery", source_url="https://pro.mcohio.org/...",
    )[0]
    assert n.source_url == "https://pro.mcohio.org/..."


def test_case_record_county_label_is_carried():
    """Each county passes its own name — bridge does not infer."""
    rec = CaseRecord(case_number="X", defendants=[Defendant(name="X")])
    assert case_record_to_notice_data(rec, "Warren")[0].county == "Warren"
    assert case_record_to_notice_data(rec, "Clark")[0].county == "Clark"


# ── probate_record_to_notice_data ─────────────────────────────────────


def test_probate_record_maps_fiduciary_to_owner():
    """The PR/executor is the contact, not the decedent."""
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
    n = probate_record_to_notice_data(rec, "Greene")
    # Property address ← subject_property
    assert n.address == "888 OAK AVE"
    assert n.city == "DAYTON"
    assert n.zip == "45405"
    # Contact ← fiduciary
    assert n.owner_name == "MARY ROBERTS"
    assert n.owner_street == "55 ELM ST"
    assert n.owner_city == "KETTERING"
    assert n.owner_zip == "45429"
    # Probate-specific identifiers
    assert n.decedent_name == "HENRY M. ROBERTS"
    assert n.date_of_death == "2024-12-15"
    assert n.owner_deceased == "yes"
    assert n.notice_type == "probate"


def test_probate_record_populates_dm_fields_for_obituary_preset():
    """Pre-population mirrors the obituary-enricher's probate preset
    so the search/heir-verification step short-circuits when we
    already know the court-named PR."""
    rec = ProbateRecord(
        decedent_name="X",
        fiduciary_name="JANE EXECUTOR",
        fiduciary_address="1 A ST, DAYTON, OH 45403",
        relationship="SPOUSE",
    )
    n = probate_record_to_notice_data(rec, "Greene")
    assert n.decision_maker_name == "JANE EXECUTOR"
    assert n.decision_maker_relationship == "SPOUSE"
    assert n.decision_maker_status == "verified_living"
    assert n.decision_maker_source == "probate_notice"
    assert n.decision_maker_street == "1 A ST"
    assert n.decision_maker_city == "DAYTON"
    assert n.decision_maker_state == "OH"
    assert n.decision_maker_zip == "45403"


def test_probate_record_handles_missing_subject_property():
    """Some probate cases don't list real estate — property fields stay
    blank but the rest of the record still produces a valid NoticeData."""
    rec = ProbateRecord(
        case_number="2025 PR 200",
        decedent_name="X",
        fiduciary_name="Y",
        fiduciary_address="",
        subject_property="",
    )
    n = probate_record_to_notice_data(rec, "Greene")
    assert n.address == ""
    assert n.city == ""
    assert n.zip == ""
    assert n.notice_type == "probate"


def test_probate_record_normalises_dates_to_iso():
    """ProbateRecord docstring says ISO but some adapters drift —
    defensively normalize through _to_iso_date."""
    rec = ProbateRecord(
        date_filed="04/01/2025",
        date_of_death="12/15/2024",
        decedent_name="X",
        fiduciary_name="Y",
    )
    n = probate_record_to_notice_data(rec, "Greene")
    assert n.date_added == "2025-04-01"
    assert n.date_of_death == "2024-12-15"


def test_probate_record_threads_source_url():
    rec = ProbateRecord(decedent_name="X", fiduciary_name="Y")
    n = probate_record_to_notice_data(
        rec, "Greene", source_url="https://courts.greenecountyohio.gov/...",
    )
    assert n.source_url == "https://courts.greenecountyohio.gov/..."


def test_probate_record_defaults_state_to_oh_when_fiduciary_state_missing():
    """If fiduciary_address didn't parse a state, default to OH (we
    only scrape OH counties for now)."""
    rec = ProbateRecord(
        decedent_name="X",
        fiduciary_name="Y",
        fiduciary_address="55 ELM ST",  # no city/state/zip
    )
    n = probate_record_to_notice_data(rec, "Greene")
    assert n.owner_state == "OH"
    assert n.decision_maker_state == "OH"
