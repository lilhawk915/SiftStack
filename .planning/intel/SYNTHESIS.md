# Ingest Synthesis Summary

Single entry point for downstream consumers (gsd-roadmapper). Points to per-type intel files and the conflicts report.

## Doc counts by type

- ADR: 1
- SPEC: 1
- PRD: 1
- DOC: 1
- **Total: 4**

All classifications: `high` confidence, `manifest_override: true`. No `UNKNOWN`-confidence-low docs.

## Cross-reference graph

- SHIP_DECISION.md → docs/known_limitations.md, docs/gypsy_migration_plan.md
- gypsy_migration_plan.md → external (backtest log, cron validation log — not in ingest set)
- known_limitations.md → external (backtest reports)
- ohio_orchestrator.md → external (src + tests)

Cycle detection: **no cycles**. Traversal depth well under cap.

## Decisions locked (from ADR)

7 decisions extracted from SHIP_DECISION.md (all precedence 0, all Accepted, several LOCKED as accuracy invariants):

- DEC-ship-probate-workflow-shift (LOCKED — ship decision)
- DEC-fc-recall-invariant (LOCKED — 100% FC recall on daily cron)
- DEC-phone-accuracy-invariant (LOCKED — 100% phone accuracy across measured intersections)
- DEC-case-number-join-invariant (LOCKED — downstream joins by case_number, not date_filed)
- DEC-pr-recall-target (Accepted — ≥ 95% target; current 88%, gap declared bookkeeping-noise)
- DEC-rollback-triggers (Accepted — operational rollback conditions)
- DEC-not-shipped (Accepted — 4 items considered and rejected with rationale)

See `intel/decisions.md`.

## Requirements extracted (from PRD)

8 requirements extracted from docs/gypsy_migration_plan.md:

- REQ-daily-csv-schema — 8-column CSV output contract to `#h3-homebuyers-ftm`
- REQ-cutover-week-1-parallel — Gypsy manual + SiftStack cron parallel operation, 5 business days
- REQ-cutover-week-2-cron-primary — SiftStack primary, Gypsy spot-checks only
- REQ-cutover-week-3-sole-source — SiftStack sole source; Gypsy redirected to higher-value work
- REQ-success-metrics — 5 tracked metrics for first 4 weeks
- REQ-rollback-plan — trigger conditions + response steps
- REQ-team-communication — 4 known-limitations items communicated to team pre-Week-1
- REQ-pre-week-1-smoke-run — final operator smoke-run before Week 1 kickoff

See `intel/requirements.md`.

## Constraints extracted (from SPEC)

3 constraints extracted from docs/known_limitations.md:

- CON-fast-close-bucketing-offset — protocol / LOW — 1-2 day docket_min vs clerical drift on fast-close case types
- CON-archived-docket-cases — protocol / LOW — ~5-10 Montgomery cases/year with truncated visible dockets; backfill-only impact
- CON-short-form-fc-case-number — api-contract / MEDIUM — `2026 CV 0XXX` short-form format missed by FC scraper; 5-10 FC cases/year potentially missed

Type breakdown: 2 protocol, 1 api-contract.

See `intel/constraints.md`.

## Context topics (from DOC + cross-doc)

8 context topics extracted, primarily from docs/ohio_orchestrator.md with supporting cross-references from the ADR and PRD:

- Ohio Orchestrator — Production Cron Slots (3 slots, county→list routing)
- Ohio Orchestrator — CLI (primary + escape hatches)
- Ohio Orchestrator — Cron Wiring (macOS launchd)
- Ohio Orchestrator — Cron Wiring (Linux cron + systemd)
- Ohio Orchestrator — Cross-Contamination Guard (5-layer enforcement)
- Ohio Orchestrator — Operator Pre-Flight Checklist
- Historical Backfill Validation (cross-reference from ADR)
- Probate Commit Stack (ship-enabling — from ADR)
- Current-State Gypsy Manual Workflow (from PRD)
- Target Delivery Channel (Slack `#h3-homebuyers-ftm`)

See `intel/context.md`.

## Conflicts

- **Blockers:** 0
- **Warnings:** 0
- **Info (transparency):** 3
  - Scope asymmetry (PRD probate-only vs DOC full-pipeline)
  - PRD 95% target vs ADR 88% measurement (reconciled by case_number join invariant)
  - SPEC Ticket 3 severity (MEDIUM) preserved over Tickets 1-2 (LOW)

See `.planning/INGEST-CONFLICTS.md`.

## Status

**READY — safe to route.** No blockers, no user-input-required warnings. All 3 INFO items are transparency notes for the roadmapper's phasing decisions.

## Files written

- `.planning/intel/decisions.md`
- `.planning/intel/requirements.md`
- `.planning/intel/constraints.md`
- `.planning/intel/context.md`
- `.planning/intel/SYNTHESIS.md` (this file)
- `.planning/INGEST-CONFLICTS.md`
