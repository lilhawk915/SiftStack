# Decisions (Synthesized from ADRs)

Every entry preserves its source and locked status. Higher-precedence sources (lower `precedence` integer) win when content contradicts. LOCKED decisions cannot be auto-overridden.

---

## DEC-ship-probate-workflow-shift

- source: /Users/ryanhawker/Desktop/SiftStack/SHIP_DECISION.md
- status: LOCKED (Accepted, 2026-06-29)
- precedence: 0

**Decision:** SHIP the SiftStack probate workflow shift starting the next business day. Run the 6 AM cron in production. Gypsy continues manual scrape in parallel for 1 week; retire manual workflow if no regressions surface.

**Scope:** SiftStack probate workflow, daily cron pipeline (OnBase + Tracerfy + Trestle), Gypsy manual scrape migration.

**Rationale:** Validated end-to-end on 2 representative days + 6-day historical holdout. Risk assessed LOW.

---

## DEC-fc-recall-invariant

- source: /Users/ryanhawker/Desktop/SiftStack/SHIP_DECISION.md
- status: LOCKED (accuracy invariant)
- precedence: 0

**Decision:** Foreclosure (FC) recall MUST be 100% on the daily cron path. Holdout v3 validated at 100% (34/34).

**Scope:** Foreclosure recall on daily cron; ship-ready bracket ≥ 95%.

---

## DEC-phone-accuracy-invariant

- source: /Users/ryanhawker/Desktop/SiftStack/SHIP_DECISION.md
- status: LOCKED (accuracy invariant)
- precedence: 0

**Decision:** Phone field accuracy MUST be 100% across measured intersections. Aggregate 65/65 perfect matches across all backtests. Zero wrong digits across the entire iteration history.

**Scope:** Phone 1 field (OnBase PDF → Claude Vision path); Trestle-scored phone tiers.

---

## DEC-case-number-join-invariant

- source: /Users/ryanhawker/Desktop/SiftStack/SHIP_DECISION.md
- status: LOCKED (invariant)
- precedence: 0

**Decision:** Downstream consumers join by `case_number`, not `date_filed`. Enforced by commit `b2039cb` (case_number propagation). Makes fast-close date-bucketing drift invisible to downstream tags.

**Scope:** Probate + FC record join semantics.

---

## DEC-pr-recall-target

- source: /Users/ryanhawker/Desktop/SiftStack/SHIP_DECISION.md
- status: Accepted (non-locked target)
- precedence: 0

**Decision:** Personal Representative (PR) recall target is ≥ 95% on daily cron path. Current holdout v3 is 88% (30/34); the 12% gap is bookkeeping noise (1-2 day clerical lag on fast-close case types), not missing data. Cases ARE captured; they appear in the adjacent day's CSV and are joined by `case_number`.

**Scope:** PR recall on multi-week backfill regime (holdout v3, 6 April days).

---

## DEC-rollback-triggers

- source: /Users/ryanhawker/Desktop/SiftStack/SHIP_DECISION.md
- status: Accepted (operational)
- precedence: 0

**Decision:** Rollback to Gypsy manual collection if ANY of: 3+ days of count-mismatches in Week 1 or Week 2, OR > 30% Tier-1 phone error rate on spot-checks, OR an unrelated bug (malformed CSV, Slack post failure).

**Scope:** 3-week cutover risk containment.

---

## DEC-not-shipped

- source: /Users/ryanhawker/Desktop/SiftStack/SHIP_DECISION.md
- status: Accepted (scope decisions)
- precedence: 0

**Decision:** The following were considered and NOT shipped:

- **Fix 4** (case_status_date for OPEN) — superseded by Fix 5 (`c911aa2`)
- **Fix 6** (petition-filing docket entry detection) — impossible; Gypsy's "Date Filed" doesn't correspond to any docket entry
- **Threshold tuning** for `REOPEN_GAP_DAYS = 14` — remaining misses are CLOSED cases that don't traverse that branch
- **Archived-docket workaround** — 6th fix, out of scope of 5-fix spec; not blocking daily cron
