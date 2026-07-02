#!/usr/bin/env python3
"""BUG-04 diagnostic replay: verify the block detector fires on the
known-block sample captured on 2026-07-01.

This is a diagnostic replay used during Phase D landing and after any
future portal HTML change. NOT a pytest test because the fixture file
lives in /tmp and is operator-produced (from the diagnostic capture),
not committed to the repo.

Usage:
    PYTHONPATH=src python scripts/verify_recaptcha_block_detection.py

Prerequisite:
    /tmp/mont_fc_results.html — captured via the diagnostic script that
    dumps `MontgomeryScraper.recon.results_html` after a live block.
    See CONTEXT.md for the capture recipe.

Exit codes:
    0 — BLOCKED detected (guardrail works as designed)
    1 — Fixture missing (actionable error printed)
    2 — Detector failed to fire on the block sample (guardrail broken)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from h3.scrapers.mcohio import (
    _detect_recaptcha_block,
    parse_results_table,
)


FIXTURE_PATH = "/tmp/mont_fc_results.html"


def main() -> int:
    if not os.path.exists(FIXTURE_PATH):
        print(
            f"ERROR: fixture {FIXTURE_PATH} not found. Run the 2026-07-01 "
            f"diagnostic capture first — see .planning/phases/"
            f"D-pro-mcohio-org-recaptcha-v3-mitigation/CONTEXT.md for the "
            f"capture recipe.",
            file=sys.stderr,
        )
        return 1

    with open(FIXTURE_PATH) as f:
        html = f.read()

    reason = _detect_recaptcha_block(html)
    n_rows = len(parse_results_table(html))

    if reason is None:
        print(
            f"NOT BLOCKED — detector did not fire on the known-block sample "
            f"({FIXTURE_PATH}, {len(html)} bytes). Either the file is stale, "
            f"the markers have drifted, or the detector is broken. "
            f"parse_results_table saw {n_rows} rows.",
            file=sys.stderr,
        )
        return 2

    print(
        f"BLOCKED: reason={reason}, html_bytes={len(html)}, "
        f"parse_results_table_rows={n_rows} (should be 0 — "
        f"silent-failure mechanism confirmed)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
