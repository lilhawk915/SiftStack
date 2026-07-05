#!/usr/bin/env python3
"""Measure entity_researcher's hit rate on the most recent Ohio daily CSV.

Rationale: after tonight's e2e test showed 0/3 entities resolved on today's
sample (all newly-formed 2025 LLCs or an HOA), we need to know the resolution
rate on typical days before deciding whether to build a direct Ohio SOS
integration. A daily cron produces ~30-50 records with ~3-5 entity-owned rows;
this script scores what fraction of those got a person's name pulled out.

Usage: python3 scripts/measure_entity_hit_rate.py [csv_path]
  (defaults to newest OH_Montgomery_daily_*.csv in output/)

Decision guidance:
  ≥80% resolved → entity_researcher is doing its job; skip SOS integration
  40-80%        → SOS integration is a real lift but not urgent
  <40%          → SOS integration is the highest-leverage next step
"""
import csv
import glob
import os
import re
import sys

# Entity markers — same signal we use elsewhere in the pipeline
_ENTITY_RE = re.compile(
    r"\b(LLC|L\.L\.C|INC|CORP|CORPORATION|TRUST|TR|ESTATE|EST|LTD|LP|"
    r"COMPANY|CO|ASSOCIATION|ASSN|BANK|CREDIT UNION|NA|HOA|FUND|"
    r"HOLDINGS|GROUP|VENTURES|PROPERTIES|REALTY|CAPITAL|MANAGEMENT)\b",
    re.IGNORECASE,
)


def is_entity_from_notes(notes: str) -> tuple[bool, str]:
    """Returns (is_entity, entity_name) — parses 'Entity: NAME |' from Notes."""
    m = re.search(r"Entity:\s*([^|]+)", notes or "")
    if m:
        return True, m.group(1).strip()
    return False, ""


def resolved_person(row) -> bool:
    """A row is 'resolved' if it has Owner First/Last Name AND is entity-owned.
    entity_researcher writes the resolved person into notice.entity_person_name,
    which the CSV writer's fallback ladder pushes into Owner First/Last."""
    first = (row.get("Owner First Name") or "").strip()
    last = (row.get("Owner Last Name") or "").strip()
    if not (first or last):
        return False
    # Reject placeholders and entity leftovers
    full = f"{first} {last}".upper()
    if _ENTITY_RE.search(full):
        return False
    if full.startswith(("UNKNOWN ", "JOHN DOE ", "JANE DOE")):
        return False
    return True


def main(path: str | None):
    if path is None:
        files = sorted(
            glob.glob("output/OH_Montgomery_daily_*.csv"),
            key=os.path.getmtime,
            reverse=True,
        )
        if not files:
            print("No Montgomery daily CSVs found in output/")
            return 1
        path = files[0]
        print(f"Analyzing newest: {path}\n")

    with open(path) as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    entity_rows = []
    for r in rows:
        is_ent, ent_name = is_entity_from_notes(r.get("Notes") or "")
        if is_ent:
            entity_rows.append((r, ent_name))

    if not entity_rows:
        print(f"No entity-owned rows in {total} total. Nothing to measure.")
        return 0

    resolved = [(r, ent) for r, ent in entity_rows if resolved_person(r)]
    unresolved = [(r, ent) for r, ent in entity_rows if not resolved_person(r)]

    print(f"Total rows: {total}")
    print(f"Entity-owned rows: {len(entity_rows)}")
    print(f"Resolved (person name in First/Last): {len(resolved)}/{len(entity_rows)} ({100*len(resolved)/len(entity_rows):.0f}%)")
    print()

    if resolved:
        print("=== RESOLVED ===")
        for r, ent in resolved[:10]:
            first = (r.get("Owner First Name") or "").strip()
            last = (r.get("Owner Last Name") or "").strip()
            print(f"  {ent:45s} → {first} {last}")
        if len(resolved) > 10:
            print(f"  ... and {len(resolved) - 10} more")
        print()

    if unresolved:
        print("=== UNRESOLVED ===")
        for r, ent in unresolved[:10]:
            print(f"  {ent}")
        if len(unresolved) > 10:
            print(f"  ... and {len(unresolved) - 10} more")
        print()

    # Decision recommendation
    rate = len(resolved) / len(entity_rows)
    print("=== VERDICT ===")
    if rate >= 0.8:
        print(f"  {rate*100:.0f}% resolution — entity_researcher is doing its job.")
        print("  Skip Ohio SOS integration; current stack is sufficient.")
    elif rate >= 0.4:
        print(f"  {rate*100:.0f}% resolution — SOS integration is a real lift but not urgent.")
        print("  Revisit if hit rate drifts lower or entity-owned rate grows.")
    else:
        print(f"  {rate*100:.0f}% resolution — SOS integration is the highest-leverage next step.")
        print("  Direct business.ohiosos.gov lookup would recover most of the gap.")

    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(path))
