"""Parser for the Montgomery County case-detail Service tab HTML.

Each service event in #tblServiceBody is rendered as 4 consecutive <tr> rows:

  1. Method header     <tr class="table-info">
        <strong>CIVIL AREA 2 CERTIFIED MAIL</strong>
  2. Party row
        Name:    <party name>
        Address: <multi-line address>
  3. Dates row
        Issue Date | Service Date | File Date | Failure Date
  4. Status row
        Service Status | Received By | Notes

This is the source of truth for the data manager's narrative notes like:
   "Defendant received summons on 03/24/2026"
   "summons in transit"
   "delivery failed on 03/25/2026"
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag


@dataclass
class ServiceEvent:
    method: str = ""             # CIVIL AREA 2 CERTIFIED MAIL, etc.
    party_name: str = ""
    party_street: str = ""       # multi-line address from the party row
    party_city: str = ""
    party_state: str = ""
    party_zip: str = ""
    issue_date: str = ""         # MM/DD/YYYY
    service_date: str = ""
    file_date: str = ""
    failure_date: str = ""
    service_status: str = ""     # CIVIL SUCCESSFUL SERVICE, IN TRANSIT, etc.
    received_by: str = ""
    notes: str = ""


def _field_value(tr: Tag, label: str) -> str:
    """Find a labeled field in a <tr> and return its value text.

    Two layout patterns:
      A) Label and value INLINE in the same col (dates row layout):
            <div class="col-md-3">
              <strong>Issue Date:</strong> 5/19/2026
            </div>
      B) Label in own col, value in SIBLING col (status row layout):
            <div class="col-md-1"><strong>Service Status:</strong></div>
            <div class="col-md-5">CIVIL SUCCESSFUL SERVICE</div>

    Strategy: if the label's parent col contains non-label text after the
    strong, treat as Pattern A. Otherwise look at the next sibling col.
    """
    label_norm = label.rstrip(":").lower()
    NO_VALUE = {"no date found.", ""}

    for strong in tr.select("strong"):
        if strong.get_text(strip=True).rstrip(":").lower() != label_norm:
            continue

        label_col = strong.parent
        if label_col is None:
            continue

        # Check for Pattern A: any non-label text or nested element after strong
        col_text = label_col.get_text(" ", strip=True)
        prefix = f"{label.rstrip(':')}:"
        i = col_text.lower().find(prefix.lower())
        inline_after = col_text[i + len(prefix):].strip() if i >= 0 else ""
        if inline_after:   # Pattern A
            if inline_after.lower() in NO_VALUE:
                return ""
            return inline_after

        # Pattern B: value lives in the next sibling col within the same .row
        row_div = label_col.parent
        if row_div is not None and "row" in (row_div.get("class") or []):
            sibling_cols = [
                c for c in row_div.select("[class*='col-']")
                if c is not label_col
            ]
            if sibling_cols:
                value = sibling_cols[0].get_text(" ", strip=True)
                if value.lower() in NO_VALUE:
                    return ""
                return value
    return ""


def _split_lines(div_text: str) -> list[str]:
    return [ln.strip() for ln in div_text.split("\n") if ln.strip()]


def parse_service_tab(html: str) -> list[ServiceEvent]:
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.select_one("#tblServiceBody")
    if tbody is None:
        return []

    rows = tbody.select("tr")
    events: list[ServiceEvent] = []

    i = 0
    while i < len(rows):
        tr = rows[i]
        if "table-info" not in (tr.get("class") or []):
            i += 1
            continue

        ev = ServiceEvent()
        ev.method = tr.get_text(strip=True)

        # Next row: party name + address. Same div-col layout as name —
        # find the `Address:` label and pull the sibling col, which has
        # a multi-line address like:
        #     5764 PENNYWELL DR
        #     HUBER HEIGHTS, OH 45424
        if i + 1 < len(rows):
            party_tr = rows[i + 1]
            for strong in party_tr.select("strong"):
                label = strong.get_text(strip=True).rstrip(":").lower()
                if label == "name":
                    parent = strong.parent
                    container = parent.parent  # the .row
                    cols = container.select("div.col-md-5")
                    if cols:
                        ev.party_name = cols[0].get_text(" ", strip=True)
                elif label == "address":
                    parent = strong.parent
                    container = parent.parent
                    cols = container.select("div.col-md-5")
                    if cols:
                        # Convert <br> to \n so we can split lines
                        for br in cols[0].find_all("br"):
                            br.replace_with("\n")
                        raw = cols[0].get_text("\n", strip=True)
                        lines = [
                            re.sub(r"\s+", " ", ln).strip()
                            for ln in raw.splitlines()
                            if ln.strip()
                        ]
                        # Last line: "CITY, STATE ZIP"
                        if lines:
                            last = lines[-1]
                            m = re.match(
                                r"^(?P<city>[A-Z][A-Z\.\-' ]+?)"
                                r"(?:,\s*(?P<state>[A-Z]{2}))?"
                                r"\s+(?P<zip>\d{5}(?:-\d{4})?)$",
                                last,
                            )
                            if m:
                                ev.party_city = m.group("city").strip().title()
                                ev.party_state = (m.group("state")
                                                  or "OH").upper()
                                ev.party_zip = m.group("zip")
                                ev.party_street = " ".join(lines[:-1]).strip()
                            else:
                                ev.party_street = " ".join(lines).strip()

        # Next row: dates
        if i + 2 < len(rows):
            dates_tr = rows[i + 2]
            ev.issue_date = _field_value(dates_tr, "Issue Date")
            ev.service_date = _field_value(dates_tr, "Service Date")
            ev.file_date = _field_value(dates_tr, "File Date")
            ev.failure_date = _field_value(dates_tr, "Failure Date")

        # Next row: status, received by, notes
        if i + 3 < len(rows):
            status_tr = rows[i + 3]
            ev.service_status = _field_value(status_tr, "Service Status")
            ev.received_by = _field_value(status_tr, "Received By")
            ev.notes = _field_value(status_tr, "Notes")

        events.append(ev)
        i += 4

    return events


def summarize_for_main_defendant(
    events: list[ServiceEvent],
    main_defendant: str,
) -> str:
    """Produce a one-liner narrative for the main defendant's service status.

    Used to populate the H3 Excel's "Litigation Stage/Notes" column —
    matches the format of the data manager's hand-written notes:
       "Defendant received summons on 03/24/2026"
       "summons issued on 03/19/2026; in transit"
       "delivery failed on 03/25/2026"
    """
    md_upper = main_defendant.upper().strip()
    matching = [e for e in events if md_upper in e.party_name.upper()]
    if not matching:
        return ""

    # Take the latest event by issue date
    latest = max(matching, key=lambda e: e.issue_date or "")
    status = (latest.service_status or "").upper()

    if "SUCCESSFUL" in status and latest.service_date:
        return (f"Defendant received summons on {latest.service_date}"
                f" (issued {latest.issue_date}, filed {latest.file_date})")
    if "TRANSIT" in status or "IN TRANSIT" in status:
        return f"summons issued on {latest.issue_date}; in transit"
    if latest.failure_date:
        return f"delivery failed on {latest.failure_date}"
    if latest.issue_date:
        return f"summons issued on {latest.issue_date}"
    return ""


if __name__ == "__main__":
    import sys
    from pathlib import Path

    path = (Path(sys.argv[1]) if len(sys.argv) > 1 else
            Path(__file__).resolve().parents[1] / "output" /
            "phase_b_run_3" / "case_03024_service.html")
    events = parse_service_tab(path.read_text())
    print(f"=== Parsed {len(events)} service events ===\n")
    for ev in events[:6]:
        print(f"  [{ev.service_status}] {ev.party_name}")
        print(f"    method  : {ev.method}")
        print(f"    dates   : issue={ev.issue_date} serve={ev.service_date} "
              f"file={ev.file_date} fail={ev.failure_date}")
        print(f"    via {ev.received_by!r} — notes: {ev.notes!r}")
        print()

    print("=== Summary for THOMAS SCOTT DAVIS (main defendant) ===")
    print(summarize_for_main_defendant(events, "THOMAS SCOTT DAVIS"))
