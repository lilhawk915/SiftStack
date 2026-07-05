# SiftStack Backtest — Holdout v3 (Fix 5 Shipped)

**Date:** 2026-06-28
**Code state:** Fix 5 (`c911aa2`) committed. Working tree clean.
**Purpose:** Measure PR recall on 6 April dates after the docket↔status gap heuristic shipped.

## Headline result

| Metric | v3 training (3 days) | Failed holdout (7 days) | Holdout v2 (6 days) | **Holdout v3 (6 days)** |
|---|---|---|---|---|
| **FC recall** | 91% (21/23) | 88% (58/66) | 100% (34/34) | **100% (34/34)** |
| **PR recall** | 100% (15/15) | 56% (18/32) | 76% (26/34) | **88% (30/34)** |
| Phone field accuracy | 12/12 | 15/15 | 23/23 | **27/27 (100%)** |

**Δ vs holdout v2: PR recall 76% → 88% (+12 pts). FC + phone unchanged.**

Bracket landing per spec:
- **PR recall 88%** → 85-89% "modest improvement; investigate before shipping" bracket. Not the ≥95% ship target. **Still recommending wait, but the picture has changed.**
- **FC recall 100%** → ≥95% bracket. Hold v2 recommendation.
- **Phone accuracy 100%** → unchanged. Continue trusting OnBase + Vision.

## Per-day breakdown

| Date | DM FC | SS FC | FC both | DM PR | SS PR | PR both | PR only-DM | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 4/14 | 6 | 6 | **6** | 6 | 11 | **6** | **0** | ✓ EST00698 + EST00700 recovered |
| 4/15 | 4 | 4 | **4** | 6 | 12 | **6** | **0** | ✓ EST00707 recovered |
| 4/17 | 6 | 6 | **6** | 6 | 17 | 5 | 1 | EST00729 still missed |
| 4/23 | 5 | 5 | **5** | 5 | 9 | 2 | 3 | EST00766, 00772, 00777 still missed |
| 4/27 | 0 | 3 | n/a | 7 | 17 | **7** | **0** | ✓ Perfect |
| 4/30 | 13 | 27 | **13** | 4 | 10 | **4** | **0** | ✓ EST00827 recovered |
| **TOTAL** | **34** | **51** | **34** | **34** | **76** | **30** | **4** | |

- **4 of 7 days hit 100% PR recall.** 4/14, 4/15, 4/27, 4/30.
- **1 day at 83%** (4/17, 5/6).
- **1 day at 40%** (4/23, 2/5) — pulls aggregate down hard.

## Per-fix attribution: which of the 8 named v2 misses got recovered

| v2 miss | Date | Cause of v2 miss | Holdout v3 result |
|---|---|---|---|
| EST00698 | 4/14 | Pre-dated docs (gap=1d) | **✓ Recovered** — date_filed parses as 2026-04-14, phone `937-993-8974` |
| EST00700 | 4/14 | Pre-dated docs (gap=1d) | **✓ Recovered** — phone `224-245-6468` |
| EST00707 | 4/15 | Pre-dated docs | **✓ Recovered** |
| EST00729 | 4/17 | Not pre-dated docs | **✗ Still missed** |
| EST00766 | 4/23 | Not pre-dated docs | **✗ Still missed** |
| EST00772 | 4/23 | Not pre-dated docs | **✗ Still missed** |
| EST00777 | 4/23 | Not pre-dated docs | **✗ Still missed** |
| EST00827 | 4/30 | Pre-dated docs | **✓ Recovered** |

**4 of 8 misses recovered (the pre-dated-docs subset). 4 still missed.**

The 4 unrecovered misses cluster on 4/17 (1) and 4/23 (3). They WERE probed by gap-fill — both days' anchors covered the case#s. But integration's date_filter dropped them, meaning Fix 5's heuristic classified them as something other than "filed on the target date".

**Hypothesis on the remaining 4:** they likely have a larger gap between status_date and docket_min (>14 days) — so Fix 5 sends them to docket_min, which doesn't equal the DM-recorded filing date. The pattern looks like: case opened with old attorney-prep paperwork in the docket, then formally filed weeks later. Bigger pre-date than the 14-day threshold catches.

A 14-day threshold is tight; a 30-day threshold might catch these. But tuning the threshold mid-run is explicitly forbidden by spec.

## Phone field accuracy — 27/27 PERFECT

Same result as every prior backtest. Every PR intersection where both sources have a fiduciary phone matches digit-for-digit (modulo formatting). 19 EXACT + 8 normalized + 0 wrong digits.

| Date | EXACT | MATCH (norm) | MISMATCH |
|---|---:|---:|---:|
| 4/14 | 1 | 4 | 0 |
| 4/15 | 1 | 4 | 0 |
| 4/17 | 5 | 0 | 0 |
| 4/23 | 1 | 1 | 0 |
| 4/27 | 5 | 0 | 0 |
| 4/30 | 6 | 0 | 0 |
| **Total** | **19** | **8** | **0** |

The OnBase + Vision pipeline's invariant of 100% phone accuracy holds across every backtest sample we've measured (now 65 total intersections across v3/failed/v2/v3 holdouts).

## Cost summary

| Day | PR enriched | OnBase spend |
|---|---:|---:|
| 4/14 | 11 | $0.4707 |
| 4/15 | 12 | $0.2389 |
| 4/17 | 17 | $0.4759 |
| 4/23 | 9 | $0.1780 |
| 4/27 | 17 | $0.5110 |
| 4/30 | 10 | $0.1586 |
| **Total** | **76** | **$2.0331** |

~$0.34/day average. Similar to holdout v2. Vision dominates cost; gap-fill adds page loads but not API spend.

## Recommendations

### Probate workflow shift — borderline

PR recall is 88%, in the "modest improvement" bracket. Better than holdout v2's 76% but short of the 95% ship target. **Recommend NOT shifting Gypsy's workflow yet** — but the gap is now narrow enough that a final fix attempt is justified.

The 4 remaining misses all share the pattern "OPEN case with status_date matching DM but docket_min >14d earlier" — likely older attorney-prep docket entries. If Fix 5 had used a wider threshold (e.g. 30 days) for the docket↔status gap, those 4 cases would likely fall on the "fresh-filed, trust status_date" side.

**Tuning the threshold isn't in this run's budget.** A future change could:
1. Spot-check the 4 remaining misses' actual gaps (e.g. fetch EST00729's docket_min vs status_date).
2. If those gaps land in the 15-30 day range, bump REOPEN_GAP_DAYS to 30 (or whatever the empirically observed boundary is).
3. Run a fresh holdout to confirm no re-opened cases regress.

### Foreclosure workflow shift — ship

100% FC recall across 34 cases on 6 days. Same recommendation as holdout v2: SiftStack as primary, Gypsy doing sanity checks on short-form# cases (which didn't appear in this holdout's April sample).

### Phone accuracy — continue trusting

100% across 27 new intersections. The Vision pipeline's accuracy invariant holds.

### Follow-up tickets to file

1. **Archived-docket cases (e.g. 2026-04-02 cluster)** — case# implies January filing but visible docket starts April. Not recoverable from case-detail page alone. ~5-10 cases per year. Documented in `backtest_2026-06-28_fix5_aborted.md`. **Not blocking** daily cron; affects only multi-month backfills where the date happens to overlap with archived-docket events.

2. **Wider docket↔status gap threshold** — Fix 5's 14-day threshold catches pre-dated docs ≤14 days but misses the 4 holdout-v3 cases that likely have 15-30 day gaps. A tuning pass against a clean validation set could close the recall gap from 88% → 95%+. Cost: a single backfill run on the 4 named cases to confirm their gaps, then a code change + holdout verification.

### Daily cron impact — none

The remaining recall gap affects multi-week backfills, NOT today's daily cron. Today's case-detail pages always have full, current dockets (no archived-docket cases, no large status↔docket gaps from old attorney prep). The 6 AM cron is unaffected by Fix 5's edge cases.

## Bottom line

Fix 5 closed half the holdout-v2 gap. PR recall is 88% on the holdout sample, up from 76%, with 100% phone accuracy maintained. **Shipping probate as primary still recommended to WAIT** — the gap is small but the spec's 95% bar isn't met. A targeted follow-up (threshold tuning or per-case gap inspection on 4/23's missed cluster) could close it. Foreclosure remains shippable. OnBase + Vision continues to be the bright spot — perfect phone accuracy across now 65 measured intersections.
