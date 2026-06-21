"""Extract owner mailing addresses from equivant CourtView case-detail HTML.

Every equivant portal (Greene, Clermont, Clark, Butler, Miami) renders each
party in a row container that holds BOTH a `<span class="pty-name">` and a
`<div class="ptyContactInfo">` with the address. We just need to walk each
pty-name's ancestors until we find a row container that has a
ptyContactInfo child.

A typical address block looks like:

    1810 RICE BOULEVARD
    FAIRBORN
    , OH
    45324

(With "AKA NAME" / "ATTENTION: ..." / "c/o ..." metadata lines we filter out.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from bs4 import BeautifulSoup, Tag


# Metadata-line markers we drop from address blocks
_NOISE_PREFIXES = (
    "AKA ", "F/K/A ", "FKA ", "DBA ", "D/B/A ", "ATTENTION:", "ATTN:",
    "C/O ", "CO/", "C\\O ", "ALSO KNOWN AS ", "FORMERLY ",
)

# Last line format: "CITY, ST ZIP"
_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>[A-Z .'\-]+?)\s*,?\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
    re.IGNORECASE,
)
_ZIP_ONLY_RE = re.compile(r"\b(\d{5}(?:-\d{4})?)\b")
_STATE_ONLY_RE = re.compile(r"\b([A-Z]{2})\b")


@dataclass
class PartyAddress:
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""

    def is_empty(self) -> bool:
        return not any([self.street, self.city, self.state, self.zip])

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.street, self.city, self.state, self.zip)


def parse_address_block(text: str) -> PartyAddress:
    """Parse a `ptyContactInfo` block's text into structured fields.

    Real-world equivant addresses come out of `get_text('\\n')` looking like
    one of:
      A) "2800 TAMARACK ROAD\\nOWENSBORO\\n, \\nKY\\n42301"   (city, state, zip on separate lines)
      B) "1810 RICE BOULEVARD\\nFAIRBORN, OH 45324"           (city/state/zip on one line)
      C) "720 SOUTH VERITY PARKWAY\\nMIDDLETOWN\\n,\\nOH\\n45044"

    We treat `|` and `\\n` as line separators, drop noise lines (AKA / ATTN /
    c/o), then work from the end of the line list:
      - last token that's a 5-digit zip → zip
      - 2-letter all-caps token before it → state
      - first line that's not all punctuation → city
      - everything else → street
    """
    if not text:
        return PartyAddress()
    # Normalize separators to newline, strip non-breaking spaces, tabs
    text = (
        text.replace("\xa0", " ")
        .replace("\t", " ")
        .replace(" | ", "\n")
        .replace("|", "\n")
    )
    lines = [
        re.sub(r"\s+", " ", ln).strip(" ,")
        for ln in text.split("\n")
        if ln and ln.strip() and ln.strip() not in {",", ":", "."}
    ]
    # Drop noise lines (AKAs, ATTENTION, c/o, lone commas/colons)
    lines = [
        ln for ln in lines
        if not any(ln.upper().startswith(p) for p in _NOISE_PREFIXES)
        and ln.strip(",.:; ") != ""
    ]
    if not lines:
        return PartyAddress()

    addr = PartyAddress()
    consumed: set[int] = set()  # indices we've used

    # Walk from end: find ZIP, STATE, CITY
    n = len(lines)
    for i in range(n - 1, -1, -1):
        if i in consumed:
            continue
        ln = lines[i]
        if not addr.zip:
            m = _ZIP_ONLY_RE.search(ln)
            if m:
                addr.zip = m.group(1)
                # If state+zip on same line, strip zip and continue
                ln_no_zip = re.sub(r"\b" + re.escape(addr.zip) + r"\b", "", ln).strip(" ,")
                if ln_no_zip:
                    lines[i] = ln_no_zip
                else:
                    consumed.add(i)
                continue
        if addr.zip and not addr.state:
            m = re.search(r"\b([A-Z]{2})\b", ln)
            if m:
                addr.state = m.group(1).upper()
                ln_no_state = re.sub(
                    r"\b" + re.escape(addr.state) + r"\b", "", ln
                ).strip(" ,")
                if ln_no_state:
                    lines[i] = ln_no_state
                else:
                    consumed.add(i)
                continue
        if addr.state and not addr.city:
            cand = ln.strip(" ,")
            if cand and not cand.startswith(","):
                addr.city = cand.title()
                consumed.add(i)
            break

    # Remaining unconsumed lines = street components (in original order)
    street_parts = [
        lines[i].strip(" ,")
        for i in range(n)
        if i not in consumed and lines[i].strip(" ,")
    ]
    # If we set city via in-line edit, the line is still in `lines` and
    # may have leftover text — exclude pure punctuation
    street_parts = [
        p for p in street_parts
        if p.strip(",.:; ") and p != addr.city
    ]
    if street_parts:
        addr.street = " ".join(street_parts).strip()
    return addr


def find_party_address(name_span: Tag) -> PartyAddress:
    """Given the `<span class='pty-name'>` for a party, walk up to its row
    container and pull the associated `ptyContactInfo` address."""
    container: Tag | None = name_span.find_parent()
    for _ in range(6):
        if container is None:
            break
        contact = container.find(class_="ptyContactInfo")
        if contact:
            return parse_address_block(
                contact.get_text("\n", strip=True)
            )
        container = container.parent
    return PartyAddress()


def find_owner_address(soup: BeautifulSoup, owner_name: str) -> PartyAddress:
    """Find the address of the defendant whose name matches `owner_name`."""
    if not owner_name:
        return PartyAddress()
    norm = " ".join(owner_name.upper().split())
    for span in soup.find_all("span", class_="pty-name"):
        if " ".join(span.get_text(" ", strip=True).upper().split()) == norm:
            return find_party_address(span)
    # Fuzzy fallback: last-name + first-name token overlap
    owner_tokens = set(re.findall(r"\w{3,}", owner_name.upper()))
    for span in soup.find_all("span", class_="pty-name"):
        cand = span.get_text(" ", strip=True).upper()
        cand_tokens = set(re.findall(r"\w{3,}", cand))
        if owner_tokens and len(owner_tokens & cand_tokens) >= 2:
            return find_party_address(span)
    return PartyAddress()


# ── Counties not using equivant's pty-name pattern ──────────────────────

# Clermont/Clark/Butler equivant variants use `<div class="ptyInfoLabel">`
# instead of `<span class="pty-name">`. Same `ptyContactInfo` follows.
def find_owner_address_ptyinfo(
    soup: BeautifulSoup, owner_name: str
) -> PartyAddress:
    if not owner_name:
        return PartyAddress()
    norm = " ".join(owner_name.upper().split())
    for label in soup.find_all("div", class_="ptyInfoLabel"):
        cand = " ".join(label.get_text(" ", strip=True).upper().split())
        if cand == norm:
            container = label.find_parent()
            for _ in range(6):
                if container is None:
                    break
                contact = container.find(class_="ptyContactInfo")
                if contact:
                    return parse_address_block(
                        contact.get_text("\n", strip=True)
                    )
                container = container.parent
    # Fuzzy fallback
    owner_tokens = set(re.findall(r"\w{3,}", owner_name.upper()))
    for label in soup.find_all("div", class_="ptyInfoLabel"):
        cand = label.get_text(" ", strip=True).upper()
        cand_tokens = set(re.findall(r"\w{3,}", cand))
        if owner_tokens and len(owner_tokens & cand_tokens) >= 2:
            container = label.find_parent()
            for _ in range(6):
                if container is None:
                    break
                contact = container.find(class_="ptyContactInfo")
                if contact:
                    return parse_address_block(
                        contact.get_text("\n", strip=True)
                    )
                container = container.parent
    return PartyAddress()
