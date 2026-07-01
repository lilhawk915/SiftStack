## Conflict Detection Report

### BLOCKERS (0)

None.

### WARNINGS (0)

None.

### INFO (3)

[INFO] Scope asymmetry: PRD is probate-only, DOC describes full Ohio pipeline
  Note: docs/gypsy_migration_plan.md scopes the migration to Montgomery County probate + foreclosure (Gypsy's manual workflow). docs/ohio_orchestrator.md describes the full production pipeline covering 7 SW Ohio counties across 4 source types (foreclosure, probate, sheriff_sale, tax_delinquent) via 3 cron slots (daily/weekly/quarterly). No contradiction — the PRD is a bounded operational cutover, the DOC is the encompassing architecture. Roadmapper should preserve both scopes: migration workstream stays Montgomery-probate-focused; ongoing operations context spans all 7 counties.

[INFO] PRD success target (≥ 95% PR recall) vs ADR-measured holdout (88%)
  Note: docs/gypsy_migration_plan.md sets a success metric "PR cases SiftStack catches that Gypsy would have ≥ 95% of Gypsy's set" (measured Week 1). SHIP_DECISION.md reports holdout v3 PR recall at 88% (30/34). This is NOT a contradiction — the ADR explicitly declares the 12% gap "bookkeeping noise, not missing data" because SiftStack captures the cases but buckets them on `docket_min` instead of Gypsy's clerical date, and downstream joins by `case_number` (LOCKED decision DEC-case-number-join-invariant) make the drift invisible. The Week 1 parallel comparison is expected to show ≥ 95% overlap because case_number-anchored counts are equivalent even when dates drift. Roadmapper should carry the ≥ 95% target as the ship-time observable, and cite DEC-case-number-join-invariant as the mechanism.

[INFO] SPEC Ticket 3 severity (MEDIUM) exceeds Tickets 1 and 2 (LOW)
  Note: docs/known_limitations.md classifies Ticket 3 (short-form FC# format) as MEDIUM priority while Tickets 1 and 2 are LOW. SHIP_DECISION.md acknowledges all three but does not re-rank them. Roadmapper should preserve the SPEC's MEDIUM label for Ticket 3 when phasing follow-up work — Ticket 3 affects FC recall (LOCKED at 100% invariant per DEC-fc-recall-invariant) and 5-10 cases per year, whereas Tickets 1-2 are cosmetic under case_number joins.
