# CLAUDE.md — SiftStack

Guidance for Claude when working in this repository. Deep reference material lives in
`docs/claude/` — read the relevant file before touching those subsystems:

- `docs/claude/datasift_ui_patterns.md` — REQUIRED before editing `datasift_uploader.py`, `extract_market_finder.py`, or any DataSift Playwright automation (styled-components, DnD, pointer interception, upload wizard, Market Finder)
- `docs/claude/ocr_and_probate_patterns.md` — REQUIRED before editing `photo_importer.py`, `image_utils.py`, `dropbox_watcher.py`, `obituary_enricher.py` (moire/OCR fixes, probate address lookup tiers, DOD sanity check, Dropbox layout)
- `docs/claude/skill_library.md` — the 13-skill REI library in `Skills for REI/improved/` + cross-skill invariants that must stay in sync with source code
- `docs/ohio_orchestrator.md` — cron wiring (launchd plists, all three slots)
- `docs/VA_DAILY_WORKFLOW.md` — the human VA's daily handoff procedure

## Project Overview

**SiftStack** — full-stack real estate investing operations platform built around
DataSift.ai (REISift) CRM. Lifecycle: data acquisition (web scrape, PDF OCR, courthouse
photo import, Dropbox polling) → 10-step enrichment (Smarty, Zillow, Knox Tax API,
obituary/heir research, Tracerfy skip trace, Trestle phone scoring) → deal analysis
(Two-Bucket ARV, rehab estimation, MAO) → DataSift upload + niche sequential marketing →
lead management (4 Pillars, STABM, deep prospecting).

**Markets:** Knox + Blount counties TN (tnpublicnotice.com scrape) and 7 SW Ohio counties
— Butler, Clark, Clermont, Greene, Miami, Montgomery, Warren — via the native OH pipeline
(`src/h3/` + `src/ohio_*.py`).

Also ships the **REI Skill Library**: 13 Claude Cowork `.skill`/`.plugin` files
distributed to the DataSift community (see `docs/claude/skill_library.md`).

## Commands

```bash
# Setup
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # then fill in credentials

# TN pipeline (tnpublicnotice.com)
python src/main.py daily                          # new notices since last run
python src/main.py historical                     # last 12 months
python src/main.py daily --split                  # separate CSV per county+type
python src/main.py daily --counties Knox --types foreclosure,probate
python src/main.py daily -v                       # verbose logging
python src/main.py daily --upload-datasift        # + upload + enrich + skip trace
python src/main.py daily --upload-datasift --no-enrich --no-skip-trace
python src/main.py daily --notify-slack           # run summary to Slack/Discord

# OH production entry point (drives all three cron slots — use this, NOT main.py)
python src/ohio_orchestrator.py daily             # Montgomery → H3 Montgomery Courthouse Data
python src/ohio_orchestrator.py weekly            # other 6 → H3 SW Ohio Courthouse Data
python src/ohio_orchestrator.py quarterly         # tax_delinquent, Montgomery only
python src/ohio_orchestrator.py daily --dry-run   # print plan, no scrape
python src/ohio_orchestrator.py daily --no-upload # scrape + CSV, no DataSift
python src/ohio_orchestrator.py weekly --headed   # visible browser (debug)

# Courthouse photo import
python src/main.py photo-import --folder ./photos --photo-county Knox --photo-type probate
python src/main.py dropbox-watch                  # auto-poll Dropbox for new photos

# DataSift preset/sequence management
python src/main.py manage-presets --discover | --add-sold-exclusion | --create-sold-sequence | --all
python src/main.py manage-sold --months-back 12   # SiftMap sold property tagging

# Tests
pytest tests/ -q                                  # unit tests (incl. 30 destination-list tests)
python test_datasift_upload.py                    # headed browser tests (test_*.py at root)
```

All source files are in `src/` and imports assume `src/` is the working directory. Run
from project root with `python src/main.py` or set `PYTHONPATH=src`.

## Guardrails

- **Never commit** `.env`, `input.json`, `*cookies.json`, or anything in `output/`/`logs/`
- **Cost-sensitive actions need explicit confirmation:** 2Captcha solves, Tracerfy skip
  trace, Trestle scoring, DataSift skip trace ($97/mo plan but still rate-relevant).
  Tracerfy/Trestle are env-gated (`TRACERFY_ENABLED=1`, `TRESTLE_ENABLED=1`) with daily
  cost caps (`TRACERFY_DAILY_COST_CAP_USD` default $5, `TRESTLE_DAILY_COST_CAP_USD`
  default $3). Don't enable them in tests.
- **Never run `--upload-datasift` from a test or experiment** — it writes to the
  production CRM. Use `--no-upload` / `--dry-run` for verification.
- **Destination-list separation is inviolable:** Montgomery records NEVER land in the SW
  Ohio list and vice versa. Enforced by `src/ohio_destination_lists.py` + 30 tests. Any
  change touching routing must keep those tests green.
- The **H3_Scrapers Apify Actor is archived** (`../H3_Scrapers/MIGRATED.md`) — do not
  deploy or modify it. GitHub Actions cron for OH is disabled (cannot pass reCAPTCHA v3);
  production runs on the local Mac via launchd with Slack failure notification.

## Architecture

**Data flows:**
- **Web scrape:** `main.py` → `scraper.py` → `captcha_solver.py` → `notice_parser.py` + `foreclosure_filter.py` → enrichment → CSV
- **PDF import:** `main.py` → `pdf_importer.py` (pypdfium2 → `image_utils.py` OCR) → enrichment → CSV
- **Photo import:** `main.py` → `photo_importer.py` (OpenCV → OCR → `llm_parser.py`) → enrichment → CSV
- **Dropbox watch:** `dropbox_watcher.py` → `photo_importer.py` → enrichment → CSV
- **Market Finder:** `extract_market_finder.py` → DataSift Market Finder (Playwright) → JSON → `generate_knox_report.py` → 7-sheet Excel

**Key modules (`src/`):**
- `main.py` — CLI entry point; filters saved searches by county/type, orchestrates scrape → dedup → export
- `scraper.py` — Playwright automation for tnpublicnotice.com; session cookies, Smart Search dropdown (ASP.NET postback), pagination, `last_run.json` daily state. Dispatcher `scrape_all()` routes saved searches into 5 buckets (TN + 4 OH source types)
- `captcha_solver.py` — reCAPTCHA v2 via 2Captcha API; primary bottleneck (~10-30s/notice), 3 retries
- `notice_parser.py` — regex extraction from free-text notice bodies; defines the `NoticeData` dataclass used throughout
- `foreclosure_filter.py` — keeps only real trustee sales (`INCLUDE_PHRASES`/`EXCLUDE_PHRASES`)
- `data_formatter.py` — dedup by address (keeps most recent) → Sift upload CSV
- `config.py` — credentials (`.env`), selectors, `SAVED_SEARCHES`, rate limits, thresholds
- `image_utils.py` — shared OCR utilities (`fix_rotation()`, `ocr_page()`)
- `photo_importer.py` / `dropbox_watcher.py` — courthouse photo pipeline (see docs/claude/ocr_and_probate_patterns.md)
- `report_generator.py` — per-record PDF deep prospecting reports (reportlab) → `output/reports/`
- `extract_market_finder.py` / `market_analyzer.py` — Market Finder extraction + 6-factor ZIP scoring (Distress 30%, Value 20%, Equity 15%, Tax Delinq 15%, Competition 10%, DOM 10%)
- `datasift_formatter.py` / `datasift_uploader.py` — NoticeData → 41-column CSV; Playwright upload + enrich + skip trace + preset/sequence/SiftMap management
- `drive_uploader.py` — Google Drive upload via service account

## Ohio Pipeline (native — June 2026)

All 7 SW Ohio counties run end-to-end inside SiftStack: foreclosure + probate +
tax_delinquent + sheriff_sale.

### Three production cron slots (mandatory destination-list separation)

| When | Mode | Source types | Counties | DataSift list |
|---|---|---|---|---|
| Daily 6:00 AM ET | `daily` | foreclosure + probate + sheriff_sale | Montgomery | **H3 Montgomery Courthouse Data** |
| Monday 6:00 AM ET | `weekly` | foreclosure + probate + sheriff_sale | other 6 | **H3 SW Ohio Courthouse Data** |
| Every 3 months | `quarterly` | tax_delinquent (+ parcel→address enrichment) | Montgomery only | H3 Montgomery Courthouse Data |

Routing enforced by `src/ohio_destination_lists.py` (30 tests). Cron wiring:
`docs/ohio_orchestrator.md`. Two-pass mode (`TWO_PASS_MODE=1`) defers Tracerfy/Trestle to
Pass 2 so Tracerfy only runs on records DataSift couldn't find. The `sift-enrich` Cowork
skill covers the operator-run enrichment step on raw daily CSVs.

### Module map

| Source type | Adapter module | Notes |
|---|---|---|
| foreclosure | `src/ohio_foreclosure_scrapers.py` | 3 integration paths (Montgomery / equivant×5 / Warren) |
| probate | `src/ohio_probate_scrapers.py` | single factory pattern, all 7 counties |
| tax_delinquent | `src/ohio_tax_delinquent_scrapers.py` | 6 counties (Clermont stub). $3k AND ≥2yr filter |
| sheriff_sale | `src/ohio_sheriff_sale_scrapers.py` | shared RealForeclose PREVIEW URL (no login) |

Each module exposes `fetch_ohio_<source>(county, ctx=None, **kw)` with a dual-return
contract: sync `list[NoticeData]` on the `override_*=` fixture path; coroutine on the
live Playwright path.

**Integration layer:** `src/h3/integration.py` — 3 pure functions, zero Apify deps:
`integrate_montgomery_foreclosure` (multi-AJAX tab + CIS PDF + service-tab fallback),
`integrate_equivant_foreclosure` (shared CourtView path for Butler/Clark/Clermont/Greene/Miami),
`integrate_warren_foreclosure` (BenchmarkCP + Auditor parcel + PJR/COMPLAINT PDF OCR fallback).

**NoticeData bridge:** `src/h3/notice_data_bridge.py` converts H3 `CaseRecord`/`ProbateRecord`
→ `NoticeData`. Probate owner mapping: fiduciary → `owner_name` (the actual contact),
decedent → `decedent_name`; pre-populates `decision_maker_*` fields.

**Verification:** replay scripts in `scripts/verify_*_replay.py` confirm byte-for-byte
equivalence against Apify KV-store baselines.

## Site-Specific Details (TN)

tnpublicnotice.com is **ASP.NET WebForms** — all navigation uses `__doPostBack()` with
ViewState; session IDs are embedded in URL paths. **reCAPTCHA v2 on every notice detail
page** (not on login/search/results); sitekey hardcoded in `config.py`.

## Saved Searches

Defined in `config.py` as `SAVED_SEARCHES`:
- **TN** — dropdown names on the tnpublicnotice.com Smart Search dashboard: Knox & Blount × (Foreclosure V2, Tax Sale V2, Tax Delinquent V2, Probate V2)
- **OH** — sentinel names `<source>:<county_lower>` (`ohio_foreclosure:`, `ohio_probate:`, `ohio_auditor:` → tax_delinquent, `ohio_sheriff:` → sheriff_sale). 4 sources × 7 counties, minus the Clermont tax_delinquent stub.

For OH production always use `ohio_orchestrator.py`, not `main.py daily` — the
orchestrator enforces the destination-list split.

## Key Domain Rules

- **Foreclosure filtering is critical.** Not all notices from "Foreclosure" saved searches are actual foreclosures — only trustee-sale language counts (`foreclosure_filter.py`)
- **Probate owner_name = Personal Representative/Executor/Administrator** — never the deceased
- **Owner names** in foreclosure notices typically appear after "executed by" in deed-of-trust language
- **Rate limiting:** 2-3s random delays, 3 retries per page
- **Address dedup:** same property appears in multiple notices; keep most recent
- **Notice types (8):** foreclosure, tax_sale, tax_delinquent, sheriff_sale, probate, eviction, code_violation, divorce

## DataSift.ai (REISift) Integration

**No REST API** — everything is Playwright automation of the web UI.
Domain: `app.reisift.io` (NOT app.datasift.ai); API host `apiv2.reisift.io`.
Env: `DATASIFT_EMAIL`, `DATASIFT_PASSWORD`, `SLACK_WEBHOOK_URL`.

- **CSV: 41 columns** — 11 core auto-mapped (address/owner/mailing/tags), Lists + Notes, 13 built-in fields, 15 custom fields (Notice Type → Source URL). See `datasift_formatter.py`
- **Every record gets the "Courthouse Data" tag** (first-to-market signal) + notice_type, county, YYYY-MM, deceased/living, DM confidence, photo_import tags
- **Lists column** maps notice_type → DataSift list name; DataSift auto-creates lists
- **Contact logic:** deceased owners → decision maker + DM mailing address; living owners → property owner
- **Post-upload (both ON by default with `--upload-datasift`):** Enrich Property Information ("Enrich Owners"/"Swap Owners" stay OFF — protects PR/DM mapping) + Skip Trace (up to 5 phones/owner, auto-tag `skip_traced_YYYY-MM`)
- **Niche sequential:** 21 filter presets in 2 folders guide records through SMS → Call → Mail → Deep Prospecting; all exclude Sold; "Sold Property Cleanup" sequence auto-fires on Sold tag

UI automation is quirky — **read `docs/claude/datasift_ui_patterns.md` before writing any
DataSift Playwright code.**

## Output

CSVs land in `output/` (gitignored); logs in `logs/` with timestamped filenames.
Sift columns: `date_added, address, city, state, zip, owner_name, notice_type, county, source_url`.

## Apify (TN legacy)

The TN scrape can still run as an Apify Actor (`.actor/actor.json`,
`input_schema.json`, Dockerfile based on `apify/actor-python-playwright:3.12`; when
`APIFY_IS_AT_HOME`/`APIFY_TOKEN` is set, `main.py` uses the Actor SDK). The OH pipeline
does NOT use Apify. `apify run --purge` locally, `apify push` to deploy.
