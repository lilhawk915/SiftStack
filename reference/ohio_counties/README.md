# Ohio Counties — Reference Data

Source-of-truth tables for the 7 SW Ohio counties served by the
tax-delinquent pipeline (Butler, Clark, Clermont, Greene, Miami,
Montgomery, Warren). Each per-county CSV is the original research
document — do **not** transcribe values into code; reference the CSV
filename in code comments when the endpoint/contact info is needed.

## Files

| File | Purpose |
|------|---------|
| `Summary.csv` | One-line-per-county overview. `Best / Easiest First-to-Market Pull` column is the endpoint used by `src/ohio_tax_delinquent_scrapers.py`. |
| `Butler.csv` | 10 data-source rows: Tax Sale, Tax Delinquency, Code Violations, Condemned, Liens, etc. |
| `Clark.csv` | Same 10-row layout per county. |
| `Clermont.csv` | ⚠ Real-estate delinquent list is published in the newspaper, not online. Only the Mobile Home delinquent PDF is available. See "Known Limitations" below. |
| `Greene.csv` | ~weekly downloadable bulk delinquent list. NextRequest FOIA portal. |
| `Miami.csv` | Live searchable delinquency report (~806 parcels). Piqua municipal utility shut-off list is high-value C-tier. |
| `Montgomery.csv` | Treasurer Delinquent List + Dayton Structural Nuisance DB. |
| `Warren.csv` | All offices on Justice Dr, Lebanon. Recorder 'AVA' covers liens. PDF-published delinquent list. |

## Priority Legend (per Summary.csv)

- **A** — Core (tax sale, tax delinquency)
- **B** — Standard (code, condemned, liens)
- **C** — Extended (utilities, permits, evictions, Medicaid)

The tax-delinquent pipeline only consumes A-tier sources today.

## Known Limitations

### Clermont — real-estate delinquent list not online
The Clermont County Auditor publishes only the **Mobile Home** delinquent
list as a downloadable PDF. The full real-estate delinquent list is
"published in newspaper" and not available as a machine-readable feed.

`src/ohio_tax_delinquent_scrapers.fetch_clermont()` raises
`NotImplementedError` deliberately. If we want coverage in the future,
a "tax-foreclosure proxy" using Sheriff Sales + Auditor Forfeited Land
list could ship as a separate `notice_type="tax_foreclosure_proxy"` —
do **not** alias those onto `tax_delinquent` because they're post-
delinquency sources (people whose foreclosures have already begun) and
would skew the dataset's outreach-conversion economics.

### Endpoint stability
Auditor portals change layouts annually. When the Butler CSV link
text or filename changes, update `OHIO_ENDPOINTS["Butler"]["csv_link_text"]`
in `src/ohio_tax_delinquent_scrapers.py` and verify via the Auditor
portal manually — do not guess from filename patterns. The CSVs in
this directory are the canonical record of the endpoint at the time
the adapter was written; update both when re-verifying.

## Transport notes

Live probe on 2026-06-16 found that **4 of the 6 implementable Ohio
counties sit behind bot-protection** (Cloudflare or Azure WAF) and
**require Playwright click-download** — plain `httpx`/`curl` requests
get a 403 with a JS challenge page, even with a browser User-Agent
+ Referer. Trying to "be clever" with raw HTTP for these counties
will waste hours; standardize on a real browser for them.

| County | Transport | CDN / WAF |
|---|---|---|
| Butler | **Playwright** click-download | Cloudflare (revize.com CDN) |
| Clark | **Playwright** scrape | Cloudflare (direct) |
| Greene | **Playwright** scrape | Azure WAF (`auditor.greenecountyohio.gov`) |
| Miami | **Playwright** scrape | Cloudflare (direct) |
| Montgomery | plain HTTP (`httpx`) | none |
| Warren | plain HTTP + `pdf_importer` | none |
| Clermont | n/a — stub (real-estate list not online) | n/a |

The base adapter pattern in `src/ohio_tax_delinquent_scrapers.py`
supports both transport modes: `fetch_<county>(ctx=..., client=...)`
where `ctx` is a Playwright `BrowserContext` (for CDN-protected
sources) and `client` is an `httpx.Client` (for plain HTTP sources).
Adapters take whichever one their source needs; `scrape_all()`
provides both — the existing TN flow already builds a Playwright
context that gets re-used at no extra cost.

For tests, every adapter accepts override-text kwargs that bypass
the network entirely (e.g. `fetch_butler(csv_override_text=...,
owners_override_text=...)`). This keeps unit tests sync and fast
even though the production code path is async.

## Updating

When a county's source URL or contact changes:

1. Edit the relevant `<County>.csv` here (source-of-truth).
2. Update `OHIO_ENDPOINTS[<County>]` in `src/ohio_tax_delinquent_scrapers.py`.
3. Bump the version comment in that adapter ("verified <month> <year>").
4. Re-run the Butler dry-run to confirm the index regex still resolves
   to a valid CSV URL.
