"""Standardize addresses via Smarty US Address Autocomplete Pro API.

A drop-in alternative to ``address_standardizer.standardize_addresses``
when the account does NOT have a US Street API (full address validation)
subscription. Autocomplete is designed for typeahead UX but its first
suggestion IS the canonical form of a complete input address — so for
cross-source address matching (the tag-stacking use case) it gives the
same canonicalization we need.

Tradeoffs vs Street API:
  - No DPV match code (can't confirm USPS-deliverable)
  - No lat/lon
  - No vacant flag, no RDI (Residential/Commercial)
  - Slightly slower — one HTTP call per address (no batch API)

Quality is otherwise comparable for address standardization:
``2803 HAMILTON NEW LONDON RD Hamilton OH 45013``
  → ``street_line='2803 Hamilton New London Rd', city='Hamilton',
     state='OH', zipcode='45013'``

Same canonical form whether the input was upper- or mixed-case, abbreviated
or spelled-out — which is exactly what address-keyed record-merge needs.
"""
from __future__ import annotations

import logging
import time

from smartystreets_python_sdk import BasicAuthCredentials, ClientBuilder
from smartystreets_python_sdk.us_autocomplete_pro import Lookup

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


def _build_client(auth_id: str, auth_token: str):
    """Build an authenticated Smarty Autocomplete Pro client."""
    creds = BasicAuthCredentials(auth_id, auth_token)
    return ClientBuilder(creds).build_us_autocomplete_pro_api_client()


def _build_search(notice: NoticeData) -> str:
    """Compose the search string Autocomplete will canonicalize.

    Autocomplete works best with a full address (street + city + state +
    zip) — that lets it disambiguate vs returning multiple suggestions
    for partial inputs. We send everything we have.
    """
    parts = [notice.address.strip()]
    if notice.city:
        parts.append(notice.city.strip())
    if notice.state:
        parts.append(notice.state.strip())
    if notice.zip:
        parts.append(notice.zip.strip())
    return " ".join(p for p in parts if p)


def standardize_addresses_autocomplete(
    notices: list[NoticeData],
    auth_id: str,
    auth_token: str,
    expected_state: str = "OH",
) -> list[NoticeData]:
    """Standardize addresses in-place via Autocomplete API.

    Mirrors the contract of ``address_standardizer.standardize_addresses``
    but uses the Autocomplete Pro API (which only requires the
    "US Address Autocomplete Pro" subscription).

    Args:
        notices: List of NoticeData (modified in-place).
        auth_id: Smarty auth-id credential.
        auth_token: Smarty auth-token credential.
        expected_state: Drop matches outside this state (safety against
            wrong-state suggestions). Default 'OH' since this module
            ships with the Ohio production pipeline.

    Returns:
        Same list (modified in-place). On any credential/API failure,
        notices pass through unchanged.
    """
    if not auth_id or not auth_token:
        logger.info(
            "Smarty credentials not configured — "
            "skipping autocomplete standardization"
        )
        return notices

    eligible = [(i, n) for i, n in enumerate(notices) if n.address.strip()]
    if not eligible:
        logger.info("No notices with addresses to standardize")
        return notices

    logger.info(
        "Standardizing %d addresses via Smarty Autocomplete "
        "(%d skipped — no address)",
        len(eligible),
        len(notices) - len(eligible),
    )

    try:
        client = _build_client(auth_id, auth_token)
    except Exception as e:
        logger.error("Failed to build Smarty Autocomplete client: %s", e)
        return notices

    matched = 0
    no_match = 0
    wrong_state = 0
    api_errors = 0

    for orig_idx, notice in eligible:
        search = _build_search(notice)
        if not search:
            no_match += 1
            continue
        lookup = Lookup(search=search)
        # Bias toward our expected state for ambiguous matches
        if expected_state:
            lookup.add_state_filter(expected_state)
        try:
            client.send(lookup)
        except Exception as e:
            api_errors += 1
            # Log first error verbatim then sample subsequent
            if api_errors <= 3:
                logger.error(
                    "Autocomplete API error on '%s': %s", search, e,
                )
            continue

        if not lookup.result:
            no_match += 1
            continue

        # First suggestion is the canonical form
        suggestion = lookup.result[0]
        if (expected_state
                and suggestion.state
                and suggestion.state.upper() != expected_state.upper()):
            wrong_state += 1
            continue

        # Overwrite address fields with standardized values
        if suggestion.street_line:
            notice.address = suggestion.street_line
        if suggestion.city:
            notice.city = suggestion.city
        if suggestion.state:
            notice.state = suggestion.state.upper()
        if suggestion.zipcode:
            notice.zip = suggestion.zipcode
        # Autocomplete doesn't expose DPV / lat/lon / vacant — leave as-is.

        matched += 1

    logger.info(
        "Autocomplete standardization: %d matched, %d no-match, "
        "%d wrong-state, %d API errors",
        matched, no_match, wrong_state, api_errors,
    )
    return notices
