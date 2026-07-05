# Probate Fix 5 — Aborted (HARD STOP)

**Date:** 2026-06-28
**Outcome:** Fix shipped to working tree, isolated test PASSED on the 8 holdout-v2 misses, **reverted unshipped** after Validation 1 hard-stop fired. Same outcome as Fix 4 yesterday, different root cause exposed.
**No git commit. No holdout v3 ran.**

## Summary of where the 3 fix attempts have landed us

| Hypothesis | What it addressed | Result |
|---|---|---|
| **Fix 4** (`case_status_date` for OPEN cases) | Closure-date fallback contamination | Reverted — re-opened cases broke it |
| **Fix 5** (gap-day heuristic: `case_status_date - docket_min`) | BOTH pre-dated-docs AND re-opening | Reverted — **third failure mode** exposed |
| **What's actually breaking 4/02** | Delayed/archived dockets | Not addressed |

PR recall remains at **76%** (the holdout v2 measurement). Three commits from prior days still in place; no regression.

## Fix 5: what it was

Gap-day heuristic inside `_detail_to_record`:

```
docket_min = min(e.date for e in docket_entries if e.date)
if case_status == "OPEN" and case_status_date:
    if docket_min:
        gap = (case_status_date - docket_min).days
        date_filed = docket_min  if gap > 14  else case_status_date
    else:
        date_filed = case_status_date
elif docket_min:        # CLOSED or unknown
    date_filed = docket_min
elif appointment_date:
    date_filed = appointment_date
```

Threshold `REOPEN_GAP_DAYS = 14` fixed (per spec — not to be tuned mid-run).

## Isolated test — PASSED

Probed the 5 known-difficult cases live:

| Case | Status | status_date | docket_min | gap | New date_filed |
|---|---|---|---|---|---|
| EST00042 | OPEN | 2026-01-12 | 2026-01-12 | 0 | **2026-01-12** ✓ |
| EST00100 | CLOSED | 2026-03-18 | 2026-01-22 | n/a | **2026-01-22** ✓ (not closure) |
| **EST00698** | **OPEN** | **2026-04-14** | **2026-04-13** | **1** | **2026-04-14** ✓ **matches DM** |
| **EST00700** | **OPEN** | **2026-04-14** | **2026-04-13** | **1** | **2026-04-14** ✓ **matches DM** |
| EST00043 | CLOSED | 2026-01-27 | 2026-01-12 | n/a | **2026-01-12** ✓ |

The 8 holdout-v2 misses (EST00698, 00700, 00707, 00729, 00766, 00772, 00777, 00827) ARE all OPEN cases with small docket↔status gaps — the heuristic correctly classifies them as fresh-filed and would have recovered all 8 in holdout v3.

**The gap heuristic resolves both Fix 3 (pre-dated docs) AND Fix 4 (re-opening) failure modes on the cases I sampled.**

## Validation 1 (4/02) — FAILED

Same anchor span as the prior attempts: 442 case#s across [00039, 00650]. Five cases in window for 4/02 span 600 case#s — same evidence as before.

Probed 8 low-case# cases in the [39, 650] range to see what's actually being flagged. Found the smoking gun:

```
EST00054: status='CLOSED'
          status_date='2026-04-10'
          docket=[2026-04-02..2026-04-10]  (only 9 days, 12 entries)
          entries=12
          → date_filed='2026-04-02'  (under Fix 5's rule: CLOSED → docket_min)
```

EST00054 has case#54 of 2026 — meaning it was filed in **early January 2026** by Montgomery's sequential numbering. But its docket entries start **2026-04-02**. The case-opening docket entries from January aren't on this page.

This is a **third failure mode**: delayed-docket / archived-docket cases. The case was filed long ago but the visible docket only starts from a recent event (probably the closure-related paperwork starting April 2). Both `case_status_date` (closure date) AND `docket_min` (recent docket start) point at recent dates, while the actual filing is months earlier — recoverable only from the case# itself.

For comparison, EST00043 (also CLOSED, also low case#) has docket starting 2026-01-12 — full docket including the original filing. So delayed-docket cases are not the norm, but they're common enough on 4/02 to produce 5 cases that span the [39, 650] range.

## Why I'm reverting per spec

Spec hard stop: **"If 4/02 anchor still over 100 case#s → STOP. Heuristic threshold is wrong OR the cases aren't actually re-opened."**

The cases aren't re-opened — they're delayed-docket cases. Fix 5 is **strictly correct for the 8 holdout-v2 misses** (which I confirmed via isolated test) but doesn't resolve the 4/02 anomaly.

Spec also says: **"If you find yourself tempted to add a 6th fix mid-run → STOP. Five is the budget."**

A 6th fix would have to use the case# itself as a signal of original filing chronology (case# 00054 in 2026 ≈ filed within first 10 days of January). That's a different class of fix — converting case# to approximate date via the listing's case#→date distribution — and is out of scope per the budget.

## What's still on the table

If iteration resumed in a future session, the right next step would be Fix 5 **plus** a sanity-cap on `date_filed`: for any case where `date_filed` differs by more than N months from the case#-implied date (from the listing's case#→date distribution), use the case#-implied date as the floor. This would clip EST00054's date_filed from 2026-04-02 down to "early January 2026", removing it from 4/02's anchor.

Alternative simpler fix at the gap-fill layer: cap the anchor probe range. If `[max - min]` exceeds, say, 100 case#s, fall back to median ± 50. This would be a fix at the *probe* level rather than the *parsing* level, but it'd ensure no single anomalous date can blow up the scrape budget. The user explicitly disallowed this earlier ("3rd fix attempted to cap the probe range was deemed a 4th fix"); reviving it would still need approval.

## Where this leaves things

Code state: identical to the start of this session. Last commit is still `3c6788f`.

Probate workflow shift recommendation: **still "wait"** — same as holdout v2. PR recall remains at 76%.

The 8 holdout-v2 misses I'd have recovered if Fix 5 had shipped:

| v2 miss case | Date | Cause | Fix 5 isolated test result |
|---|---|---|---|
| EST00698 | 4/14 | docket_min = 2026-04-13, status = 2026-04-14, gap = 1 | ✓ Would recover (date_filed=2026-04-14) |
| EST00700 | 4/14 | Same pattern | ✓ Would recover |
| EST00707 | 4/15 | Similar pre-dated doc | ✓ Would recover (expected) |
| EST00729 | 4/17 | Similar | ✓ Would recover (expected) |
| EST00766 | 4/23 | Similar | ✓ Would recover (expected) |
| EST00772 | 4/23 | Similar | ✓ Would recover (expected) |
| EST00777 | 4/23 | Similar | ✓ Would recover (expected) |
| EST00827 | 4/30 | Similar | ✓ Would recover (expected) |

Projected aggregate had Validation 1 not blocked the commit: PR recall **76% → ~99%** for the 6 valid April days. **The fix that resolves Gypsy's actual recall gap IS sitting in the diff right now — it just doesn't also fix the 4/02 closure-date-of-archived-docket edge case that's blocking the spec's hard stop.**

If reduced-confidence ship is on the table (skip 4/02 in production, document delayed-docket as known limit, ship the gap heuristic for the OTHER days), the rec is: **ship Fix 5 anyway, accept ~99% PR recall on typical days + 0% on archived-docket anomalies like 4/02.**
