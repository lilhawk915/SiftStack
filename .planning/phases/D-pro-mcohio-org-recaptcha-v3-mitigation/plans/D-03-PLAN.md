---
phase: D-pro-mcohio-org-recaptcha-v3-mitigation
plan: 03
type: execute
wave: 3
depends_on:
  - D-01
  - D-02
files_modified:
  - requirements.txt
  - src/h3/scrapers/mcohio.py
  - tests/test_mcohio_stealth.py
autonomous: false
contingent: true
requirements:
  - BUG-04
must_haves:
  truths:
    - "playwright-stealth is applied to the MontgomeryScraper's BrowserContext, reducing fingerprint signals (webdriver flag, plugins array, WebGL vendor, chrome runtime)"
    - "Combined with D.2's token injection, the reCAPTCHA v3 score rises above the portal's threshold consistently across 5 consecutive daily cron runs"
    - "TN pipeline (`captcha_solver.py` + `scraper.py`) is unchanged — no global playwright-stealth application"
    - "Probate + sheriff sale scrapers unchanged"
  artifacts:
    - path: "requirements.txt"
      provides: "playwright-stealth pin (>=1.0.6)"
      contains: "playwright-stealth"
    - path: "src/h3/scrapers/mcohio.py"
      provides: "Stealth applied to the BrowserContext returned by _launch_browser"
      contains: "stealth_async"
    - path: "tests/test_mcohio_stealth.py"
      provides: "Test asserting stealth is applied when the module is available; graceful skip when it isn't"
      exports: ["test_stealth_applied_when_available", "test_stealth_optional_when_missing"]
  key_links:
    - from: "src/h3/scrapers/mcohio.py::_launch_browser"
      to: "playwright_stealth.stealth_async"
      via: "await stealth_async(ctx) immediately after new_context()"
      pattern: "stealth_async"
---

<objective>
CONTINGENT: only execute if the Plan D.2 checkpoint reports
`"D.2 partial: block still fires"` or if the 5-consecutive-run observation
gate (ROADMAP Phase D success criterion #2) fails on D.2 alone.

Layer `playwright-stealth` fingerprint patches onto the Montgomery scraper's
BrowserContext to reduce the score baseline the portal computes. Combined
with D.2's server-solved token, this should push the reCAPTCHA v3 score
above the portal's threshold reliably.

Purpose: Backstop primary mitigation. Stealth reduces datacenter/automation
fingerprint signals (missing plugins, webdriver flag, WebGL vendor spoofing,
Chrome runtime absence). It cannot bypass IP-reputation scoring but it
raises the baseline enough that the 2Captcha-supplied token is accepted more
consistently. Free (open-source library). Contingent, not mandatory.

Output:
  - `requirements.txt` — add `playwright-stealth>=1.0.6`
  - `src/h3/scrapers/mcohio.py` — apply `stealth_async(ctx)` inside
    `_launch_browser`
  - `tests/test_mcohio_stealth.py` — regression test
</objective>

<execution_context>
Standard execute-plan flow with a blocking human-verify checkpoint to
re-run the 5-consecutive-run observation window after stealth ships.
</execution_context>

<context>
@.planning/ROADMAP.md
@.planning/phases/D-pro-mcohio-org-recaptcha-v3-mitigation/CONTEXT.md
@.planning/phases/D-pro-mcohio-org-recaptcha-v3-mitigation/plans/D-01-PLAN.md
@.planning/phases/D-pro-mcohio-org-recaptcha-v3-mitigation/plans/D-02-PLAN.md
@src/h3/scrapers/mcohio.py
@requirements.txt
</context>

<tasks>

<task type="checkpoint:decision" gate="blocking">
  <name>Task 1: Confirm D.3 is warranted based on D.2 observation results</name>
  <decision>Execute D.3 (playwright-stealth layer) or hold?</decision>
  <context>
    D.3 is written but should only ship if D.2 alone does not clear the
    5-consecutive-run success bar. Executing D.3 preemptively adds a
    dependency (`playwright-stealth`) and mutates the browser context for
    every Montgomery cron run. If D.2 is clearing runs, D.3 adds surface
    area without value.
  </context>
  <options>
    <option id="ship-d3">
      <name>Ship D.3 now</name>
      <pros>Belt-and-suspenders: stealth + token together handle the widest range of portal-side tightening; free.</pros>
      <cons>Adds a dependency; couples Montgomery FC to a third-party fingerprint patch that could drift with Playwright upgrades.</cons>
    </option>
    <option id="hold-d3">
      <name>Hold D.3 until D.2 fails observation</name>
      <pros>Minimum blast radius; keeps the change set to the essential fix.</pros>
      <cons>If D.2 fails mid-week, execution latency is 1-2 hours before the fix lands.</cons>
    </option>
    <option id="cancel-d3">
      <name>Cancel D.3 permanently</name>
      <pros>Simplest.</pros>
      <cons>Leaves no local backstop before the operator has to buy a residential proxy subscription.</cons>
    </option>
  </options>
  <resume-signal>
    Reply `ship-d3`, `hold-d3`, or `cancel-d3`. Default (auto-advance): if
    the D.2 Task 4 checkpoint reply was `"D.2 partial: block still fires"`,
    proceed as `ship-d3` without further prompt. Otherwise treat as
    `hold-d3`.
  </resume-signal>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Add playwright-stealth dependency + apply to MontgomeryScraper context</name>
  <files>requirements.txt, src/h3/scrapers/mcohio.py, tests/test_mcohio_stealth.py</files>
  <behavior>
    - Test 1 (stealth applied when available): with `playwright_stealth`
      importable, `_launch_browser` calls `stealth_async(ctx)` exactly
      once, with the context returned by `browser.new_context(...)`, and
      the call happens AFTER `add_init_script` (so stealth's patches take
      precedence over our own webdriver-hiding init script — stealth's
      set is a superset).
    - Test 2 (graceful degradation when missing): with `playwright_stealth`
      NOT importable (simulate via `sys.modules['playwright_stealth'] =
      None` context manager), `_launch_browser` completes without raising,
      logs a WARNING `"playwright-stealth unavailable — proceeding without
      fingerprint patches"`, and returns a valid browser + context. This
      protects operators whose local venv hasn't been reinstalled after
      the requirements change.
  </behavior>
  <action>
    In `requirements.txt`:
    - Append `playwright-stealth>=1.0.6` after the existing
      `playwright>=1.40.0` line. Maintain alphabetical-ish grouping — the
      current file is grouped by concern, so place immediately below
      `playwright>=1.40.0` to signal the coupling.

    In `src/h3/scrapers/mcohio.py`:
    - At the top of `_launch_browser`, wrap the import in a try/except:
      ```
      try:
          from playwright_stealth import stealth_async
          _STEALTH_AVAILABLE = True
      except ImportError:
          stealth_async = None
          _STEALTH_AVAILABLE = False
      ```
      Do this at method scope (not module scope) so the import cost is
      only paid when a scrape actually starts, and so tests can monkeypatch
      it easily.
    - After the existing `await ctx.add_init_script(...)` line
      (which sets `navigator.webdriver` to undefined), add:
      ```
      if _STEALTH_AVAILABLE and stealth_async is not None:
          await stealth_async(ctx)
          self._dlog("stealth_applied", vendor="playwright-stealth")
          self.log.info("playwright-stealth applied to browser context")
      else:
          self.log.warning(
              "playwright-stealth unavailable — proceeding without "
              "fingerprint patches (install via `pip install "
              "playwright-stealth>=1.0.6`)"
          )
          self._dlog("stealth_unavailable")
      ```
    - Do NOT remove the existing `--disable-blink-features=
      AutomationControlled` launch arg or the webdriver init script.
      Stealth is additive, not a replacement — the two layers combined
      are more robust than either alone, and rolling one back is easy if
      needed.
    - Do NOT apply stealth in any other scraper module. Per D-05 and
      CLAUDE.md's TN pipeline scope fence, `src/scraper.py`,
      `src/h3/scrapers/*` other than `mcohio.py`, and the probate /
      sheriff paths must remain untouched.

    Create `tests/test_mcohio_stealth.py`:
    - Test 1: mock `playwright.async_api.async_playwright` end-to-end
      (patch `p.chromium.launch` and the returned browser's `new_context`)
      such that `_launch_browser` can be invoked in isolation. Patch
      `playwright_stealth.stealth_async` with an `AsyncMock`. Assert
      `AsyncMock` was awaited exactly once with the mocked context object.
      Assert call order: `add_init_script` awaited before `stealth_async`.
    - Test 2: set `sys.modules['playwright_stealth'] = None` (or use
      `unittest.mock.patch.dict(sys.modules, {'playwright_stealth': None})`)
      to force the import to fail. Run `_launch_browser` again; assert
      it completes without raising, the warning log was emitted (spy on
      `scraper.log.warning`), and the debug log has a `"stealth_unavailable"`
      entry.

    Do NOT add pytest-playwright or attempt a real Chromium launch in
    unit tests. All Playwright objects are mocked.
  </action>
  <verify>
    <automated>PYTHONPATH=src pytest tests/test_mcohio_stealth.py tests/test_recaptcha_v3_solver.py tests/test_mcohio_block_detection.py -x -v</automated>
  </verify>
  <done>
    All Phase D test files green together. `grep -n "stealth_async"
    src/h3/scrapers/mcohio.py` shows the import + call site (2 hits).
    `grep -n "playwright-stealth" requirements.txt` shows exactly one hit.
    `git diff src/scraper.py src/captcha_solver.py src/ohio_probate_scrapers.py
    src/ohio_sheriff_sale_scrapers.py` all empty — D-05 invariant intact.
    Local install: `pip install -r requirements.txt` succeeds without
    conflicts.
  </done>
</task>

<task type="checkpoint:human-verify" gate="blocking">
  <name>Task 3: Re-run 5-consecutive-run observation window with stealth applied</name>
  <what-built>
    Full D.1 + D.2 + D.3 stack: block detection + v3 token solving +
    playwright-stealth fingerprint reduction.
  </what-built>
  <how-to-verify>
    1. Install the new dependency locally:
       ```bash
       cd /Users/ryanhawker/Desktop/SiftStack
       pip install -r requirements.txt
       ```
       (In production, `.venv/bin/pip install -r requirements.txt` per the
       systemd/launchd env.)

    2. Sanity replay:
       ```bash
       PYTHONPATH=src python -c "
       import asyncio
       from ohio_foreclosure_scrapers import fetch_montgomery_foreclosure
       async def main():
           records = await fetch_montgomery_foreclosure(
               date_from='2026-06-29', date_to='2026-06-30', max_cases=50
           )
           print(f'{len(records)} unique cases returned')
       asyncio.run(main())
       "
       ```
       Expected: 9 unique cases (± 1). Log shows both
       `"Solving reCAPTCHA v3 for https://pro.mcohio.org"` AND
       `"playwright-stealth applied to browser context"`.

    3. Observation window: let the daily 6 AM cron run for 5 consecutive
       weekdays. Tail `logs/ohio_daily.log` each morning and record:
       - Row count parsed
       - Case count grouped
       - Presence of "reCAPTCHA blocked" ERROR line (must be zero across
         all 5 runs)

    4. If ANY of the 5 runs raises `RecaptchaBlockedError`, D.3 was
       insufficient — proceed to residential-proxy path (deferred idea in
       CONTEXT.md; operator decision).

    5. If all 5 runs clean, close Phase D as complete.
  </how-to-verify>
  <resume-signal>
    Reply with one of:
      - `"D.3 verified: 5/5 clean runs"` — Phase D complete, close.
      - `"D.3 partial: N/5 clean, M/5 blocked"` — insufficient; escalate
        to residential-proxy operator decision.
      - `"D.3 regressed something"` — halt, roll back Task 2 changes,
        diagnose.
  </resume-signal>
</task>

</tasks>

<verification>
Test gate:
```bash
PYTHONPATH=src pytest tests/test_mcohio_stealth.py tests/test_recaptcha_v3_solver.py tests/test_mcohio_block_detection.py -x -v
```

Scope-fence gate (all must return empty diffs):
```bash
git diff src/scraper.py src/captcha_solver.py \
         src/ohio_probate_scrapers.py \
         src/ohio_sheriff_sale_scrapers.py \
         src/ohio_orchestrator.py
```

Observation gate: Task 3 checkpoint result.
</verification>

<success_criteria>
- ROADMAP Phase D criterion #2 (5 consecutive clean cron runs) — met after
  D.3 layers on top of D.2, if D.2 alone was insufficient
- D-04 + D-05 scope fences intact (no changes outside Montgomery FC + the
  new solver module + requirements.txt)
- Graceful degradation: production keeps working if
  `playwright-stealth` install fails for any reason (WARNING log, no
  exception)
- No regression to TN pipeline
</success_criteria>

<output>
Create `.planning/phases/D-pro-mcohio-org-recaptcha-v3-mitigation/D-03-SUMMARY.md`
when done, listing: `playwright-stealth` version installed, the 5-day
observation window results table (date / rows / cases / blocked-y-n), and
final Phase D disposition (close vs escalate to residential proxy).
</output>
