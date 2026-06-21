"""Download and parse Warren foreclosure Complaint PDFs for property addresses.

Warren's BenchmarkCP portal hides docket documents behind a Silverlight
viewer, but the underlying PDF is served from a stable URL:

    /BenchmarkCP/Image.aspx/PDFViewer2?cid={docket_id}&digest={digest}

Both `cid` (the docket ID) and `digest` are visible attributes on the
`<a class="casedocketimage">` links in the case-detail page. The endpoint
requires the same session cookies as the rest of BenchmarkCP, so we hit
it via `page.context.request.get(...)` from inside the actor.

The endpoint sometimes returns the PDF bytes directly (Content-Type:
application/pdf) and sometimes returns an HTML wrapper that embeds the
PDF via `<iframe src="...pdf">` or `<embed src="...">`. We handle both.

Once we have the PDF bytes, we extract text via `pypdfium2` and run a
small bag of regex patterns common to mortgage / tax foreclosure
complaints filed in Ohio courts:
    - "commonly known as <ADDRESS>"
    - "located at <ADDRESS>"
    - "real property situated at <ADDRESS>"
    - "Property Address: <ADDRESS>"
    - "premises located at <ADDRESS>"
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup


@dataclass
class WarrenPropertyAddress:
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""

    def is_empty(self) -> bool:
        return not any([self.street, self.city, self.state, self.zip])

    def as_dict(self) -> dict:
        return {
            "street": self.street, "city": self.city,
            "state": self.state, "zip": self.zip,
        }


def find_pjr_docket_link(case_html: str) -> tuple[str, str] | None:
    """Return (cid, digest) for the Preliminary Judicial Report entry.

    Title companies file the PJR within days of the foreclosure complaint
    and the PDF includes the property's street address inside the legal
    description section. PJRs are generated as searchable PDFs (unlike
    Warren's scanned complaints), so we can use a plain text extract.

    Scoring rules:
        +5 row text contains "PRELIMINARY JUDICIAL REPORT"
        +3 row text contains "JUDICIAL REPORT" (catches some variants)
        −5 row contains "NOTICE OF FILING" (notice doc references PJR
            but isn't the PJR itself — though sometimes it's the only
            place the PJR is attached; we still prefer the original)
        −5 row contains "SUMMONS" / "SERVICE COPY" / "ANSWER" /
            "PRAECIPE" / "WAIVER" (unrelated docket entries that
            sometimes contain "report" text)

    Returns the highest-scoring row's (cid, digest), or None if no PJR
    docket entry is found. Ties broken by larger leading page count
    (PJRs are typically 5-15 pages).
    """
    if not case_html:
        return None
    soup = BeautifulSoup(case_html, "html.parser")
    best: tuple[int, int, str, str] | None = None
    for a in soup.find_all("a", class_="casedocketimage"):
        tr = a.find_parent("tr")
        if not tr:
            continue
        row = tr.get_text(" ", strip=True).upper()
        if "JUDICIAL REPORT" not in row:
            continue
        score = 3
        if "PRELIMINARY JUDICIAL REPORT" in row:
            score = 5
        if "NOTICE OF FILING" in row:
            score -= 4  # net 1 — keep as last resort
        if any(k in row for k in (
            "SUMMONS", "SERVICE COPY", "ANSWER", "PRAECIPE",
            "WAIVER", "ATTORNEY", "ASSIGNED",
        )):
            score -= 5
        if score <= 0:
            continue
        m = re.match(r"\s*(\d{1,3})\b", row)
        pages = int(m.group(1)) if m else 0
        rel = a.get("rel") or a.get("id") or ""
        if isinstance(rel, list):
            rel = rel[0] if rel else ""
        digest = a.get("digest") or ""
        if not rel or not digest:
            continue
        candidate = (score, pages, str(rel), str(digest))
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return None
    return best[2], best[3]


def find_complaint_docket_link(case_html: str) -> tuple[str, str] | None:
    """Walk the docket table and return (cid, digest) for the Complaint.

    Warren dockets are noisy — multiple rows can match "COMPLAINT" because
    *SUMMONS ISSUED. REASON: COMPLAINT FOR FORECLOSURE* etc. is a separate
    docket entry from the actual complaint. We score candidates and pick
    the best:
        +3 if the row text contains COMPLAINT IN FORECLOSURE
        +3 if the row text contains COMPLAINT FOR FORECLOSURE (and no SUMMONS)
        +1 if the row text contains COMPLAINT (generic)
        −5 if the row text contains SUMMONS / SERVICE COPY / PRAECIPE /
           CERTIFIED MAIL (those are mail/service entries, not the complaint)
    Returns the highest-scoring row; ties broken by the entry with the
    largest leading page-count prefix (complaints are usually 7-30 pages,
    summons are 1-2 pages).
    """
    if not case_html:
        return None
    soup = BeautifulSoup(case_html, "html.parser")
    best: tuple[int, int, str, str] | None = None  # (score, pages, cid, digest)
    for a in soup.find_all("a", class_="casedocketimage"):
        tr = a.find_parent("tr")
        if not tr:
            continue
        row = tr.get_text(" ", strip=True).upper()
        if "COMPLAINT" not in row:
            continue
        score = 1
        if "COMPLAINT IN FORECLOSURE" in row:
            score = 5
        elif "COMPLAINT FOR FORECLOSURE" in row and "SUMMONS" not in row:
            score = 5
        elif "COMPLAINT FOR BREACH" in row and "SUMMONS" not in row:
            # Warren files some tax/HOA foreclosures as "Breach of Contract"
            score = 4
        if any(k in row for k in (
            "SUMMONS", "SERVICE COPY", "CERTIFIED MAIL", "PRAECIPE",
            "ATTORNEY", "JUDGE", "ASSIGNED", "RETURN OF SERVICE",
        )):
            score -= 5
        if score <= 0:
            continue
        # First int in the row is usually the page count
        m = re.match(r"\s*(\d{1,3})\b", row)
        pages = int(m.group(1)) if m else 0
        rel = a.get("rel") or a.get("id") or ""
        if isinstance(rel, list):
            rel = rel[0] if rel else ""
        digest = a.get("digest") or ""
        if not rel or not digest:
            continue
        candidate = (score, pages, str(rel), str(digest))
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return None
    return best[2], best[3]


# ── PDF text extraction ────────────────────────────────────────────────

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF byte string.

    Tries pypdfium2 text extraction first (fast, works on born-digital
    PDFs). If the result is suspiciously small (<100 chars across the
    first 5 pages), falls back to OCR via tesseract — Warren's
    Preliminary Judicial Reports come through as scanned images, so
    pypdfium2 only returns page-break newlines.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return ""
    if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        return ""
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception:
        return ""
    parts: list[str] = []
    page_count = len(doc)
    pages_to_read = min(5, page_count)
    for i in range(pages_to_read):
        try:
            page = doc[i]
            text_page = page.get_textpage()
            parts.append(text_page.get_text_range())
            text_page.close()
            page.close()
        except Exception:
            continue
    text = "\n".join(parts).strip()

    # OCR fallback for scanned PDFs (Warren PJRs)
    if len(text) < 100 and page_count > 0:
        ocr_text = _ocr_pdf_pages(doc, max_pages=min(6, page_count))
        if ocr_text and len(ocr_text) > len(text):
            text = ocr_text

    try:
        doc.close()
    except Exception:
        pass
    return text


def _ocr_pdf_pages(doc, max_pages: int = 3) -> str:
    """OCR the first `max_pages` pages of an already-open pypdfium2 doc.

    Returns the concatenated tesseract text. Returns "" if pytesseract
    or tesseract isn't installed, or if any rendering step fails.
    """
    try:
        import pytesseract
    except ImportError:
        return ""
    parts: list[str] = []
    for i in range(max_pages):
        try:
            page = doc[i]
            # 200 DPI is enough for body text in a title-company PJR;
            # higher slows OCR without improving accuracy on legal docs.
            scale = 200 / 72  # 200 DPI
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            page.close()
            txt = pytesseract.image_to_string(pil_image)
            if txt:
                parts.append(txt)
        except Exception:
            continue
    return "\n".join(parts)


# ── Address regex patterns ─────────────────────────────────────────────

# Each pattern captures the address span; we then normalize into street /
# city / state / zip via a follow-up split.
_ADDRESS_PROMPTS = [
    r"commonly\s+known\s+as[:\s]+",
    r"located\s+at[:\s]+",
    r"real\s+property\s+situated\s+at[:\s]+",
    r"the\s+property\s+situated\s+at[:\s]+",
    r"property\s+address[:\s]+",
    r"premises\s+located\s+at[:\s]+",
    r"the\s+real\s+estate\s+located\s+at[:\s]+",
    # PJR-specific phrasings
    r"property\s+is\s+commonly\s+known\s+as[:\s]+",
    r"street\s+address[:\s]+",
    r"site\s+address[:\s]+",
    r"address\s+of\s+property[:\s]+",
    r"address\s+of\s+real\s+estate[:\s]+",
    r"subject\s+property[:\s]+",
]

_ADDRESS_RE = re.compile(
    r"(?:%s)"
    r"(?P<addr>\d{1,6}\s+[A-Z0-9][\w\s\.,#\-/&]{8,100}?"
    r"(?:[A-Z]{2}|Ohio)\s*\d{5}(?:-\d{4})?)" % "|".join(_ADDRESS_PROMPTS),
    re.IGNORECASE | re.DOTALL,
)

# Last-resort scan: any "<num> <street> <city>, OH <zip>" anywhere in the
# extracted text. PJRs include the legal description and frequently say
# the address on its own line without a prompt phrase.
_BARE_ADDRESS_RE = re.compile(
    r"(?P<addr>\d{1,6}\s+[A-Z0-9][A-Za-z0-9\.\-' ]{2,60}"
    r"\s+(?:DR|DRIVE|ST|STREET|AVE|AVENUE|RD|ROAD|BLVD|BOULEVARD|"
    r"LN|LANE|CT|COURT|PL|PLACE|WAY|PKWY|PARKWAY|TER|TERRACE|"
    r"TRL|TRAIL|HWY|HIGHWAY|CIR|CIRCLE|RUN|PIKE|RIDGE|RDG|"
    r"SQ|SQUARE|PT|POINT)"
    r"(?:\s+[A-Za-z0-9\.\-' #]+)?"
    r",?\s+[A-Z][A-Za-z\.\-' ]{2,30}"
    r",?\s+(?:OH|Ohio)\s+\d{5}(?:-\d{4})?)",
    re.IGNORECASE,
)

# Common street-name suffixes — used to anchor the street/city boundary.
# Order matters: longer forms first so e.g. "DRIVE" wins over "DR".
_STREET_SUFFIXES_RE = re.compile(
    r"\b(?:DRIVE|STREET|AVENUE|BOULEVARD|PARKWAY|HIGHWAY|"
    r"CIRCLE|TERRACE|SQUARE|TURNPIKE|TRAIL|PLACE|COURT|ROAD|"
    r"LANE|RIDGE|ROUTE|POINT|GROVE|ALLEY|PIKE|PATH|RUN|"
    r"BLVD|PKWY|HWY|TPKE|PWAY|"
    r"DR|ST|AVE|RD|LN|CT|PL|CIR|TER|TRL|RDG|RT|PT|SQ|"
    r"HOLLOW|HOLW|HILL|HEIGHTS|HTS|MEWS|RIDGE|RDG)\b",
    re.IGNORECASE,
)

# Final-line breakdown: "STREET, CITY, STATE ZIP". We deliberately use a
# greedy match for the city and let _split_street_from_pre handle the
# street boundary using street-suffix anchors. Non-greedy `.+?` on
# street picks too little (e.g. just the house number).
_PARTS_RE = re.compile(
    r"^(?P<pre>.+?)[,\s]+(?P<state>OH|Ohio|[A-Z]{2})\s+"
    r"(?P<zip>\d{5}(?:-\d{4})?)\s*$",
    re.IGNORECASE,
)


def _split_street_from_pre(pre: str) -> tuple[str, str]:
    """Split "STREET CITY" → (street, city) using street-suffix anchors.

    Finds the LAST occurrence of a known street suffix; everything up
    to and including the suffix is street; everything after is city.
    Handles addresses like:
        "625 Central Avenue Carlisle" → ("625 Central Avenue", "Carlisle")
        "3122 Village View Ln Morrow" → ("3122 Village View Ln", "Morrow")
    """
    pre = pre.strip(" ,.")
    matches = list(_STREET_SUFFIXES_RE.finditer(pre))
    if not matches:
        return pre, ""
    last = matches[-1]
    street = pre[: last.end()].strip(" ,.")
    city = pre[last.end():].strip(" ,.").title()
    return street, city


def _parse_address_match(addr_text: str) -> WarrenPropertyAddress:
    """Break a captured address string into street/city/state/zip.

    Tesseract sometimes inserts a stray comma right after the house
    number on Warren PJRs (e.g. "625, Central Avenue Carlisle OH ...").
    We collapse that pattern before running the structural regex,
    otherwise the parser splits on the wrong comma and ends up with
    street="625", city="Central Avenue Carlisle".
    """
    out = WarrenPropertyAddress()
    if not addr_text:
        return out
    s = re.sub(r"\s+", " ", addr_text).strip(" .,")
    # OCR fix: "<num>, <street word>" → "<num> <street word>"
    s = re.sub(r"^(\d{1,6}),\s+([A-Z])", r"\1 \2", s)
    m = _PARTS_RE.match(s)
    if not m:
        out.street = s
        return out
    street, city = _split_street_from_pre(m.group("pre"))
    out.street = street
    out.city = city
    state = m.group("state").upper()
    out.state = "OH" if state in ("OH", "OHIO") else state
    out.zip = m.group("zip")
    return out


def parse_property_address(pdf_text: str) -> WarrenPropertyAddress:
    """Find a property address inside extracted PDF text (complaint or PJR).

    Tries prompt-prefixed addresses first (highest precision), falls back
    to bare-pattern matching anywhere in the text (catches PJRs that
    state the address without a leading phrase like "commonly known as").
    """
    if not pdf_text:
        return WarrenPropertyAddress()
    for m in _ADDRESS_RE.finditer(pdf_text):
        addr = _parse_address_match(m.group("addr"))
        if not addr.is_empty():
            return addr
    # Fallback: bare street+city+state+zip pattern
    for m in _BARE_ADDRESS_RE.finditer(pdf_text):
        addr = _parse_address_match(m.group("addr"))
        if not addr.is_empty() and addr.zip:
            return addr
    return WarrenPropertyAddress()


# ── PDF download (called from the scraper's Playwright session) ────────

PORTAL_HOST = "https://clerkofcourt.co.warren.oh.us"


async def download_complaint_pdf(page, cid: str, digest: str) -> tuple[bytes, dict]:
    """Download the PDF for the given docket cid+digest.

    Warren's BenchmarkCP wraps the PDF in a viewer page that uses a
    3-step async API to generate and stream the actual PDF:

        1. POST ImageAsync.aspx/GetPDFRequestGuid {cid, digest, time,
           redacted: 'False'} → returns a per-request GUID
        2. (Optional) Poll ImageAsync.aspx/GetPDFProgress {guid, time}
           until it returns 0 (rendering complete)
        3. GET ImageAsync.aspx/GetPDF?guid=<guid> → PDF bytes

    Cookies from the just-fetched case-detail page authenticate the
    request automatically since page.context.request shares the
    BrowserContext's cookie jar.

    Returns (pdf_bytes, diag) where diag records each step's status.
    """
    diag: dict = {}
    base = f"{PORTAL_HOST}/BenchmarkCP"
    # Time field is just a cache-buster; JS uses `new Date()` which
    # JSON-stringifies to an ISO timestamp, but the server only checks
    # presence, not format.
    time_field = "2026-01-01T00:00:00.000Z"

    # ── Step 1: request a GUID ─────────────────────────────────────────
    try:
        resp1 = await page.context.request.post(
            f"{base}/ImageAsync.aspx/GetPDFRequestGuid",
            form={
                "cid": cid, "digest": digest,
                "time": time_field, "redacted": "False",
            },
            timeout=30000,
        )
        diag["s1_status"] = resp1.status
        if not resp1.ok:
            return b"", diag
        body1 = (await resp1.body()).decode("utf-8", errors="ignore")
        diag["s1_body"] = body1[:200]
        # Response may be `"<guid>"` (JSON string) or just <guid>
        guid = body1.strip().strip('"').strip()
        if not guid or len(guid) > 128:
            return b"", diag
        diag["guid"] = guid
    except Exception as e:
        diag["s1_error"] = str(e)
        return b"", diag

    # ── Step 2: poll for completion (best-effort, max 12s) ─────────────
    for _ in range(40):  # ~12s @ 300ms
        try:
            rp = await page.context.request.post(
                f"{base}/ImageAsync.aspx/GetPDFProgress",
                form={"guid": guid, "time": time_field},
                timeout=10000,
            )
            if rp.ok:
                pbody = (await rp.body()).decode("utf-8", errors="ignore")
                # Server returns 0 when rendering is done, >0 while
                # in progress (percent). May be `"0"` or just `0`.
                if pbody.strip().strip('"') == "0":
                    break
        except Exception:
            pass
        await page.wait_for_timeout(300)

    # ── Step 3: stream the actual PDF bytes ────────────────────────────
    try:
        resp3 = await page.context.request.get(
            f"{base}/ImageAsync.aspx/GetPDF?guid={guid}",
            timeout=30000,
        )
        diag["s3_status"] = resp3.status
        diag["s3_ct"] = (resp3.headers or {}).get("content-type", "")
        if not resp3.ok:
            return b"", diag
        body = await resp3.body()
        diag["s3_bytes"] = len(body)
        if body[:4] == b"%PDF":
            diag["strategy"] = "imageasync_api"
            return body, diag
        return b"", diag
    except Exception as e:
        diag["s3_error"] = str(e)
        return b"", diag
