"""Generic replay harness — verify every available H3 baseline against
the SiftStack-native port.

Walks every subdir under /tmp/h3_baseline/, detects the type
(Montgomery FC vs equivant FC vs Warren FC vs probate), reconstructs
H3-style captures from the saved HTML/JSON, runs them through the
new SiftStack integration + bridge, and diffs against H3's saved
final CSV.

Prints a one-table PASS / FAIL / NO-BASELINE / EMPTY summary for
each county × type combo.

Run: ``python scripts/verify_all_h3_baselines.py``
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from h3.integration import (
    integrate_equivant_foreclosure,
    integrate_montgomery_foreclosure,
    integrate_warren_foreclosure,
)
from h3.notice_data_bridge import (
    case_record_to_notice_data,
    probate_record_to_notice_data,
)
from h3.output_writers.h3_format import CaseRecord, Defendant
from h3.output_writers.probate_format import ProbateRecord
from h3.scrapers.mcohio import (
    CaseDetailCapture as McohioCapture,
    CaseScreenCapture,
    DocketEntry,
)


BASELINE_ROOT = Path("/tmp/h3_baseline")


# ── Per-county configs ──────────────────────────────────────────────


@dataclass
class CaseDir:
    """Detected shape of a baseline directory."""
    county: str
    lead_type: str
    path: Path
    csv_path: Path | None
    integrator: str   # "montgomery" | "equivant" | "warren" | "probate"
    parser_module: str | None = None  # only set for probate


# Maps directory-name prefix to integrator + per-county config.
DIR_CONFIG: dict[str, dict] = {
    # FORECLOSURE
    "montgomery_foreclosure_full": {
        "county": "Montgomery", "lead_type": "foreclosure",
        "integrator": "montgomery",
    },
    "butler_foreclosure_full": {
        "county": "Butler", "lead_type": "foreclosure",
        "integrator": "equivant", "county_arg": "butler",
    },
    "clark_foreclosure_full": {
        "county": "Clark", "lead_type": "foreclosure",
        "integrator": "equivant", "county_arg": "clark",
    },
    "clermont_foreclosure_full": {
        "county": "Clermont", "lead_type": "foreclosure",
        "integrator": "equivant", "county_arg": "clermont",
    },
    "greene_foreclosure_full": {
        "county": "Greene", "lead_type": "foreclosure",
        "integrator": "equivant", "county_arg": "greene",
    },
    "miami_foreclosure_full": {
        "county": "Miami", "lead_type": "foreclosure",
        "integrator": "equivant", "county_arg": "miami",
    },
    # PROBATE — uses per-county parser module
    "butler_probate_full": {
        "county": "Butler", "lead_type": "probate",
        "integrator": "probate",
        "parser_module": "h3.parsers.butler_probate_case_detail",
        "case_re": r"case_(PE\d{2}-\d{2}-\d{4})_detail",
    },
    "clark_probate_full": {
        "county": "Clark", "lead_type": "probate",
        "integrator": "probate",
        # Clark uses butler_probate_case_detail per H3 scraper imports
        "parser_module": "h3.parsers.butler_probate_case_detail",
        "case_re": r"case_(\S+?)_detail",
    },
    "clermont_probate_full": {
        "county": "Clermont", "lead_type": "probate",
        "integrator": "probate",
        "parser_module": "h3.parsers.clermont_probate_case_detail",
        "case_re": r"case_(\S+?)_detail",
    },
    "greene_probate_full": {
        "county": "Greene", "lead_type": "probate",
        "integrator": "probate",
        "parser_module": "h3.parsers.greene_probate_case_detail",
        "case_re": r"case_(\d{4}-EST-\d{4})_detail",
    },
    "miami_probate_full": {
        "county": "Miami", "lead_type": "probate",
        "integrator": "probate",
        # Miami uses butler_probate_case_detail per H3 scraper imports
        "parser_module": "h3.parsers.butler_probate_case_detail",
        "case_re": r"case_(\S+?)_detail",
    },
    "montgomery_probate_full": {
        "county": "Montgomery", "lead_type": "probate",
        "integrator": "probate",
        "parser_module": "h3.parsers.probate_case_detail",
        "case_re": r"case_(\S+?)_detail",
    },
    "warren_probate_full": {
        "county": "Warren", "lead_type": "probate",
        "integrator": "probate",
        "parser_module": "h3.parsers.warren_probate_case_detail",
        "case_re": r"case_(\S+?)_detail",
    },
}


def _find_baseline_csv(d: Path) -> Path | None:
    """Locate H3's integrated CSV (foreclosure) or probate_leads.csv."""
    for p in d.glob("*.csv"):
        # Skip the SiftStack-flavored siftstack.csv
        if "siftstack" in p.name.lower():
            continue
        return p
    return None


# ── Replay logic per integrator ─────────────────────────────────────


def _replay_montgomery(d: Path) -> list:
    """Reconstruct CaseDetailCaptures for Montgomery, run integration + bridge."""
    by_case = defaultdict(dict)
    for p in d.glob("*_case_*.*"):
        m = re.search(r"case_(\d{4})_CV_(\d{5})", p.name)
        if not m:
            continue
        key = f"{m.group(1)} CV {m.group(2)}"
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
            if parts.get(sname):
                screens.append(CaseScreenCapture(
                    screen=sname, final_url="", html=parts[sname]))
        des = [
            DocketEntry(
                docketid=e.get("docketid",""), case_id=e.get("case_id",""),
                date_filed=e.get("date_filed",""),
                document_type=e.get("document_type",""),
                description=e.get("description",""),
                download_url=e.get("download_url",""),
            )
            for e in parts.get("docket_entries", [])
        ]
        caps.append(McohioCapture(
            case_number=case_num, case_id="", screens=screens,
            docket_entries=des, pdfs=[],
        ))
    records = integrate_montgomery_foreclosure(caps)
    notices = []
    for r in records:
        notices.extend(case_record_to_notice_data(r, "Montgomery"))
    return records, notices


@dataclass
class _EquivantCap:
    """Lightweight stand-in for h3.scrapers.{county}.CaseDetailCapture."""
    case_number: str
    html: str = ""
    final_url: str = ""
    error: str = ""


def _replay_equivant(d: Path, county_arg: str) -> list:
    """Same shape as Clermont replay — works for Butler/Clark/Greene/Miami too.

    Case-number format varies by county:
      Clermont/Clark/Greene/Miami: ``2026 CVE 00819`` (3 parts)
      Butler:                      ``CV 2026 06 1385`` (4 parts)
    Extract everything between ``case_`` and ``_detail``; replace
    underscores with spaces — covers both shapes.
    """
    caps = []
    for p in sorted(d.glob("*_detail.html")):
        m = re.search(r"case_(.+?)_detail\.html$", p.name)
        cn = m.group(1).replace("_", " ") if m else p.stem
        caps.append(_EquivantCap(
            case_number=cn, html=p.read_text(errors="replace"),
        ))
    records = integrate_equivant_foreclosure(caps, county_arg)
    notices = []
    for r in records:
        notices.extend(case_record_to_notice_data(r, county_arg.capitalize()))
    return records, notices


def _replay_probate(d: Path, parser_module: str, case_re: str,
                    county: str) -> list:
    """Probate replay — works across all 7 county parsers."""
    import importlib
    mod = importlib.import_module(parser_module)
    # The various parsers have slightly different function names —
    # find the one that's the canonical "parse case detail" entry.
    parse_fn = (
        getattr(mod, "parse_case_detail", None)
        or getattr(mod, "parse_butler_probate_case_detail", None)
    )
    if parse_fn is None:
        raise RuntimeError(f"No parser found in {parser_module}")

    records: list[ProbateRecord] = []
    case_re_compiled = re.compile(case_re)
    for p in sorted(d.glob("*_detail.html")):
        m = case_re_compiled.search(p.name)
        if not m:
            continue
        cn = m.group(1)
        html = p.read_text(errors="replace")
        try:
            detail = parse_fn(html)
        except Exception as e:
            continue
        if not detail.case_number:
            detail.case_number = cn
        # Build a ProbateRecord from the parsed detail. Parsers all
        # produce different dataclass shapes, but ProbateRecord field
        # names + the parser's attributes mostly align. Use getattr
        # defensively to handle the variations.
        records.append(ProbateRecord(
            case_number=detail.case_number,
            case_type=getattr(detail, "case_type", "") or getattr(detail, "filing_type", ""),
            date_filed=getattr(detail, "file_date", ""),
            decedent_name=getattr(detail, "decedent_name", ""),
            date_of_death=getattr(detail, "date_of_death", ""),
            action=getattr(detail, "action", ""),
            relationship=getattr(detail, "fiduciary_relationship", ""),
            fiduciary_name=getattr(detail, "fiduciary_name", ""),
            fiduciary_address=getattr(detail, "fiduciary_address", ""),
            fiduciary_phone=getattr(detail, "fiduciary_phone", ""),
            subject_property=getattr(detail, "decedent_address", "")
                or getattr(detail, "subject_property", ""),
        ))
    notices = [probate_record_to_notice_data(r, county) for r in records]
    return records, notices


# ── Diff against baseline CSV ───────────────────────────────────────


def _diff_foreclosure(records, notices, csv_path: Path) -> tuple[str, str]:
    """Returns (verdict, details). 'verdict' is PASS / FAIL / NO-BASELINE."""
    if not csv_path or not csv_path.exists():
        return ("NO-BASELINE", "no H3 CSV to diff against")
    with csv_path.open() as f:
        baseline = list(csv.DictReader(f))
    h3_cases = {r["Case Number"].strip() for r in baseline
                if r.get("Case Number","").strip()}
    sift_cases = {r.case_number for r in records}
    common = sift_cases & h3_cases
    if sift_cases == h3_cases:
        return ("PASS",
                f"H3 {len(h3_cases)}cs / SiftStack {len(sift_cases)}cs / "
                f"{len(notices)} NoticeData rows")
    return ("FAIL",
            f"H3 {len(h3_cases)} cases vs SF {len(sift_cases)} — "
            f"only-H3={sorted(h3_cases-sift_cases)[:3]}, "
            f"only-SF={sorted(sift_cases-h3_cases)[:3]}")


def _diff_probate(records, notices, csv_path: Path) -> tuple[str, str]:
    if not csv_path or not csv_path.exists():
        return ("NO-BASELINE", "no H3 probate_leads.csv")
    with csv_path.open() as f:
        baseline = list(csv.DictReader(f))
    h3_cases = {r.get("Case Number","").strip() for r in baseline
                if r.get("Case Number","").strip()}
    sf_cases = {r.case_number for r in records}

    # Detect H3's post-parse date-window filter (main.py lines 220-243):
    # H3 captures every case the search returned, then filters down to
    # the requested date window before writing the CSV. The replay
    # harness skips that filter, so SF is a superset. As long as
    # H3 ⊆ SF, this is a SUPERSET match, not a regression.
    if h3_cases and h3_cases <= sf_cases and len(sf_cases) > len(h3_cases):
        return ("SUPERSET-OK",
                f"H3 {len(h3_cases)} ⊆ SF {len(sf_cases)} "
                f"(H3 date-window filter not replayed here)")
    if sf_cases == h3_cases:
        # Same coverage; check if fields are populated
        sample_h3_nonempty = sum(
            1 for r in baseline
            if any(v.strip() for k,v in r.items() if k != "Case Number")
        )
        if sample_h3_nonempty == 0:
            return ("INHERITED-GAP",
                    f"{len(h3_cases)} cases match; H3 baseline has all-empty "
                    f"fields beyond case# (H3 parser incomplete)")
        return ("PASS",
                f"H3 {len(h3_cases)}cs / SiftStack {len(sf_cases)}cs / "
                f"{len(notices)} NoticeData")
    return ("FAIL",
            f"H3 {len(h3_cases)} vs SF {len(sf_cases)}")


# ── Driver ──────────────────────────────────────────────────────────


def main():
    results = []
    for dirname, cfg in DIR_CONFIG.items():
        d = BASELINE_ROOT / dirname
        if not d.exists():
            results.append((cfg["county"], cfg["lead_type"], "MISSING-DIR",
                            f"{d} not present"))
            continue
        csv_path = _find_baseline_csv(d)

        try:
            if cfg["integrator"] == "montgomery":
                records, notices = _replay_montgomery(d)
                verdict, msg = _diff_foreclosure(records, notices, csv_path)
            elif cfg["integrator"] == "equivant":
                records, notices = _replay_equivant(d, cfg["county_arg"])
                verdict, msg = _diff_foreclosure(records, notices, csv_path)
            elif cfg["integrator"] == "warren":
                from h3.scrapers.warren import CaseDetailCapture as WCap
                records, notices = [], []   # not implemented in this harness
                verdict, msg = ("SKIP-HARNESS",
                                "Warren replay needs PDF bytes (not pulled)")
            elif cfg["integrator"] == "probate":
                records, notices = _replay_probate(
                    d, cfg["parser_module"], cfg["case_re"], cfg["county"],
                )
                verdict, msg = _diff_probate(records, notices, csv_path)
            else:
                verdict, msg = ("UNKNOWN", "no integrator")
            # Detect "empty source" up-front
            if csv_path is None and verdict == "NO-BASELINE":
                # Check if scrape actually captured anything
                n_html = len(list(d.glob("*_detail.html"))) + \
                         len(list(d.glob("*_party.html")))
                if n_html == 0:
                    verdict = "EMPTY-SOURCE"
                    msg = ("H3 run captured 0 cases (no data for this "
                           "county/week)")
        except Exception as e:
            verdict, msg = ("ERROR", f"{type(e).__name__}: {e}")
        results.append((cfg["county"], cfg["lead_type"], verdict, msg))

    # ── Render table ────────────────────────────────────────────────
    print()
    print("═" * 90)
    print(f"  {'COUNTY':<12s}  {'LEAD TYPE':<14s}  {'VERDICT':<14s}  {'DETAILS'}")
    print("═" * 90)
    verdict_counts = defaultdict(int)
    for c, lt, v, msg in sorted(results):
        verdict_counts[v] += 1
        # Color-style icons
        icon = {
            "PASS": "✓", "SUPERSET-OK": "✓",
            "INHERITED-GAP": "⚠",
            "EMPTY-SOURCE": "○", "NO-BASELINE": "○",
            "SKIP-HARNESS": "·", "MISSING-DIR": "?",
            "FAIL": "✗", "ERROR": "✗", "UNKNOWN": "?",
        }.get(v, " ")
        print(f"  {c:<12s}  {lt:<14s}  {icon} {v:<12s}  {msg[:50]}")
    print("═" * 90)
    print(f"  Totals: " + ", ".join(f"{v}={n}" for v,n in sorted(verdict_counts.items())))


if __name__ == "__main__":
    main()
