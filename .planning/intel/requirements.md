# Requirements (Synthesized from PRDs)

Each requirement carries source PRD path, description, acceptance criteria, and scope. Same requirement across multiple PRDs with divergent acceptance is preserved as competing variants (see INGEST-CONFLICTS.md).

---

## REQ-daily-csv-schema

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md
- scope: Daily CSV output posted to Slack `#h3-homebuyers-ftm`

**Description:** SiftStack's 6 AM cron produces a daily CSV with the following columns and source-of-truth mapping.

**Acceptance criteria:**

- `Case Number` — court portal, 100% stable join key
- `Property address` — Auditor enrichment (probate) / case detail (foreclosure), 100% match to portal
- `Owner Mailing` — court detail page, 100% match to portal
- `Phone 1` — OnBase PDF → Claude Vision, 100% accuracy on 65/65 measured cases
- `Phone Tier` — Trestle activity-score classification (new field, replaces Gypsy intuition)
- `Notice Type` — source classification, 100% accuracy
- `Owner Deceased` — probate flag, 100% accuracy
- `Decedent Name` — court detail page, 100% accuracy

---

## REQ-cutover-week-1-parallel

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md
- scope: Week 1 of Gypsy → SiftStack migration

**Description:** Run Gypsy's manual daily scrape and the SiftStack 6 AM cron in parallel for 5 business days to build operational confidence and surface regressions not seen in backtest sample.

**Acceptance criteria:**

- Gypsy continues manual data collection as usual
- Gypsy compares her FC count vs SiftStack's FC count and PR count vs SiftStack's PR count daily
- Flags in `#h3-monitoring` if counts differ by more than 2 in either direction
- Gypsy spot-checks 3 random probate phones per day; flags any Tier-1 wrong number / disconnect
- Operator (Ryan) reviews `#h3-monitoring` daily and investigates discrepancies same-day
- After 5 days, operator decides continue-to-Week-2 or extend-Week-1

---

## REQ-cutover-week-2-cron-primary

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md
- scope: Week 2 of Gypsy → SiftStack migration

**Description:** SiftStack becomes source of truth; Gypsy skips manual scrape and runs spot-check protocol only.

**Acceptance criteria:**

- Gypsy skips manual scrape entirely
- Gypsy picks 3 random probate cases per day from CSV, validates phone connects
- Tier-1 target: > 80% connect on first attempt
- Tier 2-4: anecdotal observation only (no hard target)
- Flags in `#h3-monitoring` if validation fails (e.g. 2 of 3 Tier-1 disconnected)
- Operator spot-checks counts vs courthouse portal directly once mid-week
- End-of-week decision: cutover to Week 3 or revert to Week 1

---

## REQ-cutover-week-3-sole-source

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md
- scope: Week 3+ of Gypsy → SiftStack migration

**Description:** SiftStack is authoritative; Gypsy's 10 hours/week redirected to higher-value work.

**Acceptance criteria:**

- No daily manual checks required of Gypsy
- Missed-case reports filed as tickets in issues log (as they surface via team channel)
- Gypsy's new role covers: deep prospecting on OnBase-phone-missing cases (~3-5/day), manual outreach on Tier-4 / Drop cases, skip-trace fallback for cases where auditor lookup didn't resolve a property

---

## REQ-success-metrics

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md
- scope: First 4 weeks of production operation

**Description:** Track five metrics for the first 4 weeks of the cutover.

**Acceptance criteria:**

- PR cases SiftStack catches that Gypsy would have: ≥ 95% of Gypsy's set (measured Week 1)
- PR cases Gypsy catches that SiftStack misses: ≤ 1 per week (measured Week 1)
- Tier-1 phone connect rate: ≥ 80% (spot-check protocol)
- Time-to-first-outreach: same-day for AM filings (compare timestamp on first dial)
- Operator (Ryan) hours spent on regressions: ≤ 30 min/week steady state

---

## REQ-rollback-plan

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md
- scope: Weeks 1-2 risk containment

**Description:** Documented rollback path if regressions surface during the cutover.

**Acceptance criteria:**

- Trigger conditions: 3+ days of count-mismatches (Gypsy catches > SiftStack), OR Tier-1 phone error rate > 30% on spot-checks, OR unrelated bug (CSV malformed, Slack post fails)
- Action: Gypsy immediately resumes manual collection
- Cron continues running in parallel for investigation
- Operator (Ryan) reviews affected cases, files tickets, prioritizes fixes
- Restart cutover at Week 1 once the regression is addressed

---

## REQ-team-communication

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md
- scope: Monday standup before Week 1 kickoff

**Description:** Four known-limitations items to communicate to the team before Week 1 begins.

**Acceptance criteria:**

- Fast-close cases may appear in CSV 1-2 days "early" vs Gypsy's timing (docket-bucketing vs clerical-bucketing) — cases ARE in the CSV, just adjacent day
- Multi-week historical backfills may skip ~5-10 cases per year (archived-docket edge case); daily cron unaffected; file ticket for quarterly-review backfill needs
- Foreclosure spot-check protocol: once per week Gypsy compares FC count vs courthouse portal for same week; file Ticket-3-style issue if SiftStack short by > 2
- OnBase phones are court-extracted, not skip-traced (Phone 1 = whatever fiduciary wrote on their application form); don't penalize pipeline for human entry errors on source form

---

## REQ-pre-week-1-smoke-run

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md
- scope: Final operator decision before Week 1

**Description:** Run the cron in parallel for 1 day before the formal Week 1 kickoff.

**Acceptance criteria:**

- launchd plist fires cleanly at 6 AM ET
- Slack post lands in the right channel (`#h3-homebuyers-ftm`)
- Validation log clean (e.g. `/tmp/cron_validation_20260628.log`)
- If clean → schedule Week 1 kickoff for next business day
