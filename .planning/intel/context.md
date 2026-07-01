# Context (Synthesized from DOCs)

Running architectural / operational notes keyed by topic. Verbatim excerpts with source attribution.

---

## Topic: Ohio Orchestrator — Production Cron Slots

- source: /Users/ryanhawker/Desktop/SiftStack/docs/ohio_orchestrator.md

The Ohio pipeline runs from three production cron slots that NEVER mix records between DataSift lists:

- **Daily 6:00 AM ET** — mode `daily` — sources: foreclosure + probate + sheriff_sale — county: Montgomery — DataSift list: **H3 Montgomery Courthouse Data**
- **Monday 6:00 AM ET** — mode `weekly` — sources: foreclosure + probate + sheriff_sale — counties: Butler, Clark, Clermont, Greene, Miami, Warren — DataSift list: **H3 SW Ohio Courthouse Data**
- **Every 3 months** — mode `quarterly` — source: tax_delinquent (+ parcel→address enrichment) — Montgomery only — DataSift list: H3 Montgomery Courthouse Data

Daily + weekly cover fresh court activity (high cadence, fast scrape). Quarterly handles the slow-changing tax-delinquent feed with expensive parcel→address enrichment (~15 min at concurrency=5). Montgomery-only for tax_delinquent because iasWorld lookup is mcrealestate.org-specific; other counties' tax_delinquent feeds are skipped to avoid producing records without addresses.

The county→list routing is enforced in `src/ohio_destination_lists.py` and locked by `tests/test_ohio_destination_lists.py` (30 tests).

---

## Topic: Ohio Orchestrator — CLI

- source: /Users/ryanhawker/Desktop/SiftStack/docs/ohio_orchestrator.md

Primary invocations:

```bash
python src/ohio_orchestrator.py daily
python src/ohio_orchestrator.py weekly
python src/ohio_orchestrator.py quarterly
```

Operator escape hatches:

```bash
python src/ohio_orchestrator.py daily --dry-run        # print plan, no scrape
python src/ohio_orchestrator.py daily --no-upload      # scrape + CSV, no DataSift
python src/ohio_orchestrator.py quarterly --no-upload  # spot-check before pushing
python src/ohio_orchestrator.py weekly --headed        # visible browser (debug)
python src/ohio_orchestrator.py weekly -v              # DEBUG-level logging
```

---

## Topic: Ohio Orchestrator — Cron Wiring (macOS launchd)

- source: /Users/ryanhawker/Desktop/SiftStack/docs/ohio_orchestrator.md

Three `.plist` files live in `~/Library/LaunchAgents/`:

- `com.siftstack.ohio-daily.plist` — StartCalendarInterval Hour=6 Minute=0, runs `daily` mode
- `com.siftstack.ohio-weekly.plist` — StartCalendarInterval Weekday=1 (Monday) Hour=6 Minute=0, runs `weekly` mode
- `com.siftstack.ohio-quarterly.plist` — Array of StartCalendarInterval entries (Jan 15 / Apr 15 / Jul 15 / Oct 15 at 6 AM), runs `quarterly` mode

Env vars in plist: `TZ=America/New_York`, `DATASIFT_EMAIL`, `DATASIFT_PASSWORD`. Logs to `logs/ohio_{mode}.log` and `logs/ohio_{mode}.err`.

Load: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<plist>`.

---

## Topic: Ohio Orchestrator — Cron Wiring (Linux)

- source: /Users/ryanhawker/Desktop/SiftStack/docs/ohio_orchestrator.md

`/etc/cron.d/siftstack-ohio` (traditional cron):

```cron
TZ=America/New_York
0 6 * * *          ryanhawker cd /opt/siftstack && /opt/siftstack/.venv/bin/python src/ohio_orchestrator.py daily
0 6 * * 1          ryanhawker cd /opt/siftstack && /opt/siftstack/.venv/bin/python src/ohio_orchestrator.py weekly
0 6 15 1,4,7,10 *  ryanhawker cd /opt/siftstack && /opt/siftstack/.venv/bin/python src/ohio_orchestrator.py quarterly
```

Preferred: `systemd` timer. Service file uses `Type=oneshot`, `EnvironmentFile=/opt/siftstack/.env`. Timer uses `OnCalendar=*-*-* 06:00:00 America/New_York` for daily, `OnCalendar=Mon *-*-* 06:00:00 America/New_York` for weekly, plus `Persistent=true` so missed runs fire on next boot.

---

## Topic: Ohio Orchestrator — Cross-Contamination Guard

- source: /Users/ryanhawker/Desktop/SiftStack/docs/ohio_orchestrator.md

The county→list routing is enforced at multiple layers; test suite fails before deploy if any is violated:

1. **`destination_list_for_county()`** raises `ValueError` on unknown counties — no silent fallback
2. **`split_by_destination_list()`** buckets EVERY notice by its county; orchestrator never bypasses this
3. **`run_daily()` plan** never includes a non-Montgomery county
4. **`run_weekly()` plan** never includes Montgomery
5. **Tests** — `test_dry_run_never_lets_montgomery_into_the_weekly_bucket` + `test_run_daily_never_writes_sw_ohio_csv` lock both directions

Off-script CLI (`--counties Montgomery,Butler` outside of daily/weekly modes) still routes correctly via `split_by_destination_list`, but production should always go through `daily` or `weekly`.

---

## Topic: Ohio Orchestrator — Operator Pre-Flight Checklist

- source: /Users/ryanhawker/Desktop/SiftStack/docs/ohio_orchestrator.md

Before first production cron firing:

- Smoke-run both modes with `--dry-run` to confirm county→list plan
- Smoke-run both modes with `--no-upload` to confirm scrape succeeds + CSV well-formed
- Check `output/OH_Montgomery_daily_*.csv` and `output/OH_SW_Ohio_weekly_*.csv` have records, county column matches bucket, `Lists` column has right value
- Manually upload one CSV via DataSift wizard to verify list mapping + tag stacking work
- Activate cron / launchd / systemd timer
- Schedule Slack-notify hook on orchestrator exit code so a failure pages the on-call

---

## Topic: Historical Backfill Validation (Cross-Reference)

- source: /Users/ryanhawker/Desktop/SiftStack/SHIP_DECISION.md (referenced from all three other docs)

Holdout v3 (6 April days, 4/02 excluded for archived-docket edge case) baseline:

- FC recall: 100% (34/34) — ≥95% ship-ready
- PR recall: 88% (30/34) — 85-89% modest improvement bracket
- Phone field accuracy: 100% (27/27) — unchanged from prior backtests
- Aggregate phone accuracy across all backtests: 65/65 perfect matches, zero wrong digits

Two representative daily-cron validations completed with full Phase 3 stack (OnBase + Tracerfy + Trestle):

- 2026-06-28 (Sat, quiet day, no FC filings): 26 records, 1 PR / 0 phones from OnBase, 0 Tracerfy matches, 17 min wall clock, $0.00 cost
- 2026-06-25 (Thu, active day): 43 records, 10 PR / 5 phones from OnBase, 5/8 Tracerfy matched, 15/9 Trestle tagged, 21 min wall clock, $0.44 cost

Both days completed without errors or Playwright crashes.

---

## Topic: Probate Commit Stack (Ship-Enabling)

- source: /Users/ryanhawker/Desktop/SiftStack/SHIP_DECISION.md

Five probate-specific commits on `main` that produced the ship-ready accuracy:

- `c911aa2` — Fix 5: docket↔status gap heuristic; recovers pre-dated-docs cases
- `3c6788f` — gap-fill log clarification + design doc
- `fdf8b5c` — Fix 2: ±50 fallback for degenerate 1-case anchors
- `cbcfe8f` — Fix 1: ±15 anchor padding
- `2040604` — gap-fill foundation (probe case#s the sparse year-wide listing skips)

Foundational (earlier):

- `b2039cb` — case_number propagation (enables downstream case_number join invariant)
- `75da6d5` — `--max-cases` flag (enables backfills at all)

---

## Topic: Current-State Gypsy Manual Workflow

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md

Manual data collection taking ~2 hours every weekday morning (~10 hours/week):

1. Opens Montgomery County probate portal (`go.mcohio.org`) and foreclosure portal (`pro.mcohio.org`) by hand
2. For probate: walks each day's new cases, opens case-detail page, clicks into application PDF, copies fiduciary phone + address into `FTM Probate` Google Sheet
3. For foreclosure: reads each new case docket, logs defendant names + addresses into `FTM Foreclosure` Google Sheet
4. Tags hot leads for morning call list; manually deduplicates against prior week's data

Error rate undocumented but observed in holdout backtests: date-bucketing drift, occasional missed cases, occasional typos.

---

## Topic: Target Delivery Channel

- source: /Users/ryanhawker/Desktop/SiftStack/docs/gypsy_migration_plan.md

SiftStack posts the daily CSV to `#h3-homebuyers-ftm` in Slack as an attachment. Team picks up the CSV and starts dialing. Post lands ~6 AM ET.
