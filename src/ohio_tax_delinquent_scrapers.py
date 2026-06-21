"""Ohio county tax-delinquent adapters — 7 SW Ohio counties.

Each adapter returns a list of ``NoticeData`` with ``notice_type="tax_delinquent"``
keyed by property address. Records feed the existing 10-step
``enrichment_pipeline.run_enrichment_pipeline()`` unchanged.

Source endpoints (source-of-truth: ``reference/ohio_counties/<County>.csv``):

    Butler      auditor.bcohio.gov/real_estate/real_estate_reports/index.php
                — direct CSV download (CLEANEST; no JS, no scrape)
    Clark       clarkcountyauditor.org/DelinquencyReport — online report
    Clermont    ⚠ ONLY Mobile Home delinquent PDF online — see clermont stub
    Greene      greeneauditor.org/property-search — searchable list, weekly refresh
    Miami       miamicountyohioauditor.gov/DelinquencyReport — live ~806 parcels
    Montgomery  mcohio.org/1521/Delinquent-List — parcel-by-parcel + records request
    Warren      auditor.warrencountyohio.gov/Documents/Home/DelqTax.pdf — PDF

Tag-stacking with probate + foreclosure happens at the DataSift UI level:
``mode="add"`` in ``datasift_uploader.upload_csv()`` merges by address into
existing records and appends ``Tags`` rather than overwriting. See
``CLAUDE.md`` § "Niche Sequential Marketing" for the tag taxonomy.

KNOWN LIMITATIONS — see README.md ``## Known Limitations``:
- Clermont: full real-estate delinquent list is published in the newspaper,
  not online. The mobile-home PDF that IS online is excluded as
  semantically narrow. Adapter raises ``NotImplementedError`` deliberately.
  If a tax-foreclosure proxy is wanted (Sheriff Sales + Forfeited Land),
  ship it as a separate ``notice_type="tax_foreclosure_proxy"``.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, TYPE_CHECKING

import httpx

from notice_parser import NoticeData

if TYPE_CHECKING:
    # Type-only import — keeps the module importable in sync-test
    # contexts where Playwright isn't installed (e.g. CI test runners
    # that only exercise fixture-based paths).
    from playwright.async_api import BrowserContext

logger = logging.getLogger(__name__)


# Per-county endpoint registry — single source of truth, mirrors the
# Summary.csv data committed under reference/ohio_counties/.
# ``refresh_cadence`` field is read by the orchestrator to decide
# whether to re-fetch a source on each weekly run or skip until the
# next scheduled window.
#   "weekly"  → re-fetch every orchestrator tick (Mon 12:30 ET).
#   "monthly" → re-fetch only when month boundary changes since last fetch.
#   "yearly"  → re-fetch only when tax-cycle year changes (annually).
# Transport is implied by `method` (csv_download, pdf_import, scrape).
OHIO_ENDPOINTS: dict[str, dict] = {
    "Butler": {
        "method": "csv_download",
        "transport": "playwright",   # Cloudflare on the revize.com CDN
        "refresh_cadence": "monthly",  # filename has "6-1-2026" → snapshot 1st of month
        "portal": "https://auditor.bcohio.gov/real_estate/real_estate_reports/index.php",
        # The CSV filename varies by report year; the portal lists current-year
        # ``Delinquent_Tax_List.csv`` as a fixed link target. We resolve the
        # actual URL by scraping the portal index on first call (cached).
        "csv_link_text": "Delinquent Tax List",
        "notes": "Direct CSV download (2-CSV join with Owners Report). "
                 "Filename includes month-of date — refreshed first-of-month.",
    },
    "Clark": {
        "method": "scrape",
        "transport": "playwright",   # Cloudflare direct
        "refresh_cadence": "weekly",
        "portal": "https://clarkcountyauditor.org/DelinquencyReport",
        "notes": "Online Delinquency Report — searchable, no bulk export.",
    },
    "Clermont": {
        "method": "not_implemented",
        "transport": "n/a",
        "refresh_cadence": "yearly",   # MH PDF is December-published; RE list is newspaper-only
        "portal": "https://www.clermontauditor.org/taxes-rates/delinquent-real-estate-tax-list/",
        "notes": "⚠ Only Mobile Home delinquent PDF posted online; real-estate "
                 "list is published in newspaper. See README known-limitations.",
    },
    "Greene": {
        "method": "scrape",
        "transport": "playwright",   # Azure WAF (not Cloudflare)
        "refresh_cadence": "weekly",
        # Live-verified 2026-06-16: greeneauditor.org no longer resolves.
        "portal": "https://auditor.greenecountyohio.gov/Search/",
        "notes": "Delinquent Tax Search behind Azure WAF; URL updated 2026-06-16 "
                 "from dead greeneauditor.org host.",
    },
    "Miami": {
        "method": "scrape",
        "transport": "playwright",   # Cloudflare direct
        "refresh_cadence": "weekly",
        "portal": "https://www.miamicountyohioauditor.gov/DelinquencyReport",
        "notes": "Live searchable delinquency report (~806 parcels).",
    },
    "Montgomery": {
        "method": "scrape",
        "transport": "http",         # plain HTTP, no WAF
        "refresh_cadence": "weekly",
        "portal": "https://www.mcohio.org/1521/Delinquent-List",
        "notes": "Inline HTML table (3,548 rows live 2026-06-16); no property "
                 "address — needs parcel→address enrichment via PROv3.",
    },
    "Warren": {
        "method": "pdf_text",
        "transport": "http",         # plain HTTP, born-digital PDF
        "refresh_cadence": "yearly",  # snapshot date e.g. 11/6/2025 — annual cycle
        "portal": "https://auditor.warrencountyohio.gov/Documents/Home/DelqTax.pdf",
        "notes": "Born-digital PDF (no OCR). Account-Number-keyed; lookup "
                 "address via Warren Auditor PerformAccountSearch.",
    },
}


# ── Butler — Playwright click-download + 2-CSV join (primary) ──────────


def fetch_butler(
    ctx: "BrowserContext | None" = None,
    client: httpx.Client | None = None,
    csv_override_text: str | None = None,
    owners_override_text: str | None = None,
):
    """Fetch Butler County's delinquent tax records, joined with owner data.

    Butler's Auditor portal hosts two CSVs (verified live 2026-06-16):

    1. **Delinquent Tax List** — narrow schema:
       ``PARID, CURRENTYEARDUE, LUC, PRIORYEARDUE``
    2. **Owners Report** — wide schema:
       ``PARCEL, LOCATION, OWNER1, OWNER2, MAILNAME1, MAILNAME2,
       MAILADR1, MAILADR2, MAILADR3, …`` (~28 cols, ~164k rows)

    We download both via Playwright (Cloudflare blocks plain HTTP from
    the revize.com CDN), build a dict keyed by ``PARID``/``PARCEL``
    from the Owners CSV, then iterate the Delinquent rows and join.
    Output is one ``NoticeData`` per delinquent parcel, with property
    address (from ``LOCATION``), owner name (``OWNER1``), owner
    mailing address (``MAILADR1/2/3``), absentee flag, and a
    Decimal-summed ``tax_delinquent_amount`` (``CURRENTYEARDUE +
    PRIORYEARDUE``).

    Signature is dual-mode by design:

    - **Async / live** — pass ``ctx`` (a Playwright ``BrowserContext``
      from ``scrape_all``). Returns ``Awaitable[list[NoticeData]]``.
      Use ``await fetch_butler(ctx=ctx)``.
    - **Sync / tests** — pass ``csv_override_text`` AND
      ``owners_override_text`` (or just ``csv_override_text`` for
      legacy tests that don't exercise the join). Returns
      ``list[NoticeData]`` directly. Use ``fetch_butler(csv_override_text=…)``.

    Args:
        ctx: Playwright ``BrowserContext`` for live downloads. Must have
            ``accept_downloads=True``. Ignored when override text is
            provided.
        client: Unused — kept for transport-mode parity with adapters
            that use plain HTTP. Butler always needs a browser due to
            Cloudflare on the revize.com CDN.
        csv_override_text: Raw text of the Delinquent Tax List CSV.
            Tests pass this to bypass the network entirely.
        owners_override_text: Raw text of the Owners Report CSV. When
            omitted alongside ``csv_override_text``, the parser runs
            without join (legacy behavior — delinquent fields populate
            but address/owner stay empty unless they happen to be in
            the same row).

    Returns:
        Sync path: ``list[NoticeData]`` (when override text is passed).
        Async path: ``Awaitable[list[NoticeData]]`` (when ``ctx`` is
        passed; the caller must ``await`` the result).
    """
    if csv_override_text is not None:
        # Sync test path. Skip Playwright entirely.
        owners = _parse_owners_csv(owners_override_text or "")
        return list(_butler_join_and_emit(csv_override_text, owners))
    # No overrides — must use Playwright (the only way past Cloudflare).
    if ctx is None:
        raise ValueError(
            "fetch_butler() requires either ctx= (live Playwright "
            "context) or csv_override_text= (fixture). Plain httpx is "
            "blocked by Cloudflare on Butler's revize.com CDN — see "
            "reference/ohio_counties/README.md § Transport notes."
        )
    return _butler_live_async(ctx)


async def _butler_live_async(ctx) -> list[NoticeData]:
    """Live download flow via Playwright. Called only from the async path."""
    delinquent_text, owners_text = await _butler_download_both(ctx)
    owners = _parse_owners_csv(owners_text)
    logger.info("Butler: loaded %d owner rows for join", len(owners))
    records = list(_butler_join_and_emit(delinquent_text, owners))
    logger.info("Butler: %d delinquent parcels after join", len(records))
    return records


async def _butler_download_both(ctx) -> tuple[str, str]:
    """Open the Auditor portal in a new page, click both CSV links.

    Each click triggers a download that Playwright captures via
    ``page.expect_download()``. We give the Owners CSV up to 60s
    because it's ~46MB and the CDN is occasionally slow.
    """
    cfg = OHIO_ENDPOINTS["Butler"]
    page = await ctx.new_page()
    try:
        await page.goto(
            cfg["portal"], wait_until="domcontentloaded", timeout=30_000,
        )
        # Belt-and-braces: small wait for the link list to render
        await page.wait_for_timeout(1_500)
        delinquent_text = await _click_and_read(
            page, "Delinquent Tax List", timeout_ms=30_000,
        )
        # Owners CSV is ~46 MB — bump the download timeout.
        owners_text = await _click_and_read(
            page, "Owners Report", timeout_ms=60_000,
        )
    finally:
        await page.close()
    return delinquent_text, owners_text


async def _click_and_read(page, link_text: str, *, timeout_ms: int) -> str:
    """Click a link by visible text and read the downloaded file as text."""
    async with page.expect_download(timeout=timeout_ms) as dl_info:
        # The Butler portal opens downloads with target=_blank — click
        # the first anchor whose text matches.
        await page.locator(
            f"a:has-text({link_text!r})"
        ).first.click()
    dl = await dl_info.value
    path = await dl.path()
    # Read once into memory; both files together are well under 50 MB.
    return Path(path).read_text(encoding="utf-8", errors="ignore")


# ── Owners CSV — schema constants + streaming dict build ───────────────

# Verified live 2026-06-16. Real-world headers from the Butler Owners
# Report CSV. Aliased keys are case-insensitive lookups so we don't
# crack on incidental whitespace or year-over-year casing changes.
_OWNERS_COLS = {
    "parcel":    ["parcel", "parcelid", "parid"],
    "location":  ["location", "site address", "property address"],
    "owner1":    ["owner1", "owner", "current owner"],
    "owner2":    ["owner2"],
    "mailname1": ["mailname1", "mail name 1"],
    "mailname2": ["mailname2", "mail name 2"],
    "mailadr1":  ["mailadr1", "mail address 1", "mailing address 1"],
    "mailadr2":  ["mailadr2", "mail address 2", "mailing address 2"],
    "mailadr3":  ["mailadr3", "mail address 3", "mailing address 3"],
}

_DELINQUENT_COLS = {
    "parcel":     ["parid", "parcel id", "parcel"],
    "current":    ["currentyeardue", "current year due", "current"],
    "prior":      ["prioryeardue", "prior year due", "prior"],
    "luc":        ["luc", "land use code"],
}


def _parse_owners_csv(text: str) -> dict[str, dict]:
    """Stream the (large) Owners CSV row-by-row into a dict keyed by parcel.

    We don't load it into memory as a list — there are ~164k Butler
    parcels and we only need the ~3,100 that match the delinquent set.
    The dict is the join target (constant-time lookup per delinquent
    row).
    """
    if not text:
        return {}
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return {}
    cols = {
        key: _find_col(reader.fieldnames, aliases)
        for key, aliases in _OWNERS_COLS.items()
    }
    if not cols["parcel"]:
        logger.warning(
            "Butler Owners CSV: no parcel column in headers %r — "
            "schema may have changed",
            reader.fieldnames,
        )
        return {}
    out: dict[str, dict] = {}
    for row in reader:
        pid = _normalize_parcel(_get(row, cols["parcel"]))
        if not pid:
            continue
        out[pid] = {
            "location":  _get(row, cols["location"]),
            "owner1":    _get(row, cols["owner1"]),
            "owner2":    _get(row, cols["owner2"]),
            "mailname1": _get(row, cols["mailname1"]),
            "mailname2": _get(row, cols["mailname2"]),
            "mailadr1":  _get(row, cols["mailadr1"]),
            "mailadr2":  _get(row, cols["mailadr2"]),
            "mailadr3":  _get(row, cols["mailadr3"]),
        }
    return out


def _butler_join_and_emit(
    delinquent_text: str, owners: dict[str, dict],
) -> Iterable[NoticeData]:
    """Iterate the Delinquent CSV and emit one NoticeData per matched parcel.

    Rows without a parcel match in the Owners dict still get emitted
    (parcel + amount only) so downstream enrichment can attempt its
    own address lookup — we don't silently drop them.
    """
    reader = csv.DictReader(io.StringIO(delinquent_text))
    if reader.fieldnames is None:
        return
    cols = {
        key: _find_col(reader.fieldnames, aliases)
        for key, aliases in _DELINQUENT_COLS.items()
    }
    if not cols["parcel"]:
        logger.warning(
            "Butler Delinquent CSV: no parcel column in headers %r",
            reader.fieldnames,
        )
        return

    for row in reader:
        pid_raw = _get(row, cols["parcel"])
        pid = _normalize_parcel(pid_raw)
        if not pid:
            continue
        amount = _sum_delinquent(
            _get(row, cols["current"]), _get(row, cols["prior"]),
        )
        owner_row = owners.get(pid)
        if owner_row is None:
            # Parcel in delinquent list but not in owners file — emit
            # a minimal record. The 10-step enrichment pipeline can
            # try to fill the address downstream.
            yield NoticeData(
                state="OH",
                notice_type="tax_delinquent",
                county="Butler",
                source_url=OHIO_ENDPOINTS["Butler"]["portal"],
                parcel_id=pid_raw,
                tax_delinquent_amount=amount,
            )
            continue
        property_street = _clean_address_line(owner_row["location"])
        owner_name = owner_row["owner1"] or owner_row["mailname1"]
        # Owner mailing address: lines may be (street) / "" / "CITY ST ZIP",
        # but in practice MAILADR1 is street and the city/state/zip
        # sits in MAILADR2 or MAILADR3.
        mail_lines = [
            owner_row["mailadr1"], owner_row["mailadr2"], owner_row["mailadr3"],
        ]
        mail_street, mail_city, mail_state, mail_zip = _split_mail_lines(
            mail_lines,
        )
        absentee = _is_absentee(property_street, mail_street)
        yield NoticeData(
            address=property_street,
            state="OH",
            notice_type="tax_delinquent",
            county="Butler",
            source_url=OHIO_ENDPOINTS["Butler"]["portal"],
            parcel_id=pid_raw,
            owner_name=owner_name,
            owner_street=mail_street,
            owner_city=mail_city,
            owner_state=mail_state or "OH",
            owner_zip=mail_zip,
            tax_delinquent_amount=amount,
            # datasift_formatter.py converts this to an absentee_owner
            # Tag column entry on upload — see _build_tags() there.
            absentee_owner=absentee,
        )


# ── Helpers ────────────────────────────────────────────────────────────


def _find_col(headers: list[str], aliases: list[str]) -> str | None:
    """Find the first header that matches any alias (case-insensitive)."""
    if not headers:
        return None
    norm = {h.strip().lower(): h for h in headers if h is not None}
    for a in aliases:
        if a in norm:
            return norm[a]
    return None


def _normalize_parcel(raw: str) -> str:
    """Canonical parcel form for join lookups. Strip surrounding quotes/spaces."""
    return (raw or "").strip().strip('"').strip()


def _sum_delinquent(current_due: str, prior_due: str) -> str:
    """Sum CURRENTYEARDUE + PRIORYEARDUE as Decimal, return as plain string.

    Handles `$`, commas, and stray whitespace. Returns "" only when both
    inputs are blank/unparseable; otherwise emits a precise sum like
    "1500.75" (no leading $, no commas). Downstream Smarty / DataSift
    consumers parse this back to a number.
    """
    total = Decimal("0")
    saw_value = False
    for raw in (current_due, prior_due):
        cleaned = re.sub(r"[^\d.\-]", "", raw or "")
        if not cleaned:
            continue
        try:
            total += Decimal(cleaned)
            saw_value = True
        except InvalidOperation:
            continue
    if not saw_value:
        return ""
    # Normalize to 2 decimals when result has fractional component
    quantized = total.quantize(Decimal("0.01"))
    # Strip trailing zeros only if it's a whole number (e.g. "100.00" → "100")
    # …actually keep .00 for dollar-amount consistency.
    return str(quantized)


def _clean_address_line(raw: str) -> str:
    """Collapse runs of whitespace in an address string.

    Butler's Owners CSV emits double-spaces like "6790  RIVER RD" —
    fix in one place before downstream consumers see it.
    """
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def _split_mail_lines(
    lines: list[str],
) -> tuple[str, str, str, str]:
    """Parse 3-line owner mailing address into (street, city, state, zip).

    Butler's MAILADR1/2/3 columns are:
        Line 1: STREET (e.g. "6790 E RIVER RD" or "PO BOX 123")
        Line 2: often blank, sometimes apt/suite or "CITY ST ZIP"
        Line 3: usually "CITY ST ZIP" (e.g. "FAIRFIELD OH 45014-2403")

    We find the city/state/zip line by regex match (last line that
    matches the pattern) and treat everything before it as street.
    """
    norm = [_clean_address_line(ln) for ln in lines if ln]
    csz_re = re.compile(
        r"^(?P<city>[A-Z][A-Z\s\.\-']+?)\s+"
        r"(?P<state>[A-Z]{2})\s+"
        r"(?P<zip>\d{5}(?:-\d{4})?)$",
        re.IGNORECASE,
    )
    city = state = zip_ = ""
    csz_idx = -1
    for i in range(len(norm) - 1, -1, -1):
        m = csz_re.match(norm[i])
        if m:
            city = m.group("city").strip().title()
            state = m.group("state").upper()
            zip_ = m.group("zip")
            csz_idx = i
            break
    street_parts = norm[:csz_idx] if csz_idx >= 0 else norm
    street = " ".join(p for p in street_parts if p).strip()
    return street, city, state, zip_


# Common street-suffix variants → canonical short form. Used by the
# absentee comparator so "RIVER RD" and "RIVER ROAD" don't read as
# different streets.
_STREET_SUFFIX_CANON = {
    "ROAD": "RD", "STREET": "ST", "AVENUE": "AVE", "DRIVE": "DR",
    "COURT": "CT", "LANE": "LN", "BOULEVARD": "BLVD", "PLACE": "PL",
    "TERRACE": "TER", "PARKWAY": "PKWY", "CIRCLE": "CIR", "TRAIL": "TRL",
    "HIGHWAY": "HWY", "POINT": "PT", "SQUARE": "SQ", "ROUTE": "RT",
}

_PO_BOX_RE = re.compile(r"\bP\.?\s*O\.?\s*BOX\b", re.IGNORECASE)


def _normalize_street(s: str) -> str:
    """Uppercase + collapse whitespace + canonicalize street suffix.

    Used only for the absentee comparator — not for stored fields.
    """
    if not s:
        return ""
    up = re.sub(r"\s+", " ", s).upper().strip(" .,")
    # Strip apt/unit/suite from comparator input
    up = re.sub(r"\b(APT|UNIT|STE|SUITE|#)\s*\S+$", "", up).strip()
    # Canonicalize ALL whole-word suffix variants → short form
    for long, short in _STREET_SUFFIX_CANON.items():
        up = re.sub(rf"\b{long}\b", short, up)
    return up


def _is_absentee(property_street: str, mail_street: str) -> str:
    """Decide the ``absentee_owner`` flag value (``"Y"`` or ``""``).

    Heuristic:
    - PO Box mailing → absentee (the owner can't live in a PO box).
    - Normalized property street and mailing street don't match →
      absentee. Normalization handles ``RD``/``ROAD`` variants,
      whitespace, apt/unit/suite suffixes, case.
    - Either side missing → blank (don't guess).

    Returns ``"Y"`` when absentee, ``""`` otherwise — matches the
    boolean-flag style used elsewhere in NoticeData (Y/blank, not Y/N).
    """
    if not property_street or not mail_street:
        return ""
    if _PO_BOX_RE.search(mail_street):
        return "Y"
    return "Y" if _normalize_street(property_street) != _normalize_street(mail_street) else ""


def _get(row: dict, col: str | None) -> str:
    if not col:
        return ""
    return (row.get(col) or "").strip()


# ── Warren — text-PDF + Auditor Account-Number lookup ────────────────


# Per-row layout in Warren's Delinquent Tax List PDF (live verify
# 2026-06-16). Header rows look like:
#   OWNER AS OF 11/6/2025 PROPERTY DESCRIPTION TOTAL DUE
# Followed by data rows with three runs of tokens we have to split:
#   <7-digit acct> <OWNER> <legal description> <$ amount>
#
# The owner name and legal description don't have a clean separator
# (no consistent comma, no tab). We anchor on the two ends we DO
# know — the leading account-number digits and the trailing currency
# value — and treat the middle as a single "owner + legal" blob to
# be split heuristically (LegalDesc starts at the first token that
# looks like a section/township/range/lot identifier).
_WARREN_ROW_RE = re.compile(
    r"^\s*"
    r"(?P<account>\d{7})\s+"
    r"(?P<middle>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})\s*$"
)

# Tokens that signal "legal description starts here" — sub-codes,
# section-township-range, "LOT:", "ACRES", etc. We split the middle
# blob at the first match.
_WARREN_LEGAL_DESC_TOKEN_RE = re.compile(
    r"\s(?="
    r"(?:\d+[-/]\d+[-/]\d+\b"          # 5-3-32, 4/4-21, etc.
    r"|LOT[:.\s]"                     # LOT: 62
    r"|\d+\.\d+\s*AC\b"                # 2.4031 AC.
    r"|[A-Z]+\s+LOT\b"                 # CENTERVILLE FOREST LOT
    r"|VIL\.\b"                       # VIL.WN.CRK/BLVD.WNC9
    r"|HORIZON HILLS|TAMARACK|ROYAL OAKS|CLEARCREEK|SAVANNAH"
    r"|WILLIAMSON|KENDRICK|TURNER BROTHERS|THROCKMORTON"
    r"|SPRINGBORO ORIG"
    r")"
    r")",
    re.IGNORECASE,
)


def fetch_warren(
    ctx=None,
    client: httpx.Client | None = None,
    pdf_override_text: str | None = None,
    pdf_override_path: Path | str | None = None,
    lookup_addresses: bool = True,
    max_address_lookups: int | None = None,
) -> list[NoticeData]:
    """Fetch Warren County's Delinquent Tax List PDF + resolve addresses.

    Warren's Auditor publishes a searchable-text PDF once per tax cycle
    (snapshot date 11/6/2025 in the live 2026-06-16 file). The PDF is
    served over plain HTTP — no Cloudflare, no WAF. We:

      1. Download the PDF via httpx (browser UA).
      2. Extract text page-by-page via pypdfium2 (no OCR needed —
         the PDF is born-digital).
      3. Parse each row → account number + owner + legal desc + $ amount.
      4. For each row, hit the Warren Auditor Account-Number search
         (also plain HTTP) to resolve the property address. Reuses
         a single httpx.Client across all lookups for connection pooling.

    Cadence: **yearly** (per OHIO_ENDPOINTS["Warren"]["refresh_cadence"]).
    The Auditor refreshes the snapshot once per tax cycle; weekly
    re-fetch is wasted work. The orchestrator should only re-run this
    adapter when the PDF's last-modified header changes OR on an
    explicit yearly schedule.

    Args:
        ctx: Unused — Warren needs no Playwright (signature kept for
            dispatcher uniformity).
        client: Optional ``httpx.Client`` to reuse. When None, a fresh
            client is built with browser-like headers.
        pdf_override_text: Pre-extracted PDF text used in place of a
            live download. Tests pass a fixture here.
        pdf_override_path: Path to an already-downloaded PDF file.
            When provided, we skip the download and extract text from
            this file. Useful for re-running parsing without hitting
            the auditor.
        lookup_addresses: When False, skip the Account-Number address
            lookup pass (returns NoticeData with parcel + owner +
            amount only). Used in tests + when caller wants to do
            address lookup separately/in parallel.
        max_address_lookups: Cap the number of address lookups (None =
            no cap). Useful for incremental dry-runs.

    Returns:
        ``list[NoticeData]`` — one record per delinquent account,
        with ``notice_type="tax_delinquent"``, ``county="Warren"``.
    """
    # ── 1. Acquire PDF text ──────────────────────────────────────────
    if pdf_override_text is not None:
        text = pdf_override_text
    elif pdf_override_path is not None:
        text = _extract_warren_pdf_text(Path(pdf_override_path))
    else:
        pdf_bytes = _warren_download_pdf(client)
        # Write to a temp file so pypdfium2 can mmap it
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp_path = Path(tf.name)
        try:
            text = _extract_warren_pdf_text(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    # ── 2. Parse rows ────────────────────────────────────────────────
    rows = list(_warren_parse_rows(text))
    logger.info("Warren: parsed %d delinquent rows from PDF", len(rows))

    # ── 3. Address lookup pass ───────────────────────────────────────
    if lookup_addresses and rows:
        from warren_auditor import (
            lookup_property_by_account,
            ParcelAddress,
        )
        # Single shared client for connection pooling across ~1,350
        # sequential lookups. With pooling each lookup is ~150ms vs
        # ~800ms with a fresh client per call.
        lookup_client = client or httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 SiftStack/1.0"},
            timeout=20,
        )
        try:
            limit = (max_address_lookups
                     if max_address_lookups is not None else len(rows))
            for i, r in enumerate(rows[:limit]):
                addr = lookup_property_by_account(
                    r["account"], client=lookup_client,
                )
                if not addr.is_empty():
                    r["_addr"] = addr
                if (i + 1) % 100 == 0:
                    logger.info("Warren: %d/%d address lookups done",
                                i + 1, limit)
        finally:
            if client is None:
                lookup_client.close()

    # ── 4. NoticeData emission ───────────────────────────────────────
    out: list[NoticeData] = []
    for r in rows:
        addr = r.get("_addr")
        out.append(NoticeData(
            address=addr.street if addr else "",
            city=addr.city if addr else "",
            state="OH",
            zip=addr.zip if addr else "",
            owner_name=r["owner"],
            notice_type="tax_delinquent",
            county="Warren",
            source_url=OHIO_ENDPOINTS["Warren"]["portal"],
            parcel_id=r["account"],
            tax_delinquent_amount=r["amount"],
            # Warren PDF has no separate mailing address — single owner
            # mailing field. The downstream enrichment pipeline can
            # populate Owner Street/City/Zip via Smarty if needed.
        ))
    return out


def _warren_download_pdf(client: httpx.Client | None) -> bytes:
    """Download the Warren Delinquent Tax List PDF.

    Returns the raw bytes. The auditor responds to plain HTTP GET with
    a browser User-Agent — no Cloudflare, no WAF.
    """
    url = OHIO_ENDPOINTS["Warren"]["portal"]
    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*;q=0.8",
    }
    c = client or httpx.Client(
        follow_redirects=True, headers=browser_headers, timeout=60,
    )
    try:
        logger.info("Warren: downloading delinquent PDF from %s", url)
        r = c.get(url)
        r.raise_for_status()
        if not r.content.startswith(b"%PDF"):
            raise RuntimeError(
                "Warren auditor returned non-PDF response — schema "
                "may have changed; re-verify against "
                "reference/ohio_counties/Warren.csv"
            )
        return r.content
    finally:
        if client is None:
            c.close()


def _extract_warren_pdf_text(pdf_path: Path) -> str:
    """Extract searchable text from the Warren PDF via pypdfium2.

    The PDF is born-digital (verified live 2026-06-16) so no OCR is
    needed. Pages 1-2 contain a header block we skip; data rows start
    after the "------" separator.
    """
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf_path))
    parts: list[str] = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            try:
                parts.append(tp.get_text_range())
            finally:
                tp.close()
                page.close()
    finally:
        doc.close()
    return "\n".join(parts)


def _warren_parse_rows(text: str) -> Iterable[dict]:
    """Parse Warren PDF text → row dicts {account, owner, legal, amount}.

    Skips the multi-line header block. Anchors each row on the
    leading 7-digit account number and the trailing dollar amount.
    Splits the middle blob (owner + legal description) at the first
    token that looks like a legal-description marker.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _WARREN_ROW_RE.match(line)
        if not m:
            continue
        account = m.group("account")
        middle = m.group("middle").strip()
        amount = m.group("amount").replace(",", "")
        # Split owner from legal description
        split_m = _WARREN_LEGAL_DESC_TOKEN_RE.search(middle)
        if split_m:
            owner = middle[:split_m.start()].strip()
            legal = middle[split_m.start():].strip()
        else:
            owner = middle
            legal = ""
        # Owner sometimes has a trailing "*" continuation marker (Warren
        # uses it when the owner name wraps to the next line). Strip.
        owner = owner.rstrip("*").strip().rstrip(";").strip()
        yield {
            "account": account,
            "owner": owner,
            "legal": legal,
            "amount": amount,
        }


# ── Montgomery / Miami / Greene / Clark — scraper-based stubs ──────────


# ── Montgomery — inline HTML scrape ────────────────────────────────────


# Match the data table headers we expect from mcohio.org/1521/Delinquent-List
_MC_TABLE_HEADERS = ("DistCode", "District Name", "Owner Name",
                      "Parcel ID", "Delq Amount")


def fetch_montgomery(
    ctx=None,
    client: httpx.Client | None = None,
    html_override_text: str | None = None,
) -> list[NoticeData]:
    """Fetch Montgomery County's Treasurer Delinquent List.

    Source: mcohio.org/1521/Delinquent-List — inline HTML table with
    ~3,548 rows verified live 2026-06-16. Schema:

        DistCode | District Name | Owner Name | Parcel ID | Delq Amount

    Transport: **plain HTTP** (no Cloudflare/WAF). httpx fetches the
    page, BeautifulSoup parses the table.

    Address gap — DELIBERATE:

        The delinquent feed exposes Owner Name + Parcel ID + Delq
        Amount but **not** property address. The auditor's iasWorld
        portal at mcrealestate.org can resolve parcel→address but
        requires a Playwright-driven disclaimer click and a
        per-parcel navigate+search+click sequence — roughly 5-10s
        per parcel × 3,548 parcels = several hours per run.

        For weekly runs we ship records WITHOUT addresses (parcel +
        owner + amount only). The downstream Smarty enrichment
        cannot resolve parcel→address (it only standardizes existing
        addresses) — so these records will land in DataSift with
        ``Owner Street`` blank. They're still useful: DataSift will
        merge them with existing probate/foreclosure records that
        share the same owner name (DataSift indexes both address
        AND owner for record-merge purposes).

        When a deeper enrichment pass is wanted, run a separate
        ``enrich_montgomery_addresses.py`` script that uses the
        iasWorld lookup with concurrent Playwright pages — that
        pattern is queued, not blocking this adapter.

    Args:
        ctx: Unused (signature kept for dispatcher uniformity).
        client: Optional ``httpx.Client`` to reuse for the single
            page fetch.
        html_override_text: Fixture text used in place of a live
            fetch. Tests pass this to avoid the network.

    Returns:
        ``list[NoticeData]`` — one record per delinquent parcel.
    """
    if html_override_text is not None:
        html_text = html_override_text
    else:
        html_text = _montgomery_fetch_html(client)
    rows = list(_montgomery_parse_rows(html_text))
    logger.info("Montgomery: parsed %d delinquent rows from HTML", len(rows))

    out: list[NoticeData] = []
    for r in rows:
        out.append(NoticeData(
            address="",
            city="",
            state="OH",
            zip="",
            owner_name=r["owner"],
            notice_type="tax_delinquent",
            county="Montgomery",
            source_url=OHIO_ENDPOINTS["Montgomery"]["portal"],
            parcel_id=r["parcel"],
            tax_delinquent_amount=r["amount"],
            # Stash District Name in raw_text — useful downstream for
            # locating the tax district / school district context.
            raw_text=f"DistCode: {r['dist_code']}; District: {r['district']}",
        ))
    return out


def _montgomery_fetch_html(client: httpx.Client | None) -> str:
    """Download the Montgomery delinquent-list HTML page."""
    url = OHIO_ENDPOINTS["Montgomery"]["portal"]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    c = client or httpx.Client(
        follow_redirects=True, headers=headers, timeout=30,
    )
    try:
        logger.info("Montgomery: fetching delinquent HTML from %s", url)
        r = c.get(url)
        r.raise_for_status()
        return r.text
    finally:
        if client is None:
            c.close()


def _montgomery_parse_rows(html_text: str) -> Iterable[dict]:
    """Find the delinquent-list table and yield row dicts.

    Schema (live 2026-06-16): ``DistCode | District Name | Owner Name |
    Parcel ID | Delq Amount``. The table is server-rendered inline so
    no JS execution is required.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "html.parser")
    for tbl in soup.find_all("table"):
        first_cells = [c.get_text(strip=True)
                       for c in tbl.find_all(["th", "td"])][:8]
        # Identify the data table by its column-header signature
        if "Parcel ID" not in first_cells or "Owner Name" not in first_cells:
            continue
        # First row has the headers; skip it. Subsequent rows have data.
        all_rows = tbl.find_all("tr")
        for tr in all_rows:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if len(cells) < 5:
                continue
            # Skip the header row itself
            if cells[0] == "DistCode" or cells[2] == "Owner Name":
                continue
            yield {
                "dist_code": cells[0],
                "district":  cells[1],
                "owner":     cells[2],
                "parcel":    cells[3],
                "amount":    cells[4].replace(",", "").replace("$", "").strip(),
            }
        break  # data table parsed; don't keep scanning other tables


# ── Miami — Playwright click-download (same pattern as Clark) ─────────


def fetch_miami(
    ctx=None,
    client: httpx.Client | None = None,
    csv_override_text: str | None = None,
) -> list[NoticeData]:
    """Fetch Miami County's Delinquency Report via the portal Export CSV.

    Source: miamicountyohioauditor.gov/DelinquencyReport — ~658 parcels
    live 2026-06-16. Same auditor software as Clark (same Export CSV
    button, same UTF-16 CSV schema), so we share the parser.

    Behind Cloudflare — requires Playwright. Tests use override text.
    """
    if csv_override_text is not None:
        return list(_clark_or_miami_parse_csv(csv_override_text, "Miami"))
    if ctx is None:
        raise ValueError(
            "fetch_miami() requires either ctx= (live Playwright) or "
            "csv_override_text= (fixture). Cloudflare blocks plain HTTP."
        )
    return _miami_live_async(ctx)


async def _miami_live_async(ctx) -> list[NoticeData]:
    cfg = OHIO_ENDPOINTS["Miami"]
    page = await ctx.new_page()
    try:
        await page.goto(cfg["portal"], wait_until="networkidle", timeout=45_000)
        await page.wait_for_timeout(3_000)
        async with page.expect_download(timeout=30_000) as dl_info:
            await page.locator(
                "button:has-text('Export CSV'), a:has-text('Export CSV')"
            ).first.click()
        dl = await dl_info.value
        path = await dl.path()
        raw = Path(path).read_bytes()
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="ignore")
    finally:
        await page.close()
    return list(_clark_or_miami_parse_csv(text, "Miami"))


# ── Greene — Playwright township-by-township Export CSV (Azure WAF) ───


def fetch_greene(
    ctx=None,
    client: httpx.Client | None = None,
    township_csv_text_map: dict[str, str] | None = None,
) -> list[NoticeData]:
    """Fetch Greene County's Delinquent List, township-by-township.

    Unlike Clark/Miami, Greene has no single Export CSV button on the
    main delinquent page. Instead the data is partitioned across ~30
    townships ("polsub" values); each township has its own Export CSV.
    The adapter:

      1. Fetches the main /DelinquentList page to enumerate township names.
      2. For each township, navigates to the township page + clicks
         Export → CSV → captures the download.
      3. Parses each CSV and deduplicates by parcel_id (the EXC
         variant townships often overlap with their parent townships).
      4. Returns a single deduplicated NoticeData list.

    Schema per township CSV:
        Property Number | Owner Name | School District | Location Address | Amount | View Property

    Crucially, Greene IS the only county that exposes property
    address inline — no separate parcel→address lookup needed.

    Transport: Azure WAF — requires Playwright.

    Args:
        ctx: Playwright ``BrowserContext`` (required for live runs).
        client: Unused.
        township_csv_text_map: ``{township_name: csv_text}`` mapping used
            in place of live downloads. Tests pass this to skip the
            network. When provided, ``ctx`` is ignored.

    Returns:
        ``list[NoticeData]`` — deduplicated by parcel_id.
    """
    if township_csv_text_map is not None:
        return list(_greene_emit_records(township_csv_text_map))
    if ctx is None:
        raise ValueError(
            "fetch_greene() requires either ctx= (live Playwright) or "
            "township_csv_text_map= (fixture). Azure WAF blocks plain HTTP."
        )
    return _greene_live_async(ctx)


async def _greene_live_async(ctx) -> list[NoticeData]:
    """Live Greene download: enumerate townships, capture each Export CSV."""
    cfg = OHIO_ENDPOINTS["Greene"]
    page = await ctx.new_page()
    csv_map: dict[str, str] = {}
    try:
        # 1. Enumerate townships from the main delinquent page
        await page.goto(
            "https://auditor.greenecountyohio.gov/DelinquentList",
            wait_until="networkidle", timeout=45_000,
        )
        await page.wait_for_timeout(3_000)
        html = await page.content()
        polsub_refs = re.findall(r"polsub[=\s']+([^&'\"]+)", html, re.IGNORECASE)
        townships = sorted(set(
            re.sub(r"%20", " ", p).strip()
            for p in polsub_refs
            if re.match(r"^[A-Z][A-Z\s&]{2,}", p) and len(p) < 80
        ))
        logger.info("Greene: discovered %d townships", len(townships))

        # 2. For each township, click Export → CSV
        from urllib.parse import quote
        for i, twp in enumerate(townships):
            twp_url = (
                "https://auditor.greenecountyohio.gov/"
                f"DelinquentList/Polsub?polsub={quote(twp)}&sort=Owner_ASC"
            )
            try:
                await page.goto(
                    twp_url, wait_until="networkidle", timeout=30_000,
                )
                await page.wait_for_timeout(1_500)
                # Click Export (opens menu), then CSV (triggers download)
                export_btn = page.locator(
                    "a:has-text('Export'), button:has-text('Export')"
                ).first
                if await export_btn.count() == 0:
                    logger.info(
                        "Greene: township %r has no Export button — skipping",
                        twp,
                    )
                    continue
                await export_btn.click()
                await page.wait_for_timeout(1_000)
                async with page.expect_download(timeout=20_000) as dl_info:
                    await page.locator(
                        "a:has-text('CSV'), button:has-text('CSV')"
                    ).first.click()
                dl = await dl_info.value
                path = await dl.path()
                csv_map[twp] = Path(path).read_text(
                    encoding="utf-8", errors="ignore",
                )
                if (i + 1) % 5 == 0:
                    logger.info(
                        "Greene: %d/%d townships downloaded",
                        i + 1, len(townships),
                    )
            except Exception as e:
                logger.warning(
                    "Greene: township %r download failed: %s", twp, e,
                )
    finally:
        await page.close()

    logger.info(
        "Greene: %d townships downloaded, parsing + dedup...",
        len(csv_map),
    )
    return list(_greene_emit_records(csv_map))


# Greene CSV column-name aliases
_GREENE_COLS = {
    "parcel":   ["property number", "parcel number", "parcel"],
    "owner":    ["owner name"],
    "school":   ["school district"],
    "address":  ["location address", "property address", "address"],
    "amount":   ["amount", "total due"],
    "view":     ["view property"],
}


def _greene_emit_records(
    csv_text_map: dict[str, str],
) -> Iterable[NoticeData]:
    """Parse all township CSVs, dedupe by parcel, emit NoticeData."""
    seen_parcels: set[str] = set()
    for township, text in csv_text_map.items():
        for rec in _greene_parse_csv(text, township):
            if rec.parcel_id and rec.parcel_id in seen_parcels:
                continue
            if rec.parcel_id:
                seen_parcels.add(rec.parcel_id)
            yield rec


def _greene_parse_csv(text: str, township: str) -> Iterable[NoticeData]:
    """Parse one township CSV into NoticeData rows."""
    if not text or not text.strip():
        return
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return
    cols = {
        key: _find_col(reader.fieldnames, aliases)
        for key, aliases in _GREENE_COLS.items()
    }
    if not cols["parcel"]:
        logger.warning(
            "Greene CSV (township=%r): no parcel column in headers %r",
            township, reader.fieldnames,
        )
        return
    for row in reader:
        parcel = _get(row, cols["parcel"])
        if not parcel:
            continue
        location = _get(row, cols["address"])
        # Greene's Location Address column carries multi-line content
        # ("street\ncity STATE zip"). Parse it.
        street, city, state, zip_ = _parse_multiline_address(location)
        amount = _get(row, cols["amount"])
        amount_clean = re.sub(r"[^\d.\-]", "", amount)
        yield NoticeData(
            address=street,
            city=city,
            state=state or "OH",
            zip=zip_,
            owner_name=_get(row, cols["owner"]),
            notice_type="tax_delinquent",
            county="Greene",
            source_url=OHIO_ENDPOINTS["Greene"]["portal"],
            parcel_id=parcel,
            tax_delinquent_amount=amount_clean,
            # Stash school district + township for downstream context
            raw_text=(
                f"township: {township}; "
                f"school: {_get(row, cols['school'])}"
            ),
        )


_GREENE_STREET_SUFFIX_RE = re.compile(
    r"\b(?:ROAD|RD|STREET|ST|AVENUE|AVE|DRIVE|DR|BOULEVARD|BLVD|"
    r"LANE|LN|COURT|CT|PLACE|PL|WAY|PARKWAY|PKWY|CIRCLE|CIR|"
    r"TERRACE|TER|TRAIL|TRL|HIGHWAY|HWY|PIKE|RUN|ROUTE|RT|SR|"
    r"SQUARE|SQ|POINT|PT|RIDGE|RDG)\b",
    re.IGNORECASE,
)


def _parse_multiline_address(raw: str) -> tuple[str, str, str, str]:
    """Greene's Location Address: 'STREET\\nCITY ST ZIP' → 4-tuple.

    Some rows have everything on one line; some have linebreaks. We
    handle both by collapsing whitespace, then anchoring the street/city
    boundary on a known street suffix (otherwise the non-greedy regex
    would split too eagerly, e.g. street='4903' + city='BATH RD DAYTON').
    """
    if not raw:
        return ("", "", "", "")
    flat = re.sub(r"\s+", " ", raw).strip()
    # Match trailing "CITY ST ZIP"; cap everything before as street.
    m = re.search(
        r"^(?P<pre>.+?)\s+(?P<state>[A-Z]{2})\s+"
        r"(?P<zip>\d{5}(?:-\d{4})?)\s*$",
        flat,
    )
    if not m:
        return (flat, "", "", "")
    pre = m.group("pre")
    # Anchor street on the LAST known street suffix in `pre`. Everything
    # after the suffix is the city. If no suffix found, dump it all into
    # street.
    suffix_matches = list(_GREENE_STREET_SUFFIX_RE.finditer(pre))
    if suffix_matches:
        last = suffix_matches[-1]
        street = pre[: last.end()].strip()
        city = pre[last.end():].strip(" ,.-").title()
    else:
        street = pre.strip()
        city = ""
    return (street, city, m.group("state"), m.group("zip"))


# ── Clark — Playwright click-download (UTF-16 CSV behind Cloudflare) ──


def fetch_clark(
    ctx=None,
    client: httpx.Client | None = None,
    csv_override_text: str | None = None,
) -> list[NoticeData]:
    """Fetch Clark County's Delinquency Report via the portal's Export CSV.

    Source: clarkcountyauditor.org/DelinquencyReport — ~2,582 delinquent
    parcels live 2026-06-16. The portal is behind Cloudflare so plain
    httpx returns a JS challenge; Playwright clears it on page-load.

    Schema (UTF-16 encoded, Excel-style ``sep=,`` directive on line 1):

        Parcel Number | Tax Payer | Certified Year | Vacant | Amount

    No property address — same deliberate gap as Montgomery; downstream
    Smarty/Zillow enrichment can resolve from parcel+county+state if
    needed.

    Args:
        ctx: Playwright ``BrowserContext`` (required for live runs —
            Cloudflare blocks non-browser). Must have
            ``accept_downloads=True``. When None, raises (unless
            override text is passed).
        client: Unused — Clark is behind Cloudflare.
        csv_override_text: Pre-decoded CSV text used in place of live
            download. Tests pass this to skip the network.

    Returns:
        ``list[NoticeData]`` — one record per delinquent parcel.
    """
    if csv_override_text is not None:
        return list(_clark_parse_csv(csv_override_text))
    if ctx is None:
        raise ValueError(
            "fetch_clark() requires either ctx= (live Playwright) or "
            "csv_override_text= (fixture). Cloudflare blocks plain HTTP."
        )
    return _clark_live_async(ctx)


async def _clark_live_async(ctx) -> list[NoticeData]:
    """Live Clark download via Playwright + Export CSV button."""
    cfg = OHIO_ENDPOINTS["Clark"]
    page = await ctx.new_page()
    try:
        await page.goto(
            cfg["portal"], wait_until="networkidle", timeout=45_000,
        )
        await page.wait_for_timeout(3_000)
        async with page.expect_download(timeout=30_000) as dl_info:
            await page.locator(
                "button:has-text('Export CSV'), a:has-text('Export CSV')"
            ).first.click()
        dl = await dl_info.value
        path = await dl.path()
        # Clark serves UTF-16 — decode explicitly
        raw = Path(path).read_bytes()
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="ignore")
    finally:
        await page.close()
    return list(_clark_parse_csv(text))


def _clark_or_miami_parse_csv(
    text: str, county: str,
) -> Iterable[NoticeData]:
    """Shared parser for Clark + Miami — both use the same Auditor software.

    Schema: ``Parcel Number | Tax Payer | Certified Year | Vacant | Amount``.
    UTF-16 encoded with optional ``sep=,`` directive on line 1.
    """
    return _generic_5col_csv_parse(text, county)


def _clark_parse_csv(text: str) -> Iterable[NoticeData]:
    """Clark wrapper — preserves the older name used in tests."""
    yield from _clark_or_miami_parse_csv(text, "Clark")


def _generic_5col_csv_parse(
    text: str, county: str,
) -> Iterable[NoticeData]:
    """Shared 5-col parser for the Clark/Miami auditor-software CSV.

    Handles the Excel-style ``sep=,`` directive on the first line.
    """
    if not text:
        return
    # Drop the BOM if present + skip the sep= directive
    text = text.lstrip("﻿")
    lines = text.splitlines()
    # Find the header line — it starts with "Parcel"
    start_idx = 0
    for i, ln in enumerate(lines):
        if ln.startswith("sep="):
            start_idx = i + 1
            continue
        if "Parcel" in ln and "Tax Payer" in ln:
            start_idx = i
            break
    csv_text = "\n".join(lines[start_idx:])
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return
    cols = {
        "parcel":    _find_col(reader.fieldnames, ["parcel number", "parcel"]),
        "tax_payer": _find_col(reader.fieldnames, ["tax payer", "owner name"]),
        "year":      _find_col(reader.fieldnames, ["certified year", "year"]),
        "vacant":    _find_col(reader.fieldnames, ["vacant"]),
        "amount":    _find_col(reader.fieldnames, ["amount", "delq amount"]),
    }
    if not cols["parcel"]:
        logger.warning(
            "Clark CSV: no 'Parcel Number' column in headers %r — "
            "schema may have changed",
            reader.fieldnames,
        )
        return
    for row in reader:
        parcel = _get(row, cols["parcel"])
        if not parcel:
            continue
        amount = _get(row, cols["amount"])
        # "$2,159.28" → "2159.28"
        amount_clean = re.sub(r"[^\d.\-]", "", amount)
        yield NoticeData(
            state="OH",
            notice_type="tax_delinquent",
            county=county,
            source_url=OHIO_ENDPOINTS[county]["portal"],
            parcel_id=parcel,
            owner_name=_get(row, cols["tax_payer"]),
            tax_delinquent_amount=amount_clean,
            tax_delinquent_years=_get(row, cols["year"]),
            # Stash vacant flag in raw_text — useful filter signal
            raw_text=(
                f"vacant: {_get(row, cols['vacant'])}"
                if cols["vacant"] else ""
            ),
        )


# ── Clermont — deliberate NotImplementedError ──────────────────────────


def fetch_clermont(*args, **kwargs) -> list[NoticeData]:
    """⚠ Clermont real-estate delinquent list is NOT online.

    The Clermont County Auditor publishes only the **Mobile Home**
    delinquent list as a PDF on their site. The full real-estate
    delinquent list is "published in newspaper" (per the Auditor's own
    page) and not available via any machine-readable feed.

    A *tax-foreclosure proxy* using Sheriff Sales + Auditor Forfeited
    Land list would be feasible, but those are post-delinquency sources
    and would skew the dataset semantically (people whose tax
    foreclosures have already proceeded vs. people who are still in
    the pre-foreclosure window where outreach is highest-converting).

    If we want that proxy in the future, ship it as a distinct
    ``notice_type="tax_foreclosure_proxy"`` — do NOT bolt it onto
    ``tax_delinquent``.

    Source: clermontauditor.org/taxes-rates/delinquent-real-estate-tax-list/
    """
    raise NotImplementedError(
        "Clermont real-estate tax-delinquent list is published in the "
        "newspaper, not online. Only the Mobile Home PDF is available "
        "and is excluded as semantically narrow. See "
        "reference/ohio_counties/Clermont.csv and README.md "
        "'Known Limitations'."
    )


# ── Dispatcher + filter ────────────────────────────────────────────────


# Business rule (decided 2026-06-19, supersedes the 2026-06-17 $8k-only
# rule): keep records that owe at least ``MIN_TAX_DELINQUENT_AMOUNT``
# AND have been delinquent for at least ``MIN_TAX_DELINQUENT_YEARS``.
# The AND rule targets compound high-equity-loss + chronic-neglect —
# both signals together correlate strongest with motivated-seller
# conversion.
#
# AMOUNT-FALLBACK: only Clark and Miami adapters currently emit the
# certified-year field. For Butler/Greene/Montgomery/Warren records
# (where ``tax_delinquent_years`` is blank), we can't evaluate the
# years half of the AND — so the predicate falls back to amount-only.
# That keeps those four counties shipping under the looser amount-only
# rule until their adapters can be extended with year extraction.
MIN_TAX_DELINQUENT_AMOUNT = Decimal("3000.00")
MIN_TAX_DELINQUENT_YEARS = 2


def _amount_meets_threshold(record: NoticeData, min_amount: Decimal) -> bool:
    """True when the record's tax_delinquent_amount is ≥ min_amount.

    Records with an empty or unparseable amount return False — that's
    not a 'yes vote' on the amount rule but the OR-filter still gives
    them a chance to qualify via the years rule.
    """
    raw = (record.tax_delinquent_amount or "").strip()
    if not raw:
        return False
    cleaned = re.sub(r"[^\d.\-]", "", raw)
    if not cleaned:
        return False
    try:
        return Decimal(cleaned) >= min_amount
    except InvalidOperation:
        return False


def _years_delinquent_at_least(
    record: NoticeData,
    min_years: int,
    today: "datetime | None" = None,
) -> bool:
    """True when the record has been delinquent ≥ min_years.

    The county adapters that emit ``tax_delinquent_years`` (Clark, Miami)
    store it as a 4-digit **certified year** — the tax-cycle year the
    parcel was first marked delinquent. We convert that to a duration:

        years_delinquent = current_year - certified_year

    A small-int (e.g. ``"3"``) is also accepted in case a future adapter
    decides to emit a pre-computed count instead.

    Records with an empty or unparseable years field return False.
    """
    from datetime import datetime  # local — avoids top-of-module import for tests
    raw = (record.tax_delinquent_years or "").strip()
    if not raw:
        return False
    # Extract the FIRST contiguous digit run — handles forms like
    # ``"TY2024"`` (prefix), ``"2024-Q3"`` (suffix), ``"  2018  "`` (padded).
    # Concatenating all digits ("2024-Q3" → "20243") would yield bogus values.
    m = re.search(r"\d+", raw)
    if not m:
        return False
    try:
        value = int(m.group(0))
    except ValueError:
        return False
    if value <= 0:
        return False
    cur_year = (today or datetime.now()).year
    # 4-digit year in a plausible range → certified-year semantics
    if 1900 <= value <= cur_year:
        return (cur_year - value) >= min_years
    # Small int → already-computed duration
    if 1 <= value <= 50:
        return value >= min_years
    return False


def _has_years_data(record: NoticeData) -> bool:
    """True when the record carries a parseable ``tax_delinquent_years``
    value.

    Used by ``_meets_filter`` to decide whether the years half of the
    AND rule applies. Counties whose adapters don't emit the field
    (Butler, Greene, Montgomery, Warren today) trip the amount-only
    fallback path; counties that do emit it (Clark, Miami) get the
    full AND.
    """
    raw = (record.tax_delinquent_years or "").strip()
    return bool(raw) and bool(re.search(r"\d+", raw))


def _meets_filter(
    record: NoticeData,
    min_amount: Decimal,
    min_years: int,
    today: "datetime | None" = None,
) -> bool:
    """True when the record passes the AND-with-amount-fallback rule.

    The amount rule is mandatory in BOTH paths. The years rule only
    applies when the record actually has a parseable years field; for
    records lacking that field (Butler/Greene/Montgomery/Warren), the
    function returns True as soon as the amount rule passes.
    """
    if not _amount_meets_threshold(record, min_amount):
        return False
    if not _has_years_data(record):
        # Amount-only fallback for adapters that don't emit years.
        return True
    return _years_delinquent_at_least(record, min_years, today)


# Backward-compat alias — the original predicate is amount-only and is
# referenced by ``orchestrate_upload.py`` and a few earlier-run tests.
# Keep both available so unrelated callers don't break.
_meets_min_amount = _amount_meets_threshold


_DISPATCH: dict[str, Callable[..., list[NoticeData]]] = {
    "butler": fetch_butler,
    "warren": fetch_warren,
    "montgomery": fetch_montgomery,
    "miami": fetch_miami,
    "greene": fetch_greene,
    "clark": fetch_clark,
    "clermont": fetch_clermont,
}


def fetch_ohio_tax_delinquent(
    county: str,
    *,
    ctx=None,
    client: httpx.Client | None = None,
    min_amount: Decimal | float | None = None,
    min_years: int | None = None,
    apply_filter: bool = True,
    today: "datetime | None" = None,
):
    """Dispatch to the per-county adapter + apply the OR-rule filter.

    Per-county adapters may return either ``list[NoticeData]`` (sync —
    Montgomery, stubs) or an awaitable yielding that (async — Butler,
    Clark, Greene, Miami, Warren via Playwright/HTTP). The caller in
    ``scraper.scrape_all`` checks ``inspect.isawaitable`` on the
    return value and awaits when needed.

    Filtering (applied AFTER the adapter returns its raw records):
    keep rows where ``tax_delinquent_amount >= min_amount`` OR the
    record has been delinquent ≥ ``min_years``. Defaults are
    ``MIN_TAX_DELINQUENT_AMOUNT`` ($3,000) and
    ``MIN_TAX_DELINQUENT_YEARS`` (2). Pass ``apply_filter=False`` to
    disable entirely; pass ``min_amount=0`` to weaken the amount rule
    (effectively years-only); pass ``min_years=1000`` to weaken the
    years rule (effectively amount-only).

    Counties that aren't yet wired up raise ``NotImplementedError``
    from their stub — callers should catch and log, not crash the
    whole run.
    """
    fn = _DISPATCH.get(county.strip().lower())
    if fn is None:
        raise ValueError(
            f"Unknown Ohio tax-delinquent county: {county!r}. "
            f"Supported: {sorted(_DISPATCH)}"
        )
    raw_result = fn(ctx=ctx, client=client)

    if not apply_filter:
        return raw_result
    amt = Decimal(str(
        min_amount if min_amount is not None
        else MIN_TAX_DELINQUENT_AMOUNT
    ))
    yrs = int(min_years if min_years is not None else MIN_TAX_DELINQUENT_YEARS)
    import inspect as _inspect
    if _inspect.isawaitable(raw_result):
        async def _filter_async():
            records = await raw_result
            return [r for r in records if _meets_filter(r, amt, yrs, today)]
        return _filter_async()
    return [r for r in raw_result if _meets_filter(r, amt, yrs, today)]
