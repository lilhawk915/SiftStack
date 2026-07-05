# SiftStack Probate Pipeline — Known Limitations

Status as of 2026-06-28. Three tickets covering edge cases that didn't make
it into the 5-fix iteration (`cbcfe8f`, `fdf8b5c`, `3c6788f`, `c911aa2`,
plus the foundational `2040604`). Each is self-contained — pick up any
one without re-reading the backtest history.

PR recall on holdout v3 (6 April days): 88% (30/34). The 12% gap is
attributable to Ticket 1. Tickets 2 and 3 are independent edge cases
not in the v3 sample but observed in prior backtests.

Daily cron path is unaffected by any of these. The limitations bite
multi-week backfills, not same-day scrapes.

---

## Ticket 1 — Fast-close bucketing offset (1-2 day clerical lag)

**Priority:** LOW

**Symptom**
4 of 34 holdout-v3 PR cases bucketed on `docket_min` instead of the
data manager's logged "Date Filed". Off by 1-2 days; cases appear
in the adjacent daily CSV instead of the target date's CSV.

| Case | docket_min | Gypsy's Date Filed | Offset |
|---|---|---|---|
| EST00729 (WILLIAM CARMACK) | 2026-04-16 | 2026-04-17 | +1 day |
| EST00766 (DAVID BAKER) | 2026-04-21 | 2026-04-23 | +2 days |
| EST00772 (JAMES AVRA) | 2026-04-21 | 2026-04-23 | +2 days |
| EST00777 (ROBERT BRENNER) | 2026-04-22 | 2026-04-23 | +1 day |

**Affected case types**
SUMMARY RELEASE, TRANSFER OF REAL ESTATE ONLY, RELEASE OF ADMIN; W/O WILL
(all "fast-close" types that file → process → close within ~1 week).

**Root cause**
Gypsy's "Date Filed" for these case types doesn't correspond to ANY
docket entry — confirmed by the diagnostic dump of all 4 cases'
docket entries. Most likely the **court-stamped filing date on the
petition PDF**, which is one to two days after the supporting-document
docket entries get logged. SiftStack reads `docket_min` (the earliest
docket entry), which is the supporting-document date, not the
petition-stamp date.

**Impact**
- **Cosmetic only** under the current downstream consumer model.
  SiftStack joins downstream tags by `case_number` (see commit
  `b2039cb`), not by `date_filed`. The case is correctly captured,
  the phone is correctly extracted, the auditor enrichment runs —
  it just appears in the 4/22 CSV instead of the 4/23 CSV.
- **Would matter** if a future workflow joins by `date_filed`
  (DataSift's daily list import does this loosely — DataSift's
  "Date Added" field is the join hint for new-record detection).
  Records uploaded to the wrong-day list would still merge by
  property/owner address, but the "first contact date" semantics
  drift by 1-2 days.

**Possible fix**
Apply a case-type-specific date shift for CLOSED fast-close case
types. Hardcoded list:
```python
FAST_CLOSE_OFFSETS = {
    "SUMMARY RELEASE": 2,
    "TRANSFER OF REAL ESTATE ONLY; W/O WILL": 1,
    "RELEASE OF ADMIN; W/O WILL": 1,
}
```
Add the offset to `docket_min` for CLOSED cases whose `case_type`
matches. Crude but covers the observed cases. Better long-term fix:
extract the court-stamp date from the application PDF via Claude
Vision — that's already in the OnBase enrichment path; just add the
filing-date field to the Vision prompt.

**Validation set**
Run a single-day backfill on 2026-04-23. Confirm all 4 of the
named EST00766/772/777 + EST00729 (latter is 4/17 but uses same
case type) appear in the CORRECT day's CSV after the fix.

**Evidence**
- `backtest_2026-06-28_holdout_v3.md` — recall = 88% with 4 misses
- Diagnostic message in the conversation history dated 2026-06-28
  showing all 4 cases' full docket dumps

---

## Ticket 2 — Archived-docket cases (4/02-style anomaly)

**Priority:** LOW

**Symptom**
A small number of cases have a case# implying early-year filing but
a visible docket that only starts months later. When the orchestrator
runs `--date-from` against the docket-implied date, the gap-fill
anchor span balloons (observed 600 case#s on 2026-04-02 vs the
normal 30-60 case# range).

**Affected cases**
~5-10 per year in Montgomery, concentrated on dates where closure
activity clusters. Canonical example: `EST00054` — case# implies
filing in early January 2026, visible docket runs 2026-04-02 to
2026-04-10 only.

**Root cause**
The case-detail HTML's visible docket appears to truncate or archive
older entries on certain cases. The original case-opening docket
entries from the actual filing date aren't on the page. Both
`case_status_date` (closure) and `docket_min` (earliest visible
entry) end up pointing at dates months after the actual filing.

**Impact**
- Backfill cost only — a backfill day with one or more archived-
  docket cases triggers a runaway gap-fill probe range (4+ hours
  per day vs the normal 30 min).
- **Daily cron unaffected.** Today's cases always have full recent
  dockets — the archive truncation only manifests on older cases.
- Mitigation in place: the 6 AM cron only scrapes today's date,
  which never has this issue. Multi-week backfills should skip the
  affected dates manually.

**Possible fix paths** (pick one)

1. **Cap the anchor probe range.** If `[max - min]` exceeds N (say 100),
   fall back to median ± 50. Cheapest fix; clips the runaway without
   addressing the underlying date_filed inaccuracy.

2. **Parse "Prior Case Number" field** if exposed on the case-detail
   page (would need to verify whether this field exists). If present,
   it indicates a re-opened case → the prior case# gives a chronology
   hint.

3. **Estimate filing date from case# sequence.** Build a `case# → date`
   map from the year-wide listing's already-captured cases, then use
   nearest-neighbor interpolation to estimate the true filing date.
   Most accurate; most invasive.

**Validation set**
2026-04-02 — the canonical broken day. After any fix, this day's
backfill should complete in ~30 min with an anchor span under 100
case#s.

**Evidence**
- `backtest_2026-06-28_fix5_aborted.md` — smoking-gun on EST00054

---

## Ticket 3 — Short-form FC case# format

**Priority:** MEDIUM

**Symptom**
Foreclosure cases with case#s in the short-form `2026 CV 0XXX` format
(4 digits after CV instead of the usual 5) don't appear in
SiftStack's foreclosure listing. 8 FC cases missed in the
2026-06-27 failed holdout.

**Affected cases**
Variable by date. The April-2026 holdout (v3) showed 0 misses —
suggests the issue is tied to specific filing batches or older
case types rather than a systematic count.

| Missed case | Date | Owner |
|---|---|---|
| 2026 CV 0484 | 5/22 | HARCUS, CARL S |
| 2026 CV 0485 | 5/22 | FORMER Y XENIA LLC |
| 2026 CV 0486 | 5/22 | FORMER Y XENIA LLC |
| 2026 CV 0501 | 5/22 | CREWE, ROBERT D |
| 2026 CV 0517 | 6/01 | CLARK, CARRIE |
| 2026 CV 0440 | 5/11 | TIBBALS, LYNN K |
| 2026 CV 0415 | 5/01 | CAMPBELL, JARROD R |
| 2026 CV 0402 | 4/22 | HEMMINGSEN, TAMMY |

**Root cause**
Unconfirmed. Hypotheses:
- pro.mcohio.org filters its foreclosure listing by case# length
- These cases use a different listing source or category
- The "MORTGAGE FORECLOSURE" action-type filter we apply excludes
  this batch

**Impact**
- 5-10 FC cases per year potentially missed
- Gypsy spot-check would catch these — the case-count delta vs
  her sheet flags them

**Possible fix**
1. Live recon on the pro.mcohio.org foreclosure portal — search
   for `2026 CV 0484` directly and see what the listing returns.
   Identify which filter (if any) excludes it.
2. If the filter is action-type, add the missing action-type to the
   scraper's filter set.
3. If it's a separate listing source, add a second scrape pass.

**Validation set**
Run a single-day backfill on 2026-05-22 (4 short-form misses) and
2026-05-01 (1 short-form miss). After the fix, all named cases
must appear in the FC bucket of the CSV.

**Evidence**
- `backtest_2026-06-27_holdout.md` — original 8-case observation
- `backtest_2026-06-27_holdout_v2.md` — no short-form misses in April,
  suggesting the issue is concentrated on certain filing batches
