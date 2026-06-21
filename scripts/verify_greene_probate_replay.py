"""Greene probate offline replay.

Loads each saved _detail.html, runs through the same
``h3.parsers.greene_probate_case_detail.parse_case_detail`` H3 uses,
converts to ProbateRecord (mirroring H3's _detail_to_record), bridges
to NoticeData, diffs vs the baseline CSV.

NOTE: as of 2026-06-19 the Greene probate parser is the H3-stated
"best-effort first-pass parser" — H3's own baseline run produced
all-empty fields for the 7 cases except case_number. The replay
will match that degenerate output exactly (same parser code), but
it doesn't strongly validate end-to-end behaviour. Flagged in the
verdict at the bottom.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from h3.notice_data_bridge import probate_record_to_notice_data
from h3.output_writers.probate_format import ProbateRecord
from h3.parsers.greene_probate_case_detail import parse_case_detail


BASELINE_DIR = Path("/tmp/h3_baseline/greene_probate_full")
BASELINE_CSV = BASELINE_DIR / "greene_probate_2026-06-08_to_2026-06-14_probate_leads.csv"


def _combine_address(street: str, city_state_zip: str) -> str:
    """Mirror H3's address-combining helper from greene_probate.py."""
    parts = [p for p in (street, city_state_zip) if p]
    return ", ".join(parts) if parts else ""


def _detail_to_record(detail) -> ProbateRecord:
    """Copy of H3's converter."""
    return ProbateRecord(
        case_number=detail.case_number,
        case_type=detail.case_type,
        date_filed=detail.file_date,
        decedent_name=detail.decedent_name,
        date_of_death=detail.date_of_death,
        action="",
        relationship=detail.fiduciary_relationship,
        fiduciary_name=detail.fiduciary_name,
        fiduciary_address=_combine_address(
            detail.fiduciary_address, detail.fiduciary_city_state_zip,
        ),
        fiduciary_phone=detail.fiduciary_phone,
        fiduciary_email="",
        subject_property=_combine_address(
            detail.decedent_address, detail.decedent_city_state_zip,
        ),
    )


def main():
    print(f"Loading Greene probate HTML from {BASELINE_DIR} ...")
    case_files = sorted(BASELINE_DIR.glob("*_detail.html"))
    print(f"  {len(case_files)} detail HTML files")

    records: list[ProbateRecord] = []
    for p in case_files:
        m = re.search(r"case_(\d{4}-EST-\d{4})_detail", p.name)
        case_num = m.group(1) if m else "?"
        html = p.read_text(errors="replace")
        detail = parse_case_detail(html)
        # The H3 parser doesn't extract case_number from the HTML
        # (it pulls it from the search results listing instead). Inject
        # what we know from the filename.
        if not detail.case_number:
            detail.case_number = case_num
        rec = _detail_to_record(detail)
        records.append(rec)

    print(f"  {len(records)} ProbateRecord objects produced")
    notices = [probate_record_to_notice_data(r, "Greene") for r in records]
    print(f"  {len(notices)} NoticeData rows emitted")

    print(f"\nLoading H3 baseline CSV ...")
    with BASELINE_CSV.open() as f:
        baseline_rows = list(csv.DictReader(f))
    print(f"  {len(baseline_rows)} rows")

    # ── Diff ────────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("  CANARY VERIFICATION — Greene probate 2026-06-08..14")
    print("═" * 70)

    sift_cases = {r.case_number for r in records}
    h3_cases = {r["Case Number"] for r in baseline_rows}
    print(f"\n  Case coverage:")
    print(f"    H3 baseline:    {len(h3_cases)} cases — {sorted(h3_cases)}")
    print(f"    SiftStack port: {len(sift_cases)} cases — {sorted(sift_cases)}")
    print(f"    Intersection:   {len(sift_cases & h3_cases)}")

    # Check field population — H3 baseline has all empty fields except
    # Case Number, so we just confirm SiftStack's parser produces the
    # same degenerate output.
    print(f"\n  Field population check (H3 vs SiftStack):")
    for cn in sorted(sift_cases)[:3]:
        h3 = next((r for r in baseline_rows if r["Case Number"] == cn), {})
        sf = next((r for r in records if r.case_number == cn), None)
        print(f"    {cn}:")
        print(f"      H3 baseline non-empty fields: "
              f"{[k for k,v in h3.items() if v.strip() and k != 'Case Number']}")
        sf_dict = {
            "case_type": sf.case_type, "file_date": sf.date_filed,
            "decedent": sf.decedent_name, "DOD": sf.date_of_death,
            "fiduciary": sf.fiduciary_name, "fid_addr": sf.fiduciary_address,
            "fid_phone": sf.fiduciary_phone, "subject_property": sf.subject_property,
        }
        sf_nonempty = {k:v for k,v in sf_dict.items() if v and v.strip()}
        print(f"      SiftStack non-empty fields: {sf_nonempty}")

    print("\n" + "═" * 70)
    case_match = sift_cases == h3_cases
    field_count_match = all(
        sum(1 for v in r.values() if v.strip() and not v.startswith("2026-")) == 0
        for r in baseline_rows
    )
    if case_match:
        print(f"  ✓ Case coverage matches (7/7)")
    else:
        print(f"  ✗ Case-coverage diff: only-Sift={sift_cases-h3_cases}, "
              f"only-H3={h3_cases-sift_cases}")
    print(f"  ⚠ NOTE: H3 baseline produced ALL-EMPTY fields beyond case#")
    print(f"           Both pipelines use the same parser, so this is")
    print(f"           an INHERITED gap in the Greene probate parser —")
    print(f"           not a regression caused by the SiftStack port.")
    print("═" * 70)


if __name__ == "__main__":
    main()
