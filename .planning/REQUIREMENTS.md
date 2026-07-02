# Requirements: SiftStack

**Defined:** 2026-07-01
**Core Value:** Every business day at 6 AM ET, the acquisitions team receives a courthouse-accurate, phone-verified probate + foreclosure lead list in `#h3-homebuyers-ftm` with zero human data entry.

## v1 Requirements

Requirements for the current milestone (Ohio pipeline ship + Gypsy migration cutover). Each maps to exactly one roadmap phase.

### Shipped Foundation (Validated)

Baseline capabilities that shipped before the roadmap was drawn. Kept in the traceability table so coverage is auditable.

- [x] **FOUND-01**: Ohio orchestrator runs 3 cron slots (daily / weekly / quarterly) with county→list routing enforced by 30 tests — `docs/ohio_orchestrator.md`
- [x] **FOUND-02**: All 7 counties + 4 source types (foreclosure, probate, tax_delinquent, sheriff_sale) scrape via native SiftStack adapters — `src/ohio_*_scrapers.py`
- [x] **FOUND-03**: OnBase PDF capture + Claude Vision phone extraction wired into probate scrape (commit `22a125a`)
- [x] **FOUND-04**: Tracerfy skip-trace enrichment wired into orchestrator (commit `6157084`)
- [x] **FOUND-05**: Trestle phone scoring wired into orchestrator (commit `2f3dea0`)
- [x] **FOUND-06**: 5-fix probate iteration recovers pre-dated-docs cases (commits `2040604` / `cbcfe8f` / `fdf8b5c` / `3c6788f` / `c911aa2`)
- [x] **FOUND-07**: `case_number` propagated through all bridges as canonical downstream join key (commit `b2039cb`)
- [x] **FOUND-08**: OnBase fiduciary phones auto-tagged as Dial First tier (commit `1ac788c`)
- [x] **FOUND-09**: PDF-pending filter drops probate records whose OnBase PDF hasn't uploaded yet (commit `0153acf`)
- [x] **FOUND-10**: Sheriff-sale scraper emits only newly-seen case#s to prevent CSV re-flood (commit `926b4d6`)
- [x] **CSV-01** (from REQ-daily-csv-schema): Daily CSV to `#h3-homebuyers-ftm` carries 8 columns — Case Number, Property address, Owner Mailing, Phone 1, Phone Tier, Notice Type, Owner Deceased, Decedent Name — with source-of-truth mapping and per-column accuracy contracts

### Active Bug Tickets

Open tickets from `docs/known_limitations.md`. Each has a validation set and possible fix paths documented.

- [ ] **BUG-01** (from CON-fast-close-bucketing-offset, LOW): Fast-close case types (SUMMARY RELEASE, TRANSFER OF REAL ESTATE ONLY W/O WILL, RELEASE OF ADMIN W/O WILL) bucket 1-2 days early on `docket_min` vs data-manager "Date Filed". 4/34 holdout cases affected. Blocks PR recall from 88% → ≥95%. Validation set: single-day backfill on 2026-04-23 (EST00729/766/772/777).
- [ ] **BUG-02** (from CON-archived-docket-cases, LOW): ~5-10 Montgomery cases/year have truncated visible dockets; balloons gap-fill anchor probe range on multi-week backfills. Daily cron unaffected. Validation set: 2026-04-02 (canonical broken day) completes in ~30 min with anchor span < 100 case#s after fix.
- [ ] **BUG-03** (from CON-short-form-fc-case-number, MEDIUM): FC cases with `2026 CV 0XXX` short-form case# (4 digits after CV) are missed by scraper listing filter. 8 cases missed in 2026-06-27 failed holdout. Blocks FC recall ≥95% target on non-April dates. Validation set: single-day backfills on 2026-05-22 (4 misses) and 2026-05-01 (1 miss); all named cases must appear in FC bucket after fix.
- [ ] **BUG-04** (from portal reCAPTCHA v3 deployment 2026-07-01, HIGH): pro.mcohio.org deployed reCAPTCHA v3 invisible bot-scoring between 2026-06-30 and 2026-07-01. FC scraper's search now returns a "reCAPTCHA score too low" block page instead of the results table on every request. Silent failure: `parse_results_table()` sees 0 `<tr>` rows and reports "Parsed 0 rows → 0 unique cases" as if the courthouse had no filings. 100% loss of Montgomery foreclosure daily records until mitigated. Confirmed via screenshot capture at `/tmp/mont_fc_results.png` on 2026-07-01. Validation set: after mitigation, replay yesterday's `2026-06-29 → 2026-06-30` query and confirm ~43 rows / 9 cases returned. Does NOT affect probate (`go.mcohio.org`) or sheriff sale (`realforeclose.com` PREVIEW URLs).

### Gypsy Migration Cutover

Operational requirements from `docs/gypsy_migration_plan.md`. Not code work but tracked as phases.

- [ ] **CUT-01** (from REQ-pre-week-1-smoke-run): Run cron in parallel for 1 day before Week 1 kickoff; launchd fires cleanly at 6 AM ET, Slack post lands in `#h3-homebuyers-ftm`, validation log clean → schedule Week 1 for next business day
- [ ] **CUT-02** (from REQ-team-communication): 4 known-limitations items communicated to team at Monday standup before Week 1 begins (fast-close timing, backfill skip, FC weekly spot-check, OnBase phones = court-extracted not skip-traced)
- [ ] **CUT-03** (from REQ-cutover-week-1-parallel): 5 business days of Gypsy manual + SiftStack cron in parallel; daily count-delta reporting; Gypsy spot-checks 3 random probate phones/day; operator investigates same-day; end-of-week decision to continue or extend
- [ ] **CUT-04** (from REQ-cutover-week-2-cron-primary): SiftStack authoritative for Week 2; Gypsy skips manual scrape, runs spot-check protocol only (3 random probate cases/day, Tier-1 >80% connect target); operator spot-checks courthouse portal once mid-week
- [ ] **CUT-05** (from REQ-cutover-week-3-sole-source): SiftStack sole source of truth; no daily manual checks required; Gypsy redirected to deep prospecting on OnBase-phone-missing cases (~3-5/day), Tier-4/Drop outreach, skip-trace fallback for unresolved auditor lookups
- [ ] **CUT-06** (from REQ-success-metrics): 5 metrics tracked through first 4 weeks — PR-catch ≥95% of Gypsy set (Week 1), PR-miss ≤1/week, Tier-1 connect ≥80%, time-to-first-outreach same-day for AM filings, operator regression time ≤30 min/week steady state
- [ ] **CUT-07** (from REQ-rollback-plan): Rollback conditions active through Weeks 1-2 — 3+ days of count-mismatches OR >30% Tier-1 phone error rate OR unrelated bug (malformed CSV, Slack post failure); action = Gypsy resumes manual, cron continues in parallel for investigation, restart cutover at Week 1 after fix

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Coverage Expansion

- **EXP-01**: Add Blount + Knox County TN via H3 unified courthouse pipeline (currently TN runs through `main.py`, OH runs through `ohio_orchestrator.py` — unify after Ohio ship stabilizes)
- **EXP-02**: Add eviction / code_violation / divorce source types to OH pipeline (already supported in TN via `photo_importer.py`)
- **EXP-03**: Linux systemd timer migration off macOS launchd (documented in `docs/ohio_orchestrator.md`, unblocks cloud/hosted-runner move)

### Accuracy Improvements

- **ACC-01**: Vision-extract court-stamp filing date from application PDF as long-term fix for fast-close bucketing (superset of BUG-01 crude offset map)
- **ACC-02**: Parse "Prior Case Number" or case# sequence interpolation for archived-docket cases (superset of BUG-02 anchor cap)
- **ACC-03**: Slack-notify hook on orchestrator exit code so a failure pages the on-call operator

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Fix 4 (case_status_date for OPEN cases) | Superseded by Fix 5 (`c911aa2`) |
| Fix 6 (petition-filing docket entry detection) | Impossible — Gypsy's "Date Filed" doesn't correspond to any docket entry |
| Threshold tuning for `REOPEN_GAP_DAYS = 14` | Remaining misses are CLOSED cases that don't traverse that branch |
| Archived-docket workaround as 6th fix | Out of scope of 5-fix spec; not blocking daily cron (tracked as BUG-02 instead) |
| H3_Scrapers Apify Actor redeploy | Archived (`../H3_Scrapers/MIGRATED.md`); Ohio pipeline is native |
| Non-Montgomery tax_delinquent on quarterly | iasWorld lookup is mcrealestate.org-specific; other counties skipped to avoid producing addressless records |
| Skip-tracing OnBase Phone 1 as accuracy fallback | Court-extracted from fiduciary application form; don't penalize pipeline for human entry errors on source form |

## Traceability

Every v1 requirement maps to exactly one phase. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| FOUND-01 | Phase 0 | Complete |
| FOUND-02 | Phase 0 | Complete |
| FOUND-03 | Phase 0 | Complete |
| FOUND-04 | Phase 0 | Complete |
| FOUND-05 | Phase 0 | Complete |
| FOUND-06 | Phase 0 | Complete |
| FOUND-07 | Phase 0 | Complete |
| FOUND-08 | Phase 0 | Complete |
| FOUND-09 | Phase 0 | Complete |
| FOUND-10 | Phase 0 | Complete |
| CSV-01 | Phase 0 | Complete |
| BUG-01 | Phase A | Pending |
| BUG-02 | Phase B | Pending |
| BUG-03 | Phase C | Pending |
| BUG-04 | Phase D | Pending |
| CUT-01 | Phase 3 | Pending |
| CUT-02 | Phase 3 | Pending |
| CUT-03 | Phase 3 | Pending |
| CUT-04 | Phase 4 | Pending |
| CUT-05 | Phase 5 | Pending |
| CUT-06 | Phase 3 | Pending |
| CUT-07 | Phase 3 | Pending |

**Coverage:**
- v1 requirements: 21 total (11 shipped foundation + 3 bug tickets + 7 cutover)
- Mapped to phases: 21
- Unmapped: 0 ✓

---
*Requirements defined: 2026-07-01*
*Last updated: 2026-07-01 after ingest synthesis + roadmap creation*
