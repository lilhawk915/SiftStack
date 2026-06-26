"""OnBase probate PDF capture + Claude Vision extraction.

Captures court-filed PDFs from Montgomery County OnBase for probate cases
and extracts fiduciary/attorney/decedent contact fields via Claude Vision.

Phase 1 of the SiftStack OnBase enrichment spec
(SiftStack_OnBase_PDF_Build_Spec.md).

═══════════════════════════════════════════════════════════════════════
EMPIRICAL EVIDENCE (verified 2026-06-25, see Phase 1 test):
═══════════════════════════════════════════════════════════════════════

  * pdfpop.aspx URLs in docket HTML CANNOT be fetched directly — Hyland
    viewer serves an 8 KB HTML wrapper, not the PDF.

  * The real PDF arrives via PdfHandler.ashx?docId=X&guid={session}&
    fileTypeId=16, loaded by JS inside a nested iframe.

  * Capture mechanism that works: Playwright's `context.route("**/*")`
    interceptor. The response-event handler races against Chromium's
    cache eviction (Protocol error "No resource with given identifier
    found"); route.fetch() captures bytes synchronously before eviction.

  * Probate PDFs are image-only — pypdfium2 text extraction yields ~83
    chars for a 2-page filing. Vision OCR is mandatory.

  * Test capture: case 2026EST00200 → 498 KB real PDF saved cleanly.

═══════════════════════════════════════════════════════════════════════

Integration points (NOT wired here — caller's responsibility):
  * src/h3/scrapers/mcohio_probate.py — adds OnBase URL capture during
    docket-entries pass.
  * src/h3/output_writers/probate_format.py — ProbateRecord already has
    fiduciary_phone/fiduciary_email fields; this module populates them.
  * src/ohio_orchestrator.py — calls enrich_probate_records() on the
    notice list after scraping but before CSV write.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Claude Vision config ──────────────────────────────────────────────
# Spec recommended claude-3-5-sonnet-latest, but Anthropic retired that
# alias before this build landed (404: "model: claude-3-5-sonnet-latest"
# during 2026-06-25 gate test). The current Sonnet snapshot is
# claude-sonnet-4-6 (Claude 4.6 family), which supports PDF document
# input identically and at the same Sonnet-tier $3/$15 per-M-token
# pricing (input/output respectively). PDFs count toward input tokens
# (server-side encodes ~1750 tokens/page) so a 2-page probate filing
# is ~3500 input tokens → ~$0.01/call.
VISION_MODEL = "claude-sonnet-4-6"
COST_INPUT_PER_M_USD = 3.0
COST_OUTPUT_PER_M_USD = 15.0

# Defaults — overridable via env vars at runtime
DEFAULT_DAILY_COST_CAP_USD = 10.0
DEFAULT_MAX_PDFS_PER_CASE = 5

# Forms most likely to contain phone/email (empirically — per the two
# user-supplied screenshots in the build spec showing Caughran on
# Notice of Probate of Will + Notice of Deposit of Original Will).
PRIORITY_FORM_KEYWORDS = (
    "notice of probate of will",
    "notice of deposit of original will",
    "application to administer",
    "application for authority",
    "fiduciary's acceptance",
    "fiduciary acceptance",
    "appointment of fiduciary",
)


# ── Data carrier ──────────────────────────────────────────────────────
@dataclass
class ProbateExtraction:
    """Aggregated fields across all PDFs extracted for one probate case."""
    case_number: str = ""

    # Contact targets — first-non-empty across PDFs wins
    fiduciary_phone: str = ""
    fiduciary_email: str = ""
    attorney_phone: str = ""
    attorney_email: str = ""
    decedent_dod_iso: str = ""

    # Diagnostics
    source_pdfs: list[str] = field(default_factory=list)
    forms_processed: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


# ── Vision prompt ─────────────────────────────────────────────────────
VISION_PROMPT = """You are extracting structured data from a Montgomery County
Probate Court filing. Examine every page of the document and return ONLY valid
JSON matching this exact schema (no markdown code fences, no commentary, no
explanation before or after the JSON):

{
  "form_type": "Notice of Probate of Will|Notice of Deposit of Original Will|Application to Administer Estate|Application for Authority to Administer Estate|Fiduciary's Acceptance|other",
  "case_number": "...",
  "decedent_name": "...",
  "decedent_dod": "YYYY-MM-DD or empty",
  "fiduciary_name": "...",
  "fiduciary_address": "full address with street, city, state, ZIP",
  "fiduciary_phone": "raw phone with formatting from the form, or empty",
  "fiduciary_email": "email or empty",
  "attorney_name": "...",
  "attorney_address": "...",
  "attorney_phone": "...",
  "attorney_email": "...",
  "attorney_ohio_id": "..."
}

Rules:
1. Use empty string "" for any field not visible on the form.
2. Do NOT infer or guess — only extract what you can read directly.
3. Phone numbers may appear near labels like "Phone", "Tel.",
   "Telephone", or simply be listed under the fiduciary's or attorney's
   address block. Capture them with the formatting shown.
4. Date of death: convert to ISO YYYY-MM-DD. Examples:
   "January 5, 2026" → "2026-01-05", "1/5/2026" → "2026-01-05".
5. If the form has multiple fiduciaries, return only the FIRST listed.
"""


# ── Stage 1: OnBase PDF capture ───────────────────────────────────────
async def capture_onbase_pdf(
    pdfpop_url: str,
    output_dir: Path,
    filename: str,
    headless: bool = True,
    timeout_ms: int = 30000,
    wait_after_load_ms: int = 10000,
) -> Optional[Path]:
    """Capture the real PDF bytes behind a Montgomery OnBase pdfpop URL.

    Returns the saved PDF path or None on failure (logged, never raises).

    If the target cache path already exists and is a valid PDF
    (starts with "%PDF" magic + larger than the 1 KB OnBase-error-
    page size), reuse it. Captures are slow (~14 s each, browser
    launch + render wait); when re-running the orchestrator against
    the same case the captured PDFs don't change.
    """
    from playwright.async_api import async_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / filename

    if out_path.exists() and out_path.stat().st_size > 1000:
        with out_path.open("rb") as f:
            head = f.read(4)
        if head == b"%PDF":
            logger.info("OnBase cache hit: %d bytes ← %s",
                        out_path.stat().st_size, out_path.name)
            return out_path

    captured: list[tuple[str, bytes]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});"
            )

            async def handle(route, request):
                # Catches PDF stream before Chromium's cache evicts it.
                try:
                    response = await route.fetch()
                    body = await response.body()
                    ct = (response.headers.get("content-type") or "").lower()
                    if "application/pdf" in ct and body.startswith(b"%PDF"):
                        captured.append((request.url, body))
                    await route.fulfill(response=response, body=body)
                except Exception:
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            await ctx.route("**/*", handle)
            page = await ctx.new_page()
            try:
                await page.goto(
                    pdfpop_url, wait_until="networkidle", timeout=timeout_ms,
                )
                await page.wait_for_timeout(wait_after_load_ms)
            except Exception as e:
                logger.warning("OnBase capture nav failed: %s (%s)",
                               pdfpop_url[:100], e)
        finally:
            await browser.close()

    if not captured:
        logger.warning("OnBase capture: no PDF intercepted at %s",
                       pdfpop_url[:120])
        return None

    # Multi-page documents (richer content) tend to be larger
    _url, body = max(captured, key=lambda x: len(x[1]))
    out_path.write_bytes(body)
    logger.info("OnBase capture: %d bytes → %s", len(body), out_path.name)
    return out_path


# ── Stage 2: Claude Vision extraction ─────────────────────────────────
def extract_probate_fields(
    pdf_path: Path,
    model: str = VISION_MODEL,
    max_tokens: int = 1024,
) -> tuple[dict, float]:
    """Extract probate fields from a captured PDF via Claude Vision.

    Returns (parsed_dict, cost_usd). On error, parsed_dict has an
    "error" key and cost is 0.

    Synchronous — callers should wrap in asyncio.to_thread() when used
    from async code so the Vision call doesn't block the event loop.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        return ({"error": "anthropic SDK not installed"}, 0.0)

    # Defensive .env fallback. The launchd plist and manual shell
    # runs don't always have ANTHROPIC_API_KEY exported (initial
    # 2026-06-25 daily run failed with TypeError "Could not resolve
    # authentication method" because of exactly this). Keep the key
    # canonical in .env and load it here when missing from os.environ
    # — avoids duplicating the secret into the plist.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        for candidate in (
            Path.cwd() / ".env",
            Path(__file__).resolve().parent.parent / ".env",
        ):
            if candidate.is_file():
                for line in candidate.read_text().splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        os.environ["ANTHROPIC_API_KEY"] = (
                            line.split("=", 1)[1].strip().strip("\"'")
                        )
                        break
                break

    pdf_bytes = pdf_path.read_bytes()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    # max_retries=5 (SDK default 2) + 3 explicit attempts with
    # exponential backoff. The retry hardening is overkill for a
    # straightforward auth issue but cheap and useful for the actual
    # transient cases (429, 503) that will inevitably surface as
    # volume scales.
    client = Anthropic(max_retries=5, timeout=120.0)

    last_err: Exception | None = None
    for attempt in range(3):  # 3 attempts on top of SDK-level retries
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "document",
                         "source": {"type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_b64}},
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                }],
            )
            last_err = None
            break
        except Exception as e:
            last_err = e
            # Exponential backoff: 2s, 6s, 18s. The orchestrator's
            # earlier failure mode showed everything-fails-then-
            # everything-works behavior at ~5-min intervals, so we
            # extend total wait across attempts to ~30 sec.
            import time
            time.sleep(2 * (3 ** attempt))
    if last_err is not None:
        return (
            {"error": f"vision call failed after 3 attempts: "
                      f"{type(last_err).__name__}: {last_err}"},
            0.0,
        )

    text = response.content[0].text.strip()

    # Sonnet sometimes wraps JSON in ```json ... ``` fences despite
    # the prompt's "no fences" instruction. Strip them.
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            candidate = parts[1]
            if candidate.startswith("json"):
                candidate = candidate[4:]
            text = candidate.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return ({"error": f"json parse failed: {e}",
                 "raw": text[:500]}, 0.0)

    cost = (
        response.usage.input_tokens / 1_000_000 * COST_INPUT_PER_M_USD
        + response.usage.output_tokens / 1_000_000 * COST_OUTPUT_PER_M_USD
    )
    return (parsed, cost)


# ── Stage 3: Per-case orchestration ───────────────────────────────────
def _is_priority_form(description: str) -> bool:
    """Match docket-entry description against priority form keywords."""
    d = (description or "").lower()
    return any(kw in d for kw in PRIORITY_FORM_KEYWORDS)


def _prioritize_docket_entries(
    entries: list[dict], max_pdfs: int,
) -> list[dict]:
    """Order entries so priority forms come first; cap at max_pdfs.

    Each entry should have at least 'description' and 'pdf_url' keys.
    """
    priority = [e for e in entries
                if _is_priority_form(e.get("description", ""))
                and e.get("pdf_url")]
    rest = [e for e in entries
            if not _is_priority_form(e.get("description", ""))
            and e.get("pdf_url")]
    return (priority + rest)[:max_pdfs]


def _merge_extraction(agg: ProbateExtraction, parsed: dict) -> None:
    """Merge one PDF's extracted fields into the aggregate.

    First-non-empty-wins, since priority forms run first.
    """
    pairs = (
        ("fiduciary_phone", "fiduciary_phone"),
        ("fiduciary_email", "fiduciary_email"),
        ("attorney_phone", "attorney_phone"),
        ("attorney_email", "attorney_email"),
        ("decedent_dod", "decedent_dod_iso"),
    )
    for src_key, attr in pairs:
        val = (parsed.get(src_key) or "").strip()
        if val and not getattr(agg, attr, ""):
            setattr(agg, attr, val)


async def enrich_case_with_onbase(
    case_number: str,
    docket_entries: list[dict],
    pdf_cache_dir: Path,
    daily_cost_remaining_usd: float,
    max_pdfs_per_case: int = DEFAULT_MAX_PDFS_PER_CASE,
) -> ProbateExtraction:
    """Process one probate case's OnBase PDFs end-to-end.

    docket_entries: list of dicts with keys 'description' and 'pdf_url'.
                    pdf_url is a pdfpop.aspx URL from the docket HTML.
    pdf_cache_dir:  parent directory for the case's PDF subfolder. Each
                    case gets its own subfolder named case_number, so
                    PDFs can be inspected after the run.
    daily_cost_remaining_usd: stop processing this case if the next
                              Vision call would push past this budget.

    Returns a ProbateExtraction with merged fields + cost + errors.
    Early-exits as soon as both fiduciary_phone and attorney_phone
    are populated (no need to keep paying for additional Vision calls).
    """
    agg = ProbateExtraction(case_number=case_number)
    prioritized = _prioritize_docket_entries(docket_entries, max_pdfs_per_case)

    if not prioritized:
        logger.info("OnBase: %s has no docket entries with pdf_url",
                    case_number)
        return agg

    case_dir = pdf_cache_dir / case_number

    for i, entry in enumerate(prioritized, 1):
        pdfpop_url = entry.get("pdf_url", "")
        description = entry.get("description", f"doc_{i}")
        if not pdfpop_url:
            continue

        # Cost guardrail — stop if next call would exceed remaining cap
        if agg.cost_usd >= daily_cost_remaining_usd:
            logger.warning("OnBase: stopping %s — daily cost cap reached "
                           "($%.4f used of $%.2f remaining)",
                           case_number, agg.cost_usd,
                           daily_cost_remaining_usd)
            agg.errors.append("daily_cost_cap_reached")
            break

        # Capture PDF
        safe_desc = re.sub(r"[^\w\-]+", "_", description)[:40]
        filename = f"{i:02d}_{safe_desc}.pdf"
        try:
            pdf_path = await capture_onbase_pdf(
                pdfpop_url, case_dir, filename,
            )
        except Exception as e:
            agg.errors.append(f"capture_failed({description}): {e}")
            continue

        if not pdf_path:
            agg.errors.append(f"no_pdf_captured({description})")
            continue

        agg.source_pdfs.append(str(pdf_path))
        agg.forms_processed.append(description)

        # Extract via Vision (sync — wrap in to_thread to keep loop free)
        parsed, cost = await asyncio.to_thread(
            extract_probate_fields, pdf_path,
        )
        agg.cost_usd += cost

        if "error" in parsed:
            agg.errors.append(
                f"vision_error({description}): {parsed['error']}"
            )
            continue

        _merge_extraction(agg, parsed)

        # Early exit: both phone targets populated
        if agg.fiduciary_phone and agg.attorney_phone:
            logger.info(
                "OnBase: %s fully enriched after %d PDF(s), cost $%.4f",
                case_number, i, agg.cost_usd,
            )
            break

    return agg


# ── Stage 4: Batch orchestrator ───────────────────────────────────────
async def enrich_probate_records(
    cases: list[dict[str, Any]],
    pdf_cache_dir: Path,
    daily_cost_cap_usd: float = DEFAULT_DAILY_COST_CAP_USD,
    max_pdfs_per_case: int = DEFAULT_MAX_PDFS_PER_CASE,
    concurrency: int = 1,
) -> dict[str, ProbateExtraction]:
    """Run OnBase enrichment for a batch of probate cases.

    cases: list of dicts, each with at least:
        - case_number (str)
        - docket_entries (list[dict] with 'description' and 'pdf_url')

    pdf_cache_dir: parent directory where per-case PDFs are cached
                   (e.g. ~/Desktop/SiftStack/onbase_cache/).

    daily_cost_cap_usd: hard ceiling — once total spend across all
                       cases passes this, remaining cases are
                       skipped with a 'daily_cost_cap_reached' error.

    concurrency: number of cases to process in parallel. Default 1
                 (sequential) — bump to 3-5 once empirical testing
                 confirms the OnBase site doesn't rate-limit.

    Returns {case_number: ProbateExtraction}.
    """
    pdf_cache_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, ProbateExtraction] = {}

    # Allow env-var override for ops tuning without code changes
    env_cap = os.environ.get("ONBASE_DAILY_COST_CAP_USD")
    if env_cap:
        try:
            daily_cost_cap_usd = float(env_cap)
        except ValueError:
            logger.warning("Invalid ONBASE_DAILY_COST_CAP_USD=%r — using "
                           "default $%.2f", env_cap, daily_cost_cap_usd)

    total_spent = 0.0
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _process_one(case: dict[str, Any]) -> tuple[str, ProbateExtraction]:
        nonlocal total_spent
        case_no = case.get("case_number", "")
        async with sem:
            # Compute remaining budget at the moment of dispatch — this
            # is approximate under concurrency > 1 (other parallel calls
            # may not have settled yet) but conservative enough.
            remaining = max(0.0, daily_cost_cap_usd - total_spent)
            if remaining <= 0:
                ex = ProbateExtraction(case_number=case_no)
                ex.errors.append("daily_cost_cap_reached")
                return (case_no, ex)

            ex = await enrich_case_with_onbase(
                case_number=case_no,
                docket_entries=case.get("docket_entries", []),
                pdf_cache_dir=pdf_cache_dir,
                daily_cost_remaining_usd=remaining,
                max_pdfs_per_case=max_pdfs_per_case,
            )
            total_spent += ex.cost_usd
            return (case_no, ex)

    tasks = [asyncio.create_task(_process_one(c)) for c in cases]
    for fut in asyncio.as_completed(tasks):
        case_no, ex = await fut
        results[case_no] = ex
        logger.info(
            "OnBase enriched %s — fid_phone=%r attn_phone=%r "
            "pdfs=%d cost=$%.4f errors=%d",
            case_no, ex.fiduciary_phone, ex.attorney_phone,
            len(ex.source_pdfs), ex.cost_usd, len(ex.errors),
        )
        # Surface the first 2 error strings at WARNING level so
        # transient Vision failures aren't silent in the log. Without
        # this, the 2026-06-25 batch failure (every Vision call
        # returned cost=$0 with errors stashed) was indistinguishable
        # from "fields just not present" in the run log.
        for err in ex.errors[:2]:
            logger.warning("  OnBase %s error: %s", case_no, err)

    logger.info(
        "OnBase batch complete: %d cases, total $%.4f / $%.2f cap",
        len(results), total_spent, daily_cost_cap_usd,
    )
    return results


# ── CLI entry point (for empirical tests + one-off case enrichment) ───
async def _cli_main(args: list[str]) -> int:
    """Quick CLI: `python -m onbase_probate_pdf CASE_NUMBER PDFPOP_URL [...]`

    Captures + extracts each URL, prints the merged ProbateExtraction.
    Useful for spot-testing one case without running the full daily.
    """
    if len(args) < 2:
        print("Usage: python -m onbase_probate_pdf CASE_NUMBER URL [URL ...]")
        return 2

    case_no, *urls = args
    # Synthesize docket_entries from positional URLs
    entries = [
        {"description": f"cli_doc_{i}", "pdf_url": u}
        for i, u in enumerate(urls, 1)
    ]
    cache = Path("/tmp/onbase_cli_cache")
    ex = await enrich_case_with_onbase(
        case_number=case_no,
        docket_entries=entries,
        pdf_cache_dir=cache,
        daily_cost_remaining_usd=DEFAULT_DAILY_COST_CAP_USD,
    )
    print(json.dumps({
        "case_number": ex.case_number,
        "fiduciary_phone": ex.fiduciary_phone,
        "fiduciary_email": ex.fiduciary_email,
        "attorney_phone": ex.attorney_phone,
        "attorney_email": ex.attorney_email,
        "decedent_dod_iso": ex.decedent_dod_iso,
        "forms_processed": ex.forms_processed,
        "cost_usd": round(ex.cost_usd, 4),
        "errors": ex.errors,
    }, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sys.exit(asyncio.run(_cli_main(sys.argv[1:])))
