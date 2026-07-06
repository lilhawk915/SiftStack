# SiftStack — What It Does, In Plain English

**One-sentence version:** Turns raw court foreclosure and probate filings into a ranked, ready-to-dial lead list every morning.

**Where the data starts:** Montgomery County, OH court records (published daily at pro.mcohio.org and go.mcohio.org).

**Where the data ends:** A CSV your dial team can work from, sorted so the best phone numbers get called first.

---

## The Flow (6 stages)

```
┌─────────────────────────────────────────────────────────────┐
│  1. SCRAPE — every morning at 6:00 AM ET                     │
│  Pulls all new foreclosure + probate + sheriff-sale          │
│  filings from Montgomery County court records.               │
│                                                              │
│  Output: raw list of ~30-50 properties per day.              │
│  Missing: owner phone numbers, property values, heir data.   │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  2. ENRICH — Pass 1 (still automatic, ~30 more minutes)      │
│  Fills in the missing data using external services:          │
│                                                              │
│  • Auditor lookup   → recovers missing owner names           │
│  • Smarty           → verifies + standardizes addresses      │
│  • Zillow           → adds property value, equity, sqft      │
│  • Obituary/Ancestry → flags deceased owners + heirs         │
│  • Entity Research  → resolves LLCs, HOAs, Trusts to people  │
│                                                              │
│  Output: enriched CSV, everything but phones.                │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  3. UPLOAD TO DATASIFT (CRM)                                 │
│  The enriched list uploads automatically to DataSift.        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  4. SKIP TRACE — DataSift's built-in engine (~30-45 min)     │
│  DataSift finds phone numbers and email addresses for        │
│  each record (unlimited on the $97/mo plan).                 │
│                                                              │
│  Output: enriched records, most with phones + emails.        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  5. ENRICH — Pass 2 (needs ~10 min of human touch)           │
│  A human exports the enriched CSV from DataSift, then runs   │
│  a single command that does two things:                      │
│                                                              │
│  • Tracerfy   → for records DataSift couldn't find phones    │
│                 for, does an address-only lookup to recover  │
│                 the owner (catches ~1-3 per day)             │
│  • Trestle    → scores every phone from 0-100 and tiers them │
│                 as Dial First / Second / Third / Fourth /    │
│                 Drop, so the team knows which to call first  │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  6. DELIVER — final dial list                                │
│  Named "Montgomery_Dial_List_YYYY-MM-DD.csv" and delivered   │
│  to the dial team via shared Drive / email.                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Daily Timing

| Time | What happens | Human needed? |
|---|---|---|
| **6:00 AM ET** | Cron auto-fires → scrape starts | No |
| **~6:35 AM** | Enriched CSV written, uploaded to DataSift, Slack confirmation posted | No |
| **~7:20 AM** | DataSift skip trace completes | No (automatic) |
| **~7:30 AM** | VA exports enriched CSV from DataSift, runs Pass 2 | **Yes** (~10 min) |
| **~7:45 AM** | Final dial list delivered to team | **Yes** (~2 min) |

**Total human effort per day:** ~15 minutes.

---

## What Each Record Ends Up With

By the time the dial list lands, each row has:

- **Property**: street address, city, state, ZIP+4, county
- **Owner**: first name, last name, mailing address (person or entity)
- **Financial signals**: estimated value, equity %, last sale date + price
- **Legal status**: notice type (foreclosure / probate / sheriff sale), case number, filing date
- **Deceased flags**: whether the owner is deceased + decision-maker (heir/executor) if identified
- **Phone numbers**: up to 30 per record (typically 3-8), each ranked with a tier tag
- **Tags**: courthouse data, month, county, absentee owner, high equity, vacant, etc.

---

## Cost Per Month

| Tool | What it costs | What it does |
|---|---|---|
| Scraper infrastructure | $0 (runs on Mac) | Court records + Chrome browser |
| Smarty | ~$5/mo (subscription) | Address verification |
| Zillow (OpenWebNinja) | ~$10/mo (subscription) | Property data |
| Anthropic (Claude) | ~$10-15/mo | Obituary search, entity research |
| Ancestry | $29/mo (subscription) | SSDI + family tree lookup |
| DataSift | $97/mo (unlimited) | CRM + skip trace + property enrichment |
| Tracerfy | ~$5/mo | Address-only fallback for DataSift misses |
| Trestle | ~$40/mo | Phone tier scoring |

**Total: ~$200/month** to produce roughly **30-50 pre-ranked leads per day**.

---

## The Big Picture

Every morning, without you touching anything, court filings become a ranked list of properties + owners + phones + tier tags. A person spends about 15 minutes moving the file from DataSift through Pass 2 and delivering it. The dial team gets a "top phone first" list — they call Dial First tier before Dial Second, and skip anything tagged Drop.

The whole system is designed so that if any single service fails, the pipeline degrades gracefully — a missing property valuation doesn't kill the record; a missing phone number still lets you mail the property; a service-tab-only defendant gets logged for manual review rather than shipping bad data to the dial team.
