"""Post Ohio orchestrator CSVs to the H3 Homebuyers Slack workspace.

Replaces the daily DataSift web-automation upload with a Slack file-drop:
the orchestrator writes a CSV, and we post it to #h3-homebuyers-ftm with
a summary message. Operators pull from Slack into whatever destination
they want (DataSift manually, mailer tool, etc.) without the SiftStack
pipeline fighting REISift's upload wizard every morning.

Environment requirements:

  SLACK_BOT_TOKEN     (xoxb-...) Saved to ~/.zshrc for interactive shells
                      AND added to the launchd plist's EnvironmentVariables
                      so the 6 AM ET cron firing has it. launchd does NOT
                      inherit ~/.zshrc — the plist export is the load-
                      bearing copy.

Bot scopes required (configured at api.slack.com/apps):
  - chat:write   (post the summary message)
  - files:write  (upload the CSV as an attachment)

If SLACK_BOT_TOKEN is not set in the runtime environment, post_csv_to_ftm
returns False without raising — the daily run still produces a CSV, just
silently skips the Slack step. This is the right behavior for first-time
testing before the token is wired through launchd.
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path

# Eager import — was lazy inside post_csv_to_ftm but that path collided
# with macOS Time Machine backing up the .venv .pyc files mid-run (the
# Slack post is the LAST step in a 12-minute scrape, plenty of time
# for TM to start churning). Importing at module load happens at
# process startup, before TM has a chance to lock cache files, so the
# OSError: [Errno 11] Resource deadlock avoided failure goes away.
# slack_sdk is a hard requirement (see requirements.txt) — if it's
# missing, the orchestrator should fail loud at startup, not 12 min
# in when the Slack post tries to fire.
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


# H3 Homebuyers Slack — #h3-homebuyers-ftm channel
SLACK_CHANNEL_ID = "C0B1ZPMMMUK"


def post_csv_to_ftm(csv_path: Path, summary: dict | None = None) -> bool:
    """Post a CSV with summary message to #h3-homebuyers-ftm.

    Args:
        csv_path: Path to the CSV file to attach.
        summary: Optional pre-computed stats. Recognized keys:
            - records (int): total record count
            - by_notice_type (dict[str, int]): counts per notice_type
            - property_address_filled (int): rows with non-empty
              Property Street Address
            - owner_street_filled (int): rows with non-empty Owner Street
            - auditor_enriched (int): how many records got auditor-derived
              property addresses
            - auditor_targets (int): how many records were eligible for
              auditor lookup
            - dedup_dropped (int): how many duplicates _dedupe_by_mailing
              removed pre-write

        If summary is None or missing keys, the function re-reads the CSV
        and computes what it can.

    Returns:
        True on successful Slack post, False otherwise (logged, never raises).
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.warning(
            "SLACK_BOT_TOKEN not set — skipping Slack post for %s. "
            "Add it to ~/.zshrc AND to the launchd plist's "
            "EnvironmentVariables before tomorrow's cron firing.",
            csv_path.name,
        )
        return False

    # Build/augment summary stats from the CSV itself for anything not
    # passed in. This lets the orchestrator pass through state-bearing
    # numbers (dedup_dropped, auditor_enriched) while we compute the
    # rest from the file.
    summary = dict(summary or {})
    _augment_summary_from_csv(csv_path, summary)

    text = _format_message(csv_path, summary)

    client = WebClient(token=token)
    try:
        # files_upload_v2's `initial_comment` posts as the message body
        # accompanying the file attachment — one threaded post, not two.
        client.files_upload_v2(
            channel=SLACK_CHANNEL_ID,
            file=str(csv_path),
            filename=csv_path.name,
            initial_comment=text,
        )
        logger.info("Slack: posted %s to #h3-homebuyers-ftm (%d records)",
                    csv_path.name, summary.get("records", 0))
        return True
    except SlackApiError as e:
        err = e.response.get("error", "unknown") if e.response else "unknown"
        logger.error("Slack post failed: %s (channel=%s, file=%s)",
                     err, SLACK_CHANNEL_ID, csv_path.name)
        return False


# ── Helpers ────────────────────────────────────────────────────────────


def _augment_summary_from_csv(csv_path: Path, summary: dict) -> None:
    """Fill in record/notice-type/address-fill stats by re-reading the CSV.

    Doesn't override caller-provided values — only fills gaps. Auditor +
    dedup counts MUST come from the orchestrator (no way to recover them
    from the post-write CSV).
    """
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        logger.warning("Could not re-read CSV for summary: %s", e)
        return

    summary.setdefault("records", len(rows))

    if "by_notice_type" not in summary:
        from collections import Counter
        summary["by_notice_type"] = dict(
            Counter(r.get("Notice Type", "?") for r in rows)
        )

    if "property_address_filled" not in summary:
        summary["property_address_filled"] = sum(
            1 for r in rows
            if (r.get("Property Street Address") or "").strip()
        )

    if "owner_street_filled" not in summary:
        summary["owner_street_filled"] = sum(
            1 for r in rows if (r.get("Owner Street") or "").strip()
        )


def _format_message(csv_path: Path, summary: dict) -> str:
    """Format the summary as a Slack-friendly markdown-ish message.

    Slack supports a subset of markdown — bold via `*text*`, bullets via
    leading `• `, code spans via backticks. No headers, no tables — keep
    it scannable.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    total = summary.get("records", 0)

    lines = [
        f"*Ohio Daily Pull — {today}*",
        f"`{csv_path.name}` — *{total}* records",
    ]

    # Notice-type breakdown
    by_type = summary.get("by_notice_type") or {}
    if by_type:
        lines.append("")
        lines.append("*By notice type:*")
        for nt, count in sorted(by_type.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"• {nt}: {count}")

    # Address fill rates
    addr_lines = []
    prop_filled = summary.get("property_address_filled")
    owner_filled = summary.get("owner_street_filled")
    if prop_filled is not None and total:
        pct = round(prop_filled * 100 / total)
        addr_lines.append(f"• Property Address: {prop_filled}/{total} ({pct}%)")
    if owner_filled is not None and total:
        pct = round(owner_filled * 100 / total)
        addr_lines.append(f"• Owner mailing: {owner_filled}/{total} ({pct}%)")
    if addr_lines:
        lines.append("")
        lines.append("*Populated fields:*")
        lines.extend(addr_lines)

    # Pipeline state numbers
    pipeline_lines = []
    enriched = summary.get("auditor_enriched")
    targets = summary.get("auditor_targets")
    if enriched is not None and targets:
        pipeline_lines.append(
            f"• Auditor enrichment: {enriched}/{targets} probate records"
        )
    dedup = summary.get("dedup_dropped")
    if dedup:
        pre = total + dedup
        pipeline_lines.append(
            f"• Dedup: dropped {dedup} duplicate mailing(s) ({pre}→{total})"
        )

    # Tracerfy skip-trace stats (present when TRACERFY_ENABLED=1
    # fired in the orchestrator). Reports records matched and phones
    # recovered against the configured daily cost cap.
    t_matched = summary.get("tracerfy_records_matched")
    t_traced  = summary.get("tracerfy_records_traced")
    if t_traced is not None:
        cost = summary.get("tracerfy_cost_usd", 0.0)
        cap  = summary.get("tracerfy_cap_usd", 0.0)
        phones_added = summary.get("tracerfy_phones_added", 0)
        pipeline_lines.append(
            f"• Tracerfy: {t_matched}/{t_traced} records matched, "
            f"+{phones_added} primary phones "
            f"(${cost:.2f} / ${cap:.2f} cap)"
        )
    # Trestle phone-intel stats (present when TRESTLE_ENABLED=1).
    # Reports unique phones scored with activity tier assignment
    # ("Dial First" through "Drop") against the per-day Trestle cap.
    tr_scored = summary.get("trestle_phones_scored")
    if tr_scored is not None:
        cost = summary.get("trestle_cost_usd", 0.0)
        cap  = summary.get("trestle_cap_usd", 0.0)
        records = summary.get("trestle_records_scored", 0)
        pipeline_lines.append(
            f"• Trestle: {tr_scored} phones scored across "
            f"{records} records (${cost:.2f} / ${cap:.2f} cap)"
        )
    if pipeline_lines:
        lines.append("")
        lines.append("*Pipeline:*")
        lines.extend(pipeline_lines)

    return "\n".join(lines)
