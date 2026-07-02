---
phase: D-pro-mcohio-org-recaptcha-v3-mitigation
plan: 02
type: execute
wave: 2
depends_on:
  - D-01
files_modified:
  - src/config.py
  - src/recaptcha_v3_solver.py
  - src/h3/scrapers/mcohio.py
  - tests/test_recaptcha_v3_solver.py
autonomous: false
requirements:
  - BUG-04
user_setup:
  - service: 2captcha
    why: "Solve reCAPTCHA v3 tokens for pro.mcohio.org Search submission (v3, not v2)"
    env_vars:
      - name: CAPTCHA_API_KEY
        source: "Already set in .env for TN pipeline — reused as-is"
      - name: PRO_MCOHIO_RECAPTCHA_V3_SITEKEY
        source: "Inspect pro.mcohio.org HTML — look for `data-sitekey` on a v3 script tag or the `grecaptcha.execute(SITEKEY, {action: ...})` call in a bundled JS file. Capture from /tmp/mont_fc_results.html or a fresh browser DevTools session."
      - name: PRO_MCOHIO_RECAPTCHA_V3_ACTION
        source: "Same source — the `action` string passed to `grecaptcha.execute(...)`. Common values: `submit`, `search`, `homepage`. Capture verbatim."
    dashboard_config:
      - task: "Confirm 2Captcha account has v3 solving enabled and non-zero balance"
        location: "https://2captcha.com — Dashboard → Statistics tab. Confirm the API key type supports `userrecaptcha` with `version=v3`."
must_haves:
  truths:
    - "Scraper solves reCAPTCHA v3 for pro.mcohio.org and receives a valid `g-recaptcha-response` token from 2Captcha"
    - "Token is injected into the page before the Search click, and the portal responds with a real `#tblSearchResults` table instead of the block page"
    - "Replay of `2026-06-29 → 2026-06-30` returns ~43 rows / 9 unique cases (matching pre-block baseline)"
    - "If 2Captcha returns no token OR the returned token is rejected (block page still fires), the scraper raises `RecaptchaBlockedError` from D.1 — no silent failure"
    - "TN pipeline (`captcha_solver.py` v2 flow) is unchanged — `pytest tests/` covering TN paths still green"
    - "Probate + sheriff sale paths still untouched"
  artifacts:
    - path: "src/recaptcha_v3_solver.py"
      provides: "New v3-specific solver (separate from v2 solver per D-04)"
      exports: ["solve_recaptcha_v3", "RecaptchaV3SolveError"]
    - path: "src/config.py"
      provides: "New env-driven config: PRO_MCOHIO_RECAPTCHA_V3_SITEKEY, PRO_MCOHIO_RECAPTCHA_V3_ACTION, PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE"
      contains: "PRO_MCOHIO_RECAPTCHA_V3_SITEKEY"
    - path: "src/h3/scrapers/mcohio.py"
      provides: "Token injection between _fill_search_form and _submit_search"
      contains: "solve_recaptcha_v3"
    - path: "tests/test_recaptcha_v3_solver.py"
      provides: "Unit tests for the new solver — mocked 2Captcha client, success + failure paths"
      exports: ["test_solve_returns_token", "test_solve_raises_on_empty_token", "test_scraper_injects_token_before_submit"]
  key_links:
    - from: "src/h3/scrapers/mcohio.py::_solve_and_inject_recaptcha_v3"
      to: "src/recaptcha_v3_solver.py::solve_recaptcha_v3"
      via: "await solve_recaptcha_v3(...)"
      pattern: "solve_recaptcha_v3\\("
    - from: "src/h3/scrapers/mcohio.py::run"
      to: "_solve_and_inject_recaptcha_v3"
      via: "call inserted after _fill_search_form, before _submit_search"
      pattern: "_solve_and_inject_recaptcha_v3"
    - from: "src/config.py"
      to: "src/h3/scrapers/mcohio.py"
      via: "import of PRO_MCOHIO_RECAPTCHA_V3_SITEKEY / _ACTION / _MIN_SCORE"
      pattern: "PRO_MCOHIO_RECAPTCHA_V3_SITEKEY"
---

<objective>
Ship the primary mitigation for BUG-04: solve pro.mcohio.org's reCAPTCHA v3
challenge server-side via 2Captcha's `userrecaptcha` v3 endpoint, inject the
returned token into the page before the Search click, and confirm the portal
returns real results.

Purpose: Restore Montgomery foreclosure daily scrape to healthy state as
measured by ROADMAP Phase D success criteria #1 and #2 (43-row replay match
+ 5 consecutive clean cron runs). Uses existing CAPTCHA_API_KEY infrastructure
so no new billing plumbing is needed. Coexists with the v2 solver
(`captcha_solver.py`) per D-04.

Output:
  - `src/recaptcha_v3_solver.py` — new module with `solve_recaptcha_v3()` and
    `RecaptchaV3SolveError` exception
  - `src/config.py` — three new env-backed constants
  - `src/h3/scrapers/mcohio.py` — new `_solve_and_inject_recaptcha_v3(page)`
    method wired into `run()` between form fill and Search click
  - `tests/test_recaptcha_v3_solver.py` — unit tests with mocked
    `TwoCaptcha` client
  - Manual verification checkpoint against the live portal
</objective>

<execution_context>
Standard execute-plan flow with ONE blocking human-verify checkpoint at the
end to observe the first live 43-row replay. Config values
`PRO_MCOHIO_RECAPTCHA_V3_SITEKEY` and `PRO_MCOHIO_RECAPTCHA_V3_ACTION` must be
captured from the live portal before the scraper task can complete; a
`checkpoint:human-action` task blocks on that capture because it requires
browser DevTools inspection Claude cannot automate reliably.
</execution_context>

<context>
@.planning/ROADMAP.md
@.planning/REQUIREMENTS.md
@.planning/phases/D-pro-mcohio-org-recaptcha-v3-mitigation/CONTEXT.md
@.planning/phases/D-pro-mcohio-org-recaptcha-v3-mitigation/plans/D-01-PLAN.md
@src/h3/scrapers/mcohio.py
@src/ohio_foreclosure_scrapers.py
@src/captcha_solver.py
@src/config.py
</context>

<tasks>

<task type="checkpoint:human-action" gate="blocking">
  <name>Task 1: Capture reCAPTCHA v3 sitekey + action string from live portal</name>
  <what-built>N/A — this is an information-capture step that must precede implementation.</what-built>
  <how-to-verify>
    The reCAPTCHA v3 sitekey and action string are embedded in
    pro.mcohio.org's JavaScript. Claude cannot reliably extract them because
    the block page hides them and DevTools navigation depends on the
    challenge-page bundle. Capture procedure (operator, ~5 min):

    1. Open a fresh Chrome incognito window (residential IP — do this from
       home, not a datacenter).
    2. Visit https://pro.mcohio.org. Click "I Agree" on the disclaimer.
    3. Open DevTools (Cmd+Opt+I) → Network tab → filter `recaptcha`.
    4. Fill in a MORTGAGE FORECLOSURE search for `2026-06-29 → 2026-06-30`
       and click Search.
    5. In the Network tab, find the request to
       `https://www.google.com/recaptcha/api2/reload` or
       `.../anchor` — the `k=` query parameter is the SITEKEY (starts
       with `6L`).
    6. In the Sources tab, search project files for
       `grecaptcha.execute(` — the second argument is `{action: "STRING"}`.
       Capture that STRING verbatim.
    7. In the Elements tab, search for `data-sitekey` — cross-check that
       the same sitekey appears there too.

    Record the two values in a private note (do NOT commit). They will be
    set in the deployment `.env` as:
      - `PRO_MCOHIO_RECAPTCHA_V3_SITEKEY=6L...`
      - `PRO_MCOHIO_RECAPTCHA_V3_ACTION=<captured string>`

    Also confirm the portal is actually running v3 (not v2 with an
    invisible widget) — v3 shows a "protected by reCAPTCHA" badge in the
    bottom-right corner but NO checkbox. If a checkbox appears, STOP and
    escalate — this plan targets v3 specifically.
  </how-to-verify>
  <resume-signal>
    Reply with the sitekey and action string (or "captured, set in .env")
    so implementation can proceed. If the portal turns out to be v2 with
    an invisible widget, reply "v2 detected" and we will adapt Plan D.2 to
    reuse `captcha_solver.py` with a `min_score`-style tweak instead.
  </resume-signal>
</task>

<task type="auto" tdd="true">
  <name>Task 2: New v3 solver module + config + unit tests</name>
  <files>src/recaptcha_v3_solver.py, src/config.py, tests/test_recaptcha_v3_solver.py</files>
  <behavior>
    - Test 1 (success): `solve_recaptcha_v3(url, sitekey, action,
      min_score)` returns the token string when the mocked
      `TwoCaptcha().recaptcha(...)` returns `{"code": "TOKEN_XYZ"}`. The
      mock is asserted to have been called with kwargs
      `sitekey=sitekey, url=url, version="v3", action=action,
      score=min_score, enterprise=0`.
    - Test 2 (empty token → raise): when the mocked client returns
      `{"code": ""}`, `solve_recaptcha_v3` raises `RecaptchaV3SolveError`
      with the message `"2Captcha returned empty token for
      pro.mcohio.org"`.
    - Test 3 (no API key → raise): when `config.CAPTCHA_API_KEY` is empty
      string, `solve_recaptcha_v3` raises `RecaptchaV3SolveError`
      immediately (before touching the network) with message containing
      `"CAPTCHA_API_KEY not set"`.
    - Test 4 (retry on transient exception): when the mocked client raises
      a generic `Exception("network hiccup")` on the first call and
      returns a valid token on the second, `solve_recaptcha_v3` retries
      once (total 2 attempts) and returns the token. Third attempt is not
      made.
    - Test 5 (exhausted retries): when all 3 attempts raise, the final
      raise is `RecaptchaV3SolveError` wrapping the last inner exception's
      message.
  </behavior>
  <action>
    Create `src/recaptcha_v3_solver.py`:
    - Public async function
      `async def solve_recaptcha_v3(url: str, sitekey: str, action: str,
      min_score: float = 0.3, max_retries: int = 3, logger: Any = None)
      -> str`.
    - Import `config` and `TwoCaptcha` from `twocaptcha` (already listed in
      `requirements.txt` as `2captcha-python>=1.2.0`).
    - If `config.CAPTCHA_API_KEY` is falsy, raise
      `RecaptchaV3SolveError("CAPTCHA_API_KEY not set — cannot solve
      reCAPTCHA v3 for pro.mcohio.org")` immediately.
    - Instantiate `TwoCaptcha(config.CAPTCHA_API_KEY)`. The 2Captcha
      Python SDK v3 signature is
      `solver.recaptcha(sitekey=..., url=..., version="v3", action=...,
      score=..., enterprise=0)`. The call is blocking (polls internally
      for ~20-60s until the token is ready) so wrap in
      `await asyncio.get_event_loop().run_in_executor(None, ...)` to
      avoid blocking the Playwright event loop.
    - Retry loop: up to `max_retries` attempts. On empty token OR
      exception, log a WARNING and retry. On success, return the token.
      If all attempts fail, raise `RecaptchaV3SolveError` with a message
      summarizing the last failure.
    - Define `class RecaptchaV3SolveError(RuntimeError)` at module top.
    - Logger fallback: same pattern as `MontgomeryScraper._StdoutLog` —
      accept an optional logger, default to `logging.getLogger(__name__)`.

    Modify `src/config.py`:
    - Add three constants after the existing `CAPTCHA_API_KEY` block:
      - `PRO_MCOHIO_RECAPTCHA_V3_SITEKEY = os.getenv(
        "PRO_MCOHIO_RECAPTCHA_V3_SITEKEY", "")`
      - `PRO_MCOHIO_RECAPTCHA_V3_ACTION = os.getenv(
        "PRO_MCOHIO_RECAPTCHA_V3_ACTION", "submit")` (default `"submit"` per
        common v3 convention — real value comes from Task 1 capture and
        overrides in `.env`)
      - `PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE = float(os.getenv(
        "PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE", "0.3"))` (2Captcha will
        target this or higher; 0.3 is the 2Captcha default for v3)
    - Do NOT modify `RECAPTCHA_SITEKEY` (that is the TN v2 sitekey per
      D-04).

    Create `tests/test_recaptcha_v3_solver.py`:
    - Import `pytest`, `pytest.MonkeyPatch`, `asyncio`,
      `recaptcha_v3_solver` module.
    - Use `unittest.mock.MagicMock` (or `pytest-mock` if already installed
      — check `requirements.txt`; if not, use `unittest.mock` from stdlib)
      to patch the `TwoCaptcha` class such that
      `TwoCaptcha(...).recaptcha` returns configurable values.
    - For the CAPTCHA_API_KEY test, monkeypatch `config.CAPTCHA_API_KEY`
      to `""`.
    - All tests are `async def` marked with `pytest.mark.asyncio`; if
      `pytest-asyncio` is not in `requirements.txt`, add it as a
      dev-only dep note (do NOT add to production `requirements.txt` for
      this plan — instead, use `asyncio.run(...)` inside sync test
      functions to keep the test suite frictionless).
    - Assert `solver.recaptcha` is called with the correct kwargs per
      Test 1.
    - For retry tests, use a `side_effect` list to sequence exception →
      exception → success.
  </action>
  <verify>
    <automated>PYTHONPATH=src pytest tests/test_recaptcha_v3_solver.py -x -v</automated>
  </verify>
  <done>
    All 5 tests pass. `python -c "from recaptcha_v3_solver import
    solve_recaptcha_v3, RecaptchaV3SolveError; print('ok')"` prints
    `ok` (with `PYTHONPATH=src`). `grep -n "PRO_MCOHIO_RECAPTCHA_V3"
    src/config.py` shows all three new constants. No changes to
    `captcha_solver.py`, `RECAPTCHA_SITEKEY`, or any TN-related config
    (verify via `git diff src/config.py` — new lines only, no deletions or
    edits to existing lines).
  </done>
</task>

<task type="auto" tdd="true">
  <name>Task 3: Wire token solving + injection into MontgomeryScraper.run()</name>
  <files>src/h3/scrapers/mcohio.py, tests/test_recaptcha_v3_solver.py</files>
  <behavior>
    - Test A (injection before submit): using a fake Page + fake solver
      (mocked to return `"TOKEN_ABC"`), a fresh `MontgomeryScraper.run()`
      invocation calls the solver ONCE with the expected args
      (`url=PORTAL_URL`, `sitekey=PRO_MCOHIO_RECAPTCHA_V3_SITEKEY`,
      `action=PRO_MCOHIO_RECAPTCHA_V3_ACTION`,
      `min_score=PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE`), then calls
      `page.evaluate(...)` with a JS snippet that sets the
      `g-recaptcha-response` textarea value to `"TOKEN_ABC"`, THEN calls
      `_submit_search`. Order asserted via mock call ordering.
    - Test B (block despite injection): if `_capture_results` observes the
      block-page markers even after token injection, `RecaptchaBlockedError`
      (from D.1) is raised — no silent 0-rows path. This proves D.1 and
      D.2 compose correctly.
    - Test C (solver failure): if `solve_recaptcha_v3` raises
      `RecaptchaV3SolveError`, `MontgomeryScraper.run()` lets the
      exception propagate; it does NOT swallow the error and does NOT
      call `_submit_search` (asserted by mock).
  </behavior>
  <action>
    In `src/h3/scrapers/mcohio.py`:
    - Add imports at the top of the file:
      - `from recaptcha_v3_solver import solve_recaptcha_v3,
        RecaptchaV3SolveError`
      - `import config`
      (Yes — `src/` is on PYTHONPATH per CLAUDE.md, so top-level imports
      of `config` and `recaptcha_v3_solver` work from anywhere in the tree.)
    - Add a new method
      `async def _solve_and_inject_recaptcha_v3(self, page: Page) -> None`
      that:
        1. Logs `self.log.info("Solving reCAPTCHA v3 for "
           f"{PORTAL_URL} (action={config.PRO_MCOHIO_RECAPTCHA_V3_ACTION},
           min_score={config.PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE})")`.
        2. If `config.PRO_MCOHIO_RECAPTCHA_V3_SITEKEY` is empty, log
           `self.log.error("PRO_MCOHIO_RECAPTCHA_V3_SITEKEY not
           configured — cannot proceed")` and raise
           `RecaptchaV3SolveError("PRO_MCOHIO_RECAPTCHA_V3_SITEKEY not
           set")`.
        3. Calls
           `token = await solve_recaptcha_v3(url=PORTAL_URL,
           sitekey=config.PRO_MCOHIO_RECAPTCHA_V3_SITEKEY,
           action=config.PRO_MCOHIO_RECAPTCHA_V3_ACTION,
           min_score=config.PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE,
           logger=self.log)`.
        4. Injects the token via `page.evaluate` using the SAME injection
           JS as `captcha_solver.py` lines 85-114 (adapted): set
           `#g-recaptcha-response` textarea value, dispatch a synthetic
           `change` event, and — critically for v3 — call any registered
           `grecaptcha.execute` callback that consumes the token. Copy
           the callback-invocation block from `captcha_solver.py`
           verbatim; it is battle-tested against the same underlying
           reCAPTCHA JS.
        5. Log `self._dlog("recaptcha_v3_injected",
           token_len=len(token))`.
        6. `await page.wait_for_timeout(500)` for the callback to settle.
    - In `run()`, insert the call between `_fill_search_form(page)` and
      `_submit_search(page)`:
        ```
        await self._fill_search_form(page)
        await self._solve_and_inject_recaptcha_v3(page)
        await self._submit_search(page)
        ```
      Order matters: form must be filled first (the token attaches to
      the pending submission), then token injected, then Search clicked.
    - Do NOT alter `_capture_results` — the D.1 detector inside it is the
      safety net if the token is rejected.
    - Do NOT remove the existing
      `--disable-blink-features=AutomationControlled` arg or the
      `webdriver` init-script — those already reduce fingerprint noise
      and were shipped before the block. Preserve as-is; D.3 (contingent)
      layers on top of them.

    Extend `tests/test_recaptcha_v3_solver.py`:
    - Add Test A / B / C using a fake `Page` stub (async `content`,
      `screenshot`, `evaluate`, `goto`, `wait_for_timeout`,
      `wait_for_load_state`, `locator` returning fake locators). Use
      `unittest.mock.patch` to swap out `solve_recaptcha_v3` inside the
      `h3.scrapers.mcohio` namespace with a `MagicMock` returning the
      desired value / raising the desired exception.
    - Assert call order via `MagicMock`'s
      `mock_calls` list: solver call must appear before `_submit_search`
      internal call (which we identify by the Search button locator's
      `.click()` mock).
    - Test B reuses the block-page HTML fixture from Plan D.1 to seed
      `page.content()` — proves the composed behavior.
  </action>
  <verify>
    <automated>PYTHONPATH=src pytest tests/test_recaptcha_v3_solver.py tests/test_mcohio_block_detection.py -x -v</automated>
  </verify>
  <done>
    All D.2 tests green AND all D.1 tests still green (co-execution).
    `grep -n "_solve_and_inject_recaptcha_v3" src/h3/scrapers/mcohio.py`
    shows one definition + one call site inside `run()`.
    `grep -n "solve_recaptcha_v3\|RecaptchaV3SolveError" src/h3/scrapers/mcohio.py`
    shows the import + solver call. `git diff src/captcha_solver.py`
    is empty (D-04 invariant).
  </done>
</task>

<task type="checkpoint:human-verify" gate="blocking">
  <name>Task 4: Live replay verification — 2026-06-29 → 2026-06-30 must return ~43 rows / 9 cases</name>
  <what-built>
    Full D.1 + D.2 stack: block detection guardrail + v3 token solving +
    injection wired into MontgomeryScraper.
  </what-built>
  <how-to-verify>
    Before running the live test, confirm the operator has set in `.env`
    (or the deployment env):
      - `CAPTCHA_API_KEY` (existing, unchanged)
      - `PRO_MCOHIO_RECAPTCHA_V3_SITEKEY` (from Task 1 capture)
      - `PRO_MCOHIO_RECAPTCHA_V3_ACTION` (from Task 1 capture)
      - `PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE` (leave unset → 0.3 default)

    Then run the replay:

    ```bash
    cd /Users/ryanhawker/Desktop/SiftStack
    PYTHONPATH=src python -c "
    import asyncio
    from ohio_foreclosure_scrapers import fetch_montgomery_foreclosure
    from datetime import date
    async def main():
        records = await fetch_montgomery_foreclosure(
            date_from='2026-06-29', date_to='2026-06-30', max_cases=50
        )
        print(f'{len(records)} unique cases returned')
        for r in records[:3]:
            print(f'  {r.case_number}: {len(r.defendants)} defendants')
    asyncio.run(main())
    "
    ```

    Expected output:
      - Prints `9 unique cases returned` (± 1 case variance is
        acceptable; the 2026-06-30 baseline was 9).
      - First 3 case_numbers match the 2026-06-30 06:03 AM cron log (grep
        `logs/ohio_daily.log` for the 2026-06-30 run to cross-check).
      - No `RecaptchaBlockedError` raised.
      - Log line `"Solving reCAPTCHA v3 for https://pro.mcohio.org
        (action=..., min_score=0.3)"` appears.
      - Log line `"Parsed 43 rows → 9 unique cases"` (± small variance)
        appears.

    If the replay returns 0 cases with a `RecaptchaBlockedError`, D.2
    alone was insufficient — proceed to executing Plan D.3
    (playwright-stealth layer). If it returns 0 cases WITHOUT the
    exception, D.1 has regressed and must be re-inspected before shipping.
    If it returns ~43 rows / 9 cases cleanly, D.2 succeeds and D.3
    remains contingent.

    After the one-shot replay, also run a full daily-cron dry to catch
    interaction bugs:
    ```bash
    python src/ohio_orchestrator.py daily --no-upload
    ```
    Confirm `output/OH_Montgomery_daily_*.csv` has non-zero FC records
    and no `RecaptchaBlockedError` in `logs/ohio_daily.err`.
  </how-to-verify>
  <resume-signal>
    Reply with one of:
      - `"D.2 verified: 9 cases returned, no block"` — D.2 succeeds, do NOT
        execute D.3 yet; observe 5 consecutive daily cron runs (success
        criterion #2) then close the phase.
      - `"D.2 partial: block still fires"` — D.2 alone insufficient,
        execute Plan D.3 next.
      - `"D.2 regressed D.1"` — the block detector isn't firing when it
        should; halt and diagnose.
  </resume-signal>
</task>

</tasks>

<verification>
Pytest gate:
```bash
PYTHONPATH=src pytest tests/test_recaptcha_v3_solver.py tests/test_mcohio_block_detection.py -x -v
```

TN regression gate:
```bash
PYTHONPATH=src pytest tests/ -x -q -k "not recaptcha_v3 and not mcohio_block"
```
Must not introduce failures in TN-pipeline tests.

Live replay gate: Task 4 checkpoint output.

5-consecutive-run observation gate: after ship, tail
`logs/ohio_daily.log` for 5 consecutive weekdays. Each run must:
- Log "Solving reCAPTCHA v3 for https://pro.mcohio.org ..."
- Log "Parsed N rows → M unique cases" with N > 0 somewhere in the sliding
  3-day window (Sundays / holidays legitimately produce 0)
- Not log "reCAPTCHA blocked at ..." (RecaptchaBlockedError)

If ANY of the 5 runs fails success criterion #2, execute Plan D.3.
</verification>

<success_criteria>
- ROADMAP Phase D criteria #1 (replay returns ~43 rows / 9 cases) ✅
- ROADMAP Phase D criteria #2 (5 consecutive clean cron runs) — measured
  post-ship
- ROADMAP Phase D criteria #3 (FC recall 100%, phone accuracy 100%) — not
  regressed, verified by absence of TN-pipeline test failures
- ROADMAP Phase D criteria #4 (probate + sheriff sale unchanged) — verified
  by `git diff` scope (no touches outside Montgomery FC + new solver
  module)
- ROADMAP Phase D criteria #5 (silent-failure detection) — already
  satisfied by D.1; D.2 preserves it (composed test B)
- D-04 (do not modify captcha_solver.py) — verified by empty
  `git diff src/captcha_solver.py`
- D-05 (do not modify probate / sheriff / orchestrator top-level flow) —
  verified by `git diff` scope
</success_criteria>

<output>
Create `.planning/phases/D-pro-mcohio-org-recaptcha-v3-mitigation/D-02-SUMMARY.md`
when done, listing: sitekey + action captured (redact sitekey to first 8
chars), replay result (rows / cases), 2Captcha cost per solve, wall-clock
overhead added per cron run.
</output>
