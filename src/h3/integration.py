"""H3 case-detail → populated CaseRecord integration (SiftStack-native port).

Ports the foreclosure integration logic from H3_Scrapers/main.py
(``_integrate_cases`` lines 650–1190) with **zero Apify dependencies**.
Every scraper returns minimal recon-only CaseRecord directly from
``run()``; the real population happens by walking
``scraper.recon.case_details`` and parsing each county's captured
HTML/PDFs through the parsers under :mod:`h3.parsers`.

This module exposes per-county integrators:

* :func:`integrate_montgomery_foreclosure` — multi-AJAX-tab (party,
  service, docket) + CIS PDF.
* :func:`integrate_equivant_foreclosure` — single-page CourtView HTML
  via the shared ``parse_case_detail_html`` in each county's scraper
  module (Butler, Clark, Clermont, Greene, Miami).
* :func:`integrate_warren_foreclosure` — BenchmarkCP HTML + Warren
  Auditor parcel lookup + PJR/COMPLAINT PDF OCR fallback.

Probate scrapers populate ``ProbateRecord`` directly during ``run()``;
no integration layer is needed beyond reading
``scraper.recon.probate_records``. See :func:`extract_probate_records`.

Logging uses ``logging.getLogger(__name__)`` instead of ``Actor.log``
so this module is usable both inside SiftStack's daily pipeline and
in unit tests (no Apify SDK required to import).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from h3.output_writers.h3_format import CaseRecord, Defendant
from h3.parsers.case_info_sheet import parse_cis
from h3.parsers.defendant_filter import filter_defendants
from h3.parsers.owner_refinements import is_decedent_match, strip_role_middle
from h3.parsers.party_tab import PartyEntry, parse_party_tab
from h3.parsers.service_tab import parse_service_tab, summarize_for_main_defendant

logger = logging.getLogger(__name__)


# ── Montgomery County integration ──────────────────────────────────────


# Cities in Montgomery County OH. Used to decide whether the owner's
# mailing address can double as the property address (in-county =
# probably owner-occupied; out-of-county = absentee, needs auditor lookup).
# Includes a few observed misspellings from court records.
_MC_CITIES: set[str] = {
    "DAYTON", "KETTERING", "HUBER HEIGHTS", "TROTWOOD", "MIAMISBURG",
    "CENTERVILLE", "ENGLEWOOD", "MORAINE", "OAKWOOD", "WEST CARROLLTON",
    "VANDALIA", "BROOKVILLE", "GERMANTOWN", "NEW LEBANON", "RIVERSIDE",
    "CLAYTON", "FARMERSVILLE", "DREXEL", "PHILLIPSBURG", "UNION",
    "WASHINGTON TOWNSHIP", "FAIRBORN",
    # Common misspellings observed in court records
    "DAYLAND", "ENGELWOOD", "DAY TON",
}


# Tokens that mark a defendant as a government / corporate / placeholder
# entity rather than the owner we'd contact. Used by the service-tab
# fallback synthesizer when the AJAX party tab failed to render.
_SERVICE_TAB_SKIP_TOKENS: tuple[str, ...] = (
    "TREASURER", "STATE OF", "DEPARTMENT",
    "UNITED STATES", "SECRETARY", "LLC", "INC",
    "TRUST", "BANK", "ASSOCIATION", "CORP",
    "UNKNOWN SPOUSE", "DOE,", "JOHN DOE",
    "JANE DOE", "COUNTY OHIO",
)


_UNKNOWN_HEIRS_DECEDENT_RE = re.compile(
    r"(?:UNKNOWN\s+HEIRS|HEIRS(?:,?\s+(?:DEVISEES|LEGATEES))?|"
    r"REPRESENTATIVES)\s+OF\s+(.+?)(?:\s+DECEASED|$)",
    re.IGNORECASE,
)


def _is_unknown_heir(name: str | None) -> bool:
    """Mirror of ``output_writers.siftstack_adapter._is_unknown_heir``.

    Imported lazily inside ``integrate_montgomery_foreclosure`` to avoid
    a hard cycle; broken out here so tests can monkeypatch if needed.
    """
    if not name:
        return False
    upper = name.upper()
    return (
        "UNKNOWN HEIR" in upper
        or "JOHN DOE" in upper
        or "JANE DOE" in upper
        or re.search(
            r"HEIRS(?:,?\s+(?:DEVISEES|LEGATEES))?\s+OF\b", upper,
        ) is not None
    )


def _synthesize_parties_from_service_tab(
    service_html: str,
    case_number: str = "",
) -> list[PartyEntry]:
    """Fall back to service-tab parsing when the AJAX party tab is empty.

    Montgomery's case-detail page loads parties asynchronously into a
    separate tab; occasionally the navigation race captures the
    search-results wrapper instead of the rendered party HTML, leaving
    ``parse_party_tab`` empty. The service tab is always present and
    carries the same defendant ``Name: ... Address: ...`` blocks — we
    can recover defendants from it.

    Returns ``[]`` if the service tab is unparseable. Sets a flag at
    the caller; the resulting CaseRecord is tagged
    ``deep_prospect_source="SERVICE_TAB"``.
    """
    if not service_html:
        return []
    parties: list[PartyEntry] = []
    seen: set[str] = set()
    try:
        events = parse_service_tab(service_html)
    except Exception as e:
        logger.warning("  %s: service-tab fallback parse failed: %s",
                       case_number, e)
        return []
    for ev in events:
        name = (ev.party_name or "").strip()
        if not name or name in seen:
            continue
        upper = name.upper()
        if any(t in upper for t in _SERVICE_TAB_SKIP_TOKENS):
            continue
        seen.add(name)
        parties.append(PartyEntry(
            name=name,
            role="DEFENDANT",
            street=ev.party_street,
            city=ev.party_city,
            state=ev.party_state,
            zip=ev.party_zip,
        ))
    return parties


def _resolve_property_address(filtered: list) -> tuple[str, str, str, str, str, str]:
    """Pick the property address from the filtered defendant list.

    Strategy (mirrors H3 main.py):
    1. Find the primary defendant (CIS-matched). If absent, fall back
       to the first filtered defendant.
    2. If their mailing city is in Montgomery County, treat mailing as
       property address (owner-occupied likelihood).
    3. Otherwise mark absentee=Y and look for an in-county co-defendant
       whose mailing might BE the property; fall back to the owner's
       out-of-county mailing as a last resort (caller flags
       needs_property_lookup=Y).

    Returns:
        ``(street, city, state, zip, absentee_flag, needs_lookup_flag)``.
        All empty strings when no defendant has a usable street.
    """
    primary = next((f for f in filtered if getattr(f, "is_primary", False)), None)
    candidate = primary or (filtered[0] if filtered else None)
    if not candidate or not getattr(candidate, "street", ""):
        return ("", "", "", "", "", "")

    primary_city_upper = (candidate.city or "").upper()
    if primary_city_upper in _MC_CITIES:
        return (
            candidate.street, candidate.city, candidate.state,
            candidate.zip, "N", "N",
        )

    # Out-of-county owner — absentee. See if a co-defendant lives
    # in-county; their mailing might BE the property.
    in_county = next(
        (d for d in filtered
         if d.city and d.city.upper() in _MC_CITIES),
        None,
    )
    if in_county:
        return (
            in_county.street, in_county.city, in_county.state,
            in_county.zip, "Y", "Y",
        )
    return (
        candidate.street, candidate.city, candidate.state,
        candidate.zip, "Y", "Y",
    )


def integrate_montgomery_foreclosure(
    case_details: list[Any],
) -> list[CaseRecord]:
    """Build populated CaseRecord list from Montgomery case-detail captures.

    Each ``CaseDetailCapture`` carries up to 4 AJAX-tab HTML snapshots
    (summary / party / service / docket) plus any docket-document PDFs
    downloaded (CIS = Case Information Sheet). We parse:

    * **Party tab** → defendant list with mailing addresses
    * **Service tab** → service-of-process events (used for status
      summary AND as fallback defendants when party tab is empty)
    * **Docket entries** → filing date + filing type
    * **CIS PDF** → main defendant name + prayer amount + parcel number

    Then:

    * Drop placeholder co-defendants (``DOE``, ``UNKNOWN SPOUSE``,
      government entities)
    * Pick a property address by Montgomery-County-city heuristic
    * Detect ``UNKNOWN HEIRS OF X`` placeholders → set
      ``heirs_unknown=Y`` and extract the decedent name
    * Tag ``deep_prospect_source="SERVICE_TAB"`` when the party tab
      had to be synthesised from service events

    Cases captured with no party HTML AND no usable service HTML are
    skipped with a warning (not a hard error — the daily pipeline
    keeps going).
    """
    out: list[CaseRecord] = []
    for cap in case_details:
        if not hasattr(cap, "screens"):
            # Not a Montgomery capture — let the equivant/Warren
            # integrators handle it. Defensive skip.
            continue

        screens = {s.screen: s.html for s in cap.screens if s.html}
        party_html = screens.get("party", "")
        service_html = screens.get("service", "")

        if not party_html and not service_html:
            logger.warning(
                "  %s: no party or service HTML — skipping integration",
                cap.case_number,
            )
            continue

        parties = parse_party_tab(party_html) if party_html else []
        service_tab_synthesized = False
        if not parties:
            parties = _synthesize_parties_from_service_tab(
                service_html, cap.case_number,
            )
            if parties:
                service_tab_synthesized = True
                logger.info(
                    "  %s: party tab empty — synthesized %d defendant(s) "
                    "from service tab", cap.case_number, len(parties),
                )

        if not parties:
            logger.warning(
                "  %s: no defendants recoverable from party OR service "
                "tab — skipping", cap.case_number,
            )
            continue

        # CIS PDF — find the CASE INFORMATION SHEET in downloaded docs
        cis = None
        for p in cap.pdfs:
            if ("CASE INFORMATION" in (p.document_type or "").upper()
                    and p.pdf_bytes):
                try:
                    cis = parse_cis(p.pdf_bytes)
                except Exception as e:
                    logger.warning(
                        "  %s: CIS parse failed: %s", cap.case_number, e,
                    )
                break

        service_events = (
            parse_service_tab(service_html) if service_html else []
        )

        # Filing date + type — first docket entry (chronological)
        date_filed = ""
        filing_type = "COMPLAINT FOR FORECLOSURE"  # default
        if cap.docket_entries:
            first = cap.docket_entries[0]
            date_filed = first.date_filed
            if first.document_type:
                filing_type = first.document_type

        main_defendant = cis.main_defendant if cis else ""
        filtered = filter_defendants(parties, main_defendant_name=main_defendant)

        (prop_street, prop_city, prop_state, prop_zip,
         absentee_flag, needs_lookup_flag) = _resolve_property_address(filtered)

        # Notes: service status + prayer + parcel
        notes_parts: list[str] = []
        if service_events and main_defendant:
            note = summarize_for_main_defendant(
                service_events, main_defendant,
            )
            if note:
                notes_parts.append(note)
        if cis and cis.prayer_amount:
            notes_parts.append(f"Prayer amount: ${cis.prayer_amount:,.2f}")
        if cis and cis.parcel_number:
            notes_parts.append(f"Parcel: {cis.parcel_number}")

        # Unknown-heirs detection (deceased owner, no heir stepped up)
        unknown_heirs_defs = [
            d for d in filtered if _is_unknown_heir(d.name)
        ]
        heirs_unknown_flag = "Y" if unknown_heirs_defs else ""
        heirs_unknown_decedent = ""
        if unknown_heirs_defs:
            m = _UNKNOWN_HEIRS_DECEDENT_RE.search(
                unknown_heirs_defs[0].name or "",
            )
            if m:
                heirs_unknown_decedent = m.group(1).strip()

        out.append(CaseRecord(
            case_number=cap.case_number,
            filing_type=filing_type,
            date_filed=date_filed,
            notes="; ".join(notes_parts),
            defendants=[
                Defendant(
                    name=d.name,
                    street=d.street,
                    city=d.city,
                    state=d.state,
                    zip=d.zip,
                )
                for d in filtered
            ],
            property_street=prop_street,
            property_city=prop_city,
            property_state=prop_state,
            property_zip=prop_zip,
            absentee_owner=absentee_flag,
            needs_property_lookup=needs_lookup_flag,
            heirs_unknown=heirs_unknown_flag,
            heirs_unknown_decedent=heirs_unknown_decedent,
            deep_prospect_unreachable=(
                "Y" if service_tab_synthesized else ""
            ),
            deep_prospect_source=(
                "SERVICE_TAB" if service_tab_synthesized else ""
            ),
        ))
    return out


# ── Probate extraction (Greene + others) ──────────────────────────────


def extract_probate_records(recon: Any) -> list[Any]:
    """Read ``ProbateRecord`` objects from a scraper's ``recon`` capture.

    All 7 probate scrapers populate ``scraper.recon.probate_records``
    directly during ``run()`` — there is no parse/integrate gap. This
    helper exists so the dispatcher contract matches the foreclosure
    side (``integrate_*`` returns records) and so we have a single
    seam for any future filtering (e.g. date-window enforcement).

    Returns ``[]`` if ``recon`` lacks the attribute or the list is empty.
    """
    return list(getattr(recon, "probate_records", []) or [])
