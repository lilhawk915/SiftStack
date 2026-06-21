"""Clermont foreclosure offline replay — equivant canary.

Same shape as verify_h3_baseline_replay.py (Montgomery) but uses the
new integrate_equivant_foreclosure() shared code path. Confirms that
the equivant integrator produces output identical to H3 for the
single-page CourtView case-detail HTMLs.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from h3.integration import integrate_equivant_foreclosure
from h3.notice_data_bridge import case_record_to_notice_data


BASELINE_DIR = Path("/tmp/h3_baseline/clermont_foreclosure_full")
BASELINE_CSV = BASELINE_DIR / "clermont_06-08-2026_to_06-14-2026.csv"


@dataclass
class ClermontCaseDetailCapture:
    """Mimics h3.scrapers.clermont.CaseDetailCapture shape."""
    case_number: str
    final_url: str = ""
    html: str = ""
    error: str = ""


def _case_num_from_filename(p: Path) -> str:
    """'clermont_..._case_2026_CVE_00819_detail.html' → '2026 CVE 00819'."""
    m = re.search(r"case_(\d{4})_(\w+)_(\d+)_detail", p.name)
    return f"{m.group(1)} {m.group(2)} {m.group(3)}" if m else ""


def load_captures() -> list[ClermontCaseDetailCapture]:
    caps = []
    for p in sorted(BASELINE_DIR.glob("*_detail.html")):
        cn = _case_num_from_filename(p)
        if not cn:
            continue
        caps.append(ClermontCaseDetailCapture(
            case_number=cn,
            html=p.read_text(errors="replace"),
        ))
    return caps


def load_h3_baseline() -> list[dict]:
    with BASELINE_CSV.open() as f:
        return list(csv.DictReader(f))


def main():
    print(f"Loading Clermont captures from {BASELINE_DIR} ...")
    caps = load_captures()
    print(f"  {len(caps)} CaseDetailCapture objects reconstructed")
    print(f"  Case numbers: {[c.case_number for c in caps]}")

    print(f"\nLoading H3 baseline CSV from {BASELINE_CSV} ...")
    baseline_rows = load_h3_baseline()
    print(f"  {len(baseline_rows)} rows")
    baseline_case_numbers = {r["Case Number"].strip() for r in baseline_rows
                              if r["Case Number"].strip()}
    print(f"  {len(baseline_case_numbers)} unique case numbers")

    print(f"\nRunning SiftStack integrate_equivant_foreclosure(county='clermont') ...")
    records = integrate_equivant_foreclosure(caps, "clermont")
    print(f"  {len(records)} CaseRecord objects produced")

    print(f"\nRunning case_record_to_notice_data bridge ...")
    notices = []
    for r in records:
        notices.extend(case_record_to_notice_data(r, "Clermont"))
    print(f"  {len(notices)} NoticeData rows emitted")

    # ── Diff ────────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("  CANARY VERIFICATION — Clermont foreclosure 2026-06-08..14")
    print("═" * 70)

    sift_case_numbers = {r.case_number for r in records}
    common = sift_case_numbers & baseline_case_numbers
    sift_only = sift_case_numbers - baseline_case_numbers
    h3_only = baseline_case_numbers - sift_case_numbers

    print(f"\n  Case-number coverage:")
    print(f"    H3 baseline:     {len(baseline_case_numbers)} cases")
    print(f"    SiftStack port:  {len(sift_case_numbers)} cases")
    print(f"    Intersection:    {len(common)}")
    if sift_only: print(f"    Only in SiftStack: {sorted(sift_only)}")
    if h3_only:   print(f"    Only in H3:        {sorted(h3_only)}")

    print(f"\n  Row counts:")
    h3_first_rows = [r for r in baseline_rows if r["Case Number"].strip()]
    print(f"    H3 CSV rows (one per defendant, blank-continuation): "
          f"{len(baseline_rows)}")
    print(f"    H3 first-row-per-case:                                {len(h3_first_rows)}")
    print(f"    SiftStack NoticeData (one per defendant, no blanks):  {len(notices)}")

    # ── Spot check ──────────────────────────────────────────────────
    print(f"\n  Spot-check cases (case# → property addr | owner):")
    print(f"    {'CASE #':<18s}  {'PROP STREET':<35s}  {'OWNER':<35s}")
    print(f"    {'-'*18}  {'-'*35}  {'-'*35}")

    h3_by_case = {r["Case Number"].strip(): r for r in h3_first_rows}
    sift_by_case = {r.case_number: r for r in records}

    diffs = 0
    for cn in sorted(common):
        h3 = h3_by_case[cn]; sf = sift_by_case[cn]
        h3_addr = f'{h3["Property Street"]}, {h3["Property City"]} {h3["Property Zip"]}'.strip()
        sf_addr = f'{sf.property_street}, {sf.property_city} {sf.property_zip}'.strip()
        h3_owner = h3["Owner Name"]
        sf_owner = sf.defendants[0].name if sf.defendants else "(none)"
        addr_m = "✓" if h3_addr == sf_addr else "✗"
        own_m  = "✓" if h3_owner == sf_owner else "✗"
        print(f"    {cn:<18s}  {sf_addr[:35]:<35s}  {sf_owner[:35]:<35s}  {addr_m}{own_m}")
        if addr_m == "✗":
            print(f"      addr: H3={h3_addr!r}")
            print(f"            SF={sf_addr!r}")
            diffs += 1
        if own_m == "✗":
            print(f"      owner: H3={h3_owner!r}  SF={sf_owner!r}")
            diffs += 1

    # ── Verdict ─────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    if (len(sift_case_numbers) == len(baseline_case_numbers)
            and not sift_only and not h3_only and diffs == 0):
        print("  ✓ PASS — case coverage matches, spot-check cases identical")
    else:
        print(f"  ! REVIEW NEEDED — coverage diff or {diffs} spot-check mismatches")
    print("═" * 70)


if __name__ == "__main__":
    main()
