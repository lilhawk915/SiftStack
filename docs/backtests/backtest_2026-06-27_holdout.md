# SiftStack Backtest — 7-Day Holdout (HARD STOP)

**Run date:** 2026-06-27
**Purpose:** Verify v3's 100% PR / 91% FC recall generalizes to dates not used during iteration.
**Code state:** Unchanged from v3 (commits b2039cb + 75da6d5 + 2040604). No new commits.

## Headline result — HARD STOP per spec

| Metric | v3 training (3 days) | **Holdout (7 days)** | Δ | Bracket |
|---|---|---|---|---|
| **FC recall** | 91% (21/23) | **88% (58/66)** | -3 pts | 80-87% modest drop |
| **PR recall** | 100% (15/15) | **56% (18/32)** | **-44 pts** | **< 85% → HARD STOP** |
| Phone field accuracy | 12/12 = 100% | **15/15 = 100%** | held | EXACT 6, normalized 9, zero wrong digits |

Per the spec's PR-recall bracket: **"< 85% → overfit warning. Don't shift Gypsy's workflow yet."** Investigating the cause before any fix attempt that would invalidate the holdout.

## Per-day breakdown

| Date | DM FC | SS FC | FC both | DM PR | SS PR | PR both | PR only-DM | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 2026-05-12 | 4 | 4 | **4** | 7 | 11 | **6** | 1 | EST00915 outside anchor |
| 2026-05-22 | 15 | 12 | 11 | 4 | 3 | 3 | 1 | EST00981 below anchor (single-case-cluster) |
| 2026-05-01 | 16 | 15 | 15 | 5 | 7 | 3 | 2 | EST00835, EST00836 outside anchor |
| 2026-05-05 | 13 | 13 | **13** | 3 | 1 | 1 | 2 | Anchor = [00849] (1-case anchor) |
| 2026-05-15 | 14 | 12 | 12 | 4 | 1 | 1 | 3 | Anchor = [00939] (1-case anchor) |
| 2026-04-22 | 4 | 3 | 3 | 5 | 4 | 1 | 4 | Date-filed semantics mismatch — see below |
| 2026-06-04 | 0 | 15 | 0 | 4 | 8 | 3 | 1 | EST01084 outside anchor [01077, 01089] |
| **TOTAL** | **66** | **74** | **58** | **32** | **35** | **18** | **14** | |

FC recall = 58/66 = **88%**.
PR recall = 18/32 = **56%**.

## Root-cause diagnosis — the gap-fill anchor is too narrow

The v3 commit (`2040604`) added a gap-fill loop that probes case# gaps inside the case-number range `[min, max]` of cases the *year-wide listing* placed in the date window. That works **when** the year-wide listing captures at least one case at both ends of the actual filed range for the day. The v3 training days satisfied that condition by luck.

The holdout exposes the failure mode:

1. **1-case anchor.** On 5/05 and 5/15, the year-wide listing returned exactly **one** in-window case. Anchor became `[X, X]` — no gaps to probe. DM's other cases (filed before/after X) were never reached. 5/05 captured 1/3 PR; 5/15 captured 1/4.

2. **Below-anchor miss.** On 5/22, listing captured EST00987/00988/00989. Anchor = `[00987, 00989]`. DM's GAIL DAVIDSON case at EST00981 sits *below* the anchor's min — gap-fill never probes case#s below `min(captured)`. Same failure mode on 5/12 (EST00915 was above anchor max), 5/01 (EST00835/00836 below), 6/04 (EST01084 above).

3. **Date-filed semantics mismatch (4/22).** Gap-fill uses `appointment_date or case_status_date` for its inline date check; the integration-level filter in `ohio_probate_scrapers._run_probate_live` uses `_record_in_window(rec.date_filed)`, where `rec.date_filed` is computed by `_detail_to_record` from the *earliest docket entry date*. These can disagree by days. On 4/22 gap-fill claimed 12 captures "inside the date window", but integration only kept 4 — so 10 cases were dropped between gap-fill's filter and the final emit, leaving DM's other 4 cases unreachable.

The v3 training sample happened to dodge all three modes:
* 5/28 anchor was [00937, 01022] — 85 case#s, brackets all DM cases.
* 6/01 anchor was [01035, 01052] — 17 case#s, brackets DAYE.
* 5/11 anchor was [00892, 00904] — 13 case#s, brackets DAVIS + MAY.

So v3's reported 100% PR recall was real but unrepresentative of the variability we see across more days.

## Foreclosure recall (88%) — within acceptable bracket

8 FC misses across 7 days. Pattern:
* 5 are **short-form case#s** (`2026 CV 0484`, `0485`, `0486`, `0501`, `0415`, `0440-like`) — same bug noted in v3, still unfixed. These appear in DM's manual scrape but not in SiftStack's pro.mcohio.org foreclosure listing.
* 3 are normal-format defendants on cases where SiftStack got the case but not all defendants (defendant-parser edge case): J & H Sales, COX NATASHA, HEMMINGSEN TAMMY, HARCUS CARL.

The 88% lands in the "80-87% modest drop" bracket per spec — within noise of v3's 91%. Holds the v3 recommendation for foreclosure unchanged.

Bonus: 16 cases SiftStack caught that DM missed entirely, including 15 NEW foreclosures on 6/04 (DM's sheet shows 0 FC for that day — apparently she hadn't processed 6/04 yet when last updated).

## Phone field accuracy — 15/15 PERFECT across the holdout

Same result as v3 training: every single PR intersection where both sources have a fiduciary phone matches digit-for-digit (modulo format). Six EXACT, nine normalized-match, zero wrong digits.

| Date | Decedent | DM phone | SS Vision phone | Verdict |
|---|---|---|---|---|
| 5/12 | (6 cases, all match) | — | — | 6×MATCH |
| 5/22 | (3 cases, all match) | — | — | 3×MATCH |
| 5/01 | (3 cases, all match) | — | — | 3×MATCH |
| 5/05 | 1 case, match | — | — | 1×MATCH |
| 5/15 | 1 case, match | — | — | 1×MATCH |
| 4/22 | 1 case, match | — | — | 1×MATCH |
| 6/04 | (no fiduciary-phone intersection) | — | — | — |

(Full per-case table omitted because every row reads the same: phones match.)

This is the v3 finding **strongly confirmed**: when OnBase has the right form and Vision extracts a phone, the phone is correct. The pipeline's phone-accuracy claim doesn't depend on which days you pick.

## Cost across 7 days

| Day | PR enriched | OnBase spend |
|---|---:|---:|
| 5/12 | 11 | $0.4304 |
| 5/22 | 3 | $0.0831 |
| 5/01 | 7 | $0.1912 |
| 5/05 | 1 | $0.0091 |
| 5/15 | 1 | $0.0088 |
| 4/22 | 4 | $0.0846 |
| 6/04 | 8 | $0.0518 |
| **Total** | **35** | **$0.8590** |

~$0.12/day average, well under any cap.

## Bracket assessment + recommendation

| Metric | Bracket per spec | Recommendation |
|---|---|---|
| **PR recall 56%** | **< 85% → overfit, don't shift workflow yet** | **Do NOT replace Gypsy's manual probate sheet with SiftStack yet.** |
| FC recall 88% | 80-87% modest drop | Holds v3 recommendation: SiftStack as primary, Gypsy doing short-form# sanity checks. |
| Phone accuracy 100% | unchanged from v3 | Continue using OnBase + Vision as primary phone source on the cases SiftStack DOES capture. |

**Bottom line:** v3 was overfit on the PR scraping side. The gap-fill works perfectly *when the year-wide listing places enough cases at both ends of the actual date range* — and it dramatically expands recall over the no-fix state — but the listing's sparse coverage is more variable than the v3 training sample showed. Three follow-up fixes (out of scope for this holdout):

1. **Expand the anchor range by N below `min` and N above `max`** before computing gaps. E.g. probe `[min−10, max+10]` instead of `[min, max]`. Cheap (20 extra single-case lookups per day) and would resolve the below-anchor / above-anchor misses (5/12, 5/22, 5/01, 6/04).

2. **Heuristic case# range from date for 1-case anchors.** Walk the listing's case#/date pairs to estimate "cases/day", convert target date to predicted case# range, probe that. Would resolve 5/05 and 5/15.

3. **Align the date-filter semantics** between gap-fill (uses `appointment_date or case_status_date`) and integration (uses earliest-docket date). Currently gap-fill captures cases that integration then discards, wasting Vision calls and creating the 4/22 confusion.

Per spec, no code changes shipped from this run. Reporting only.
