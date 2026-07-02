---
phase: D-pro-mcohio-org-recaptcha-v3-mitigation
type: context
requirement: BUG-04
priority: HIGH
created: 2026-07-01
---

# Phase D Context — pro.mcohio.org reCAPTCHA v3 Mitigation

Diagnostic evidence, scope fences, and locked decisions for the reCAPTCHA v3 fix.
Written to skip `/gsd-discuss-phase`; discovery is complete before planning.

## Diagnostic Evidence

### Failure Signature

Between the 2026-06-30 06:03 AM cron run (43 rows / 9 unique cases, healthy) and
the 2026-07-01 06:00 AM cron run (0 rows / 0 unique cases), `pro.mcohio.org`
deployed **reCAPTCHA v3 invisible bot-scoring**. Every daily cron run since
06:00 AM ET on 2026-07-01 returns 0 records. The scraper's Playwright flow
completes without exception, but the portal serves a block page in place of
`#tblSearchResults`.

### Block Page Content (screenshot: /tmp/mont_fc_results.png)

> reCAPTCHA (a system for detecting whether you are a real user or a bot) has
> flagged you as likely being a bot or automated browser instead of a real
> human. This could be for the following reasons: [datacenter IP, too many
> searches in quick succession, automated requests, missing CAPTCHA token,
> previous IP flagged]
>
> We suggest you leave PRO and try the search again in 20 minutes...

### Re-verification (2026-07-01, 21:01 ET)

17+ minutes after the last query (well past the portal's suggested "20 minutes"
cool-down), the block page still fires. The IP-score decays slowly OR the
scraper's IP is persistently flagged. **Wait-and-retry is NOT a viable
strategy.**

### Silent-Failure Mechanism

`parse_results_table()` in `src/h3/scrapers/mcohio.py:176` selects
`#tblSearchResults`. The block page has no such element → `tbody is None` →
returns `[]` → `group_rows_into_cases([])` → `[]` → orchestrator logs
`"Parsed 0 rows → 0 unique cases"` as if the courthouse had no filings.
No exception is raised. No alert fires. The Slack post lands with a probate-only
CSV and nobody notices until Gypsy's manual scrape flags a count-delta the
following business day.

## Scope Fences (LOCKED)

| Item | In scope | Out of scope |
|------|----------|--------------|
| pro.mcohio.org Montgomery FC scraper | ✅ | — |
| go.mcohio.org Montgomery probate scraper | — | ❌ (different subdomain, unaffected) |
| realforeclose.com sheriff sale PREVIEW URLs | — | ❌ (no reCAPTCHA) |
| Equivant / Warren / other 6 OH counties FC | — | ❌ (different portal integration paths) |
| TN pipeline (`captcha_solver.py` v2 flow) | — | ❌ (must not regress) |
| Orchestrator top-level flow (`src/ohio_orchestrator.py`) | — | ❌ (no changes to wave scheduling / list routing) |
| Residential proxy path | — | ⏸ deferred (see below) |

## Locked Decisions

- **D-01**: Primary mitigation is **2Captcha v3 token solving**. Uses the
  existing `CAPTCHA_API_KEY` config. Cost projected <$1/mo at daily-cron
  cadence. Chosen over playwright-stealth-only because stealth reduces the
  score baseline but cannot guarantee a passing score on a datacenter IP.

- **D-02**: **Silent-failure detection ships first** as Plan D.1, independent
  of and before the 2Captcha v3 integration. Never again should a
  reCAPTCHA / bot-block page silently masquerade as "courthouse had no
  filings today". This guardrail is required regardless of which mitigation
  proves durable; it becomes the tripwire if the portal switches anti-bot
  vendors in the future.

- **D-03**: **playwright-stealth is contingent on Plan D.2 alone not clearing
  the 5-consecutive-run success bar.** Plan D.3 is written and ready to ship,
  but not executed unless Plan D.2 fails observation.

- **D-04**: **Do NOT modify `src/captcha_solver.py`.** That module solves
  reCAPTCHA v2 for the TN pipeline. Phase D adds a NEW module
  (`src/recaptcha_v3_solver.py`) for the v3 protocol. The two solvers coexist.

- **D-05**: **Do NOT modify probate scraper, sheriff sale scraper, or
  `ohio_orchestrator.py` top-level flow.** Every touched file listed in a
  plan's `files_modified` frontmatter must be Montgomery-FC-specific or
  brand-new.

## Deferred Ideas

- **Residential proxy** — routes requests through non-datacenter IPs via the
  existing `proxy_config_url` parameter in `MontgomeryScraper.__init__`.
  Cost: $20-100/mo. Deferred as an operator decision: the operator should
  own the cost tradeoff. If D.2 + D.3 both prove insufficient, the operator
  will subscribe to a residential proxy (Bright Data, Oxylabs, or similar)
  and pass the URL via `PRO_MCOHIO_PROXY_URL` env var. **Do not build the
  subscription integration in Phase D.** The parameter already exists.

- **Slack-notify hook on reCAPTCHA block detection** — ACC-03 in
  REQUIREMENTS.md v2 tracks the general on-call alert. Plan D.1 will log
  the block-page detection at ERROR level and raise a typed exception; the
  Slack integration is v2 work.

## Related Artifacts

- **ROADMAP.md Phase D**: `.planning/ROADMAP.md` (lines 99-114)
- **Requirement**: BUG-04 in `.planning/REQUIREMENTS.md` (line 33)
- **Failure screenshot**: `/tmp/mont_fc_results.png`
- **Block page HTML** (captured 2026-07-01): `/tmp/mont_fc_results.html`
- **Reference solver (v2)**: `src/captcha_solver.py`
- **Target scraper**: `src/h3/scrapers/mcohio.py` (class `MontgomeryScraper`)
- **Public wrapper**: `src/ohio_foreclosure_scrapers.py`
  (`fetch_montgomery_foreclosure`, `_run_montgomery_live`)
- **Config**: `src/config.py` (existing `CAPTCHA_API_KEY` will be reused)
- **Last known-good cron output**: 2026-06-30 06:03 AM (43 rows / 9 cases,
  logs/ohio_daily.log)

## Success Criteria (from ROADMAP + user brief)

1. Replay of `2026-06-29 → 2026-06-30` query returns ~43 rows / 9 unique cases
2. 5 consecutive daily 6 AM cron runs each return non-zero somewhere in the
   sliding window with NO reCAPTCHA block pages
3. FC recall = 100% invariant intact; phone accuracy = 100% invariant intact
4. Probate + sheriff sale paths unchanged
5. Silent-failure detection: scraper raises/logs "reCAPTCHA blocked" if it
   ever hits the block page (no more silent 0 records)

## Plan Set

| Plan | Purpose | Ship Order | Wave |
|------|---------|------------|------|
| D.1  | Silent-failure detection guardrail — ship IMMEDIATELY so we never silently lose FC data again | 1st (blocking) | 1 |
| D.2  | 2Captcha v3 integration — primary fix; injects token before Search click | 2nd | 2 |
| D.3  | playwright-stealth fingerprint reduction — contingent on D.2 alone not meeting success bar #2 | 3rd (contingent) | 3 |

D.1 ships even if D.2/D.3 never do — its value is independent. D.2 is the
primary fix. D.3 is written now so if the 5-consecutive-run observation window
(success criterion #2) fails on D.2 alone, execution is fast.
