"""Ohio two-pass workflow — Pass 2.

Consumes a DataSift-exported CSV (from `Manage → Export` after DataSift's
Skip Trace completes), runs Tracerfy Advanced batch on records DataSift
couldn't populate, merges the results back, runs Trestle on the combined
phone set, and writes a final dial-list CSV.

The pass 1 → DataSift → pass 2 workflow:

    # 06:00 AM — Pass 1 (cron):
    python src/ohio_orchestrator.py daily --two-pass
        → scrapes, enriches (Smarty/Auditor/Obituary/Ancestry),
          uploads to DataSift, exits. Tracerfy + Trestle SKIPPED.

    # 06:45 AM (after DataSift Skip Trace completes) — Pass 2:
    python src/ohio_pass2.py --csv output/enriched/<datasift-export>.csv
        → identifies rows DataSift left phone-less, runs Tracerfy
          Advanced on THOSE only (~5-10 rows vs 30), merges phones
          back, Trestle-scores the merged phone set, writes
          output/dial_list_YYYYMMDD.csv.

Why "after DataSift" instead of "before":
  * Tracerfy runs on ~5-10 rows/day instead of 30 (only DataSift misses)
  * No wasted spend on overlap — DataSift's automatic address-based
    owner-swap covers many cases at $0
  * Trestle scores the merged (fuller) phone set instead of just
    Tracerfy's subset
  * Cost delta: ~$18/mo savings vs Tracerfy-before-DataSift

The DataSift export format (from `tests.csv` reference) differs from
SiftStack's upload format: phones live in ``Phone 1..30`` slots with
per-phone ``Phone Type N`` / ``Phone Tags N`` / ``Phone Is Connected N``
sibling columns. We preserve DataSift's structure and only mutate
phone/tag slots.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# DataSift's export CSV has 30 phone slots + 10 email slots. Each phone
# has 5 sibling columns (Phone N, Phone Type N, Phone Status N,
# Phone Tags N, Phone Is Connected N).
MAX_PHONE_SLOTS = 30
MAX_EMAIL_SLOTS = 10


def _phone_col(n: int) -> str:
    return f"Phone {n}"


def _phone_tag_col(n: int) -> str:
    return f"Phone Tags {n}"


def _phone_type_col(n: int) -> str:
    return f"Phone Type {n}"


def _phone_connected_col(n: int) -> str:
    return f"Phone Is Connected {n}"


def _phone_status_col(n: int) -> str:
    return f"Phone Status {n}"


def _email_col(n: int) -> str:
    return f"Email {n}"


def _row_has_phone(row: dict) -> bool:
    """A row is 'covered' if any of its 30 phone slots has a value."""
    for i in range(1, MAX_PHONE_SLOTS + 1):
        if (row.get(_phone_col(i)) or "").strip():
            return True
    return False


def _row_phones(row: dict) -> list[str]:
    """Return all populated phones from a DataSift row."""
    return [
        (row.get(_phone_col(i)) or "").strip()
        for i in range(1, MAX_PHONE_SLOTS + 1)
        if (row.get(_phone_col(i)) or "").strip()
    ]


def _write_phone_at_slot(row: dict, slot: int, phone: str,
                          phone_type: str = "", is_connected: str = "") -> None:
    """Populate one phone slot with all sibling metadata columns."""
    row[_phone_col(slot)] = phone
    row[_phone_type_col(slot)] = phone_type
    row[_phone_status_col(slot)] = "UNKNOWN"
    row[_phone_connected_col(slot)] = is_connected
    # Phone Tags stays blank — Trestle populates it in step 6


def _next_empty_phone_slot(row: dict) -> int | None:
    """First slot 1..30 that's empty. None if all full."""
    for i in range(1, MAX_PHONE_SLOTS + 1):
        if not (row.get(_phone_col(i)) or "").strip():
            return i
    return None


def _write_email_at_slot(row: dict, slot: int, email: str) -> None:
    row[_email_col(slot)] = email


def _next_empty_email_slot(row: dict) -> int | None:
    for i in range(1, MAX_EMAIL_SLOTS + 1):
        if not (row.get(_email_col(i)) or "").strip():
            return i
    return None


def _row_to_notice(row: dict):
    """Build a NoticeData for the Tracerfy Advanced batch from a
    DataSift export row. Only the fields Tracerfy needs are populated
    (address + city + state + zip). Owner fields stay blank so the
    row routes to the advanced batch."""
    from notice_parser import NoticeData
    return NoticeData(
        county=(row.get("Property county") or "Montgomery"),
        state=(row.get("Property state") or "OH"),
        notice_type="foreclosure",
        case_number="",
        owner_name="",  # force advanced-batch route
        address=(row.get("Property address") or "").strip(),
        city=(row.get("Property city") or "").strip(),
        zip=(row.get("Property zip") or "").strip(),
    )


def _merge_tracerfy_into_row(row: dict, notice) -> tuple[int, int]:
    """After Tracerfy runs on the notice, write its phones/emails into
    empty slots on the DataSift row. Returns (phones_added, emails_added)."""
    from tracerfy_skip_tracer import PHONE_FIELDS, EMAIL_FIELDS
    phones_added = 0
    emails_added = 0

    # If Tracerfy discovered an owner name, populate First/Last only
    # when the DataSift row's owner fields are still blank.
    tracerfy_owner = (notice.owner_name or "").strip()
    if tracerfy_owner:
        # Split "First Last" naively
        parts = tracerfy_owner.split()
        if parts and not (row.get("First Name") or "").strip():
            row["First Name"] = parts[0]
        if len(parts) >= 2 and not (row.get("Last Name") or "").strip():
            row["Last Name"] = " ".join(parts[1:])

    # Append Tracerfy phones into the next empty slots.
    # Only add phones that don't already exist on the row.
    existing_phones = set(_row_phones(row))
    for f in PHONE_FIELDS:
        p = (getattr(notice, f, "") or "").strip()
        if not p or p in existing_phones:
            continue
        slot = _next_empty_phone_slot(row)
        if slot is None:
            break  # row full
        _write_phone_at_slot(row, slot, p,
                              phone_type="MOBILE" if "mobile" in f else "LANDLINE",
                              is_connected="")
        existing_phones.add(p)
        phones_added += 1

    existing_emails = {(row.get(_email_col(i)) or "").strip().lower()
                        for i in range(1, MAX_EMAIL_SLOTS + 1)
                        if (row.get(_email_col(i)) or "").strip()}
    for f in EMAIL_FIELDS:
        e = (getattr(notice, f, "") or "").strip()
        if not e or e.lower() in existing_emails:
            continue
        slot = _next_empty_email_slot(row)
        if slot is None:
            break
        _write_email_at_slot(row, slot, e)
        existing_emails.add(e.lower())
        emails_added += 1

    return phones_added, emails_added


def _trestle_score_all_rows(rows: list[dict], api_key: str | None) -> dict:
    """Trestle-score every phone across every row. Writes tier tag into
    the row's ``Phone Tags N`` column for the matching phone slot.

    Returns stats dict: {phones_scored, tier_tags_applied, cost}.
    """
    from phone_validator import score_record_phones, clean_phone, COST_PER_PHONE
    from notice_parser import NoticeData

    # Build synthetic NoticeData with all phones packed into flat fields
    # so score_record_phones can process them uniformly.
    class _RowNotice:
        """Duck-typed NoticeData shim — carries only the phone fields
        score_record_phones needs and a back-reference to the source row."""
        def __init__(self, row):
            self._row = row
            self.heir_map_json = ""
            phones = _row_phones(row)
            # phone_validator uses primary_phone + mobile_1..5 + landline_1..3
            self.primary_phone = phones[0] if len(phones) > 0 else ""
            for i in range(1, 6):
                setattr(self, f"mobile_{i}", phones[i] if i < len(phones) else "")
            for i in range(1, 4):
                idx = 5 + i
                setattr(self, f"landline_{i}", phones[idx] if idx < len(phones) else "")

    shims = [_RowNotice(r) for r in rows if _row_has_phone(r)]
    if not shims:
        logger.info("Trestle: no phones to score across %d rows", len(rows))
        return {"phones_scored": 0, "tier_tags_applied": 0, "cost": 0.0}

    logger.info("Trestle: scoring phones across %d rows", len(shims))
    results = score_record_phones(shims, api_key=api_key)

    # Apply tiers back to the row's Phone Tags N column that matches
    # each phone.
    tier_tags_applied = 0
    for shim in shims:
        row = shim._row
        for slot in range(1, MAX_PHONE_SLOTS + 1):
            p = (row.get(_phone_col(slot)) or "").strip()
            if not p:
                continue
            cleaned = clean_phone(p)
            score = results.get(cleaned)
            if score:
                row[_phone_tag_col(slot)] = score.get("tier", "")
                tier_tags_applied += 1

    cost = len(results) * COST_PER_PHONE
    logger.info("Trestle: scored %d phones, applied %d tier tags "
                "(cost $%.4f)", len(results), tier_tags_applied, cost)
    return {
        "phones_scored": len(results),
        "tier_tags_applied": tier_tags_applied,
        "cost": cost,
    }


async def run_pass2(csv_path: Path, output_path: Path | None = None) -> dict:
    """Run Pass 2 on a DataSift-exported CSV.

    Steps:
      1. Parse CSV
      2. Identify miss rows (no phones)
      3. Convert misses → NoticeData, run Tracerfy Advanced
      4. Merge Tracerfy results back into the CSV rows
      5. Trestle-score every phone across all rows
      6. Write final CSV (defaults to output/dial_list_YYYYMMDD.csv)

    Returns stats dict.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    with csv_path.open() as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    logger.info("Pass 2: loaded %d rows from %s", len(rows), csv_path)

    # Step 2: identify misses
    misses = [r for r in rows if not _row_has_phone(r)]
    hits_datasift = len(rows) - len(misses)
    logger.info(
        "Pass 2 miss detection: %d/%d rows have phones from DataSift, "
        "%d rows are misses",
        hits_datasift, len(rows), len(misses),
    )

    stats = {
        "total_rows": len(rows),
        "datasift_hits": hits_datasift,
        "datasift_misses": len(misses),
        "tracerfy_recovered": 0,
        "tracerfy_phones_added": 0,
        "tracerfy_emails_added": 0,
        "tracerfy_cost_usd": 0.0,
        "trestle_phones_scored": 0,
        "trestle_tier_tags_applied": 0,
        "trestle_cost_usd": 0.0,
    }

    # Step 3+4: Tracerfy Advanced on misses only
    if misses:
        miss_notices = [_row_to_notice(r) for r in misses]
        # Filter: must have address + city
        valid = [
            (r, n) for r, n in zip(misses, miss_notices)
            if n.address and n.city
        ]
        if valid:
            logger.info("Pass 2: submitting %d miss row(s) to Tracerfy "
                         "Advanced batch (~$%.2f)",
                         len(valid), len(valid) * 0.04)
            notices_to_trace = [n for _, n in valid]
            from tracerfy_skip_tracer import batch_skip_trace
            t_stats = batch_skip_trace(notices_to_trace)
            stats["tracerfy_cost_usd"] = t_stats.get("cost", 0.0)
            stats["tracerfy_recovered"] = t_stats.get("advanced_matched", 0)

            # Step 4: merge results back into the source rows
            for r, n in valid:
                p_added, e_added = _merge_tracerfy_into_row(r, n)
                stats["tracerfy_phones_added"] += p_added
                stats["tracerfy_emails_added"] += e_added
        else:
            logger.info("Pass 2: %d miss rows but none had address+city — "
                         "cannot run Tracerfy Advanced", len(misses))

    # Step 5: Trestle on the merged phone set
    import config
    trestle_key = getattr(config, "TRESTLE_API_KEY", "") or None
    if trestle_key and os.environ.get("TRESTLE_ENABLED") == "1":
        t_stats = _trestle_score_all_rows(rows, trestle_key)
        stats["trestle_phones_scored"] = t_stats["phones_scored"]
        stats["trestle_tier_tags_applied"] = t_stats["tier_tags_applied"]
        stats["trestle_cost_usd"] = t_stats["cost"]
    else:
        logger.info("Trestle: skipped (no key or TRESTLE_ENABLED != 1)")

    # Step 6: write final CSV
    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        output_path = out_dir / f"dial_list_{ts}.csv"
    output_path = Path(output_path)

    with output_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    stats["output_path"] = str(output_path)
    logger.info("Pass 2 complete: wrote final dial list → %s", output_path)
    return stats


def _cli():
    parser = argparse.ArgumentParser(
        description="Ohio two-pass workflow — Pass 2 (post-DataSift).",
    )
    parser.add_argument("--csv", required=True,
                        help="Path to the DataSift-exported CSV.")
    parser.add_argument("--output", default=None,
                        help="Where to write the final dial list "
                             "(default: output/dial_list_YYYYMMDD_HHMMSS.csv).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    result = asyncio.run(run_pass2(
        csv_path=Path(args.csv),
        output_path=Path(args.output) if args.output else None,
    ))
    print("\n" + "=" * 60)
    print("PASS 2 SUMMARY")
    print("=" * 60)
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
