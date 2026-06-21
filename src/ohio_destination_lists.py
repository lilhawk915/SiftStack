"""Ohio destination-list routing — county → DataSift list.

The OH pipeline ships into TWO completely separate DataSift lists,
never merged. The destination is determined ENTIRELY by the county
of each record; the scraper / adapter / integrator are all
county-agnostic and don't know which list they're feeding.

  Montgomery records → ``H3 Montgomery Courthouse Data`` (DAILY)
      The Montgomery list is the active calling list — the AM dials
      from it every day. Daily breakage costs 7× weekly.

  Other 6 records   → ``H3 SW Ohio Courthouse Data`` (WEEKLY)
      Butler, Clark, Clermont, Greene, Miami, Warren. Secondary
      inventory for the Egypt AM's broader reach / future expansion.

This module is intentionally tiny + has no side effects so it can be
imported anywhere in the pipeline without pulling Playwright,
httpx, etc.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from notice_parser import NoticeData

# Public list-name constants. These strings MUST match what's
# configured in DataSift exactly — case-sensitive.
LIST_MONTGOMERY_DAILY = "H3 Montgomery Courthouse Data"
LIST_SW_OHIO_WEEKLY   = "H3 SW Ohio Courthouse Data"


# The 6 weekly counties (everything except Montgomery).
WEEKLY_COUNTIES: frozenset[str] = frozenset({
    "Butler", "Clark", "Clermont", "Greene", "Miami", "Warren",
})


def destination_list_for_county(county: str) -> str:
    """Return the DataSift list name a county's records ship into.

    Comparison is case-insensitive on the input ("montgomery",
    "Montgomery", "  MONTGOMERY  " all route the same).

    Raises ``ValueError`` for any county outside the 7 SW Ohio
    counties — the caller should never present an unknown county;
    surfacing this loudly catches typos in CLI args / config drift.
    """
    if not county:
        raise ValueError(
            "destination_list_for_county: empty county. "
            "Records must carry a county to be routed."
        )
    normalized = county.strip().title()
    if normalized == "Montgomery":
        return LIST_MONTGOMERY_DAILY
    if normalized in WEEKLY_COUNTIES:
        return LIST_SW_OHIO_WEEKLY
    raise ValueError(
        f"destination_list_for_county: unknown OH county {county!r}. "
        f"Expected one of "
        f"{sorted({'Montgomery'} | WEEKLY_COUNTIES)}."
    )


T = TypeVar("T", bound=NoticeData)


def split_by_destination_list(
    notices: Iterable[T],
) -> dict[str, list[T]]:
    """Bucket NoticeData rows by their destination DataSift list.

    Returns a dict mapping list_name → list of notices for that list.
    Missing list_names are simply absent from the result (no empty
    buckets). Records with unknown counties bubble up as
    ``ValueError`` from :func:`destination_list_for_county` — we
    intentionally don't silently drop them.

    Use this just before uploading:

        buckets = split_by_destination_list(all_notices)
        for list_name, batch in buckets.items():
            # one upload call per list, with the right list_name=
            ...
    """
    out: dict[str, list[T]] = {}
    for n in notices:
        list_name = destination_list_for_county(n.county)
        out.setdefault(list_name, []).append(n)
    return out
