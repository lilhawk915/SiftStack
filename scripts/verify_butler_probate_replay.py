"""Butler probate offline replay — the meaningful probate canary.

Greene's H3 parser is incomplete (empty fields); Butler's is mature
and produces populated ProbateRecord with decedent / DOD / fiduciary
name+phone+address / subject_property. This script reproduces H3's
Butler probate pipeline using the same parser, then diffs against
H3's own baseline CSV.

Same shape as verify_h3_baseline_replay.py.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from h3.notice_data_bridge import probate_record_to_notice_data
from h3.output_writers.probate_format import ProbateRecord
from h3.parsers.butler_probate_case_detail import parse_case_detail


BASELINE_DIR = Path("/tmp/h3_baseline/butler_probate_full")
BASELINE_CSV = BASELINE_DIR / "butler_probate_2026-06-08_to_2026-06-14_probate_leads.csv"


def _combine(*parts: str) -> str:
    return ", ".join(p for p in parts if p)


def _detail_to_record(detail) -> ProbateRecord:
    """Mirror butler_probate.py:_detail_to_record."""
    return ProbateRecord(
        case_number=detail.case_number,
        case_type=detail.filing_type,
        date_filed=detail.file_date,
        decedent_name=detail.decedent_name,
        date_of_death=detail.date_of_death,
        action="",
        relationship=detail.fiduciary_relationship,
        fiduciary_name=detail.fiduciary_name,
        fiduciary_address=_combine(
            detail.fiduciary_address, detail.fiduciary_city_state_zip,
        ),
        fiduciary_phone=detail.fiduciary_phone,
        fiduciary_email="",
        subject_property=_combine(
            detail.decedent_address, detail.decedent_city_state_zip,
        ),
        co_fiduciary_name=detail.co_fiduciary_name,
        notes=(
            f"Type: {detail.fiduciary_type}"
            + (f"; Atty: {detail.attorney_name}"
               if detail.attorney_name else "")
        ),
    )


def main():
    print(f"Loading Butler probate HTML from {BASELINE_DIR} ...")
    case_files = sorted(BASELINE_DIR.glob("*_detail.html"))
    print(f"  {len(case_files)} detail HTML files")

    records: list[ProbateRecord] = []
    parser_failures = []
    for p in case_files:
        m = re.search(r"case_(PE\d{2}-\d{2}-\d{4})_detail", p.name)
        case_num = m.group(1) if m else "?"
        html = p.read_text(errors="replace")
        try:
            detail = parse_case_detail(html)
            if not detail.case_number:
                detail.case_number = case_num
            rec = _detail_to_record(detail)
            records.append(rec)
        except Exception as e:
            parser_failures.append((case_num, str(e)))

    print(f"  {len(records)} ProbateRecord objects produced "
          f"({len(parser_failures)} parser failures)")
    if parser_failures:
        for cn, e in parser_failures[:3]:
            print(f"    FAIL {cn}: {e[:80]}")

    notices = [probate_record_to_notice_data(r, "Butler") for r in records]
    print(f"  {len(notices)} NoticeData rows emitted")

    print(f"\nLoading H3 baseline CSV ...")
    with BASELINE_CSV.open() as f:
        baseline = list(csv.DictReader(f))
    print(f"  {len(baseline)} rows")
    h3_cases = {r["Case Number"] for r in baseline}
    sift_cases = {r.case_number for r in records}

    # ── Diff ────────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("  CANARY VERIFICATION — Butler probate 2026-06-08..14")
    print("═" * 70)
    print(f"\n  Case-number coverage:")
    print(f"    H3 baseline:    {len(h3_cases)} cases")
    print(f"    SiftStack port: {len(sift_cases)} cases")
    print(f"    Intersection:   {len(sift_cases & h3_cases)}")
    only_sift = sift_cases - h3_cases
    only_h3 = h3_cases - sift_cases
    if only_sift: print(f"    Only in Sift: {sorted(only_sift)}")
    if only_h3:   print(f"    Only in H3:   {sorted(only_h3)}")

    print(f"\n  Spot-check 5 cases (decedent / fiduciary / property):")
    print(f"    {'CASE #':<14s}  {'DECEDENT':<26s} {'FIDUCIARY':<22s} {'addr':<3s} {'dod':<3s}")
    print(f"    {'-'*14}  {'-'*26} {'-'*22} {'-'*3} {'-'*3}")

    h3_by_case = {r["Case Number"]: r for r in baseline}
    sf_by_case = {r.case_number: r for r in records}

    diffs = 0
    spot_cases = sorted(sift_cases & h3_cases)[:5]
    for cn in spot_cases:
        h3 = h3_by_case[cn]; sf = sf_by_case[cn]
        h3_dec, sf_dec = h3["TESTATOR/ DECEDENT"], sf.decedent_name
        h3_fid, sf_fid = h3["NAME of Applicant/Beneficiary/Fiduciary"], sf.fiduciary_name
        h3_addr = h3["SUBJECT PROPERTY"]
        sf_addr = sf.subject_property
        h3_dod, sf_dod = h3["DOD"], sf.date_of_death
        addr_m = "✓" if h3_addr == sf_addr else "✗"
        dec_m  = "✓" if h3_dec == sf_dec else "✗"
        dod_m  = "✓" if h3_dod == sf_dod else "✗"
        print(f"    {cn:<14s}  {sf_dec[:26]:<26s} {sf_fid[:22]:<22s} {addr_m:<3s} {dod_m:<3s}")
        if h3_dec != sf_dec or h3_fid != sf_fid or h3_addr != sf_addr or h3_dod != sf_dod:
            diffs += 1
            if h3_dec != sf_dec:
                print(f"      ✗ decedent: H3={h3_dec!r} vs SF={sf_dec!r}")
            if h3_fid != sf_fid:
                print(f"      ✗ fiduciary: H3={h3_fid!r} vs SF={sf_fid!r}")
            if h3_addr != sf_addr:
                print(f"      ✗ property addr: H3={h3_addr!r} vs SF={sf_addr!r}")
            if h3_dod != sf_dod:
                print(f"      ✗ DOD: H3={h3_dod!r} vs SF={sf_dod!r}")

    print("\n" + "═" * 70)
    if sift_cases == h3_cases and diffs == 0:
        print("  ✓ PASS — case coverage matches, 5 spot-check cases identical")
    else:
        print(f"  ! REVIEW NEEDED — "
              f"coverage diff: {len(only_sift)+len(only_h3)} cases, "
              f"spot-check diffs: {diffs}")
    print("═" * 70)


if __name__ == "__main__":
    main()
