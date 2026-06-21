"""Tests for h3.integration.integrate_equivant_foreclosure — the
shared CourtView integration path used by Butler / Clark / Clermont /
Greene / Miami foreclosure adapters.

Strategy: build fake equivant CaseDetailCapture objects (shape:
``case_number, html, ...``) and monkeypatch the per-county
``parse_case_detail_html`` + ``_looks_like_person`` + the BS4-based
party-address finders. We test the integration LOGIC (defendant
ordering, address resolution, action-based filter, decedent handling),
not the bs4 parsers themselves.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from h3.integration import (
    _EQUIVANT_COUNTIES,
    _equivant_iso_to_us_date,
    _resolve_equivant_property_address,
    integrate_equivant_foreclosure,
)
from h3.output_writers.h3_format import CaseRecord


# ── Shared fakes ─────────────────────────────────────────────────────


@dataclass
class FakeEquivantCapture:
    """Mimics each county's CaseDetailCapture (case_number + html)."""
    case_number: str
    html: str = "<html><body><div id='someparent'/></body></html>"


@dataclass
class FakeCountyDetail:
    """Mimics <County>CaseDetail dataclass shape uniform across all 5."""
    case_number: str = ""
    case_type: str = ""
    file_date: str = ""           # ISO (parser returns ISO)
    plaintiff: str = ""
    defendants: list = field(default_factory=list)
    attorney: str = ""
    action: str = ""              # primary safety-filter signal
    decedent: str = ""

    @property
    def primary_owner(self) -> str:
        """In real <County>CaseDetail this picks the first non-decedent
        real-person defendant. For tests we just take defendants[0]."""
        return self.defendants[0] if self.defendants else ""


@dataclass
class FakePartyAddress:
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""

    def is_empty(self) -> bool:
        return not (self.street or self.city or self.zip)


def _patch_equivant(monkeypatch, county: str, *,
                    detail: FakeCountyDetail,
                    owner_addr: FakePartyAddress | None = None):
    """Install fakes for the parse + party-address + _looks_like_person
    chain so the integrator runs deterministically."""
    import h3.integration as integ
    import types

    # The integrator imports the per-county module via importlib —
    # replace it in sys.modules with a SimpleNamespace (no bound-method
    # trap — lambdas attached to a class become methods that bind self).
    cfg = _EQUIVANT_COUNTIES[county]
    fake_mod = types.SimpleNamespace(
        parse_case_detail_html=lambda _html, _d=detail: _d,
        _looks_like_person=lambda name: not (
            name.upper().endswith(" LLC") or name.upper().endswith(" INC")
            or "BANK" in name.upper() or "TRUST" in name.upper()
        ),
    )
    monkeypatch.setitem(sys.modules, cfg["module"], fake_mod)

    # The BS4-based address finders — monkeypatch at the integration
    # module level (they're imported at the top of h3.integration).
    if owner_addr is not None:
        monkeypatch.setattr(integ, "find_owner_address",
                            lambda soup, owner: owner_addr)
        monkeypatch.setattr(integ, "find_owner_address_ptyinfo",
                            lambda soup, owner: owner_addr)
    else:
        empty = FakePartyAddress()
        monkeypatch.setattr(integ, "find_owner_address",
                            lambda soup, owner: empty)
        monkeypatch.setattr(integ, "find_owner_address_ptyinfo",
                            lambda soup, owner: empty)


# ── ISO → US date conversion ─────────────────────────────────────────


@pytest.mark.parametrize("iso,us", [
    ("2026-06-09", "06/09/2026"),
    ("2025-12-31", "12/31/2025"),
    ("",           ""),
    ("not a date", "not a date"),   # passes through on parse failure
    ("06/09/2026", "06/09/2026"),   # already US — falls through
])
def test_equivant_iso_to_us_date(iso, us):
    assert _equivant_iso_to_us_date(iso) == us


# ── Property-address resolver (state-level heuristic) ────────────────


def test_resolve_property_in_state_oh_returns_owner_mailing():
    addr = FakePartyAddress("123 OAK ST", "DAYTON", "OH", "45403")
    s, c, st, z, abs_, lookup = _resolve_equivant_property_address(addr)
    assert (s, c, st, z) == ("123 OAK ST", "DAYTON", "OH", "45403")
    assert abs_ == "N"
    assert lookup == "N"


def test_resolve_property_out_of_state_flags_absentee_and_blank_property():
    addr = FakePartyAddress("999 N AVE", "MIAMI", "FL", "33101")
    s, c, st, z, abs_, lookup = _resolve_equivant_property_address(addr)
    assert (s, c, st, z) == ("", "", "", "")   # blank — needs Auditor lookup
    assert abs_ == "Y"
    assert lookup == "Y"


def test_resolve_property_no_owner_addr_returns_blanks_with_lookup_y():
    s, c, st, z, abs_, lookup = _resolve_equivant_property_address(None)
    assert (s, c, st, z) == ("", "", "", "")
    assert abs_ == ""
    assert lookup == "Y"


# ── integrate_equivant_foreclosure — end-to-end with fakes ───────────


@pytest.mark.parametrize("county", ["butler", "clark", "clermont",
                                     "greene", "miami"])
def test_equivant_happy_path_in_state(monkeypatch, county):
    """In-state owner → property block populated, absentee=N."""
    detail = FakeCountyDetail(
        case_number="2026 CV 00001",
        file_date="2026-06-10",
        action="Foreclosures",
        defendants=["SMITH JOHN", "SMITH JANE"],
        plaintiff="WELLS FARGO BANK NA",
        attorney="JANE LAWYER",
    )
    addr = FakePartyAddress("123 OAK ST", "DAYTON", "OH", "45403")
    _patch_equivant(monkeypatch, county, detail=detail, owner_addr=addr)

    cap = FakeEquivantCapture(case_number="2026 CV 00001")
    out = integrate_equivant_foreclosure([cap], county)
    assert len(out) == 1
    r = out[0]
    assert r.case_number == "2026 CV 00001"
    assert r.filing_type == "Foreclosures"
    assert r.date_filed == "06/10/2026"
    assert r.property_street == "123 OAK ST"
    assert r.property_state == "OH"
    assert r.absentee_owner == "N"
    assert r.needs_property_lookup == "N"
    # Primary defendant carries the address; co-def gets blanks
    assert r.defendants[0].name == "SMITH JOHN"
    assert r.defendants[0].street == "123 OAK ST"
    assert r.defendants[1].name == "SMITH JANE"
    assert r.defendants[1].street == ""
    # Notes carry plaintiff + attorney
    assert "WELLS FARGO BANK NA" in r.notes
    assert "JANE LAWYER" in r.notes


def test_equivant_action_filter_drops_non_foreclosure(monkeypatch):
    """Action='PERSONAL INJURY' → dropped (search leakage safety filter)."""
    detail = FakeCountyDetail(
        case_number="2026 CV 99999",
        action="PERSONAL INJURY - AUTO",
        defendants=["SMITH JOHN"],
    )
    _patch_equivant(monkeypatch, "clermont", detail=detail,
                    owner_addr=FakePartyAddress("1 ELM", "DAYTON", "OH", "45403"))
    out = integrate_equivant_foreclosure(
        [FakeEquivantCapture("2026 CV 99999")], "clermont")
    assert out == []


def test_equivant_action_filter_keeps_when_action_blank(monkeypatch):
    """Empty Action → keep (parser couldn't extract it; don't drop)."""
    detail = FakeCountyDetail(
        case_number="2026 CV 00002",
        action="",  # blank
        case_type="CV - General",
        defendants=["SMITH JOHN"],
    )
    _patch_equivant(monkeypatch, "clermont", detail=detail,
                    owner_addr=FakePartyAddress("1 ELM", "DAYTON", "OH", "45403"))
    out = integrate_equivant_foreclosure(
        [FakeEquivantCapture("2026 CV 00002")], "clermont")
    assert len(out) == 1
    # filing_type falls through to case_type when action is blank
    assert out[0].filing_type == "CV - General"


def test_equivant_out_of_state_owner_flags_absentee(monkeypatch):
    """Owner mailing in FL → absentee=Y + property block blank."""
    detail = FakeCountyDetail(
        case_number="2026 CV 00003",
        action="Foreclosures",
        defendants=["OUT OF STATE OWNER"],
    )
    addr = FakePartyAddress("999 BISCAYNE", "MIAMI", "FL", "33101")
    _patch_equivant(monkeypatch, "miami", detail=detail, owner_addr=addr)
    out = integrate_equivant_foreclosure(
        [FakeEquivantCapture("2026 CV 00003")], "miami")
    assert len(out) == 1
    r = out[0]
    assert r.absentee_owner == "Y"
    assert r.needs_property_lookup == "Y"
    assert r.property_street == ""    # cleared — needs Auditor lookup
    # Owner mailing still attached to the defendant for outreach
    assert r.defendants[0].state == "FL"


def test_equivant_decedent_flags_unknown_heirs(monkeypatch):
    """detail.decedent populated → heirs_unknown=Y + decedent passed through."""
    detail = FakeCountyDetail(
        case_number="2026 CV 00004",
        action="Foreclosures",
        defendants=["UNKNOWN HEIRS OF JANE DOE DECEASED",
                    "JOHN HEIR"],
        decedent="JANE DOE",
    )
    _patch_equivant(monkeypatch, "butler", detail=detail,
                    owner_addr=FakePartyAddress("1 ELM", "DAYTON", "OH", "45403"))
    out = integrate_equivant_foreclosure(
        [FakeEquivantCapture("2026 CV 00004")], "butler")
    assert len(out) == 1
    assert out[0].heirs_unknown == "Y"
    assert out[0].heirs_unknown_decedent == "JANE DOE"


def test_equivant_skips_capture_with_montgomery_screens_shape(monkeypatch):
    """Defensive: a Montgomery capture (has .screens attribute) leaked
    into the equivant integrator should be skipped, not crash."""
    class MontgomeryStyleCapture:
        case_number = "X"
        screens = []   # presence of attribute is what matters
    detail = FakeCountyDetail(defendants=["SMITH"])
    _patch_equivant(monkeypatch, "butler", detail=detail)
    out = integrate_equivant_foreclosure(
        [MontgomeryStyleCapture()], "butler",
    )
    assert out == []


def test_equivant_skips_empty_html(monkeypatch):
    """Empty HTML (capture failed mid-scrape) → skip + log."""
    detail = FakeCountyDetail()
    _patch_equivant(monkeypatch, "butler", detail=detail)
    out = integrate_equivant_foreclosure(
        [FakeEquivantCapture(case_number="X", html="")], "butler",
    )
    assert out == []


def test_equivant_skips_capture_when_parse_raises(monkeypatch):
    """Parser exception (malformed HTML) → log + skip, no crash."""
    import h3.integration as integ
    import types
    cfg = _EQUIVANT_COUNTIES["clark"]
    def boom(_h): raise ValueError("broken HTML")
    fake_mod = types.SimpleNamespace(
        parse_case_detail_html=boom,
        _looks_like_person=lambda _n: True,
    )
    monkeypatch.setitem(sys.modules, cfg["module"], fake_mod)
    out = integrate_equivant_foreclosure(
        [FakeEquivantCapture("X")], "clark")
    assert out == []


def test_equivant_unsupported_county_raises():
    with pytest.raises(ValueError, match="unsupported county"):
        integrate_equivant_foreclosure([], "hamilton")


# ── Adapter override path — confirm all 5 wired correctly ────────────


@pytest.mark.parametrize("county_lower", ["butler", "clark", "clermont",
                                           "greene", "miami"])
def test_each_equivant_adapter_override_path_returns_noticedata(
    monkeypatch, county_lower,
):
    """fetch_<county>_foreclosure(override_case_details=...) wires
    through to integrate_equivant_foreclosure + bridge."""
    from ohio_foreclosure_scrapers import _DISPATCH

    detail = FakeCountyDetail(
        case_number="2026 CV 12345",
        file_date="2026-06-10",
        action="Foreclosures",
        defendants=["SMITH JOHN", "SMITH JANE"],
    )
    addr = FakePartyAddress("123 OAK ST", "DAYTON", "OH", "45403")
    _patch_equivant(monkeypatch, county_lower, detail=detail, owner_addr=addr)

    fetcher = _DISPATCH[county_lower]
    result = fetcher(override_case_details=[FakeEquivantCapture("2026 CV 12345")])
    assert len(result) == 2
    assert all(r.notice_type == "foreclosure" for r in result)
    # Bridge applies county capitalisation
    assert result[0].county == county_lower.capitalize()
    assert result[0].state == "OH"
    assert result[0].owner_name == "SMITH JOHN"
    # source_url threaded from registry
    assert result[0].source_url.startswith("https://")
