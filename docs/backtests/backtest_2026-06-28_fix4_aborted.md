# Probate Fix 4 — Aborted (HARD STOP)

**Date:** 2026-06-28
**Outcome:** Fix shipped to working tree, validated in isolation, **reverted unshipped** after Validation 1 hard-stop fired.
**No git commit.** No holdout v3 ran. No PR-recall improvement to report yet.

## What the spec asked for

Holdout v2 (`backtest_2026-06-27_holdout_v2.md`) attributed 76% PR recall (still below the 85% bracket) to `_detail_to_record`'s `date_filed` semantics. Two named failure modes:

1. Docket-entry inference picks up pre-dated supporting documents → `min(docket_dates)` shifts `date_filed` earlier than actual filing date.
2. `case_status_date` fallback for CLOSED cases returns the closure date, not the filing date → made 2026-04-02 unrunnable (anchor span 600 case#s).

The 4th-fix prescription was to add an explicit "Date Filed" extractor from the case-detail HTML, with new precedence: explicit field → docket inference → empty (no `case_status_date` fallback).

## What recon found

There is no explicit "Date Filed" label in the Montgomery probate case-detail HTML. The summary section has these labels (verified live on EST00698):

```
"Decedent's Name"     'JAYSON BROWN'
'Date of Death'       '03/31/2026'
'Case Number'         '2026EST00698'
'Case Type'           '2 FULL ADMIN; W/O WILL'
'Case Status'         'OPEN 04-14-2026'       ← inline date here
'Appointment date'    '04-17-2026'
'Attorney'            ...
'Fiduciary'           ...
'Co-Fiduciary'        ''
'Related Cases'       ''
'Balance'             '65.00'
```

The filing date is implicit in `Case Status: OPEN 04-14-2026`. The existing `_parse_status` already extracts that into `case_status_date`. For OPEN cases, it IS the filing date — already documented in the module docstring's line 24.

## The fix I shipped (then reverted)

Adjusted precedence in `_detail_to_record`:
1. If `case_status == "OPEN"` and `case_status_date` set → use it (authoritative).
2. Else (CLOSED or unknown status) → fall back to `min(docket_entries.date)`.
3. Last resort: `appointment_date`.
4. **No `case_status_date` fallback for CLOSED cases** — that's the closure date.

## Isolated test — the fix WAS working

Probed 4 sample cases spanning the [00039, 00650] case# range to confirm the new precedence behaves correctly:

| Case | Status | status_date | docket_min | New date_filed |
|---|---|---|---|---|
| EST00042 | OPEN | 2026-01-12 | 2026-01-12 | **2026-01-12** ✓ uses status_date |
| EST00100 | **CLOSED** | 2026-03-18 | 2026-01-22 | **2026-01-22** ✓ uses docket_min (NOT closure date) |
| EST00300 | OPEN | 2026-02-23 | 2026-02-19 | **2026-02-23** ✓ uses status_date (NOT pre-dated docket entry) |
| EST00500 | OPEN | 2026-03-18 | 2026-03-18 | **2026-03-18** ✓ both agree |

The fix correctly resolves both named failure modes on these samples.

## But Validation 1 (4/02) still failed

Ran the 4/02 backfill with the fix. Gap-fill log:
```
gap-fill: probing 442 case# gaps in [2026EST00039, 2026EST00650] (anchor: 5 cases in window, ±15 padded)
```

Anchor span 612 case#s — identical to the pre-fix state. 5 cases across case# range 00039 to 00650 were flagged as `date_filed == 2026-04-02`.

This triggered the spec's hard stop: **"If 4/02 still has an anchor span over 100 case#s after the fix → STOP. The closure-date bug wasn't actually the root cause."**

## Real root cause of 4/02 (NOT closure date)

5 cases with case#s spanning 00039 → 00650 reading as `date_filed == 2026-04-02` can't be explained by my fix's coverage. The most-likely explanation is **case re-opening**: a case originally filed in January (low case#) closed in March, then RE-OPENED on 2026-04-02. Its `Case Status` row reads "OPEN 04-02-2026" — same format as a freshly-filed case. My fix uses `case_status_date` for OPEN cases as filing date, which for re-opened cases is the **re-open date**, not the original filing date.

Probate cases get re-opened for: amended fiduciary appointments, supplemental distributions, late-discovered assets, partial revocations, etc. The re-opened-on-4/02 cluster wouldn't be unusually large — but with year-wide listing returning only 345 sparse cases, the few re-opens dated 4/02 dominate the in-window set.

`case_status_date` is the date of the **current status**, not strictly the **original filing date**. There's no single field in the case-detail summary that captures original filing date.

## Where the actual filing date lives

The case docket itself (separate page from case detail) has a first-entry "CASE OPENED" or "PETITION FILED" docket line dated to the actual filing date. For NEW cases that's also the docket_min. For RE-OPENED cases the case-opening entry is still in the docket from the original filing — so `min(docket_dates)` from re-opened cases should give the original filing date.

But that's already the old precedence (`min(docket_dates)` was the original primary, my fix demoted it for OPEN cases). So the OLD code would correctly handle re-opened cases — except the OLD code suffered from the pre-dated-document bug, which is what holdout v2 exposed.

The two failure modes pull in opposite directions:
- For OPEN, freshly-filed cases with pre-dated documents: prefer `case_status_date` over docket_min.
- For RE-OPENED cases: prefer docket_min over `case_status_date`.

Distinguishing the two requires a signal we don't currently extract — typically a "PRIOR CASE" reference on the case-detail page, or a flag indicating re-opening.

## Why I'm not iterating

Per spec: **"If you find yourself tempted to widen the matcher OR add a 5th fix mid-run → STOP. Report and decide."**

Adding the case-re-opening detector (parse "Prior Case Number" field, or detect docket_min ≪ case_status_date for OPEN cases) would be a 5th fix. The spec says stop.

## Recommended next step (NOT shipped)

If this work continues: detect re-opened cases by `(case_status == "OPEN" AND docket_min ≪ case_status_date)`, and treat those as needing docket_min as filing date instead of case_status_date. Concretely:

```python
if detail.case_status == "OPEN" and detail.case_status_date:
    # Default: status date IS filing date for OPEN cases.
    filing_date = detail.case_status_date
    # BUT: if docket has entries pre-dating status_date by more than
    # ~14 days, this is likely a re-opened case where status_date is
    # the re-open date. Trust the older docket entries for filing date.
    if docket_entries:
        dated = [e.date for e in docket_entries if e.date]
        if dated:
            docket_min = min(dated)
            if (date.fromisoformat(detail.case_status_date)
                  - date.fromisoformat(docket_min)).days > 14:
                filing_date = docket_min
```

This would handle BOTH failure modes. Validation would be on 4/02 (5 cases → some other count, anchor span <100) AND 4/14 (EST00698 + EST00700 captured).

## What the spec said to report

1. **4/02 ran cleanly?** No. Anchor still 442 case#s. Hard stop fired before the run completed.
2. **Aggregate FC + PR recall across 7 days?** Did not run holdout v3 — fix unshipped.
3. **Per-fix attribution for the 8 named v2 misses?** Not measured — would require running 4/14, 4/15, 4/17, 4/23, 4/30 with the (unshipped) fix.
4. **Phone field accuracy?** Unchanged (no run completed).
5. **Probate workflow shift recommendation?** **Still "wait"** — same as holdout v2. PR recall remains at 76%.

## Code state after this attempt

Identical to before. Last commit on main is still `3c6788f`. No new commits. The reverted fix is gone from the working tree. The 3 commits from yesterday (cbcfe8f + fdf8b5c + 3c6788f) are unchanged and continue to deliver the +20-pt PR recall improvement (56% → 76%) reported in holdout v2.
