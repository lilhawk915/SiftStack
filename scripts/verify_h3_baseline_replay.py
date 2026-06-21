"""Offline replay verification — feed H3 baseline captures through the
new SiftStack-native integration + bridge, compare against H3's own
final CSV.

Inputs (pre-fetched from Apify KV store):
  /tmp/h3_baseline/montgomery_foreclosure_full/
    - INPUT  (H3 run input — date window, mode, etc.)
    - {case}_party.html / _service.html / _docket.html / _summary.html
    - {case}_docket_entries.json
    - parsed_cases.json
    - montgomery_06-08-2026_to_06-14-2026.csv  ← H3 BASELINE OUTPUT

This script:
  1. Reconstructs CaseDetailCapture for each of the 14 cases
  2. Runs them through integrate_montgomery_foreclosure (SiftStack port)
  3. Bridges through case_record_to_notice_data → list[NoticeData]
  4. Compares against H3's baseline CSV:
       * total row counts
       * case-number coverage
       * spot-check 5 addresses + 5 case numbers
  5. Reports PASS / FAIL with concrete diffs

Both H3 and SiftStack operate on IDENTICAL captured HTML — any diff
is purely from the port, not from portal nondeterminism.

Run: ``python scripts/verify_h3_baseline_replay.py``
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

# Make src/ importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from h3.integration import integrate_montgomery_foreclosure
from h3.notice_data_bridge import case_record_to_notice_data
from h3.scrapers.mcohio import (
    CaseDetailCapture,
    CaseScreenCapture,
    DocketEntry,
)


BASELINE_DIR = Path("/tmp/h3_baseline/montgomery_foreclosure_full")
BASELINE_CSV = BASELINE_DIR / "montgomery_06-08-2026_to_06-14-2026.csv"


def _case_key(path: Path) -> str:
    """Extract '2026 CV 03452' from a filename like
    'montgomery_..._case_2026_CV_03452_party.html'."""
    m = re.search(r"case_(\d{4})_CV_(\d{5})", path.name)
    if not m:
        return ""
    return f"{m.group(1)} CV {m.group(2)}"


def load_captures() -> list[CaseDetailCapture]:
    """Reconstruct CaseDetailCapture objects from the baseline HTML files."""
    by_case = defaultdict(dict)
    for p in BASELINE_DIR.glob("*_case_*.*"):
        key = _case_key(p)
        if not key:
            continue
        if p.name.endswith("_party.html"):
            by_case[key]["party"] = p.read_text(errors="replace")
        elif p.name.endswith("_service.html"):
            by_case[key]["service"] = p.read_text(errors="replace")
        elif p.name.endswith("_docket.html"):
            by_case[key]["docket"] = p.read_text(errors="replace")
        elif p.name.endswith("_summary.html"):
            by_case[key]["summary"] = p.read_text(errors="replace")
        elif p.name.endswith("_docket_entries.json"):
            by_case[key]["docket_entries"] = json.loads(p.read_text())

    caps = []
    for case_num, parts in sorted(by_case.items()):
        screens = []
        for sname in ("summary", "party", "service", "docket"):
            html = parts.get(sname, "")
            if html:
                screens.append(CaseScreenCapture(
                    screen=sname, final_url="", html=html,
                ))
        docket_entries_raw = parts.get("docket_entries", [])
        docket_entries = [
            DocketEntry(
                docketid=d.get("docketid", ""),
                case_id=d.get("case_id", ""),
                date_filed=d.get("date_filed", ""),
                document_type=d.get("document_type", ""),
                description=d.get("description", ""),
                download_url=d.get("download_url", ""),
            )
            for d in docket_entries_raw
        ]
        caps.append(CaseDetailCapture(
            case_number=case_num,
            case_id="",
            screens=screens,
            docket_entries=docket_entries,
            pdfs=[],   # H3 didn't fetch PDFs in this run
        ))
    return caps


def load_h3_baseline() -> list[dict]:
    """Parse H3's baseline CSV (15 cases, ~52 rows)."""
    with BASELINE_CSV.open() as f:
        return list(csv.DictReader(f))


def main():
    print(f"Loading H3 baseline captures from {BASELINE_DIR} ...")
    caps = load_captures()
    print(f"  {len(caps)} CaseDetailCapture objects reconstructed")
    print(f"  Case numbers: {[c.case_number for c in caps]}")

    print(f"\nLoading H3 baseline CSV from {BASELINE_CSV} ...")
    baseline_rows = load_h3_baseline()
    print(f"  {len(baseline_rows)} rows")
    baseline_case_numbers = {r["Case Number"] for r in baseline_rows
                              if r["Case Number"]}
    print(f"  {len(baseline_case_numbers)} unique case numbers")

    print(f"\nRunning SiftStack integrate_montgomery_foreclosure ...")
    records = integrate_montgomery_foreclosure(caps)
    print(f"  {len(records)} CaseRecord objects produced")

    print(f"\nRunning case_record_to_notice_data bridge ...")
    notices = []
    for r in records:
        notices.extend(case_record_to_notice_data(r, "Montgomery"))
    print(f"  {len(notices)} NoticeData rows emitted")

    # ── Diff ────────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("  CANARY VERIFICATION — Montgomery foreclosure 2026-06-08..14")
    print("═" * 70)

    sift_case_numbers = {r.case_number for r in records}
    common = sift_case_numbers & baseline_case_numbers
    sift_only = sift_case_numbers - baseline_case_numbers
    h3_only = baseline_case_numbers - sift_case_numbers

    print(f"\n  Case-number coverage:")
    print(f"    H3 baseline:     {len(baseline_case_numbers)} unique cases")
    print(f"    SiftStack port:  {len(sift_case_numbers)} unique cases")
    print(f"    Intersection:    {len(common)} cases")
    if sift_only:
        print(f"    Only in SiftStack: {sorted(sift_only)}")
    if h3_only:
        print(f"    Only in H3:       {sorted(h3_only)}")

    print(f"\n  Row counts:")
    print(f"    H3 CSV (one row per defendant, blank for continuation):")
    h3_first_rows = [r for r in baseline_rows if r["Case Number"]]
    print(f"      Total CSV rows: {len(baseline_rows)}")
    print(f"      First-row-per-case: {len(h3_first_rows)}")
    print(f"    SiftStack NoticeData (one per defendant, no blanks):")
    print(f"      Total NoticeData: {len(notices)}")

    # ── Spot check 5 cases ──────────────────────────────────────────
    print(f"\n  Spot-check 5 cases (case# → property addr | owner):")
    print(f"    {'CASE #':<18s}  {'PROP STREET':<35s}  {'OWNER':<40s}  {'SRC'}")
    print(f"    {'-'*18}  {'-'*35}  {'-'*40}  ----")

    # Build a lookup: case# → first-row in H3 baseline
    h3_by_case = {r["Case Number"]: r for r in h3_first_rows}
    sift_by_case = defaultdict(list)
    for r in records:
        sift_by_case[r.case_number] = r

    diff_count = 0
    for cn in sorted(common)[:5]:
        h3 = h3_by_case[cn]
        sf = sift_by_case[cn]
        h3_addr = f'{h3["Property Street"]}, {h3["Property City"]} {h3["Property Zip"]}'
        sf_addr = (
            f'{sf.property_street}, {sf.property_city} {sf.property_zip}'
        )
        h3_owner = h3["Owner Name"]
        sf_owner = sf.defendants[0].name if sf.defendants else "(none)"
        addr_match = "✓" if h3_addr == sf_addr else "✗"
        owner_match = "✓" if h3_owner == sf_owner else "✗"
        print(f"    {cn:<18s}  {sf_addr[:35]:<35s}  {sf_owner[:40]:<40s}  H3↔SF")
        if not (h3_addr == sf_addr and h3_owner == sf_owner):
            diff_count += 1
            print(f"      {addr_match} property: H3={h3_addr!r}")
            print(f"        SiftStack={sf_addr!r}")
            print(f"      {owner_match} owner:    H3={h3_owner!r}")
            print(f"        SiftStack={sf_owner!r}")

    # ── Verdict ─────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    if (len(sift_case_numbers) == len(baseline_case_numbers)
            and not sift_only and not h3_only
            and diff_count == 0):
        print("  ✓ PASS — case coverage matches, 5 spot-check cases identical")
    else:
        print(f"  ! REVIEW NEEDED — case coverage diff or "
              f"{diff_count} spot-check mismatches")
    print("═" * 70)

    # ── Tag verification ────────────────────────────────────────────
    print(f"\n  Tag pipeline check (first record):")
    if notices:
        n = notices[0]
        print(f"    notice_type:    {n.notice_type}")
        print(f"    county:         {n.county}")
        print(f"    state:          {n.state}")
        print(f"    source_url:     {n.source_url}")
        print(f"    owner_name:     {n.owner_name}")
        print(f"    address line:   "
              f"{n.address}, {n.city} {n.state} {n.zip}")
        # Tag set
        import datasift_formatter as df
        tags = df._build_tags(n)
        print(f"    Tags column:    {tags}")
        print(f"    Lists column:   {df.NOTICE_TYPE_TO_LIST.get(n.notice_type, '')}")


if __name__ == "__main__":
    main()
