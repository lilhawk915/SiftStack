"""Tracerfy batch skip trace — phones + emails for all records.

Submits all records to POST /v1/api/trace/ (batch endpoint, $0.02/record),
polls for results, and populates NoticeData phone/email fields.
Runs as a separate pipeline step before DataSift CSV generation.

Signing chain support: traces ALL signing-authority heirs (not just DM #1)
so the user has full contact info for every heir who must sign to close a deal.
"""

import csv
import io
import json
import logging
import time

import requests

import config as cfg
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# Tracerfy batch response phone/email fields
PHONE_FIELDS = [
    "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
    "mobile_5", "landline_1", "landline_2", "landline_3",
]
EMAIL_FIELDS = ["email_1", "email_2", "email_3", "email_4", "email_5"]

TRACERFY_TRACE_URL = "https://tracerfy.com/v1/api/trace/"
TRACERFY_QUEUE_URL = "https://tracerfy.com/v1/api/queue/"


def _get_contacts_for_trace(
    notice: NoticeData, max_signing_traces: int = 5,
) -> list[tuple[str, str, str, str, str, str]]:
    """Determine who to skip-trace for this notice.

    Returns list of (first_name, last_name, address, city, zip, heir_key).
    heir_key is the full name used to match results back to the right heir.

    For deceased owners: traces DM #1 + all signing-authority heirs with addresses.
    For living owners: traces the property owner only.
    """
    contacts = []

    if (notice.owner_deceased == "yes"
            and notice.decision_maker_name
            and notice.decision_maker_name.strip()):

        # Always include DM #1 (primary contact)
        dm_name = notice.decision_maker_name.strip()
        address = notice.decision_maker_street or notice.address or ""
        city_val = notice.decision_maker_city or notice.city or ""
        zip_code = notice.decision_maker_zip or notice.zip or ""
        first, last = _split_name(dm_name)
        if first and last:
            contacts.append((first, last, address, city_val, zip_code, dm_name))

        # Add other signing-authority heirs from heir_map_json
        if notice.heir_map_json:
            try:
                heirs = json.loads(notice.heir_map_json)
            except (json.JSONDecodeError, TypeError):
                heirs = []

            seen = {dm_name.lower()}
            for heir in heirs:
                if len(contacts) >= max_signing_traces:
                    break
                heir_name = heir.get("name", "").strip()
                if not heir_name or heir_name.lower() in seen:
                    continue
                if not heir.get("signing_authority"):
                    continue
                if heir.get("status") == "deceased":
                    continue
                if not heir.get("street"):
                    continue  # No address = can't trace effectively
                seen.add(heir_name.lower())
                h_first, h_last = _split_name(heir_name)
                if h_first and h_last:
                    contacts.append((
                        h_first, h_last,
                        heir["street"],
                        heir.get("city", ""),
                        heir.get("zip", ""),
                        heir_name,
                    ))
    else:
        # Living owner — single contact
        name = (notice.owner_name or "").strip()
        if name:
            first, last = _split_name(name)
            if first and last:
                contacts.append((
                    first, last,
                    notice.address or "",
                    notice.city or "",
                    notice.zip or "",
                    name,
                ))

    return contacts


def _split_name(name: str) -> tuple[str, str]:
    """Split a full name into (first, last). Returns ('', '') if unparseable."""
    parts = name.strip().split()
    if len(parts) < 2:
        return ("", "")
    return (parts[0], parts[-1])


# Keep backward-compatible single-contact function for callers that expect it
def _get_contact_for_trace(notice: NoticeData) -> tuple[str, str, str, str, str]:
    """Legacy single-contact wrapper. Returns (first, last, address, city, zip)."""
    contacts = _get_contacts_for_trace(notice, max_signing_traces=1)
    if contacts:
        first, last, addr, city, zip_code, _ = contacts[0]
        return (first, last, addr, city, zip_code)
    return ("", "", "", "", "")


def _lookup_missing_heir_addresses(
    notice: NoticeData, api_key: str | None,
) -> int:
    """Fill in mailing addresses for signing-authority heirs that lack one.

    For each living heir with signing_authority=true but no `street`, runs the
    existing DM address waterfall (Knox Tax → Serper/Firecrawl → DDG) and stores
    the result back onto the heir. Mutates notice.heir_map_json in place.

    Returns the number of heirs that gained an address.
    """
    if not notice.heir_map_json:
        return 0
    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(heirs, list):
        return 0

    # Lazy import to avoid a hard dependency cycle on obituary_enricher
    from obituary_enricher import _lookup_dm_address

    city_hint = (notice.city or "").strip()
    filled = 0
    for heir in heirs:
        if not isinstance(heir, dict):
            continue
        if not heir.get("signing_authority"):
            continue
        if heir.get("status") == "deceased":
            continue
        if (heir.get("street") or "").strip():
            continue
        heir_name = (heir.get("name") or "").strip()
        if not heir_name:
            continue

        try:
            addr = _lookup_dm_address(
                heir_name, city_hint, api_key or "", tracerfy_tier1=False,
            )
        except Exception as e:
            logger.debug("Heir address lookup failed for %s: %s", heir_name, e)
            continue
        if addr and addr.get("street"):
            heir["street"] = addr.get("street", "")
            heir["city"] = addr.get("city", "") or city_hint
            heir["state"] = addr.get("state", "") or "TN"
            heir["zip"] = addr.get("zip", "")
            heir["address_source"] = addr.get("source", "")
            filled += 1
            logger.info(
                "  Heir address filled: %s → %s, %s",
                heir_name, heir["street"], heir.get("city", ""),
            )

    if filled:
        notice.heir_map_json = json.dumps(heirs, ensure_ascii=False)
    return filled


def batch_skip_trace(
    notices: list[NoticeData],
    max_signing_traces: int = 5,
    lookup_heir_addresses: bool = True,
    address_lookup_api_key: str | None = None,
) -> dict:
    """Run Tracerfy batch skip trace on all records.

    Submits a single batch CSV to POST /v1/api/trace/, polls for results,
    and populates phone/email fields on each NoticeData object.

    For deceased owners, traces ALL signing-authority heirs (up to max_signing_traces
    per property). DM #1's phones go to flat NoticeData fields; other heirs'
    phones/emails are stored in their heir_map_json entry.

    When lookup_heir_addresses is True, signing-authority heirs without a known
    mailing address get one looked up (Knox Tax → people search) before the trace
    so Tracerfy has enough info to return phones. Uses ANTHROPIC_API_KEY (or the
    explicit override) for LLM-based extraction from people-search pages.

    Returns stats dict: {total, submitted, matched, phones_found, emails_found,
                         cost, signing_heirs_traced, heir_addresses_filled}.
    """
    stats = {
        "total": len(notices),
        "submitted": 0,
        "matched": 0,
        "phones_found": 0,
        "emails_found": 0,
        "cost": 0.0,
        "signing_heirs_traced": 0,
        "heir_addresses_filled": 0,
        "credits_exhausted": False,
    }

    if not cfg.TRACERFY_API_KEY:
        logger.warning("Tracerfy API key not set — skipping batch skip trace")
        return stats

    # Fill missing heir addresses BEFORE building the trace batch — otherwise
    # those heirs get silently dropped at the `if not heir.get("street")` check
    # in _get_contacts_for_trace and never get Tracerfy phones.
    if lookup_heir_addresses:
        llm_key = address_lookup_api_key or getattr(cfg, "ANTHROPIC_API_KEY", "") or None
        for notice in notices:
            if notice.owner_deceased != "yes":
                continue
            try:
                stats["heir_addresses_filled"] += _lookup_missing_heir_addresses(notice, llm_key)
            except Exception:
                logger.exception("Heir address lookup pass failed for notice")
        if stats["heir_addresses_filled"]:
            logger.info("Heir address backfill: %d heir(s) gained an address",
                        stats["heir_addresses_filled"])

    # Route rows into three buckets:
    #   1. Normal batch — has a usable person name → trace_type='normal', 1 credit/row
    #   2. Advanced batch — no usable name but has address → trace_type='advanced', 2 credits/row
    #      Tracerfy identifies the property owner AND their contacts from address alone.
    #      Two sources: (a) blank owner_name (mcohio parser missed defendant), (b) entity
    #      owner_name where entity_researcher didn't resolve a person (LLC/HOA with no
    #      indexed officers).
    #   3. Sidecar — neither name nor address (or deceased with no DM) → manual lookup.
    #
    # Pre-flight guards:
    #   * Deceased-owner-no-DM: Tracerfy returns nothing for dead people; would waste
    #     credits — sidecar for the deep-prospecting skill instead.
    #   * Advanced-batch pre-check: requires address + city; without those the
    #     advanced trace has nothing to look up.
    from datasift_formatter import _is_entity_name
    lookup_map: list[tuple[NoticeData, str, str, str, str, str, str]] = []
    advanced_candidates: list[NoticeData] = []
    skipped_deceased = 0
    skipped_no_address = 0
    manual_rows: list[dict] = []

    def _sidecar(notice: NoticeData, reason: str) -> None:
        manual_rows.append({
            "case_number": getattr(notice, "case_number", "") or "",
            "notice_type": getattr(notice, "notice_type", "") or "",
            "owner_name":  getattr(notice, "owner_name", "") or "",
            "property_address": (
                f"{getattr(notice, 'address', '')}, "
                f"{getattr(notice, 'city', '')} "
                f"{getattr(notice, 'zip', '')}"
            ).strip(", "),
            "county":      getattr(notice, "county", "") or "",
            "reason": reason,
        })

    for notice in notices:
        # Guard 1: deceased owner with no living DM → skip (sidecar)
        if (
            (notice.owner_deceased or "").strip().lower() == "yes"
            and not (notice.decision_maker_name or "").strip()
        ):
            skipped_deceased += 1
            _sidecar(notice, "deceased_no_dm")
            continue

        contacts = _get_contacts_for_trace(notice, max_signing_traces)

        # Detect entity-only rows: has a name in owner_name, but it's an entity
        # (LLC/HOA/Trust/etc.) AND entity_researcher couldn't resolve a person.
        # These would get submitted to normal-trace with the entity name and
        # come back 0-match — same failure mode as blank owner. Route to advanced.
        owner_name = (notice.owner_name or "").strip()
        entity_person = (getattr(notice, "entity_person_name", "") or "").strip()
        entity_unresolved = (
            owner_name
            and _is_entity_name(owner_name)
            and not entity_person
        )

        if not contacts or entity_unresolved:
            # No usable person name → advanced batch if we have address + city
            addr = (notice.address or "").strip()
            city_val = (notice.city or "").strip()
            if addr and city_val:
                advanced_candidates.append(notice)
            else:
                skipped_no_address += 1
                _sidecar(
                    notice,
                    "entity_no_person_and_no_address" if entity_unresolved
                    else "blank_owner_no_address",
                )
            continue

        for i, (first, last, address, city, zip_code, heir_key) in enumerate(contacts):
            # Skip DM #1 if already has phones
            if i == 0 and notice.primary_phone:
                continue
            # Skip heirs already traced (have phones in heir_map_json)
            if i > 0 and _heir_has_phones(notice, heir_key):
                continue
            lookup_map.append((notice, first, last, address, city, zip_code, heir_key))

    # Emit pre-flight summary + write sidecar
    if skipped_deceased:
        logger.info(
            "Tracerfy pre-flight: skipped %d deceased-owner rows with "
            "no living DM (use deep-prospecting skill for heir research)",
            skipped_deceased,
        )
        stats["skipped_deceased"] = skipped_deceased
    if skipped_no_address:
        logger.info(
            "Tracerfy pre-flight: skipped %d rows with no usable name AND "
            "no address (unrecoverable — check scraper)",
            skipped_no_address,
        )
        stats["skipped_no_address"] = skipped_no_address
    if manual_rows:
        import csv as _csv
        from pathlib import Path as _Path
        sidecar_path = _Path("output") / "needs_manual_lookup.csv"
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        header = list(manual_rows[0].keys())
        exists = sidecar_path.exists()
        with sidecar_path.open("a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=header)
            if not exists:
                w.writeheader()
            w.writerows(manual_rows)
        logger.info(
            "Tracerfy pre-flight: %d row(s) logged to %s",
            len(manual_rows), sidecar_path,
        )

    if not lookup_map and not advanced_candidates:
        logger.info(
            "Tracerfy: no records to skip-trace (all have phones or no name+address)"
        )
        return stats

    # ── Normal batch: 1 credit/row, requires names ──
    if lookup_map:
        stats["submitted"] = len(lookup_map)
        stats["signing_heirs_traced"] = sum(
            1 for n, _, _, _, _, _, hk in lookup_map
            if n.decision_maker_name and hk != n.decision_maker_name
        )
        est_cost = len(lookup_map) * 0.02
        logger.info(
            "Tracerfy normal batch: submitting %d contacts (%d notices, "
            "%d signing heirs) — est $%.2f",
            len(lookup_map),
            len(set(id(n) for n, *_ in lookup_map)),
            stats["signing_heirs_traced"], est_cost,
        )
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["first_name", "last_name", "address", "city", "state",
                         "zip", "mail_address", "mail_city", "mail_state"])
        for notice_ref, first, last, address, city, zip_code, _ in lookup_map:
            state = notice_ref.state or "TN"
            writer.writerow([first, last, address, city, state, zip_code, "", "", ""])
        csv_content = csv_buffer.getvalue()
        csv_buffer.close()
        records = _submit_and_poll(csv_content, "normal", stats,
                                     label="normal batch")
        if stats.get("credits_exhausted"):
            return stats
        if records is not None:
            _match_results(records, lookup_map, stats)
            stats["cost"] += len(lookup_map) * 0.02
            logger.info(
                "  Tracerfy normal batch complete: %d matched, %d phones, "
                "%d emails (cumulative cost $%.2f)",
                stats["matched"], stats["phones_found"], stats["emails_found"],
                stats["cost"],
            )

    # ── Advanced batch: 2 credits/row, address-only (Tracerfy finds owner) ──
    if advanced_candidates:
        stats["advanced_submitted"] = len(advanced_candidates)
        est_cost = len(advanced_candidates) * 0.04
        logger.info(
            "Tracerfy advanced batch: submitting %d address-only rows — "
            "est $%.2f (owner name will be recovered from address)",
            len(advanced_candidates), est_cost,
        )
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["address", "city", "state", "zip"])
        for n in advanced_candidates:
            writer.writerow([
                (n.address or "").strip(),
                (n.city or "").strip(),
                (n.state or "OH").strip(),
                (n.zip or "").strip(),
            ])
        csv_content = csv_buffer.getvalue()
        csv_buffer.close()
        # For advanced trace only address/city/state are required; docs say
        # first/last/mail_* are optional. We still send column names so the
        # API knows which columns are which.
        records = _submit_and_poll(
            csv_content, "advanced", stats,
            label="advanced batch",
            extra_form={
                "address_column": "address",
                "city_column": "city",
                "state_column": "state",
                "zip_column": "zip",
            },
            skip_default_cols=True,  # don't send first_name_column etc.
        )
        if stats.get("credits_exhausted"):
            return stats
        if records is not None:
            _match_advanced_results(records, advanced_candidates, stats)
            stats["cost"] += len(advanced_candidates) * 0.04
            logger.info(
                "  Tracerfy advanced batch complete: %d matched, "
                "cumulative cost $%.2f",
                stats.get("advanced_matched", 0), stats["cost"],
            )

    return stats


def _submit_and_poll(
    csv_content: str,
    trace_type: str,
    stats: dict,
    *,
    label: str,
    extra_form: dict | None = None,
    skip_default_cols: bool = False,
) -> list | None:
    """Submit a CSV to POST /v1/api/trace/ and poll until complete.

    Returns the records list on success, ``None`` on any failure (logged).
    Sets ``stats['credits_exhausted']`` on 402 so the caller can short-circuit.

    Args:
        trace_type: "normal" (1 credit/row, requires names) or "advanced"
            (2 credits/row, address-only).
        skip_default_cols: When True, don't send the first_name/last_name/
            mail_* column mappings. Used for advanced trace where those
            fields aren't in the CSV.
    """
    form_data: dict = {"trace_type": trace_type}
    if not skip_default_cols:
        form_data.update({
            "first_name_column": "first_name",
            "last_name_column": "last_name",
            "address_column": "address",
            "city_column": "city",
            "state_column": "state",
            "zip_column": "zip",
            "mail_address_column": "mail_address",
            "mail_city_column": "mail_city",
            "mail_state_column": "mail_state",
            "mailing_zip_column": "zip",
        })
    if extra_form:
        form_data.update(extra_form)

    try:
        resp = requests.post(
            TRACERFY_TRACE_URL,
            headers={"Authorization": f"Bearer {cfg.TRACERFY_API_KEY}"},
            data=form_data,
            files={"csv_file": (f"{label.replace(' ', '_')}.csv",
                                 csv_content, "text/csv")},
            timeout=30,
        )
        if resp.status_code == 402:
            stats["credits_exhausted"] = True
            logger.error(
                "Tracerfy %s 402 — INSUFFICIENT CREDITS. Response: %s",
                label, resp.text[:500],
            )
            return None
        if resp.status_code != 200:
            logger.warning("Tracerfy %s %d response: %s",
                           label, resp.status_code, resp.text[:500])
        resp.raise_for_status()
        queue_data = resp.json()
        queue_id = queue_data.get("queue_id")
        if not queue_id:
            logger.warning("Tracerfy %s returned no queue_id", label)
            return None
        est_wait = queue_data.get("estimated_wait_seconds", "unknown")
        logger.info("  Tracerfy %s job %s submitted (est. %ss)",
                    label, queue_id, est_wait)

        for attempt in range(60):
            time.sleep(5)
            result_resp = requests.get(
                f"{TRACERFY_QUEUE_URL}{queue_id}",
                headers={"Authorization": f"Bearer {cfg.TRACERFY_API_KEY}"},
                timeout=15,
            )
            result_resp.raise_for_status()
            result_data = result_resp.json()

            if isinstance(result_data, list):
                return result_data
            if isinstance(result_data, dict):
                status = result_data.get("status", "")
                if status == "failed":
                    logger.warning("Tracerfy %s job %s failed", label, queue_id)
                    return None
                if status != "completed":
                    if attempt % 6 == 5:
                        logger.info("  Tracerfy %s still processing (%ds)...",
                                    label, (attempt + 1) * 5)
                    continue
                return result_data.get("records", [])
        logger.warning("Tracerfy %s job %s timed out after 5 min", label, queue_id)
        return None
    except Exception as e:
        logger.warning("Tracerfy %s failed: %s", label, e)
        return None


def _heir_has_phones(notice: NoticeData, heir_key: str) -> bool:
    """Check if a specific heir already has phone data in heir_map_json."""
    if not notice.heir_map_json:
        return False
    try:
        heirs = json.loads(notice.heir_map_json)
        for h in heirs:
            if h.get("name", "").lower() == heir_key.lower():
                return bool(h.get("phones"))
    except (json.JSONDecodeError, TypeError):
        pass
    return False


def _match_advanced_results(
    records: list,
    advanced_candidates: list[NoticeData],
    stats: dict,
) -> None:
    """Match Tracerfy advanced-batch responses back to NoticeData by address.

    Advanced trace responses include first_name/last_name that Tracerfy
    IDENTIFIED (we didn't submit any). Match by property address (case-
    insensitive street + city), populate the discovered name onto
    ``owner_name`` if it's currently blank OR is an entity, and populate
    phones/emails on the flat NoticeData fields.

    Does NOT overwrite an existing person's owner_name — protects records
    where entity_researcher already resolved a person (rare, but possible
    if entity_researcher resolves after Tracerfy has already run in a
    future pipeline reordering).
    """
    from datasift_formatter import _is_entity_name

    stats.setdefault("advanced_matched", 0)
    stats.setdefault("advanced_owner_recovered", 0)

    # Index candidates by (street_lower, city_lower) for O(1) matching
    idx: dict[tuple[str, str], NoticeData] = {}
    for n in advanced_candidates:
        key = (
            (n.address or "").strip().lower(),
            (n.city or "").strip().lower(),
        )
        if key[0] and key[1]:
            idx.setdefault(key, n)

    for rec in records:
        if not isinstance(rec, dict):
            continue
        rec_addr = (rec.get("address") or "").strip().lower()
        rec_city = (rec.get("city") or "").strip().lower()
        if not rec_addr or not rec_city:
            continue
        notice = idx.get((rec_addr, rec_city))
        if notice is None:
            continue

        # Extract phones + emails from response
        phones = [
            v.strip() for v in (rec.get(f) or "" for f in PHONE_FIELDS)
            if v.strip()
        ]
        emails = [
            v.strip() for v in (rec.get(f) or "" for f in EMAIL_FIELDS)
            if v.strip()
        ]
        rec_first = (rec.get("first_name") or "").strip()
        rec_last = (rec.get("last_name") or "").strip()

        # Populate discovered owner name if currently blank or entity
        current = (notice.owner_name or "").strip()
        if rec_first and rec_last and (
            not current or _is_entity_name(current)
        ):
            # Store as "FIRST LAST" so downstream _split_name works
            notice.owner_name = f"{rec_first} {rec_last}"
            stats["advanced_owner_recovered"] += 1

        # Also populate mailing address if Tracerfy provided one and
        # our record's owner_street is blank
        mail_street = (rec.get("mail_address") or "").strip()
        if mail_street and not (notice.owner_street or "").strip():
            notice.owner_street = mail_street
            notice.owner_city = (rec.get("mail_city") or "").strip()
            notice.owner_state = (rec.get("mail_state") or notice.state or "").strip()

        # Flat phones/emails onto NoticeData
        if phones and not notice.primary_phone:
            for i, field in enumerate(PHONE_FIELDS):
                if i < len(phones):
                    setattr(notice, field, phones[i])
        if emails and not (notice.email_1 or "").strip():
            for i, field in enumerate(EMAIL_FIELDS):
                if i < len(emails):
                    setattr(notice, field, emails[i])

        if phones or emails or (rec_first and rec_last):
            stats["advanced_matched"] += 1
            stats["phones_found"] += len(phones)
            stats["emails_found"] += len(emails)
            logger.info(
                "    [advanced] %s → %s %s: %d phones, %d emails",
                notice.address, rec_first or "?", rec_last or "?",
                len(phones), len(emails),
            )


def _match_results(records: list, lookup_map: list, stats: dict) -> None:
    """Match Tracerfy batch response records back to NoticeData objects.

    DM #1's phones/emails go to flat NoticeData fields (backward compat).
    Other signing heirs' phones/emails go into their heir_map_json entry.
    """
    for rec in records:
        if not isinstance(rec, dict):
            continue

        rec_first = (rec.get("first_name") or "").strip().lower()
        rec_last = (rec.get("last_name") or "").strip().lower()
        if not rec_first or not rec_last:
            continue

        # Find matching entry in lookup_map
        for notice, first, last, address, city, zip_code, heir_key in lookup_map:
            if first.lower() != rec_first or last.lower() != rec_last:
                continue

            # Extract phones and emails from response
            phones = []
            for field in PHONE_FIELDS:
                value = (rec.get(field) or "").strip()
                if value:
                    phones.append(value)

            emails = []
            for field in EMAIL_FIELDS:
                value = (rec.get(field) or "").strip()
                if value:
                    emails.append(value)

            if not phones and not emails:
                break

            # Is this the primary DM (#1)?
            is_primary = (
                notice.decision_maker_name
                and heir_key.lower() == notice.decision_maker_name.strip().lower()
            ) or notice.owner_deceased != "yes"

            if is_primary and not notice.primary_phone:
                # Populate flat NoticeData phone/email fields (backward compat)
                for i, field in enumerate(PHONE_FIELDS):
                    if i < len(phones):
                        setattr(notice, field, phones[i])
                for i, field in enumerate(EMAIL_FIELDS):
                    if i < len(emails):
                        setattr(notice, field, emails[i])
            elif not is_primary:
                # Store on the heir's entry in heir_map_json
                _store_heir_phones(notice, heir_key, phones, emails)

            stats["matched"] += 1
            stats["phones_found"] += len(phones)
            stats["emails_found"] += len(emails)
            logger.info("    %s %s: %d phones, %d emails%s",
                        first, last, len(phones), len(emails),
                        " (signing heir)" if not is_primary else "")
            break


def _store_heir_phones(
    notice: NoticeData, heir_key: str,
    phones: list[str], emails: list[str],
) -> None:
    """Store phones/emails on a specific heir's entry in heir_map_json."""
    if not notice.heir_map_json:
        return
    try:
        heirs = json.loads(notice.heir_map_json)
        for h in heirs:
            if h.get("name", "").lower() == heir_key.lower():
                h["phones"] = phones
                h["emails"] = emails
                break
        notice.heir_map_json = json.dumps(heirs, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
