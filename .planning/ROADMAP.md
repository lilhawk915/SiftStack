# Roadmap: SiftStack

## Overview

SiftStack's Ohio pipeline foundation is production-shipped: the Montgomery daily cron and SW Ohio weekly cron scrape 4 source types across 7 counties, enrich phones via OnBase + Claude Vision + Tracerfy + Trestle, and post a daily CSV to `#h3-homebuyers-ftm`. What remains: (1) close three known-limitations tickets that gate PR recall from 88% → ≥95% and FC recall on non-April dates, and (2) run the 3-week Gypsy migration cutover that retires the manual scrape.

The roadmap reflects that posture — Phase 0 collapses shipped work; Phases A/B/C are the three open bug tickets from `known_limitations.md` sorted by leverage over the ship-ready accuracy floor; Phases 3/4/5 are the operational Gypsy cutover weeks. Bug phases (A/B/C) and cutover phases (3/4/5) can run interleaved — Phase A does not gate Phase 3.

## Milestones

- ✅ **v1.0 Ohio Ship** — Phase 0 (shipped 2026-06-29, commit `926b4d6`)
- 🚧 **v1.1 Ship-Ready Recall + Cutover** — Phases A / B / C / 3 / 4 / 5

## Phases

**Phase Numbering:**
- Integer phases (3, 4, 5): planned milestone work (Gypsy migration weeks)
- Letter phases (A, B, C): active bug tickets from `known_limitations.md` — sorted by leverage over accuracy floor, executable in any order relative to integer phases
- Phase 0: collapsed shipped work (traceability only)

Decimal phases (e.g. 3.1) reserved for urgent insertions after roadmap creation.

- [x] **Phase 0: Ohio Pipeline Foundation** - Native OH scrape + enrichment stack + probate iteration shipped
- [ ] **Phase A: Fast-close bucketing offset** - Close the 12% PR recall gap (88% → ≥95%) on fast-close case types
- [ ] **Phase B: Archived-docket cases** - Cap backfill anchor probe so multi-week backfills don't run 4+ hours
- [ ] **Phase C: Short-form FC case# format** - Recover 5-10 FC cases/year missed by the `2026 CV 0XXX` listing filter
- [ ] **Phase 3: Gypsy Migration Week 1 — Parallel Operation** - Pre-flight smoke run + team comms + 5 business days of Gypsy manual + cron parallel
- [ ] **Phase 4: Gypsy Migration Week 2 — Cron Primary** - SiftStack authoritative; Gypsy on spot-check-only protocol
- [ ] **Phase 5: Gypsy Migration Week 3 — Sole Source** - Manual scrape retired; Gypsy redirected to higher-value work

## Phase Details

<details>
<summary>✅ Phase 0: Ohio Pipeline Foundation — SHIPPED 2026-06-29</summary>

### Phase 0: Ohio Pipeline Foundation
**Goal**: Native SiftStack Ohio pipeline scraping 4 source types across 7 counties with OnBase + Tracerfy + Trestle enrichment, delivering the daily 8-column CSV to `#h3-homebuyers-ftm` at 6 AM ET.
**Depends on**: Nothing (first phase, shipped before roadmap drawn)
**Requirements**: FOUND-01, FOUND-02, FOUND-03, FOUND-04, FOUND-05, FOUND-06, FOUND-07, FOUND-08, FOUND-09, FOUND-10, CSV-01
**Success Criteria** (what must be TRUE — all verified on holdout v3 + 2 representative daily-cron validations):
  1. Daily 6 AM ET cron produces `output/OH_Montgomery_daily_*.csv` with 8 documented columns; Slack post lands in `#h3-homebuyers-ftm`
  2. Monday 6 AM ET weekly cron produces `output/OH_SW_Ohio_weekly_*.csv` for Butler/Clark/Clermont/Greene/Miami/Warren
  3. Foreclosure recall = 100% on holdout v3 (34/34 April days); accuracy invariant holds
  4. Phone-field accuracy = 100% aggregate across all backtests (65/65); zero wrong digits
  5. Montgomery records NEVER land in the SW Ohio DataSift list and vice versa (30 tests locked in `tests/test_ohio_destination_lists.py`)
  6. Daily cron wall clock ≤ 21 min on active days; cost ≤ $0.44/day; Playwright never crashes across 2 representative validation days
**Plans**: N/A (shipped)
**Shipping commits**:
- `22a125a` — Phase 1: OnBase PDF capture + Claude Vision extraction
- `c737f40` — Wire OnBase enrichment into probate scrape phase (opt-in via env)
- `6157084` — Phase 2: Wire Tracerfy skip-trace into orchestrator
- `2f3dea0` — Phase 3: Wire Trestle phone scoring into orchestrator
- `2040604` / `cbcfe8f` / `fdf8b5c` / `3c6788f` / `c911aa2` — 5-fix probate iteration
- `1ac788c` — Probate: OnBase fiduciary phones tagged Dial First
- `0153acf` — Probate: drop records where OnBase PDF hasn't uploaded yet
- `926b4d6` — Sheriff sale: emit only newly-seen case#s
- `b2039cb` — Propagate case_number through bridges + Case Number CSV column
- `75da6d5` — `--max-cases` flag (enables backfills at all)

</details>

### Phase A: Fast-close bucketing offset
**Goal**: Close the 12% PR recall gap by getting fast-close case types (SUMMARY RELEASE, TRANSFER OF REAL ESTATE ONLY W/O WILL, RELEASE OF ADMIN W/O WILL) to bucket on the same date the data manager logs, not 1-2 days earlier on `docket_min`.
**Depends on**: Phase 0
**Requirements**: BUG-01
**Success Criteria** (what must be TRUE):
  1. Single-day backfill on 2026-04-23 places EST00729, EST00766, EST00772, EST00777 in the 2026-04-23 CSV (not the 2026-04-21/22 CSVs)
  2. Full holdout v3 replay shows PR recall ≥ 95% (was 88% = 30/34)
  3. FC recall = 100% and phone accuracy = 100% invariants remain intact — Phase A must not regress either floor
  4. No downstream tag consumers break — case_number join semantics still hold as the canonical path
**Plans**: TBD (crude path = case-type offset map in `docket_min` computation; better long-term path = Vision-extract court-stamp filing date from application PDF, promoting to ACC-01 in v2)
**Priority**: LOW (bookkeeping noise — cases ARE captured, just on adjacent day; downstream `case_number` join makes this cosmetic)

### Phase B: Archived-docket cases
**Goal**: Prevent multi-week historical backfills from ballooning to 4+ hours when they hit dates containing archived-docket cases whose visible docket starts months after the actual filing.
**Depends on**: Phase 0
**Requirements**: BUG-02
**Success Criteria** (what must be TRUE):
  1. Backfill on 2026-04-02 (canonical broken day, EST00054) completes in ~30 min with gap-fill anchor span < 100 case#s (down from observed 600)
  2. Daily 6 AM cron behavior is unchanged — no impact on today's-cases path
  3. FC recall = 100% and phone accuracy = 100% invariants remain intact — Phase B must not regress either floor
  4. Operator can run quarterly-review multi-week backfills without manually skipping affected dates
**Plans**: TBD (cheapest = cap anchor probe range at N=100 with median±50 fallback; medium = parse "Prior Case Number" field; most accurate = case# sequence interpolation, promoting to ACC-02 in v2)
**Priority**: LOW (backfill cost only; daily cron unaffected)

### Phase C: Short-form FC case# format
**Goal**: Recover the 5-10 FC cases/year that use `2026 CV 0XXX` short-form (4 digits after CV) and currently don't appear in SiftStack's foreclosure listing.
**Depends on**: Phase 0
**Requirements**: BUG-03
**Success Criteria** (what must be TRUE):
  1. Single-day backfill on 2026-05-22 recovers all 4 short-form misses (2026 CV 0484 HARCUS, 2026 CV 0485 + 0486 FORMER Y XENIA LLC, 2026 CV 0501 CREWE)
  2. Single-day backfill on 2026-05-01 recovers 2026 CV 0415 CAMPBELL, JARROD R
  3. April 2026 holdout v3 still returns 0 short-form misses (fix must not regress the batch-quiet dates)
  4. FC recall on non-April dates rises to ≥ 95% target; 100% recall invariant holds on daily cron
**Plans**: TBD (Step 1 = live recon on pro.mcohio.org searching `2026 CV 0484` directly to identify the excluding filter; Step 2 = if action-type filter, add missing type to scraper filter set; Step 3 = if separate listing source, add second scrape pass)
**Priority**: MEDIUM (highest-severity open ticket; batch-concentrated so a single missed date can drop 4 cases)

### Phase 3: Gypsy Migration Week 1 — Parallel Operation
**Goal**: Complete pre-flight + team-comms gates, then run 5 business days of Gypsy manual + SiftStack cron in parallel so any regressions not seen in backtest surface before Gypsy stops the manual scrape.
**Depends on**: Phase 0. Does NOT depend on Phase A/B/C — cutover proceeds on shipped accuracy floor; A/B/C run interleaved.
**Requirements**: CUT-01, CUT-02, CUT-03, CUT-06, CUT-07
**Success Criteria** (what must be TRUE):
  1. Pre-Week-1 smoke run: launchd plist fires cleanly at 6 AM ET, Slack post lands in `#h3-homebuyers-ftm`, validation log clean (e.g. `/tmp/cron_validation_YYYYMMDD.log`)
  2. Monday standup covers all 4 known-limitations items (fast-close timing, backfill skip, FC weekly spot-check, OnBase phones = court-extracted)
  3. For 5 business days: Gypsy runs manual scrape as usual AND compares FC count vs SiftStack + PR count vs SiftStack daily; flags in `#h3-monitoring` if either count differs by more than 2 in either direction
  4. Gypsy spot-checks 3 random probate phones per day; flags any Tier-1 wrong-number / disconnect in `#h3-monitoring`
  5. Operator (Ryan) reviews `#h3-monitoring` daily and investigates any discrepancy same-day
  6. Week 1 success metrics captured: PR-catch ≥95% of Gypsy's set; PR-miss ≤1 case; Tier-1 connect ≥80% on spot-checks
  7. Rollback trigger conditions monitored continuously; rollback executed if any trigger fires (3+ days count-mismatches, >30% Tier-1 phone error, unrelated bug)
  8. End-of-week operator decision: continue to Week 2 OR extend Week 1
**Plans**: TBD (operational — not code plans; likely a checklist plan for pre-flight, a daily-monitoring plan, and an end-of-week decision plan)

### Phase 4: Gypsy Migration Week 2 — Cron Primary
**Goal**: SiftStack becomes authoritative; Gypsy skips the manual scrape and runs only the spot-check protocol so we confirm the pipeline stands alone before Gypsy is redirected.
**Depends on**: Phase 3
**Requirements**: CUT-04
**Success Criteria** (what must be TRUE):
  1. Gypsy skips the full manual scrape all 5 business days of Week 2
  2. Gypsy picks 3 random probate cases per day from the SiftStack CSV and validates phone connects; Tier-1 target ≥ 80% first-attempt connect
  3. Operator spot-checks courthouse portal directly at least once mid-week and confirms counts match SiftStack within ±2
  4. Any spot-check failure (e.g. 2 of 3 Tier-1 disconnected) is flagged in `#h3-monitoring` and investigated same-day
  5. Rollback trigger conditions still monitored; Week 2 count-mismatches count toward the 3+ day threshold
  6. End-of-week operator decision: cutover to Week 3 (retire manual) OR revert to Week 1 (Gypsy resumes manual)
**Plans**: TBD

### Phase 5: Gypsy Migration Week 3 — Sole Source
**Goal**: SiftStack is authoritative permanently; Gypsy's 10 hours/week are redirected to deep prospecting, Tier-4/Drop outreach, and skip-trace fallback — work that has higher leverage than the manual scrape.
**Depends on**: Phase 4
**Requirements**: CUT-05
**Success Criteria** (what must be TRUE):
  1. Gypsy performs zero daily manual scrape checks
  2. Gypsy's new workload covers: deep prospecting on OnBase-phone-missing cases (~3-5/day), manual outreach on Tier-4 / Drop cases, skip-trace fallback for cases where auditor lookup didn't resolve a property address
  3. Missed-case reports arrive as tickets in the issues log (via team channel) rather than as daily count deltas — routine has changed from monitoring to exception-based
  4. Operator (Ryan) steady-state regression time ≤ 30 min/week (Week 4+ measurement of CUT-06)
  5. Time-to-first-outreach = same-day for AM filings — first dial timestamp verified against CSV post time
**Plans**: TBD

## Progress

**Execution Order:**
- Phase 0: complete (shipped 2026-06-29)
- Phases A / B / C: bug tickets, run in any order relative to Phase 3/4/5 cutover — do not gate cutover; priority order for scheduling is **C > A > B** (C is MEDIUM severity, A closes the accuracy target, B is backfill-only)
- Phases 3 → 4 → 5: strict sequential, each week-long, gated by end-of-week operator decision

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 0. Ohio Pipeline Foundation | N/A | Complete | 2026-06-29 |
| A. Fast-close bucketing offset | 0/TBD | Not started | - |
| B. Archived-docket cases | 0/TBD | Not started | - |
| C. Short-form FC case# format | 0/TBD | Not started | - |
| 3. Gypsy Week 1 — Parallel | 0/TBD | Not started | - |
| 4. Gypsy Week 2 — Cron Primary | 0/TBD | Not started | - |
| 5. Gypsy Week 3 — Sole Source | 0/TBD | Not started | - |
