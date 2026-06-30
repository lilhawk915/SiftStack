"""Sheriff sale "new-only" emission filter.

Tracks every sheriff_sale case# the cron has ever emitted to a small
JSON state file. On each daily run, the orchestrator filters out
already-seen case#s so the dial team's CSV ships only NEW auctions
instead of re-emitting the full upcoming-auction calendar every
morning.

State file format (output/.seen_sheriff_cases.json):

    {
      "2026 CV 03753": {
        "first_seen":   "2026-06-25",
        "auction_date": "2026-07-10"
      },
      ...
    }

Cold-start (file missing or corrupt) → empty state → all current
upcoming auctions ship as new. Steady state → 1-3 new cases/day.

Backfill-safe: caller is responsible for not invoking
``filter_to_new_sheriff_sale`` when running historical backfills
(filter would suppress all the cases the backfill is trying to
recover). Cron callsite gates on ``SHERIFF_NEW_ONLY != "0"``;
backfills can set ``SHERIFF_NEW_ONLY=0`` to disable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

STATE_PATH = Path("output/.seen_sheriff_cases.json")


def _normalize_case_number(case_number: str) -> str:
    """Whitespace+case canonical form for the dedup key.

    Mont/SW-Ohio courthouses occasionally vary capitalization or
    inner whitespace across days. The normalized form is what we
    store in the state file, so a case# that re-appears tomorrow
    with a different case won't bypass the filter.
    """
    return " ".join(case_number.upper().split())


def _load_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            logger.warning(
                "Sheriff state file %s is not a dict (got %s) — "
                "treating as cold start", path, type(data).__name__,
            )
            return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Sheriff state file %s unreadable (%s) — treating as "
            "cold start; current run will emit all", path, e,
        )
        return {}


def _save_state(path: Path, state: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def filter_to_new_sheriff_sale(
    notices: Iterable[NoticeData],
    *,
    today_iso: str,
    state_path: Path = STATE_PATH,
) -> list[NoticeData]:
    """Drop sheriff_sale notices whose case# has been emitted before.

    Non-sheriff_sale records pass through unchanged. The state file
    is loaded, updated with newly-seen case#s, and pruned of cases
    whose auction date is in the past. Records without a case# (rare;
    means the scraper couldn't pull one) are always kept — there's
    no key to dedup against.

    Args:
        notices: full mixed-source notice list from ``scrape_all``.
        today_iso: ISO date (YYYY-MM-DD) for first-seen stamping and
            past-auction pruning.
        state_path: defaults to ``output/.seen_sheriff_cases.json``.
            Overridable for tests.

    Returns:
        Filtered list with the same non-sheriff_sale records and only
        the previously-unseen sheriff_sale records.
    """
    notices = list(notices)
    state = _load_state(state_path)

    out: list[NoticeData] = []
    new_count = suppressed_count = 0
    for n in notices:
        if n.notice_type != "sheriff_sale":
            out.append(n)
            continue
        key = _normalize_case_number(n.case_number or "")
        if not key:
            out.append(n)
            continue
        if key in state:
            suppressed_count += 1
            continue
        out.append(n)
        new_count += 1
        state[key] = {
            "first_seen":   today_iso,
            "auction_date": n.auction_date or "",
        }

    state = {
        k: v for k, v in state.items()
        if (v.get("auction_date") or "9999-99-99") >= today_iso
    }
    _save_state(state_path, state)

    logger.info(
        "Sheriff sale: emitted %d new (suppressed %d previously-seen, "
        "state tracks %d)", new_count, suppressed_count, len(state),
    )
    return out
