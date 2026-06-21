"""Tests for h3.integration — the SiftStack-native port of H3's
``main.py:_integrate_cases``.

Strategy: drive the integration logic with fake ``CaseDetailCapture``
objects and monkeypatched parsers. Each test exercises a specific
behaviour of the integration layer (filtering, address resolution,
unknown-heir detection, service-tab fallback, etc.) without paying
the cost of bs4-parsing realistic HTML — the parsers have their own
implicit coverage via the H3 production pipeline.

The fakes mirror the dataclasses defined in ``h3.scrapers.mcohio``:
``CaseScreenCapture``, ``DocketEntry``, ``PdfDownload``,
``CaseDetailCapture``. We import the originals where shape matches the
real production input — that way a future refactor of the dataclass
shape catches the tests with a real import error rather than passing
silently against a stale fake.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from h3.integration import (
    _MC_CITIES,
    _is_unknown_heir,
    _resolve_property_address,
    _synthesize_parties_from_service_tab,
    extract_probate_records,
    integrate_montgomery_foreclosure,
)
from h3.output_writers.h3_format import CaseRecord, Defendant
from h3.parsers.party_tab import PartyEntry
from h3.parsers.service_tab import ServiceEvent
from h3.scrapers.mcohio import (
    CaseDetailCapture,
    CaseScreenCapture,
    DocketEntry,
    PdfDownload,
    ReconCapture,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_capture(
    *,
    case_number: str = "2025 CV 00001",
    party_html: str | None = "<html/>",
    service_html: str | None = "",
    docket_entries: list[DocketEntry] | None = None,
    pdfs: list[PdfDownload] | None = None,
) -> CaseDetailCapture:
    """Build a CaseDetailCapture with the screens/docket/pdfs we want.

    Only the *presence* of HTML matters here — the parsers are
    monkeypatched in each test. Pass ``party_html=None`` to omit the
    party screen entirely.
    """
    screens: list[CaseScreenCapture] = []
    if party_html is not None:
        screens.append(CaseScreenCapture(
            screen="party", final_url="", html=party_html,
        ))
    if service_html:
        screens.append(CaseScreenCapture(
            screen="service", final_url="", html=service_html,
        ))
    return CaseDetailCapture(
        case_number=case_number,
        case_id="abc-123",
        screens=screens,
        docket_entries=docket_entries or [],
        pdfs=pdfs or [],
    )


def _party(name, role="DEFENDANT", street="", city="", state="", zip="",
           is_primary=False):
    """Build a PartyEntry-shaped dict that mimics filter_defendants' output.

    ``filter_defendants`` enriches each PartyEntry with an ``is_primary``
    boolean — the integration code reads ``getattr(f, 'is_primary', False)``,
    so we replicate that here with a SimpleNamespace-ish dataclass.
    """
    p = PartyEntry(
        name=name, role=role, street=street, city=city,
        state=state, zip=zip,
    )
    # filter_defendants attaches this attribute post-hoc; mirror that.
    p.is_primary = is_primary  # type: ignore[attr-defined]
    return p


# ── _is_unknown_heir ──────────────────────────────────────────────────


@pytest.mark.parametrize("name,expected", [
    ("UNKNOWN HEIRS OF JOHN SMITH",            True),
    ("UNKNOWN HEIRS, DEVISEES OF JANE DOE",    True),
    ("HEIRS OF MARY ROBERTS DECEASED",         True),
    ("HEIRS, LEGATEES OF ALAN T. BAKER",       True),
    ("JOHN DOE",                                True),
    ("JANE DOE",                                True),
    ("REGULAR PERSON",                          False),
    ("KOHLRIESER PATRICK D",                    False),
    ("DOE EVICTIONS LLC",                       False),   # not a placeholder
    ("",                                        False),
    (None,                                      False),
])
def test_is_unknown_heir_recognises_all_variants(name, expected):
    assert _is_unknown_heir(name) is expected


# ── _MC_CITIES static check ───────────────────────────────────────────


def test_montgomery_cities_set_includes_core_cities():
    """Smoke check — the in-county heuristic must include DAYTON +
    suburbs. Missing entries silently route owners to absentee."""
    for required in ("DAYTON", "KETTERING", "HUBER HEIGHTS", "TROTWOOD",
                     "MIAMISBURG", "CENTERVILLE", "ENGLEWOOD", "OAKWOOD"):
        assert required in _MC_CITIES


# ── _resolve_property_address ─────────────────────────────────────────


def test_resolve_address_owner_in_county_returns_owner_mailing():
    """In-county owner → property address = owner mailing, absentee=N."""
    filtered = [
        _party("SMITH JOHN", street="123 OAK ST", city="DAYTON",
               state="OH", zip="45403", is_primary=True),
        _party("SMITH JANE", street="123 OAK ST", city="DAYTON",
               state="OH", zip="45403"),
    ]
    s, c, st, z, abs_, lookup = _resolve_property_address(filtered)
    assert (s, c, st, z) == ("123 OAK ST", "DAYTON", "OH", "45403")
    assert abs_ == "N"
    assert lookup == "N"


def test_resolve_address_owner_out_of_state_falls_to_in_county_codef():
    """Absentee owner + in-county co-defendant → use co-def's address,
    flag absentee=Y, needs_lookup=Y (still a best guess)."""
    filtered = [
        _party("OWNER OUT", street="999 N AVE", city="MIAMI",
               state="FL", zip="33101", is_primary=True),
        _party("OCCUPANT IN", street="55 ELM ST", city="KETTERING",
               state="OH", zip="45429"),
    ]
    s, c, st, z, abs_, lookup = _resolve_property_address(filtered)
    assert (s, c, st, z) == ("55 ELM ST", "KETTERING", "OH", "45429")
    assert abs_ == "Y"
    assert lookup == "Y"


def test_resolve_address_no_in_county_codef_falls_back_to_owner():
    """No in-county candidate at all → use owner's out-of-county
    mailing as last resort. Caller must still flag absentee + lookup."""
    filtered = [
        _party("OWNER OUT", street="999 N AVE", city="MIAMI",
               state="FL", zip="33101", is_primary=True),
        _party("CODEF ALSO OUT", street="42 MAIN", city="CHICAGO",
               state="IL", zip="60601"),
    ]
    s, c, st, z, abs_, lookup = _resolve_property_address(filtered)
    assert (s, c, st, z) == ("999 N AVE", "MIAMI", "FL", "33101")
    assert abs_ == "Y"
    assert lookup == "Y"


def test_resolve_address_empty_filtered_returns_blanks():
    """No defendants → all blanks, no flags set (downstream decides)."""
    s, c, st, z, abs_, lookup = _resolve_property_address([])
    assert (s, c, st, z, abs_, lookup) == ("", "", "", "", "", "")


def test_resolve_address_primary_without_street_returns_blanks():
    """Primary defendant exists but has no mailing → blanks. The
    integration layer must not promote a partial address."""
    filtered = [
        _party("OWNER NO ADDR", is_primary=True),
        _party("CODEF NO ADDR"),
    ]
    s, c, st, z, abs_, lookup = _resolve_property_address(filtered)
    assert (s, c, st, z, abs_, lookup) == ("", "", "", "", "", "")


# ── _synthesize_parties_from_service_tab ──────────────────────────────


def test_synth_skips_government_and_corporate_entities(monkeypatch):
    """When the service-tab is the fallback source, government /
    corporate / placeholder names must be filtered out before they
    contaminate the defendant list."""
    import h3.integration as mod
    fake_events = [
        ServiceEvent(party_name="SMITH JOHN", party_street="1 ELM",
                     party_city="DAYTON", party_state="OH", party_zip="45403"),
        ServiceEvent(party_name="TREASURER OF MONTGOMERY COUNTY OHIO"),
        ServiceEvent(party_name="STATE OF OHIO DEPARTMENT OF TAXATION"),
        ServiceEvent(party_name="JANE DOE"),
        ServiceEvent(party_name="UNKNOWN SPOUSE OF SMITH JOHN"),
        ServiceEvent(party_name="ACME BANK NA"),
        ServiceEvent(party_name="SMITH JANE", party_street="1 ELM",
                     party_city="DAYTON", party_state="OH", party_zip="45403"),
    ]
    monkeypatch.setattr(mod, "parse_service_tab", lambda html: fake_events)
    parties = _synthesize_parties_from_service_tab(
        "any non-empty html", case_number="2025 CV 99999",
    )
    names = [p.name for p in parties]
    assert names == ["SMITH JOHN", "SMITH JANE"]
    # The kept ones must carry the address from the service event
    assert parties[0].street == "1 ELM"
    assert parties[0].city == "DAYTON"


def test_synth_handles_empty_html_without_exception():
    assert _synthesize_parties_from_service_tab("") == []


def test_synth_swallows_parser_exceptions(monkeypatch):
    """A broken parser must not crash the daily run — log + return []."""
    import h3.integration as mod
    def boom(_html):
        raise RuntimeError("parser exploded")
    monkeypatch.setattr(mod, "parse_service_tab", boom)
    assert _synthesize_parties_from_service_tab(
        "x", case_number="2025 CV 1") == []


def test_synth_dedupes_by_name(monkeypatch):
    """Two service events for the same defendant → one PartyEntry."""
    import h3.integration as mod
    fake_events = [
        ServiceEvent(party_name="SMITH JOHN", party_street="1 ELM"),
        ServiceEvent(party_name="SMITH JOHN", party_street="1 ELM"),
        ServiceEvent(party_name="JONES BOB", party_street="2 OAK"),
    ]
    monkeypatch.setattr(mod, "parse_service_tab", lambda html: fake_events)
    parties = _synthesize_parties_from_service_tab("x")
    assert [p.name for p in parties] == ["SMITH JOHN", "JONES BOB"]


# ── integrate_montgomery_foreclosure — end-to-end with mocked parsers ─


def _patch_parsers(
    monkeypatch,
    *,
    parties=None,
    service_events=None,
    cis=None,
    filtered_override=None,
):
    """Install fake parsers + filter so we test integration, not parsers."""
    import h3.integration as mod
    monkeypatch.setattr(
        mod, "parse_party_tab", lambda html: list(parties or []),
    )
    monkeypatch.setattr(
        mod, "parse_service_tab", lambda html: list(service_events or []),
    )
    monkeypatch.setattr(
        mod, "parse_cis", lambda bytes_: cis,
    )
    if filtered_override is not None:
        monkeypatch.setattr(
            mod, "filter_defendants",
            lambda ps, main_defendant_name="": list(filtered_override),
        )


def test_integrate_skips_capture_with_no_party_or_service_html():
    """A capture with nothing parseable should be skipped, not crash."""
    cap = _make_capture(party_html=None, service_html="")
    out = integrate_montgomery_foreclosure([cap])
    assert out == []


def test_integrate_skips_capture_without_screens_attribute():
    """Equivant/Warren captures don't have ``.screens`` — defensive skip."""
    class NotAMontgomeryCapture:
        case_number = "X"
    out = integrate_montgomery_foreclosure([NotAMontgomeryCapture()])
    assert out == []


def test_integrate_populates_case_record_from_party_tab(monkeypatch):
    """Happy path: party tab parses cleanly, address resolved in-county."""
    smith = _party("SMITH JOHN", street="123 OAK ST", city="DAYTON",
                   state="OH", zip="45403", is_primary=True)
    spouse = _party("SMITH JANE", street="123 OAK ST", city="DAYTON",
                    state="OH", zip="45403")
    _patch_parsers(
        monkeypatch,
        parties=[smith, spouse],
        filtered_override=[smith, spouse],
    )
    cap = _make_capture(
        case_number="2025 CV 12345",
        party_html="<table id=tblPartyBody></table>",
        docket_entries=[DocketEntry(
            docketid="d1", case_id="c1",
            date_filed="03/15/2025",
            document_type="COMPLAINT FOR FORECLOSURE",
            description="Plaintiff files for foreclosure",
        )],
    )
    out = integrate_montgomery_foreclosure([cap])
    assert len(out) == 1
    r = out[0]
    assert r.case_number == "2025 CV 12345"
    assert r.filing_type == "COMPLAINT FOR FORECLOSURE"
    assert r.date_filed == "03/15/2025"
    assert r.property_street == "123 OAK ST"
    assert r.property_city == "DAYTON"
    assert r.absentee_owner == "N"
    assert r.needs_property_lookup == "N"
    assert len(r.defendants) == 2
    assert r.defendants[0].name == "SMITH JOHN"
    assert r.defendants[0].street == "123 OAK ST"
    assert r.heirs_unknown == ""
    assert r.deep_prospect_source == ""


def test_integrate_falls_back_to_service_tab_when_party_empty(monkeypatch):
    """party tab returns [] → synth from service tab → flag deep_prospect."""
    smith = _party("SMITH JOHN", street="1 ELM", city="DAYTON",
                   state="OH", zip="45403")
    smith.is_primary = True
    _patch_parsers(
        monkeypatch,
        parties=[],
        service_events=[ServiceEvent(party_name="SMITH JOHN",
                                      party_street="1 ELM",
                                      party_city="DAYTON",
                                      party_state="OH",
                                      party_zip="45403")],
        filtered_override=[smith],
    )
    cap = _make_capture(
        party_html="<empty/>",
        service_html="<table id=tblServiceBody/>",
        docket_entries=[DocketEntry(
            docketid="d1", case_id="c1", date_filed="04/01/2025",
            document_type="COMPLAINT", description="",
        )],
    )
    out = integrate_montgomery_foreclosure([cap])
    assert len(out) == 1
    r = out[0]
    assert r.deep_prospect_unreachable == "Y"
    assert r.deep_prospect_source == "SERVICE_TAB"
    assert r.defendants[0].name == "SMITH JOHN"


def test_integrate_flags_unknown_heirs_with_decedent_name(monkeypatch):
    """``UNKNOWN HEIRS OF X DECEASED`` → heirs_unknown=Y + decedent
    parsed."""
    heir_placeholder = _party(
        "UNKNOWN HEIRS OF MICHAEL D. JOHNSON DECEASED",
        is_primary=True,
    )
    _patch_parsers(
        monkeypatch,
        parties=[heir_placeholder],
        filtered_override=[heir_placeholder],
    )
    cap = _make_capture(party_html="<x/>")
    out = integrate_montgomery_foreclosure([cap])
    assert len(out) == 1
    assert out[0].heirs_unknown == "Y"
    assert out[0].heirs_unknown_decedent == "MICHAEL D. JOHNSON"


def test_integrate_picks_in_county_codef_when_owner_absentee(monkeypatch):
    """Absentee owner + in-county co-defendant → property address comes
    from the co-defendant, absentee=Y."""
    owner = _party("OWNER OUT", street="999 N AVE", city="LOS ANGELES",
                   state="CA", zip="90001", is_primary=True)
    codef = _party("CODEF IN", street="42 OAK", city="KETTERING",
                   state="OH", zip="45429")
    _patch_parsers(
        monkeypatch,
        parties=[owner, codef],
        filtered_override=[owner, codef],
    )
    cap = _make_capture(party_html="<x/>")
    out = integrate_montgomery_foreclosure([cap])
    assert len(out) == 1
    r = out[0]
    assert r.property_street == "42 OAK"
    assert r.property_city == "KETTERING"
    assert r.absentee_owner == "Y"
    assert r.needs_property_lookup == "Y"


def test_integrate_uses_default_filing_type_when_no_docket(monkeypatch):
    """No docket entries → fall through to the default filing_type."""
    smith = _party("SMITH JOHN", street="1 ELM", city="DAYTON",
                   state="OH", zip="45403", is_primary=True)
    _patch_parsers(monkeypatch, parties=[smith], filtered_override=[smith])
    cap = _make_capture(party_html="<x/>", docket_entries=[])
    out = integrate_montgomery_foreclosure([cap])
    assert out[0].filing_type == "COMPLAINT FOR FORECLOSURE"
    assert out[0].date_filed == ""


def test_integrate_attaches_cis_notes(monkeypatch):
    """When the CIS PDF parses, prayer + parcel land in notes."""
    smith = _party("SMITH JOHN", street="1 ELM", city="DAYTON",
                   state="OH", zip="45403", is_primary=True)

    class FakeCIS:
        main_defendant = "SMITH JOHN"
        prayer_amount = 142_750.55
        parcel_number = "R72 04805A0027"

    _patch_parsers(
        monkeypatch,
        parties=[smith],
        cis=FakeCIS(),
        filtered_override=[smith],
    )
    cap = _make_capture(
        party_html="<x/>",
        pdfs=[PdfDownload(
            docketid="d1",
            document_type="CASE INFORMATION SHEET",
            pdf_bytes=b"%PDF-1.4 fake",
        )],
    )
    out = integrate_montgomery_foreclosure([cap])
    notes = out[0].notes
    assert "Prayer amount: $142,750.55" in notes
    assert "Parcel: R72 04805A0027" in notes


def test_integrate_cis_pdf_parse_failure_does_not_crash(monkeypatch):
    """Broken CIS PDF → log + carry on. Notes simply omit the CIS bits."""
    smith = _party("SMITH JOHN", street="1 ELM", city="DAYTON",
                   state="OH", zip="45403", is_primary=True)
    import h3.integration as mod
    monkeypatch.setattr(
        mod, "parse_party_tab", lambda html: [smith],
    )
    monkeypatch.setattr(
        mod, "parse_service_tab", lambda html: [],
    )
    monkeypatch.setattr(
        mod, "parse_cis",
        lambda b: (_ for _ in ()).throw(RuntimeError("bad PDF")),
    )
    monkeypatch.setattr(
        mod, "filter_defendants",
        lambda ps, main_defendant_name="": [smith],
    )
    cap = _make_capture(
        party_html="<x/>",
        pdfs=[PdfDownload(
            docketid="d1",
            document_type="CASE INFORMATION SHEET",
            pdf_bytes=b"%PDF-bad",
        )],
    )
    out = integrate_montgomery_foreclosure([cap])
    assert len(out) == 1   # didn't crash
    # CIS-derived notes absent; service summary may or may not appear
    assert "Prayer amount" not in out[0].notes


def test_integrate_handles_multiple_captures_independently(monkeypatch):
    """Two captures in one batch → two CaseRecords, no cross-talk."""
    p1 = _party("A FIRST", street="1 A ST", city="DAYTON",
                state="OH", zip="45403", is_primary=True)
    p2 = _party("B SECOND", street="2 B ST", city="KETTERING",
                state="OH", zip="45429", is_primary=True)
    # Sequence calls — first call returns [p1], second returns [p2]
    iter_parties = iter([[p1], [p2]])
    iter_filtered = iter([[p1], [p2]])
    import h3.integration as mod
    monkeypatch.setattr(
        mod, "parse_party_tab", lambda html: next(iter_parties),
    )
    monkeypatch.setattr(mod, "parse_service_tab", lambda html: [])
    monkeypatch.setattr(mod, "parse_cis", lambda b: None)
    monkeypatch.setattr(
        mod, "filter_defendants",
        lambda ps, main_defendant_name="": next(iter_filtered),
    )
    caps = [
        _make_capture(case_number="C1", party_html="<x/>"),
        _make_capture(case_number="C2", party_html="<y/>"),
    ]
    out = integrate_montgomery_foreclosure(caps)
    assert [r.case_number for r in out] == ["C1", "C2"]
    assert out[0].property_street == "1 A ST"
    assert out[1].property_street == "2 B ST"


# ── extract_probate_records ───────────────────────────────────────────


def test_extract_probate_records_returns_list_from_recon():
    """The probate scrapers populate ``recon.probate_records`` directly."""
    # Use any object that quacks like ProbateRecord (the extractor
    # doesn't introspect — it just returns the list).
    fake_records = ["record1", "record2", "record3"]
    recon = ReconCapture()
    recon.probate_records = fake_records  # type: ignore[attr-defined]
    out = extract_probate_records(recon)
    assert out == fake_records


def test_extract_probate_records_handles_missing_attribute():
    """A recon object that never set the attribute → empty list, no crash."""
    recon = ReconCapture()  # no probate_records attribute set
    out = extract_probate_records(recon)
    assert out == []


def test_extract_probate_records_handles_none_attribute():
    """Explicit None on the attribute → empty list."""
    recon = ReconCapture()
    recon.probate_records = None  # type: ignore[attr-defined]
    out = extract_probate_records(recon)
    assert out == []


def test_extract_probate_records_returns_copy_not_reference():
    """Caller shouldn't be able to mutate ``recon.probate_records`` via
    the returned list."""
    original = ["a", "b"]
    recon = ReconCapture()
    recon.probate_records = original  # type: ignore[attr-defined]
    out = extract_probate_records(recon)
    out.append("c")
    assert original == ["a", "b"]   # untouched
