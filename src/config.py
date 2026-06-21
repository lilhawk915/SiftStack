"""Configuration for SiftStack — full-stack REI operations platform."""

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = PROJECT_ROOT / "last_run.json"
SEEN_IDS_FILE = PROJECT_ROOT / "seen_ids.json"
SEEN_IDS_PRUNE_DAYS = 90
# Notices that exhausted all CAPTCHA retries during scraping.
# Persisted so the next run's summary can surface them instead of
# silently dropping — and a future retry pass can prioritize them.
CAPTCHA_FAILED_IDS_FILE = PROJECT_ROOT / "captcha_failed_ids.json"
CAPTCHA_FAILED_PRUNE_DAYS = 14
COOKIES_FILE = PROJECT_ROOT / "cookies.json"
DROPBOX_STATE_FILE = PROJECT_ROOT / "dropbox_state.json"
PHOTO_STATE_FILE = PROJECT_ROOT / "photo_state.json"

# ── Dropbox Watcher ────────────────────────────────────────────────────
DROPBOX_POLL_INTERVAL = int(os.getenv("DROPBOX_POLL_INTERVAL", "900"))  # seconds (default 15 min)
DROPBOX_ROOT_FOLDER = os.getenv("DROPBOX_ROOT_FOLDER", "")  # root folder path in Dropbox, e.g. "/TN Public Notice"
DROPBOX_STORAGE_WARN_PERCENT = 80  # warn when storage usage exceeds this %

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ── Credentials ────────────────────────────────────────────────────────
TNPN_EMAIL = os.getenv("TNPN_EMAIL", "")
TNPN_PASSWORD = os.getenv("TNPN_PASSWORD", "")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")  # 2Captcha API key
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # Claude Haiku for LLM parsing
SMARTY_AUTH_ID = os.getenv("SMARTY_AUTH_ID", "")        # Smarty address standardization
SMARTY_AUTH_TOKEN = os.getenv("SMARTY_AUTH_TOKEN", "")
OPENWEBNINJA_API_KEY = os.getenv("OPENWEBNINJA_API_KEY", "")  # Zillow property enrichment
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")              # Serper.dev Google Search API
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")        # Firecrawl JS-rendered scraping
TRACERFY_API_KEY = os.getenv("TRACERFY_API_KEY", "")          # Tracerfy skip tracing
TRESTLE_API_KEY = os.getenv("TRESTLE_API_KEY", "")            # Trestle phone validation
DATASIFT_EMAIL = os.getenv("DATASIFT_EMAIL", "")              # DataSift.ai login
DATASIFT_PASSWORD = os.getenv("DATASIFT_PASSWORD", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")        # Slack/Discord webhook
ANCESTRY_EMAIL = os.getenv("ANCESTRY_EMAIL", "")              # Ancestry.com login
ANCESTRY_PASSWORD = os.getenv("ANCESTRY_PASSWORD", "")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "")            # Dropbox OAuth2 app key
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")

# ── LLM Backend ──────────────────────────────────────────────────────
LLM_BACKEND = os.getenv("LLM_BACKEND", "anthropic")           # "anthropic", "ollama", or "openrouter"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")  # Anthropic model name
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")        # Local Ollama model
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1/")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")       # OpenRouter API key
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# ── Site URLs ──────────────────────────────────────────────────────────
BASE_URL = "https://www.tnpublicnotice.com"
LOGIN_URL = f"{BASE_URL}/authenticate.aspx"
SMART_SEARCH_URL = f"{BASE_URL}/Smartsearch/Default.aspx"

# ── ASP.NET Selectors ─────────────────────────────────────────────────
# Login form
SEL_LOGIN_EMAIL = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtEmailAddress"
SEL_LOGIN_PASSWORD = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtPassword"
SEL_LOGIN_SUBMIT = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_btnAuth"

# Smart Search dashboard
SEL_SAVED_SEARCHES_DROPDOWN = "#ctl00_ContentPlaceHolder1_as1_ddlSavedSearches"
SEL_PER_PAGE_DROPDOWN = 'select[name$="ddlPerPage"]'

# Search results (authenticated grid)
SEL_RESULTS_GRID = "#ctl00_ContentPlaceHolder1_WSExtendedGrid1_GridView1"
SEL_VIEW_BUTTON_PATTERN = "input[name$='btnView']"
SEL_NEXT_PAGE_BUTTON = "input[title='Next page']"
SEL_PAGE_INFO = "td:has-text('Page ')"

# Notice detail page
SEL_CAPTCHA_IFRAME = "iframe[src*='recaptcha']"
SEL_VIEW_NOTICE_BUTTON = "#ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_btnViewNotice"
RECAPTCHA_SITEKEY = "6LdtSg8sAAAAADTdRyZxJ2R2sS82pKALNMvMqSyL"

# ── Rate Limiting ──────────────────────────────────────────────────────
REQUEST_DELAY_MIN = 2.0  # seconds between requests
REQUEST_DELAY_MAX = 3.0
MAX_RETRIES = 3
RESULTS_PER_PAGE = 50  # max the site allows

# ── Image Processing ───────────────────────────────────────────────────
BLUR_THRESHOLD = int(os.getenv("BLUR_THRESHOLD", "100"))   # Laplacian variance; below = rejected as blurry
TESSERACT_PSM_PDF = 3    # fully automatic — best for PDF tax sale tables
TESSERACT_PSM_PHOTO = 4  # assume single column of variable-size text — best for terminal screen photos

# ── Notice Types ───────────────────────────────────────────────────────
NOTICE_TYPES = ["foreclosure", "probate", "tax_delinquent"]


@dataclass
class SavedSearch:
    """Represents a saved search on tnpublicnotice.com OR a non-TN data source.

    For tnpublicnotice.com entries, ``saved_search_name`` must match the
    exact text of the dropdown option on the Smart Search dashboard.

    For non-TN data sources (Ohio county auditors, etc.), ``saved_search_name``
    is a sentinel of the form ``"ohio_auditor:<county_lower>"`` that the
    dispatcher in ``scraper.scrape_all()`` recognizes and routes to the
    appropriate adapter (e.g. ``ohio_tax_delinquent_scrapers.fetch_butler``).
    """
    county: str
    notice_type: str  # One of NOTICE_TYPES
    saved_search_name: str  # Exact dropdown name OR sentinel like "ohio_auditor:butler"


# ── Saved Searches ─────────────────────────────────────────────────────
# tnpublicnotice.com entries — names must match the dropdown exactly.
# Ohio entries use a sentinel `saved_search_name` of the form
# ``"ohio_auditor:<county_lower>"`` which the dispatcher recognizes and
# routes to ``ohio_tax_delinquent_scrapers.py``.
SAVED_SEARCHES: list[SavedSearch] = [
    # ── TN — tnpublicnotice.com ─────────────────────────────────────
    SavedSearch("Knox", "foreclosure", "Foreclosure V2 Knox"),
    SavedSearch("Blount", "foreclosure", "Foreclosure V2 Blount"),
    # ── OH — county auditor tax delinquent (NEW) ────────────────────
    # Best-source endpoint per reference/ohio_counties/Summary.csv.
    # Butler is the cleanest (direct CSV download); rolling out first.
    # See reference/ohio_counties/<County>.csv for per-county detail.
    SavedSearch("Butler",     "tax_delinquent", "ohio_auditor:butler"),
    SavedSearch("Clark",      "tax_delinquent", "ohio_auditor:clark"),
    SavedSearch("Clermont",   "tax_delinquent", "ohio_auditor:clermont"),
    SavedSearch("Greene",     "tax_delinquent", "ohio_auditor:greene"),
    SavedSearch("Miami",      "tax_delinquent", "ohio_auditor:miami"),
    SavedSearch("Montgomery", "tax_delinquent", "ohio_auditor:montgomery"),
    SavedSearch("Warren",     "tax_delinquent", "ohio_auditor:warren"),
    # ── OH — sheriff-sale auctions (RealForeclose, all 7 counties) ──
    # Sentinel ``ohio_sheriff:<county>`` is routed by ``scraper.scrape_all``
    # to ``ohio_sheriff_sale_scrapers.fetch_ohio_sheriff_sale``. Source
    # is the shared Realauction.com PREVIEW page (public, no login).
    SavedSearch("Butler",     "sheriff_sale", "ohio_sheriff:butler"),
    SavedSearch("Clark",      "sheriff_sale", "ohio_sheriff:clark"),
    SavedSearch("Clermont",   "sheriff_sale", "ohio_sheriff:clermont"),
    SavedSearch("Greene",     "sheriff_sale", "ohio_sheriff:greene"),
    SavedSearch("Miami",      "sheriff_sale", "ohio_sheriff:miami"),
    SavedSearch("Montgomery", "sheriff_sale", "ohio_sheriff:montgomery"),
    SavedSearch("Warren",     "sheriff_sale", "ohio_sheriff:warren"),
    # ── OH — court-case foreclosure (per-county courthouse portals) ─
    # Sentinel ``ohio_foreclosure:<county>`` routes to
    # ``ohio_foreclosure_scrapers.fetch_ohio_foreclosure`` which wraps
    # the H3-ported per-county scrapers. Montgomery is fully live;
    # other 6 are stubs until Phase 4 — the dispatcher in
    # ``scraper.scrape_all`` catches NotImplementedError and continues.
    SavedSearch("Butler",     "foreclosure", "ohio_foreclosure:butler"),
    SavedSearch("Clark",      "foreclosure", "ohio_foreclosure:clark"),
    SavedSearch("Clermont",   "foreclosure", "ohio_foreclosure:clermont"),
    SavedSearch("Greene",     "foreclosure", "ohio_foreclosure:greene"),
    SavedSearch("Miami",      "foreclosure", "ohio_foreclosure:miami"),
    SavedSearch("Montgomery", "foreclosure", "ohio_foreclosure:montgomery"),
    SavedSearch("Warren",     "foreclosure", "ohio_foreclosure:warren"),
    # ── OH — probate (per-county probate court portals) ─────────────
    # Sentinel ``ohio_probate:<county>`` routes to
    # ``ohio_probate_scrapers.fetch_ohio_probate``. Greene is fully
    # live; other 6 are stubs until Phase 4.
    SavedSearch("Butler",     "probate", "ohio_probate:butler"),
    SavedSearch("Clark",      "probate", "ohio_probate:clark"),
    SavedSearch("Clermont",   "probate", "ohio_probate:clermont"),
    SavedSearch("Greene",     "probate", "ohio_probate:greene"),
    SavedSearch("Miami",      "probate", "ohio_probate:miami"),
    SavedSearch("Montgomery", "probate", "ohio_probate:montgomery"),
    SavedSearch("Warren",     "probate", "ohio_probate:warren"),
]

# Counties served by the Ohio auditor tax-delinquent pipeline. Used by
# scraper.scrape_all() to dispatch these entries to the dedicated
# ohio_tax_delinquent_scrapers module instead of running the TN flow.
OHIO_TAX_DELINQUENT_COUNTIES = [
    "Butler", "Clark", "Clermont", "Greene",
    "Miami", "Montgomery", "Warren",
]
OHIO_AUDITOR_SENTINEL_PREFIX = "ohio_auditor:"

# Counties served by the Ohio sheriff-sale pipeline. Mirrors the
# tax-delinquent county list — every OH county we cover for tax
# delinquency also has a RealForeclose sheriff-sale calendar.
OHIO_SHERIFF_SALE_COUNTIES = [
    "Butler", "Clark", "Clermont", "Greene",
    "Miami", "Montgomery", "Warren",
]
OHIO_SHERIFF_SENTINEL_PREFIX = "ohio_sheriff:"

# Counties served by the Ohio foreclosure court-case pipeline.
# CANARY STATUS (2026-06-19): Montgomery is fully live; the other 6
# are stubs raising NotImplementedError until Phase 4 of the H3 port.
# The dispatcher in scraper.scrape_all() catches stub exceptions and
# continues with the rest of the run.
OHIO_FORECLOSURE_COUNTIES = [
    "Butler", "Clark", "Clermont", "Greene",
    "Miami", "Montgomery", "Warren",
]
OHIO_FORECLOSURE_SENTINEL_PREFIX = "ohio_foreclosure:"

# Counties served by the Ohio probate pipeline. Same 7 counties.
# CANARY STATUS (2026-06-19): Greene is fully live; the other 6 stub.
OHIO_PROBATE_COUNTIES = [
    "Butler", "Clark", "Clermont", "Greene",
    "Miami", "Montgomery", "Warren",
]
OHIO_PROBATE_SENTINEL_PREFIX = "ohio_probate:"

# ── Entity Detection ──────────────────────────────────────────────────
# Business entity patterns — shared across obituary_enricher, tax_enricher,
# and enrichment_pipeline for entity filtering.
BUSINESS_RE = re.compile(
    r"\b(?:LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO\b|LTD|LP|L\.P|"
    r"PARTNERSHIP|ASSOCIATION|ASSOC|BANK|CREDIT UNION|CHURCH|MINISTRIES|"
    r"HOUSING|AUTHORITY|DEVELOPMENT|ENTERPRISES|PROPERTIES|INVESTMENTS|"
    r"GROUP|HOLDINGS|MANAGEMENT|SERVICES|FOUNDATION|ORGANIZATION)\b",
    re.IGNORECASE,
)

# Trust/estate patterns — personal trusts are NOT business entities
TRUST_NAME_RE = re.compile(
    r"^(?:THE\s+)?([\w]+(?:\s+[\w.]+)+?)\s+(?:REVOCABLE\s+)?(?:LIVING\s+)?TRUST\b",
    re.IGNORECASE,
)
ESTATE_OF_RE = re.compile(
    r"^(?:THE\s+)?ESTATE\s+OF\s+([\w]+(?:\s+[\w.]+)+?)(?:\s*,|\s*$)",
    re.IGNORECASE,
)

_config_logger = logging.getLogger(__name__)


# ── State File Utilities ─────────────────────────────────────────────


def save_state(path: Path, data: dict) -> None:
    """Write JSON state to disk atomically (write tmp → rename).

    Creates a .bak copy of the previous file before overwriting.
    """
    # Back up current file
    if path.exists():
        try:
            bak = path.with_suffix(path.suffix + ".bak")
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass  # Best-effort backup

    # Atomic write: tmp → rename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    """Load JSON state from disk, falling back to .bak if corrupt."""
    for candidate in [path, path.with_suffix(path.suffix + ".bak")]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                _config_logger.warning("Failed to read %s: %s", candidate, e)
    return {}
