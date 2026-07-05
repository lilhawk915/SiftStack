# Gypsy → SiftStack Probate Migration Plan

**Status:** Ready to execute pending operator approval (Ryan).
**Last accuracy validation:** backtest_2026-06-28_holdout_v3.md
  — 100% FC recall, 88% PR recall, 100% phone accuracy across
  65 measured intersections.

## Current state (what Gypsy does today)

Manual data collection that takes ~2 hours every weekday morning:

1. Opens the Montgomery County probate portal (`go.mcohio.org`)
   and the foreclosure portal (`pro.mcohio.org`) by hand.
2. Walks each day's new cases. For probate, opens the case-detail
   page, then clicks into the application PDF, copies the
   fiduciary phone + address into `FTM Probate` Google Sheet.
3. For foreclosure, reads each new case docket and logs defendant
   names + addresses into `FTM Foreclosure` Google Sheet.
4. Tags hot leads for the morning call list. Manually deduplicates
   against the prior week's data.

Time cost: ~2 hours/day × 5 days = 10 hours/week.
Error rate: undocumented but observed in holdout backtests
(date-bucketing drift, occasional missed cases, occasional typos
that didn't surface in our spot checks).

## Target state (after migration)

SiftStack's 6 AM cron does all of the above automatically and
posts the result to `#h3-homebuyers-ftm` in Slack as a CSV
attachment. The team picks it up and starts dialing.

Specifically, the daily CSV will have:

| Column | Source | Confidence |
|---|---|---|
| Case Number | Court portal | 100% (stable join key) |
| Property address | Auditor enrichment (probate) / case detail (foreclosure) | 100% match to portal |
| Owner Mailing | Court detail page | 100% match to portal |
| Phone 1 | OnBase PDF → Claude Vision | 100% accuracy on 65/65 measured cases |
| Phone Tier | Trestle activity-score classification | new — replaces Gypsy's intuition |
| Notice Type | Source classification | 100% accuracy |
| Owner Deceased | Probate flag | 100% accuracy |
| Decedent Name | Court detail page | 100% accuracy |

Gypsy's role shifts to:
- **Spot-checking** SiftStack output during weeks 1-2.
- **Higher-value work** thereafter — likely additional manual
  research on the deep-prospecting (L3/L4) cases SiftStack
  surfaces but where the fiduciary phone is missing or
  low-confidence.

## Cutover plan

### Week 1 — Parallel (Gypsy manual + SiftStack cron)

**Goal:** Build operational confidence and surface any regressions
that didn't appear in the backtest sample.

**Daily checklist (for Gypsy):**
1. Continue manual data collection as usual.
2. After completing the day's manual sheet, pull the SiftStack CSV
   from `#h3-homebuyers-ftm` (it'll be posted ~6 AM ET).
3. Compare counts: Gypsy's FC count vs SiftStack's FC count;
   Gypsy's PR count vs SiftStack's PR count.
4. If counts differ by more than 2 in either direction, flag in
   `#h3-monitoring`. Operator reviews same day.
5. Spot-check 3 random probate phones from SiftStack's CSV by
   calling/texting them. If any are wrong number / disconnected
   on Tier-1 ("Dial First") cases, flag.

**Operator checklist (for Ryan):**
- Review `#h3-monitoring` daily for flags.
- Investigate any case Gypsy has that SiftStack doesn't, OR vice
  versa. Document in a running issues log.
- After 5 days, decide: continue to Week 2, or extend Week 1.

### Week 2 — Cron primary, Gypsy spot-checks only

**Goal:** Move team to SiftStack as the source of truth while
keeping a safety net.

**Daily checklist (for Gypsy):**
1. Skip the manual scrape.
2. Open SiftStack's daily CSV.
3. Sample protocol: pick 3 random probate cases from yesterday's
   CSV (any tier). For each, validate the phone connects:
   - Tier 1 (Dial First) target: > 80% should connect on the
     first attempt.
   - Tier 2-4: anecdotal observation only — no hard target.
4. If validation fails (e.g. 2 of 3 Tier-1 phones are
   disconnected), flag in `#h3-monitoring`.

**Operator checklist (for Ryan):**
- Spot-check counts vs the courthouse portal directly once mid-week.
- Decide at end of week 2: cutover to Week 3, or revert to Week 1.

### Week 3+ — SiftStack sole source

**Goal:** Free Gypsy's 10 hours/week for higher-value work.

**Daily checklist (for Gypsy):**
- None. SiftStack is authoritative.
- If she notices a missed case during the day (someone mentions
  one in the team channel), file a ticket in the issues log so
  we can investigate.

**New Gypsy role (TBD by Ryan):**
- Deep prospecting on cases without OnBase phones (~3-5/day).
- Manual outreach scripts for cases SiftStack flags as Tier 4 / Drop.
- Skip-trace fallback for cases where the auditor lookup didn't
  resolve a property.

## Known limitations to communicate to the team

Read these out loud at the Monday standup before Week 1 starts:

1. **Fast-close cases may appear in CSV 1-2 days "early" vs Gypsy's
   historical timing.** SiftStack buckets these on the docket
   filing date; Gypsy buckets them on her clerical processing
   date. The cases ARE in the CSV — they just appear in the
   adjacent day. Faster outreach, not a bug.

2. **Multi-week historical backfills may skip ~5-10 cases per
   year** due to an edge case with archived court dockets. The
   daily cron is unaffected. If a manual backfill is needed for
   a quarterly review, file a ticket with the affected date
   range and we'll handle the archived-docket cases.

3. **Foreclosure spot-check protocol:** once per week, Gypsy
   compares SiftStack's FC count vs the courthouse portal's
   listing count for the same week. If SiftStack is short by
   more than 2, file as a Ticket 3-style issue (short-form
   case# bug).

4. **OnBase phones are court-extracted, not skip-traced.** The
   phone in "Phone 1" is whatever the fiduciary wrote on their
   application form. If it's wrong/disconnected, it's the
   fiduciary's mistake, not SiftStack's. Don't penalize the
   pipeline for human entry errors on the source form.

## Success metrics (track for first 4 weeks)

| Metric | Target | How measured |
|---|---|---|
| PR cases SiftStack catches that Gypsy would have | ≥ 95% of Gypsy's set | Week 1 comparison |
| PR cases Gypsy catches that SiftStack misses | ≤ 1 per week | Week 1 comparison |
| Tier-1 phone connect rate | ≥ 80% | Spot-check protocol |
| Time-to-first-outreach | Same-day for AM filings | Compare timestamp on first dial |
| Operator (Ryan) hours spent on regressions | ≤ 30 min/week steady state | Operator log |

## Rollback plan

If at any point in Week 1 or Week 2:
- 3+ days of count-mismatches (Gypsy catches > SiftStack), OR
- A Tier-1 phone error rate over 30% on spot-checks, OR
- An unrelated bug surfaces (e.g. CSV malformed, Slack post fails)

Then:
1. Gypsy immediately resumes manual collection.
2. The 6 AM cron continues to run in parallel for investigation.
3. Operator (Ryan) reviews the affected cases, files tickets,
   prioritizes fixes.
4. Restart the cutover plan at Week 1 once the regression is
   addressed.

## Final operator decision (Ryan)

Run the cron in parallel for 1 day before the formal Week 1 kickoff
to ensure the launchd plist is firing cleanly and the Slack post
lands in the right channel. If today's run (validation in
`/tmp/cron_validation_20260628.log`) looks clean, schedule the
Week 1 kickoff for the next business day.
