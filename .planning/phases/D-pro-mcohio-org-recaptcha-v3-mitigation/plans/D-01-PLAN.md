---
phase: D-pro-mcohio-org-recaptcha-v3-mitigation
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/h3/scrapers/mcohio.py
  - tests/test_mcohio_block_detection.py
autonomous: true
requirements:
  - BUG-04
must_haves:
  truths:
    - "Scraper raises RecaptchaBlockedError when pro.mcohio.org returns the reCAPTCHA v3 block page"
    - "Scraper logs 'reCAPTCHA blocked' at ERROR level with the offending page URL and captured HTML byte count"
    - "Existing successful runs (43 rows / 9 cases replay) continue to succeed — no false positives on real results tables"
    - "Probate scraper (go.mcohio.org) is untouched"
    - "Sheriff sale scraper (realforeclose.com) is untouched"
  artifacts:
    - path: "src/h3/scrapers/mcohio.py"
      provides: "Block-page detection in _capture_results plus RecaptchaBlockedError exception class"
      contains: "class RecaptchaBlockedError"
    - path: "tests/test_mcohio_block_detection.py"
      provides: "Unit tests covering block-page detection and false-positive avoidance"
      exports: ["test_block_page_raises", "test_healthy_page_does_not_raise", "test_empty_results_does_not_raise"]
  key_links:
    - from: "src/h3/scrapers/mcohio.py::_capture_results"
      to: "RecaptchaBlockedError"
      via: "raise after detection sentinel matches"
      pattern: "raise RecaptchaBlockedError"
    - from: "tests/test_mcohio_block_detection.py"
      to: "parse_results_table + block detection helper"
      via: "fixture HTML strings representing block page and healthy page"
      pattern: "RecaptchaBlockedError"
---

<objective>
Ship a silent-failure tripwire so pro.mcohio.org's reCAPTCHA v3 block page can
NEVER again masquerade as "the courthouse had no filings today". After this
plan lands, any block-page response raises `RecaptchaBlockedError` and the
orchestrator logs a loud error instead of writing a 0-row FC bucket into the
Montgomery CSV.

Purpose: Independent of the primary reCAPTCHA v3 fix (Plan D.2), this
guardrail must ship first. Even if D.2 succeeds forever, D.1's value is that
the NEXT anti-bot vendor change will page the operator on day 1, not day N.

Output:
  - New exception class `RecaptchaBlockedError` in
    `src/h3/scrapers/mcohio.py`
  - Detection sentinel in `_capture_results()` that inspects
    `self.recon.results_html` for the block page's marker text before
    returning
  - Unit tests locking both directions (block page raises; healthy page +
    quiet-day 0-results page do NOT raise)
</objective>

<execution_context>
Standard SiftStack execute-plan flow. No orchestrator changes; this plan
lives entirely inside the Montgomery FC scrape module and its test.
</execution_context>

<context>
@.planning/ROADMAP.md
@.planning/REQUIREMENTS.md
@.planning/phases/D-pro-mcohio-org-recaptcha-v3-mitigation/CONTEXT.md
@src/h3/scrapers/mcohio.py
@src/ohio_foreclosure_scrapers.py
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Add RecaptchaBlockedError + block-page detection sentinel</name>
  <files>src/h3/scrapers/mcohio.py, tests/test_mcohio_block_detection.py</files>
  <behavior>
    - Test 1 (block page raises): given `results_html` containing the marker
      substring "reCAPTCHA (a system for detecting whether you are a real
      user or a bot) has flagged you" (case-insensitive), `_capture_results`
      raises `RecaptchaBlockedError` with a message containing the portal URL
      AND the html byte count. The exception carries a `.reason` attribute
      set to the string `"score_too_low"`.
    - Test 2 (healthy page does NOT raise): given a fixture `results_html`
      that has a valid `#tblSearchResults` tbody with 3+ `<tr>` rows carrying
      the `openTab('caseInfo', ...)` onclick pattern, `_capture_results`
      returns normally and `self.recon.parsed_rows` is populated with the
      3 rows.
    - Test 3 (quiet weekday, no filings, does NOT raise): given
      `results_html` with `#tblSearchResults` present but empty (`<tbody
      id='tblSearchResults'></tbody>`), `_capture_results` returns normally,
      `parsed_rows == []`, and NO exception fires. This is the false-positive
      protection — a genuinely quiet Sunday must not trigger the tripwire.
    - Test 4 (defensive): if the marker text appears in the middle of an
      otherwise healthy results table (impossible in practice, but proves the
      detector precedence), the block-page check wins. Enforces D-02
      "loud > quiet" invariant.
  </behavior>
  <action>
    In `src/h3/scrapers/mcohio.py`:
    - Add a top-level exception class `RecaptchaBlockedError(RuntimeError)`
      with `__init__(self, reason: str, url: str, html_bytes: int, *,
      snippet: str = "")` that stores `self.reason`, `self.url`,
      `self.html_bytes`, `self.snippet` and formats a message of the form
      `f"reCAPTCHA blocked ({reason}) at {url} — html={html_bytes} bytes"`.
      Place immediately below the module docstring / imports so it can be
      imported by callers.
    - Add a module-level constant `RECAPTCHA_BLOCK_MARKERS: tuple[str, ...]`
      containing the two canonical sentinels observed on the 2026-07-01
      block page: `"reCAPTCHA (a system for detecting whether you are a
      real user or a bot) has flagged you"` and
      `"try the search again in 20 minutes"`. Both must be matched
      case-insensitively; detection fires when ANY marker is present.
    - Add a pure helper `_detect_recaptcha_block(html: str) -> str | None`
      that returns `"score_too_low"` if any marker matches, else `None`.
      Case-insensitive substring match; do NOT compile heavy regex — plain
      `.lower()` + `in` check.
    - Inside `MontgomeryScraper._capture_results`, IMMEDIATELY after the
      line `self.recon.results_html = await page.content()` and BEFORE the
      screenshot capture, call `_detect_recaptcha_block` on the captured
      HTML. If it returns non-None, take the screenshot (so the operator has
      forensic evidence), append a `self._dlog("recaptcha_blocked", ...)`
      entry with `url=page.url`, `reason=<returned string>`, and
      `html_bytes=len(self.recon.results_html)`, log via
      `self.log.error("reCAPTCHA blocked at {url} — reason={reason} — "
      "html={n} bytes — see debug_log for full trail")`, and then
      `raise RecaptchaBlockedError(reason=..., url=page.url,
      html_bytes=len(self.recon.results_html), snippet=self.recon.results_html[:500])`.
      The raise MUST happen before `parse_results_table` is called — we do
      NOT want a bogus empty-rows path.
    - Do NOT change `parse_results_table` behavior. Do NOT change
      `group_rows_into_cases`. Do NOT change the run() control flow apart
      from letting the exception propagate. Per D-04, do NOT touch
      `src/captcha_solver.py`.
    - Per user brief, the block-page HTML sample lives at
      `/tmp/mont_fc_results.html`. Extract the exact marker phrasing from
      that file when authoring the fixture; if it has drifted from the
      quoted phrase, prefer the file's phrasing but keep both sentinels in
      `RECAPTCHA_BLOCK_MARKERS` so we cover the "try the search again in 20
      minutes" secondary marker too.

    Create `tests/test_mcohio_block_detection.py`:
    - Import `pytest`, `RecaptchaBlockedError`, `_detect_recaptcha_block`,
      and `parse_results_table` from `h3.scrapers.mcohio`.
    - Fixtures:
      - `BLOCK_HTML` = minimal HTML string containing the marker phrase (no
        `#tblSearchResults`).
      - `HEALTHY_HTML` = fixture derived from a real 2026-06-30 result — a
        `<tbody id='tblSearchResults'>` with 3 `<tr onclick="openTab(
        'caseInfo', 'case_id=62390491&amp;screen=summary',1,'2026 CV
        03347');">` rows, each with the 6 `<td>` cells matching the columns
        `parse_results_table` reads (case number, action type, party name,
        _, status, role). Include one DEFENDANT and one PLAINTIFF row for
        realism.
      - `QUIET_HTML` = `<tbody id='tblSearchResults'></tbody>` inside a
        minimal `<html>` shell.
      - `DEFENSIVE_HTML` = HEALTHY_HTML with the block marker phrase
        injected as an HTML comment above the tbody.
    - Test the pure helper directly:
      - `_detect_recaptcha_block(BLOCK_HTML) == "score_too_low"`
      - `_detect_recaptcha_block(HEALTHY_HTML) is None`
      - `_detect_recaptcha_block(QUIET_HTML) is None`
    - Test integration via a mock: build a `MontgomeryScraper` (with
      dummy dates), monkeypatch `page.content` and `page.screenshot` and
      `page.url` on a fake Page object, and:
      - block case → `pytest.raises(RecaptchaBlockedError) as exc`, assert
        `exc.value.reason == "score_too_low"`, assert `"pro.mcohio.org" in
        str(exc.value)` (URL fragment).
      - healthy case → no exception, `scraper.recon.parsed_rows` has 3
        entries, `scraper.recon.parsed_cases` groups them correctly.
      - quiet case → no exception, `scraper.recon.parsed_rows == []`,
        `scraper.recon.parsed_cases == []`.
    - Do NOT hit the network. All Page interactions are mocked via a
      simple stub class defining `async content`, `async screenshot`, and
      `url` attribute.
  </action>
  <verify>
    <automated>PYTHONPATH=src pytest tests/test_mcohio_block_detection.py -x -v</automated>
  </verify>
  <done>
    All 4 tests pass. `grep -n "class RecaptchaBlockedError" src/h3/scrapers/mcohio.py`
    returns a hit. `grep -n "_detect_recaptcha_block" src/h3/scrapers/mcohio.py`
    returns at least one definition + one call site inside
    `_capture_results`. `python -c "from h3.scrapers.mcohio import
    RecaptchaBlockedError, _detect_recaptcha_block; print('ok')"` prints
    `ok` (with `PYTHONPATH=src`).
  </done>
</task>

<task type="auto">
  <name>Task 2: Replay-verify the 2026-06-29 → 2026-06-30 block-page scenario end-to-end</name>
  <files>scripts/verify_recaptcha_block_detection.py</files>
  <action>
    Create a small replay script at
    `scripts/verify_recaptcha_block_detection.py` that:
    - Reads `/tmp/mont_fc_results.html` (the captured 2026-07-01 block page).
      If the file does not exist, fail with an actionable error (`"Run the
      2026-07-01 diagnostic capture first — see CONTEXT.md"`); do NOT skip.
    - Constructs a `MontgomeryScraper(date_from='2026-06-29',
      date_to='2026-06-30')` instance without invoking `.run()`.
    - Manually seeds `scraper.recon.results_html` with the file contents and
      calls the block detector helper `_detect_recaptcha_block` directly.
    - Prints one of:
      - `"BLOCKED: reason=score_too_low, html_bytes=N"` and exits 0 if the
        block is detected — this is the WIN state for the guardrail.
      - `"NOT BLOCKED — detector did not fire on the known-block sample. "
        "Either the file is stale, the markers have drifted, or the
        detector is broken."` and exits 2 if detection fails.
    - Also parses the file through `parse_results_table` and prints the row
      count to confirm the silent-failure mechanism (should be 0 rows even
      though the block was detected).
    - Docstring explains: this is a diagnostic replay used during Phase D
      landing and after any future portal HTML change. It is NOT a pytest
      test because the fixture file lives in `/tmp` and is
      operator-produced, not committed.

    Do NOT commit `/tmp/mont_fc_results.html` to the repo. The script depends
    on the operator having captured it locally.
  </action>
  <verify>
    <automated>test -f /tmp/mont_fc_results.html && PYTHONPATH=src python scripts/verify_recaptcha_block_detection.py || echo "SKIP: /tmp/mont_fc_results.html not present"</automated>
  </verify>
  <done>
    When run against `/tmp/mont_fc_results.html`, script prints
    `"BLOCKED: reason=score_too_low, ..."` and exits 0. When the file is
    absent, script prints the actionable error and exits non-zero (verified
    by manually renaming the file and re-running).
  </done>
</task>

</tasks>

<verification>
Full guardrail smoke:

1. `PYTHONPATH=src pytest tests/test_mcohio_block_detection.py -x -v` — all
   4 tests green.
2. `PYTHONPATH=src pytest tests/ -x -q` — no regressions in the broader
   suite (this plan should touch zero non-Montgomery-FC tests).
3. `PYTHONPATH=src python scripts/verify_recaptcha_block_detection.py`
   against `/tmp/mont_fc_results.html` prints `BLOCKED: ...`.
4. `grep -rn "RecaptchaBlockedError" src/ tests/` shows: the class
   definition, the raise inside `_capture_results`, and the test imports —
   nothing else. Guardrail scope is contained to Montgomery FC.

Post-ship monitoring:
- If ANY future daily cron run raises `RecaptchaBlockedError`, the
  orchestrator's exit-code alarm (ACC-03 hook, when it lands) will page the
  on-call. Until ACC-03 ships, the ERROR-level log line in
  `logs/ohio_daily.err` is the tripwire — Ryan tails it as part of the
  morning routine.
</verification>

<success_criteria>
- Silent-failure detection ships **before** the primary D.2 mitigation.
- 5th ROADMAP Phase D success criterion (`scraper raises or logs
  "reCAPTCHA blocked" if it ever encounters the block page again`) is
  fully satisfied by this plan alone.
- Zero regressions to `parse_results_table` (locked by Test 2 + Test 3).
- Zero changes to probate, sheriff sale, or the TN v2 solver.
</success_criteria>

<output>
Create `.planning/phases/D-pro-mcohio-org-recaptcha-v3-mitigation/D-01-SUMMARY.md`
when done, listing: the exception class location, the two markers that
matched, and the pytest command that ran green.
</output>
