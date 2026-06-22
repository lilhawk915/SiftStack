"""Tests for the Montgomery County Auditor (mcrealestate.org) name lookup."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from h3.scrapers.mc_auditor import (  # noqa: E402
    AuditorResult,
    _decedent_tokens,
    _normalize_owner_search_name,
    _parse_search_results,
    parse_detail_html,
)


# ── Name normalization ─────────────────────────────────────────────────


class TestNormalizeOwnerSearchName:
    def test_first_last(self):
        assert _normalize_owner_search_name("MARY BOYER") == "BOYER MARY"

    def test_first_middle_last(self):
        assert _normalize_owner_search_name("MARY A BOYER") == "BOYER MARY A"

    def test_two_middle_names(self):
        # FIRST MIDDLE1 MIDDLE2 LAST → LAST FIRST MIDDLE1 MIDDLE2
        assert _normalize_owner_search_name("MARY ANN ELIZABETH BOYER") \
            == "BOYER MARY ANN ELIZABETH"

    def test_already_last_first_with_comma(self):
        # iasWorld already expects last-first; if input has a comma,
        # it's already in that order
        assert _normalize_owner_search_name("BOYER, MARY ANN") \
            == "BOYER MARY ANN"

    def test_strips_jr_suffix(self):
        assert _normalize_owner_search_name("JOHN SMITH JR") \
            == "SMITH JOHN"

    def test_strips_sr_suffix(self):
        assert _normalize_owner_search_name("ROBERT JONES SR.") \
            == "JONES ROBERT"

    def test_strips_roman_numeral_suffix(self):
        # Regex covers II, III, IV, V — names with VIII+ are vanishingly
        # rare in probate. Pick a realistic suffix.
        assert _normalize_owner_search_name("ROBERT JONES III") \
            == "JONES ROBERT"

    def test_single_name_passes_through(self):
        assert _normalize_owner_search_name("MADONNA") == "MADONNA"

    def test_empty_input(self):
        assert _normalize_owner_search_name("") == ""

    def test_whitespace_only(self):
        assert _normalize_owner_search_name("   ") == ""

    def test_strips_periods(self):
        # Common in OCR'd / inconsistently formatted names
        assert _normalize_owner_search_name("JOHN P. SMITH") \
            == "SMITH JOHN P"


# ── Token extraction for ambiguity disambiguation ────────────────────


class TestDecedentTokens:
    def test_basic(self):
        assert _decedent_tokens("MARY BOYER") == {"MARY", "BOYER"}

    def test_strips_short_tokens(self):
        # Single-char tokens (like middle initial "A") aren't useful
        # for matching — they generate false positives
        toks = _decedent_tokens("MARY A BOYER")
        assert toks == {"MARY", "BOYER"}

    def test_handles_comma_format(self):
        assert _decedent_tokens("BOYER, MARY ANN") == {"BOYER", "MARY", "ANN"}

    def test_strips_suffix(self):
        toks = _decedent_tokens("JOHN SMITH JR")
        assert "JR" not in toks
        assert toks == {"JOHN", "SMITH"}

    def test_empty(self):
        assert _decedent_tokens("") == set()


# ── Search-results parsing ────────────────────────────────────────────


SEARCH_RESULTS_FIXTURE = """
<thead><tr><th>Parcel ID</th><th>Owner</th><th>Location</th></tr></thead>
<tbody>
  <tr onclick="javascript:selectSearchRow('../Datalets/Datalet.aspx?sIndex=0&idx=1')">
    <td>N64 02312 0003</td><td>BOYER MARY ANN TR</td><td>2059 STAYMAN DR</td>
  </tr>
  <tr onclick="javascript:selectSearchRow('../Datalets/Datalet.aspx?sIndex=0&idx=2')">
    <td>R72 11111 1234</td><td>BOYER MARY</td><td>123 OTHER ST</td>
  </tr>
</tbody>
"""


class TestSearchResultsParser:
    def test_extracts_idx_owner_parcel(self):
        rows = _parse_search_results(SEARCH_RESULTS_FIXTURE)
        assert len(rows) == 2
        assert rows[0].idx == 1
        assert rows[0].parcel == "N64 02312 0003"
        assert rows[0].owner == "BOYER MARY ANN TR"
        assert rows[0].location == "2059 STAYMAN DR"
        assert rows[1].idx == 2
        assert rows[1].owner == "BOYER MARY"

    def test_skips_rows_without_idx_onclick(self):
        html = """
          <tr onclick="javascript:selectSearchRow('../Datalets/Datalet.aspx?sIndex=0&idx=5')">
            <td>P1</td><td>SMITH JOHN</td><td>111 OAK ST</td>
          </tr>
          <tr><td>HEADER</td><td>HEADER</td><td>HEADER</td></tr>
        """
        rows = _parse_search_results(html)
        assert len(rows) == 1
        assert rows[0].idx == 5

    def test_no_rows(self):
        assert _parse_search_results("") == []
        assert _parse_search_results("<tr><td>empty</td></tr>") == []


# ── Detail-page parsing ──────────────────────────────────────────────


DETAIL_HTML_FIXTURE = """
<table>
  <tr><td class='DataletHeaderBottom'>PARCEL LOCATION: 2059 STAYMAN DR </td><td>NBHD: 14</td></tr>
</table>
<table>
  <tr><td class='DataletSideHeading'>Name</td><td class='DataletData'>BOYER MARY ANN TR</td></tr>
  <tr><td class='DataletSideHeading'>Mailing Address</td><td class='DataletData'>2059 STAYMAN DR</td></tr>
  <tr><td class='DataletSideHeading'>City, State, Zip</td><td class='DataletData'>DAYTON, OH  45440 1664</td></tr>
  <tr><td class='DataletSideHeading'>Acres</td><td class='DataletData'>.279</td></tr>
  <tr><td class='DataletSideHeading'>Year Built</td><td class='DataletData'>1960</td></tr>
  <tr><td class='DataletSideHeading'>Building Style</td><td class='DataletData'>RANCH</td></tr>
  <tr><td class='DataletSideHeading'>Total Rms/Bedrms/Baths/Half Ba</td><td class='DataletData'>5/3/1/1</td></tr>
  <tr><td class='DataletSideHeading'>Square Feet of Living Area</td><td class='DataletData'>1,348</td></tr>
  <tr><td class='DataletSideHeading'>Total</td><td class='DataletData'>65,580 187,370</td></tr>
</table>
"""


class TestParseDetailHtml:
    def test_address_fields(self):
        r = parse_detail_html(DETAIL_HTML_FIXTURE)
        assert r.street == "2059 STAYMAN DR"
        assert r.city == "Dayton"
        assert r.state == "OH"
        assert r.zip == "45440-1664"

    def test_owner(self):
        r = parse_detail_html(DETAIL_HTML_FIXTURE)
        assert r.owner == "BOYER MARY ANN TR"

    def test_bonus_fields(self):
        r = parse_detail_html(DETAIL_HTML_FIXTURE)
        assert r.year_built == "1960"
        assert r.acres == ".279"
        assert r.structure_type == "RANCH"
        assert r.bedrooms == "3"
        assert r.bathrooms == "1"
        assert r.living_sqft == "1,348"

    def test_estimated_value_is_market_not_assessed(self):
        # Total row format is "ASSESSED MARKET" — we want MARKET ($187,370)
        # so callers see what the property is actually worth.
        r = parse_detail_html(DETAIL_HTML_FIXTURE)
        assert r.estimated_value == "187,370"

    def test_full_address_combines_correctly(self):
        r = parse_detail_html(DETAIL_HTML_FIXTURE)
        assert r.full_address == "2059 STAYMAN DR, Dayton, OH 45440-1664"

    def test_zip_without_plus4(self):
        html = """
          <tr><td class='DataletSideHeading'>Mailing Address</td><td class='DataletData'>1 OAK</td></tr>
          <tr><td class='DataletSideHeading'>City, State, Zip</td><td class='DataletData'>DAYTON, OH  45402</td></tr>
        """
        r = parse_detail_html(html)
        assert r.zip == "45402"
        assert r.full_address == "1 OAK, Dayton, OH 45402"

    def test_falls_back_to_parcel_location_header_when_no_mailing(self):
        # Some Datalets have no mailing-address row, e.g. abandoned parcels
        html = """
          <td class='DataletHeaderBottom'>PARCEL LOCATION: 999 GHOST RD </td>
          <table>
            <tr><td class='DataletSideHeading'>Name</td><td class='DataletData'>HEIRS UNKNOWN</td></tr>
            <tr><td class='DataletSideHeading'>City, State, Zip</td><td class='DataletData'>DAYTON, OH  45417</td></tr>
          </table>
        """
        r = parse_detail_html(html)
        assert r.street == "999 GHOST RD"
        assert r.city == "Dayton"

    def test_found_property_is_false_when_no_street(self):
        r = parse_detail_html("<html></html>")
        assert r.found is False
        assert r.full_address == ""

    def test_found_property_is_true_when_street_present(self):
        r = parse_detail_html(DETAIL_HTML_FIXTURE)
        assert r.found is True


# ── AuditorResult.full_address handling of partial data ─────────────


class TestAuditorResultFullAddress:
    def test_complete(self):
        r = AuditorResult(street="123 OAK", city="Dayton",
                          state="OH", zip="45402")
        assert r.full_address == "123 OAK, Dayton, OH 45402"

    def test_no_zip(self):
        r = AuditorResult(street="123 OAK", city="Dayton", state="OH")
        assert r.full_address == "123 OAK, Dayton, OH"

    def test_no_state(self):
        r = AuditorResult(street="123 OAK", city="Dayton", zip="45402")
        assert r.full_address == "123 OAK, Dayton 45402"

    def test_street_only(self):
        r = AuditorResult(street="123 OAK")
        assert r.full_address == "123 OAK"

    def test_empty(self):
        r = AuditorResult()
        assert r.full_address == ""
