"""Shared owner-name refinements applied across all foreclosure scrapers.

Two real-world quirks the equivant CourtView portals expose:

1. **Role middle-words in party names.** Defendants are sometimes listed
   with their fiduciary role wedged into the name: `ZAVALA, Trustee, LINDA C`
   instead of `ZAVALA, LINDA C`. The DM's downstream process expects the
   clean version. `strip_role_middle()` removes the middle-word role.

2. **Deceased decedents with surviving heirs.** When the named borrower
   has died, the foreclosure docket lists multiple parties:
     - `UNKNOWN HEIRS ... OF X DECEASED` (placeholder; not a real owner)
     - `X` (the deceased; should NOT be flagged as owner)
     - `Y` (surviving spouse / heir; IS the owner the DM wants)

   `extract_decedent()` scans the defendant list for "OF <NAME> DECEASED"
   patterns and returns the decedent's name. `is_decedent_match()` checks
   whether a candidate-owner string refers to that same decedent (using
   fuzzy first-/last-name match since the placeholder name and the
   separately-listed defendant rarely match character-for-character).
"""
from __future__ import annotations

import re
from typing import Iterable


# ── Role middle-word stripping ─────────────────────────────────────────

# Middle role tokens to remove from a comma-formatted name. Each is matched
# AS A MIDDLE COMMA-SEPARATED TOKEN — e.g. "ZAVALA, Trustee, LINDA C"
# becomes "ZAVALA, LINDA C". Words at the start (LAST name position) or
# end of the name are NOT stripped.
_ROLE_WORDS = {
    "trustee", "trustees", "fiduciary", "fiduciaries", "executor",
    "executors", "executrix", "administrator", "administrators",
    "administratrix", "treasurer", "guardian", "guardians",
    "successor", "co-trustee",
}


def strip_role_middle(name: str) -> str:
    """Remove a role middle-word from a comma-separated name.

    >>> strip_role_middle("ZAVALA, Trustee, LINDA C")
    'ZAVALA, LINDA C'
    >>> strip_role_middle("GAY, Fiduciary, JENNIFER")
    'GAY, JENNIFER'
    >>> strip_role_middle("RICHARDSON, JAMES")
    'RICHARDSON, JAMES'
    """
    if not name or "," not in name:
        return name
    parts = [p.strip() for p in name.split(",")]
    if len(parts) < 3:
        return name
    # Walk inner tokens (skip first = last-name, last = first-name); drop
    # any token that's purely a role word.
    cleaned = [parts[0]]
    for p in parts[1:-1]:
        if p.lower() in _ROLE_WORDS:
            continue
        cleaned.append(p)
    cleaned.append(parts[-1])
    return ", ".join(cleaned)


# ── Decedent extraction ────────────────────────────────────────────────

# Use a greedy `.* OF` so we match the LAST "OF" before DECEASED — long
# placeholder phrases like "...GUARDIANS OF MINOR ... HEIRS OF JAMES H
# RICHARDSON DECEASED" have several "OF" tokens but only the final one
# fronts the actual decedent name.
_DECEASED_OF_RE = re.compile(
    r".*\bOF\s+([A-Z][A-Z\s\.\-']{2,40}?)[\s,]+DECEASED",
    re.IGNORECASE,
)


def extract_decedent(defendant_texts: Iterable[str]) -> str:
    """Scan defendant party texts for an `... OF <NAME> DECEASED` pattern.

    Returns the decedent's name (e.g. `JAMES H RICHARDSON`), or "" if no
    deceased-decedent placeholder is in the list. When the placeholder
    has multiple "OF" tokens (the common "UNKNOWN HEIRS ... GUARDIANS OF
    ... HEIRS OF X DECEASED" pattern), the LAST one wins.
    """
    for text in defendant_texts:
        if not text:
            continue
        m = _DECEASED_OF_RE.search(text)
        if m:
            cand = _normalize_decedent_name(m.group(1))
            # Reject if the captured name still contains placeholder
            # vocabulary — means our regex over-matched on another
            # phrase. Skip; this defendant doesn't carry a decedent.
            placeholder_words = {
                "HEIRS", "DEVISEES", "LEGATEES", "EXECUTORS",
                "ADMINISTRATORS", "SPOUSES", "ASSIGNS", "GUARDIANS",
                "MINOR", "INCOMPETENT", "UNKNOWN",
            }
            if any(w in cand.upper().split() for w in placeholder_words):
                continue
            return cand
    return ""


def _normalize_decedent_name(raw: str) -> str:
    """Trim and collapse whitespace from a captured decedent name."""
    return re.sub(r"\s+", " ", raw).strip(" .,")


def is_decedent_match(candidate_name: str, decedent: str) -> bool:
    """True if `candidate_name` (a defendant entry) refers to the decedent.

    Compares the simple-name forms of both — e.g. `RICHARDSON, JAMES`
    matches decedent `JAMES H RICHARDSON` because the tokens overlap.
    """
    if not candidate_name or not decedent:
        return False
    cand_tokens = set(_simple_tokens(candidate_name))
    dec_tokens = set(_simple_tokens(decedent))
    if not cand_tokens or not dec_tokens:
        return False
    # Require at least two shared tokens (typically last-name + first-name)
    # so we don't false-match on "JAMES" alone.
    shared = cand_tokens & dec_tokens
    return len(shared) >= 2


_TOKEN_SPLIT_RE = re.compile(r"[\s,]+")


def _simple_tokens(s: str) -> list[str]:
    """Split a name into uppercase tokens, dropping short connectors."""
    if not s:
        return []
    parts = _TOKEN_SPLIT_RE.split(s.upper())
    # Drop short tokens (initials, "OF", "AND", "JR" etc. cluttering match)
    return [
        p for p in parts
        if p and len(p) >= 3 and p not in {"THE", "AND", "ANY", "ETC"}
    ]
