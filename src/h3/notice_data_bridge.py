"""Bridge: H3's ``CaseRecord`` / ``ProbateRecord`` → SiftStack ``NoticeData``.

The H3 codebase has its own internal dataclasses optimized for the
legacy data-manager Excel output. SiftStack's enrichment + DataSift
upload pipeline consumes ``NoticeData`` (defined in
``src/notice_parser.py``). This module is the seam.

Conversion rules:

* **One CaseRecord → one or more NoticeData rows.** Multi-defendant
  cases (a typical foreclosure has the borrower + spouse + sometimes
  unknown heirs) become one NoticeData row per defendant, with the
  property block shared across all rows. The primary defendant (CIS
  match, or first if no CIS) sits at index 0.

* **One ProbateRecord → one NoticeData row.** The fiduciary (PR /
  executor / administrator) is the contact (``owner_name``); the
  decedent goes in ``decedent_name``. Fiduciary mailing address goes
  to ``owner_street``/``city``/``state``/``zip``. The combined
  ``subject_property`` string is parsed into the property address
  fields when possible.

Both converters take an explicit ``county`` parameter — neither H3
record carries county metadata internally. They also accept an
optional ``source_url`` for traceability back to the originating
portal.
"""
from __future__ import annotations

import re
from datetime import datetime

from h3.output_writers.h3_format import CaseRecord, Defendant
from h3.output_writers.probate_format import ProbateRecord
from notice_parser import NoticeData


# ── Helpers ───────────────────────────────────────────────────────────


_ADDRESS_LINE_RE = re.compile(
    r"^(?P<street>.+?),\s*"
    r"(?P<city>[A-Za-z .'-]+),\s*"
    r"(?P<state>[A-Z]{2})\s+"
    r"(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)


def _parse_combined_address(combined: str) -> tuple[str, str, str, str]:
    """Split a ``'STREET, CITY, STATE ZIP'`` string into 4 fields.

    Probate scrapers emit subject-property addresses as a single
    pre-joined string. This helper splits it back into the discrete
    fields SiftStack's enrichment + datasift_formatter expect.
    Returns ``('', '', '', '')`` for unparseable input.
    """
    if not combined or not combined.strip():
        return ("", "", "", "")
    m = _ADDRESS_LINE_RE.match(combined.strip())
    if not m:
        return (combined.strip(), "", "", "")  # keep raw in street slot
    return (
        m.group("street").strip(),
        m.group("city").strip(),
        m.group("state").strip(),
        m.group("zip").strip(),
    )


def _to_iso_date(raw: str) -> str:
    """Convert ``'MM/DD/YYYY'`` → ``'YYYY-MM-DD'``. Empty on failure.

    H3's CaseRecord stores dates in US format; SiftStack's NoticeData
    expects ISO. ProbateRecord already emits ISO (per its docstring)
    but we run it through this defensively in case a county scraper
    drifts.
    """
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


# ── CaseRecord → NoticeData ───────────────────────────────────────────


def case_record_to_notice_data(
    rec: CaseRecord,
    county: str,
    *,
    source_url: str = "",
) -> list[NoticeData]:
    """Convert one CaseRecord into one-or-more NoticeData rows.

    A foreclosure case typically has multiple defendants (borrower +
    spouse + lender + sometimes UNKNOWN HEIRS). DataSift's downstream
    tag-stacking merges by property address, so emitting one row per
    defendant means we tag each living co-owner separately for
    outreach. The property block is shared across all rows in the
    same case.

    Returns a list of length ``len(rec.defendants)``, or 1 if the
    case has no defendants (property-only row).
    """
    iso_date = _to_iso_date(rec.date_filed)
    # Property-only fallback when the case has no parties.
    if not rec.defendants:
        return [NoticeData(
            notice_type="foreclosure",
            county=county,
            state="OH",
            address=rec.property_street,
            city=rec.property_city,
            zip=rec.property_zip,
            date_added=iso_date,
            absentee_owner=rec.absentee_owner,
            source_url=source_url,
            raw_text=rec.notes or "",
        )]

    notices: list[NoticeData] = []
    for d in rec.defendants:
        # Property-block fields stay constant; owner fields per row.
        n = NoticeData(
            notice_type="foreclosure",
            county=county,
            state="OH",
            owner_name=d.name,
            # Property location
            address=rec.property_street,
            city=rec.property_city,
            zip=rec.property_zip,
            # Owner's mailing address (used for absentee owners; for
            # owner-occupied cases this typically equals property).
            owner_street=d.street,
            owner_city=d.city,
            owner_state=d.state,
            owner_zip=d.zip,
            # When defendants are placeholder UNKNOWN HEIRS OF X, the
            # decedent name is carried separately so the obituary
            # enricher + DM-search can target X (not the placeholder).
            decedent_name=rec.heirs_unknown_decedent,
            owner_deceased="yes" if rec.heirs_unknown == "Y" else "",
            date_added=iso_date,
            absentee_owner=rec.absentee_owner,
            source_url=source_url,
            raw_text=rec.notes or "",
        )
        notices.append(n)
    return notices


# ── ProbateRecord → NoticeData ────────────────────────────────────────


def probate_record_to_notice_data(
    rec: ProbateRecord,
    county: str,
    *,
    source_url: str = "",
) -> NoticeData:
    """Convert one ProbateRecord into one NoticeData row.

    Owner mapping:
      * ``owner_name``  = ``fiduciary_name`` (the PR/executor — the
        actual person we want to contact, not the deceased)
      * ``decedent_name`` = ``decedent_name``
      * ``date_of_death`` = ``date_of_death``
      * ``owner_deceased`` = ``"yes"`` — every probate notice means
        the named decedent is dead, by definition
      * ``decision_maker_name`` / ``_relationship`` / ``_status`` /
        ``_street``/_city/_state/_zip = fiduciary fields. This is what
        the existing obituary-enricher pipeline expects for cases where
        the DM is already known (skips the obit search, uses the
        court-named PR directly).

    Property location: try to parse ``subject_property`` into the
    street/city/state/zip fields. Falls through to leaving them blank
    if the combined address isn't parseable — downstream Smarty
    standardization can still recover.
    """
    street, city, state, zip5 = _parse_combined_address(rec.subject_property)
    fid_street, fid_city, fid_state, fid_zip = _parse_combined_address(
        rec.fiduciary_address,
    )
    iso_filed = _to_iso_date(rec.date_filed)
    iso_dod = _to_iso_date(rec.date_of_death)

    # Property-address fallback: when the auditor lookup didn't find
    # a parcel for this decedent (the common case — only ~15-25% of
    # decedents have Montgomery property under their name), the
    # `subject_property` field is empty. DataSift's "Add Data" mode
    # merges duplicates by (Property Street, Property City, Property
    # ZIP) — an empty merge key means re-uploads NEVER dedup and the
    # same probate record accumulates as a fresh row each daily run.
    # Fall back to the fiduciary mailing address so every row has a
    # non-empty merge key. The mailing address is semantically the
    # executor's contact location (often a family member or attorney),
    # not the estate property itself; downstream code that needs the
    # strict subject-property meaning should consult the SOURCE record
    # (rec.subject_property) directly. For the dedup + outreach
    # pipeline DataSift drives, a stable per-decedent address is
    # what matters.
    property_street = street or fid_street
    property_city   = city   or fid_city
    property_state  = state  or fid_state or "OH"
    property_zip    = zip5   or fid_zip

    return NoticeData(
        notice_type="probate",
        county=county,
        state="OH",
        date_added=iso_filed,
        # Property address — auditor-derived subject_property when
        # available, else fiduciary mailing (see fallback note above).
        address=property_street,
        city=property_city,
        zip=property_zip,
        # Owner contact = fiduciary (the executor/administrator —
        # already named by the court, no obituary search needed)
        owner_name=rec.fiduciary_name,
        owner_street=fid_street,
        owner_city=fid_city,
        owner_state=fid_state or state or "OH",
        owner_zip=fid_zip,
        # Probate identifying fields
        decedent_name=rec.decedent_name,
        date_of_death=iso_dod,
        owner_deceased="yes",
        # Pre-populate the obituary-enricher's DM fields so the
        # probate-preset branch in obituary_enricher.py (which detects
        # PR + decedent + no obituary needed) fires cleanly.
        decision_maker_name=rec.fiduciary_name,
        decision_maker_relationship=rec.relationship,
        decision_maker_status="verified_living",  # the court appointed them
        decision_maker_source="probate_notice",
        decision_maker_street=fid_street,
        decision_maker_city=fid_city,
        decision_maker_state=fid_state or "OH",
        decision_maker_zip=fid_zip,
        source_url=source_url,
        raw_text=rec.notes or "",
    )
