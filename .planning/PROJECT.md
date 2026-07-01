# SiftStack

## What This Is

SiftStack is a full-stack real-estate investing operations platform built around the DataSift.ai CRM. The current focus is a native Ohio pipeline that scrapes four courthouse source types (foreclosure, probate, tax_delinquent, sheriff_sale) for Montgomery County plus six SW Ohio counties (Butler, Clark, Clermont, Greene, Miami, Warren), enriches them via OnBase PDF + Claude Vision + Tracerfy + Trestle, and posts a daily CSV to `#h3-homebuyers-ftm` for the acquisitions team to dial.

The pipeline is production-shipped on the Montgomery daily cron (commit `926b4d6`) and about to enter the Gypsy migration cutover — a 3-week wind-down of the manual scrape that Gypsy has been running for ~2 hours/day in favor of the 6 AM cron.

## Core Value

Every business day at 6 AM ET, the acquisitions team receives a courthouse-accurate, phone-verified probate + foreclosure lead list in `#h3-homebuyers-ftm` with zero human data entry.

Two accuracy invariants keep that value trustworthy: **100% FC recall** and **100% phone-field accuracy on OnBase-sourced Phone 1**. Everything else (PR recall, tier tuning, backfill correctness) is optimization on top of a shipped baseline.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. -->

- Ohio orchestrator with 3 cron slots (daily / weekly / quarterly) and county→list routing enforced by 30 tests — `docs/ohio_orchestrator.md`
- 4 source-type adapters (foreclosure, probate, tax_delinquent, sheriff_sale) covering all 7 counties — `src/ohio_*_scrapers.py`
- OnBase PDF capture + Claude Vision phone extraction (Phase 1, commit `22a125a`)
- Tracerfy skip-trace wired into orchestrator (Phase 2, commit `6157084`)
- Trestle phone scoring wired into orchestrator (Phase 3, commit `2f3dea0`)
- 5-fix probate iteration recovering pre-dated-docs cases (commits `2040604` → `cbcfe8f` → `fdf8b5c` → `3c6788f` → `c911aa2`)
- Dial First tier tagging for OnBase fiduciary phones (commit `1ac788c`)
- PDF-pending filter — drops probate records whose OnBase PDF hasn't uploaded yet (commit `0153acf`)
- Sheriff-sale dedup — emit only newly-seen case#s (commit `926b4d6`)
- `case_number` propagation as canonical join key across all bridges (commit `b2039cb`)
- 100% FC recall on holdout v3 (34/34 April days)
- 100% phone-field accuracy aggregate 65/65 across all backtests, zero wrong digits
- 2 representative daily-cron validations passed (2026-06-25 active, 2026-06-28 quiet) — full Phase 3 stack, no Playwright crashes, ≤ 21 min wall clock, ≤ $0.44 cost

### Active

<!-- Current scope. Building toward these. -->

Open tickets (3, from `docs/known_limitations.md`):

- [ ] **CON-fast-close-bucketing-offset** (LOW) — 4/34 holdout PR cases bucket 1-2 days early on fast-close case types; blocks PR recall from 88% → ≥95%. Fix: case-type offset map OR Vision court-stamp extraction. `known_limitations.md#ticket-1`
- [ ] **CON-archived-docket-cases** (LOW) — ~5-10 Montgomery cases/year have truncated visible dockets; balloons backfill anchor probe. Daily cron unaffected. Fix: cap anchor probe range OR parse "Prior Case Number". `known_limitations.md#ticket-2`
- [ ] **CON-short-form-fc-case-number** (MEDIUM) — 5-10 FC cases/year with `2026 CV 0XXX` short-form case# are missed by scraper listing filter. Fix: live recon on pro.mcohio.org + adjust filter. `known_limitations.md#ticket-3`

Gypsy migration cutover (3 weeks, operational not code):

- [ ] **REQ-team-communication** + **REQ-pre-week-1-smoke-run** — pre-Week-1 gates
- [ ] **REQ-cutover-week-1-parallel** — 5 business days Gypsy manual + SiftStack cron in parallel
- [ ] **REQ-cutover-week-2-cron-primary** — SiftStack authoritative, Gypsy on spot-check protocol
- [ ] **REQ-cutover-week-3-sole-source** — Gypsy redirected to higher-value work
- [ ] **REQ-success-metrics** — track 5 metrics through Week 4
- [ ] **REQ-rollback-plan** — trigger conditions + response steps active through Weeks 1-2

### Out of Scope

<!-- Explicit boundaries. Includes reasoning to prevent re-adding. -->

- **Fix 4** (case_status_date for OPEN cases) — superseded by Fix 5 (commit `c911aa2`)
- **Fix 6** (petition-filing docket entry detection) — impossible; Gypsy's "Date Filed" doesn't correspond to any docket entry
- **Threshold tuning** for `REOPEN_GAP_DAYS = 14` — remaining misses are CLOSED cases that don't traverse that branch
- **Archived-docket workaround** as a 6th fix — out of scope of the shipped 5-fix spec; not blocking daily cron (tracked as Phase B active ticket instead)
- **H3_Scrapers Apify Actor redeploy** — archived (`../H3_Scrapers/MIGRATED.md`); Ohio pipeline is native inside SiftStack
- **Non-Montgomery tax_delinquent** on quarterly — iasWorld lookup is mcrealestate.org-specific; other counties skipped to avoid producing records without addresses
- **Skip-tracing OnBase phones as accuracy fallback** — OnBase Phone 1 is court-extracted from what the fiduciary wrote on their application form; not penalized for human entry errors on the source form

## Context

**Technical environment:**

- Python 3.14 + Playwright Chromium + macOS launchd (6 AM ET daily)
- Production repo: `/Users/ryanhawker/Desktop/SiftStack`
- Full architecture reference: `CLAUDE.md` (repo root), `docs/ohio_orchestrator.md`
- CRM: DataSift.ai (`app.reisift.io`) — no REST API, uploads via Playwright automation
- Two production DataSift lists: **H3 Montgomery Courthouse Data** (daily), **H3 SW Ohio Courthouse Data** (weekly)

**Prior work / current posture:**

- Montgomery daily cron shipped and validated end-to-end on 2 representative days + 6-day historical holdout
- The SW Ohio weekly cron (Butler, Clark, Clermont, Greene, Miami, Warren) also runs from the same orchestrator on Mondays
- Quarterly tax_delinquent cron scoped to Montgomery only
- Cross-contamination guard prevents Montgomery records from ever landing in the SW Ohio list — 5-layer enforcement, locked by `tests/test_ohio_destination_lists.py` (30 tests)
- Pre-shipping: 5-fix probate iteration + Dial First tier + PDF-pending filter + sheriff-sale dedup, all validated on holdout v3

**Team + workflow:**

- Ryan (operator / developer / Claude driver) — reviews `#h3-monitoring`, files tickets, prioritizes fixes
- Gypsy (data manager, currently manual) — during Weeks 1-2 she runs the parallel scrape + spot-check protocol; from Week 3 she is redirected to deep prospecting on OnBase-phone-missing cases (~3-5/day), manual outreach on Tier-4 / Drop cases, skip-trace fallback where auditor lookup didn't resolve a property
- Team channel: `#h3-monitoring` for regressions, `#h3-homebuyers-ftm` for the daily CSV delivery

**Known posture heading into Week 1:**

- FC recall 100%, phone accuracy 100% — locked as invariants
- PR recall 88% (30/34 holdout) — 4 fast-close cases bucket 1-2 days off but ARE captured; downstream case_number join makes this cosmetic (Phase A closes the cosmetic gap)
- Daily cron cost: ~$0.44 on an active day, ~$0.00 on a quiet day; wall clock 15-21 min

## Constraints

- **Tech stack**: Python 3.14 + Playwright Chromium — required because ASP.NET WebForms + reCAPTCHA v2 on every notice detail page force browser automation; no direct HTTP path
- **Runtime**: macOS launchd on the operator's local machine (Linux cron + systemd timer paths documented in `docs/ohio_orchestrator.md` for future migration)
- **Delivery channel**: Slack `#h3-homebuyers-ftm` at ~6 AM ET; missing that window = missed morning call-list opportunity
- **DataSift integration**: No REST API — all uploads via Playwright browser automation of the DataSift web UI
- **County→list separation**: Montgomery records must NEVER land in the SW Ohio list and vice versa (enforced by 30 tests, ADR-level invariant)
- **CAPTCHA**: reCAPTCHA v2 required on every notice detail page even when logged in; solved via 2Captcha API (~10-30s per notice)
- **Accuracy floors**: FC recall = 100% on daily cron, phone-field accuracy = 100% across measured intersections; both are LOCKED invariants
- **PR recall target**: ≥ 95% on daily cron (currently 88%, blocked on Phase A)
- **Case-number canonical join**: Downstream consumers join by `case_number`, not `date_filed` — makes fast-close bucketing drift invisible to downstream tags (commit `b2039cb`)

## Key Decisions

<!-- Decisions that constrain future work. Add throughout project lifecycle. -->

<decisions>

| Decision | Source | Rationale | Status |
|----------|--------|-----------|--------|
| **SHIP probate workflow shift** starting next business day; run 6 AM cron in production; Gypsy parallel manual for 1 week | `SHIP_DECISION.md` (2026-06-29) | Validated end-to-end on 2 representative days + 6-day historical holdout; risk LOW | LOCKED |
| **FC recall = 100%** on daily cron path (accuracy invariant) | `SHIP_DECISION.md` | Holdout v3: 34/34; ship-ready bracket ≥95% | LOCKED |
| **Phone-field accuracy = 100%** across measured intersections (accuracy invariant) | `SHIP_DECISION.md` | Aggregate 65/65 perfect matches across all backtests; zero wrong digits | LOCKED |
| **Downstream joins by `case_number`, not `date_filed`** | `SHIP_DECISION.md` + commit `b2039cb` | Makes fast-close date-bucketing drift invisible to downstream tags | LOCKED |
| **OnBase PDF → Claude Vision is authoritative phone source** for Phone 1 | `SHIP_DECISION.md` | 65/65 measured accuracy; not to be second-guessed by skip-trace substitution | LOCKED |
| PR recall target ≥95%; current 88% is bookkeeping noise, not missing data | `SHIP_DECISION.md` | 12% gap = 1-2 day clerical lag on fast-close case types; cases ARE captured in adjacent day's CSV; joined by `case_number` | ✓ (target, not invariant) |
| Rollback triggers: 3+ days count-mismatch OR >30% Tier-1 phone error OR unrelated bug | `SHIP_DECISION.md` | 3-week cutover risk containment | ✓ (operational) |
| Fix 4 / Fix 6 / threshold tuning / archived-docket 6th fix NOT shipped | `SHIP_DECISION.md` | Superseded, impossible, or out of scope — see PROJECT.md Out of Scope | ✓ |

</decisions>

---
*Last updated: 2026-07-01 after ingest synthesis + roadmap creation*
