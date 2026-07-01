# Constraints (Synthesized from SPECs)

Each constraint records source, type (api-contract | schema | nfr | protocol), and content. Constraints that contradict a higher-precedence ADR are auto-resolved in favor of the ADR (see INGEST-CONFLICTS.md).

---

## CON-fast-close-bucketing-offset

- source: /Users/ryanhawker/Desktop/SiftStack/docs/known_limitations.md (Ticket 1)
- type: protocol (data-bucketing behavior)
- priority: LOW

**Constraint:** 4 of 34 holdout-v3 PR cases bucket on `docket_min` instead of the data manager's logged "Date Filed". Offset by 1-2 days. Cases appear in the adjacent daily CSV.

**Affected case types:** SUMMARY RELEASE, TRANSFER OF REAL ESTATE ONLY; W/O WILL, RELEASE OF ADMIN; W/O WILL (all fast-close types that file → process → close within ~1 week).

**Observed cases:**

- EST00729 (WILLIAM CARMACK): docket_min 2026-04-16, Gypsy Date Filed 2026-04-17 (+1 day)
- EST00766 (DAVID BAKER): docket_min 2026-04-21, Gypsy Date Filed 2026-04-23 (+2 days)
- EST00772 (JAMES AVRA): docket_min 2026-04-21, Gypsy Date Filed 2026-04-23 (+2 days)
- EST00777 (ROBERT BRENNER): docket_min 2026-04-22, Gypsy Date Filed 2026-04-23 (+1 day)

**Root cause:** Gypsy's Date Filed doesn't correspond to any docket entry — most likely court-stamped filing date on petition PDF, which posts 1-2 days after supporting-document docket entries. SiftStack reads `docket_min` (earliest docket entry, supporting-document date).

**Impact:**

- Cosmetic only under current case_number-keyed consumers (see DEC-case-number-join-invariant)
- Would matter if a future workflow joins by `date_filed` (DataSift's daily list import loosely does this via "Date Added" field)

**Possible fix (crude):** hardcoded case-type offset map:

```python
FAST_CLOSE_OFFSETS = {
    "SUMMARY RELEASE": 2,
    "TRANSFER OF REAL ESTATE ONLY; W/O WILL": 1,
    "RELEASE OF ADMIN; W/O WILL": 1,
}
```

Add offset to `docket_min` for CLOSED cases whose `case_type` matches.

**Possible fix (better long-term):** Extract court-stamp date from application PDF via Claude Vision (already in OnBase enrichment path — add filing-date field to Vision prompt).

**Validation set:** Single-day backfill on 2026-04-23; confirm EST00766/772/777 + EST00729 appear on the correct day's CSV.

**Evidence:** backtest_2026-06-28_holdout_v3.md; conversation diagnostic 2026-06-28 with full docket dumps.

---

## CON-archived-docket-cases

- source: /Users/ryanhawker/Desktop/SiftStack/docs/known_limitations.md (Ticket 2)
- type: protocol (gap-fill anchor probe)
- priority: LOW

**Constraint:** ~5-10 Montgomery cases per year have case# implying early-year filing but visible docket that only starts months later. Backfill on `--date-from` against docket-implied date balloons gap-fill anchor span (observed 600 case#s on 2026-04-02 vs normal 30-60).

**Canonical example:** EST00054 — case# implies filing early January 2026, visible docket runs 2026-04-02 to 2026-04-10 only.

**Root cause:** Case-detail HTML's visible docket truncates or archives older entries on certain cases. Original case-opening docket entries aren't on the page. Both `case_status_date` (closure) and `docket_min` (earliest visible entry) point at dates months after actual filing.

**Impact:**

- Backfill cost only — day with 1+ archived-docket cases triggers runaway gap-fill probe range (4+ hours per day vs normal 30 min)
- Daily cron unaffected (today's cases always have full recent dockets)
- Mitigation in place: 6 AM cron only scrapes today's date; multi-week backfills should skip affected dates manually

**Possible fix paths (pick one):**

1. Cap anchor probe range: if `[max - min]` > N (say 100), fall back to median ± 50. Cheapest; clips runaway without addressing underlying date_filed inaccuracy.
2. Parse "Prior Case Number" field on case-detail page (if exposed) — re-opened case indicator gives chronology hint.
3. Estimate filing date from case# sequence: build `case# → date` map from year-wide listing's already-captured cases, then nearest-neighbor interpolation. Most accurate; most invasive.

**Validation set:** 2026-04-02 (canonical broken day). After fix, backfill completes in ~30 min with anchor span < 100 case#s.

**Evidence:** backtest_2026-06-28_fix5_aborted.md — smoking-gun on EST00054.

---

## CON-short-form-fc-case-number

- source: /Users/ryanhawker/Desktop/SiftStack/docs/known_limitations.md (Ticket 3)
- type: api-contract (scraper listing filter)
- priority: MEDIUM

**Constraint:** Foreclosure cases with case#s in short-form `2026 CV 0XXX` format (4 digits after CV vs usual 5) don't appear in SiftStack's foreclosure listing. 8 FC cases missed in 2026-06-27 failed holdout; 0 misses in April 2026 v3 holdout.

**Observed missed cases (all 2026 CV 0XXX):**

- 2026 CV 0484 — HARCUS, CARL S (5/22)
- 2026 CV 0485 — FORMER Y XENIA LLC (5/22)
- 2026 CV 0486 — FORMER Y XENIA LLC (5/22)
- 2026 CV 0501 — CREWE, ROBERT D (5/22)
- 2026 CV 0517 — CLARK, CARRIE (6/01)
- 2026 CV 0440 — TIBBALS, LYNN K (5/11)
- 2026 CV 0415 — CAMPBELL, JARROD R (5/01)
- 2026 CV 0402 — HEMMINGSEN, TAMMY (4/22)

**Root cause (unconfirmed hypotheses):**

- pro.mcohio.org filters foreclosure listing by case# length
- Cases use a different listing source or category
- "MORTGAGE FORECLOSURE" action-type filter excludes this batch

**Impact:**

- 5-10 FC cases per year potentially missed
- Gypsy spot-check would catch these via case-count delta vs her sheet

**Possible fix:**

1. Live recon on pro.mcohio.org — search `2026 CV 0484` directly, identify excluding filter
2. If action-type filter, add missing type to scraper filter set
3. If separate listing source, add second scrape pass

**Validation set:** Single-day backfills on 2026-05-22 (4 short-form misses) and 2026-05-01 (1 miss). After fix, all named cases must appear in FC bucket of CSV.

**Evidence:** backtest_2026-06-27_holdout.md (original 8-case observation); backtest_2026-06-27_holdout_v2.md (no misses in April, suggesting batch-concentrated issue).
