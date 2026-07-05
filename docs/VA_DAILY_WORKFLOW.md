# SiftStack Daily Workflow — VA Handoff Guide

**What you're doing:** Every morning, a computer program automatically pulls fresh foreclosure and probate cases from Montgomery County, OH court records, enriches them with property info, then uploads them to our CRM (DataSift). Your job is to finish the last two steps a human still needs to touch: **download the enriched file from DataSift, run one command to fill in the missing phone numbers, and hand the final list to the dial team.**

**Time required:** ~20 minutes per day (10 min of active work + 30-45 min of waiting for DataSift to finish its skip trace).

**When to run this:** Every day between **7:00 AM and 10:00 AM ET**.

**Where the work happens:** All of it happens on **Ryan's Mac**, not yours. You remote in from your own computer using a screen-sharing tool. Your machine is basically a keyboard-and-monitor for Ryan's Mac. No software installs, no code downloads on your side.

---

## Part 0 — One-time setup (do this the FIRST DAY only, then never again)

Skip this part after your first successful login. Everything here happens once and doesn't need to be repeated daily.

### What you need to have before starting

Before you can do anything, Ryan needs to have sent you:
- **His Tailscale invite** (or a Chrome Remote Desktop PIN — whichever remote-access method he chose). See Step 0.1 below.
- **DataSift login** — a username (Ryan's email) and a password. Ideally shared via a password manager like 1Password or Bitwarden, not raw text. If he sent it plain text, immediately save it into your own password manager and delete the message.
- **The location of this document** — bookmark this file in your notes app so you can find it every morning.

Confirm all three are in hand before proceeding.

---

### Step 0.1 — Install the remote-access tool

Ryan will have set up ONE of these two options. He'll tell you which. Follow only the matching sub-section.

#### Option A: Tailscale (Ryan's recommended path)

Tailscale is a free VPN app that lets you connect to Ryan's Mac securely without exposing it to the public internet.

1. On your own computer, go to **https://tailscale.com/download** and download the app for your operating system (Mac, Windows, or Linux).
2. Install it. On Mac, drag to Applications. On Windows, run the installer.
3. Open Tailscale. It'll ask you to sign in.
4. **Click the invite link Ryan sent you** (or sign in with the Google/email account he added to his team plan). This connects your machine to his private network.
5. Once signed in, Tailscale runs in your system tray/menu bar. You'll see a small icon.
6. Click the Tailscale icon → you should see Ryan's Mac in the list (name might be "MacBook-Pro" or similar). Note its **Tailscale IP** — it'll be a number like `100.64.10.42`. Write this down.
7. Now use your operating system's built-in screen-sharing to connect:
   - **On your Mac**: Open Finder → click **Go** menu → **Connect to Server** → type `vnc://100.64.10.42` (using the Tailscale IP you noted) → Connect. Log in with the username `ryanhawker` and the password Ryan gave you (may be his Mac password, ask him).
   - **On Windows**: Install a free VNC viewer like **RealVNC Viewer** or **TigerVNC** → connect to `100.64.10.42:5900` → same credentials.
8. You should now see Ryan's Mac desktop on your screen. **You're in.**

#### Option B: Chrome Remote Desktop (fallback if Ryan chose this instead)

Chrome Remote Desktop is Google's remote-access tool. Simpler to set up but a bit less private.

1. On your own computer, install **Google Chrome** (if you don't have it).
2. Sign into Chrome with your Google account.
3. Ryan will have added your email as an authorized user for his machine. Confirm with him this is done.
4. In Chrome, go to **https://remotedesktop.google.com/access**
5. You should see a machine listed (probably named "Ryan's MacBook" or similar).
6. Click it → enter the PIN Ryan gave you (usually 6 digits) → connect.
7. You should now see Ryan's Mac desktop in a Chrome tab.

**Either method — if you see Ryan's desktop, you're in. Proceed to Step 0.2.**

---

### Step 0.2 — Verify you can actually work on the Mac (2 min)

Confirm the basics work before you rely on this daily.

1. On Ryan's Mac (which is now on your screen), open **Terminal**:
   - Press **Cmd+Space** (opens Spotlight search)
   - Type **Terminal** → press Enter
   - A black or white window opens with a text prompt
2. Type this exactly and press Enter:
   ```bash
   pwd
   ```
3. It should print something like: `/Users/ryanhawker`
   - If yes, you're in the right place ✅
   - If no or error, ping Ryan
4. Type this and press Enter:
   ```bash
   ls /Users/ryanhawker/Desktop/SiftStack
   ```
5. You should see a list of folders and files (like `src`, `output`, `scripts`, `docs`, `.env`, etc.). If yes, SiftStack is installed and accessible ✅. If you get "No such file or directory," ping Ryan.
6. Type this and press Enter to test the DataSift-facing browser:
   ```bash
   open -a "Google Chrome" https://app.reisift.io
   ```
7. Chrome should launch and navigate to the DataSift login page. If it does, you're set for daily work. Close Chrome for now.

---

### Step 0.3 — Save DataSift credentials somewhere safe (2 min)

The DataSift login isn't in the SOP for security reasons. Ryan gives it to you separately.

**Recommended: use a password manager**
- Bitwarden (free) — https://bitwarden.com
- 1Password (paid) — https://1password.com
- Or whatever your team already uses

Add DataSift as an entry:
- Name: **DataSift**
- URL: https://app.reisift.io
- Username: (from Ryan)
- Password: (from Ryan)

**Do NOT** save these in a plain text file on your desktop or in your email. Password manager or nothing.

---

### Step 0.4 — Bookmark the important stuff (1 min)

Set up these bookmarks in your browser (any browser, on your own computer):

1. **DataSift**: https://app.reisift.io
2. **This SOP document**: https://github.com/lilhawk915/SiftStack/blob/main/docs/VA_DAILY_WORKFLOW.md (or wherever Ryan hosts it)
3. **Ryan's Slack DM** (if using Slack) — for questions

On Ryan's Mac (the one you remote into), keep these open in tabs when you're working:
- DataSift Records page
- DataSift Activity page (top-right menu)

---

### Step 0.5 — First-day sanity run (optional but wise)

Once Steps 0.1-0.4 are complete, do a dry run of Steps 1-2 of the daily workflow (below) to confirm everything works before you start being on the hook for real deliveries.

If Steps 1-2 work correctly on the first try, you're ready. If anything fails, ping Ryan while it's fresh.

---

### Setup complete — you're ready

You never need to redo Part 0 again. From tomorrow onward, jump straight to **Part 3 (the daily workflow)**. Come back to Part 0 only if you get a new computer or Ryan changes something on his end.

---

## Part 1 — What this system does (read once, then reference as needed)

### The problem
Every day, new foreclosure and probate cases hit the Montgomery County court records. Each one is a potential real estate investment lead. But the raw court filing only has:
- Property address
- Case number
- Sometimes owner name (often missing or in "unknown heirs" form)

To actually call and offer to buy the property, we need:
- Owner's phone number (multiple, ideally)
- Owner's email
- Property details (equity, value, beds/baths)
- A signal for whether the phone number is worth dialing

We use **three tools** to fill in that missing data:

| Tool | Cost | What it does |
|---|---|---|
| **DataSift (our CRM)** | $97/month unlimited | Adds phones, emails, and property attributes to any address we upload |
| **Tracerfy** | ~$0.02-0.04 per row | Independent skip-trace service that sometimes finds phones DataSift misses |
| **Trestle** | ~$0.015 per phone | Scores every phone number as "Dial First / Second / Third / Fourth / Drop" |

### The two-pass workflow
Because the tools are complementary (each finds phones the other misses), we run them in **two passes**:

**Pass 1** — happens automatically at 6:00 AM ET, no human needed:
- Computer scrapes the day's court records
- Enriches with property info (Smarty verifies addresses, Zillow adds property attributes, our Auditor tool fills in missing owner names, Obituary/Ancestry flag deceased owners)
- Uploads the enriched list to DataSift

**Pass 2** — needs a human (that's you). This is what you'll do daily:
- After DataSift's skip trace has finished (~30-45 min after upload), export the enriched CSV
- Run one Terminal command that:
  - Identifies records DataSift didn't find phones for
  - Runs Tracerfy on those specific records only
  - Merges results back together
  - Scores every phone with Trestle
  - Writes the final dial list
- Hand the final dial list to the dial team

---

## Part 2 — Additional access (beyond what Part 0 covers)

Part 0 already set up remote access to Ryan's Mac and DataSift credentials. Confirm you also have:

1. **Slack access to `#h3-homebuyers-ftm`** (optional but helpful — the morning cron bot posts here when Pass 1 finishes; if you see the bot post, you know the cron worked)
2. **A place to save/share the final dial list** (Google Drive folder, email, or whatever the dial team uses — ask Ryan)

That's it. Everything else in the daily workflow uses tools that are already on Ryan's Mac.

---

## Part 3 — The daily workflow (the actual steps)

### Step 1: Verify the 6 AM cron ran (~2 min)

The overnight computer job should have fired automatically. Confirm before doing anything else — this is your "did the cron work?" health check.

The Mac is configured to wake at 5:55 AM and auto-log in, so the cron reliably fires through sleep, restarts, and power outages. Missed runs are rare but possible (macOS forced updates that reboot mid-cron, transient network issues, portal outages). This step catches those.

**On the Mac, open Terminal** (Cmd+Space → type "Terminal" → Enter).

**Paste this exact command:**

```bash
ls -lt /Users/ryanhawker/Desktop/SiftStack/output/OH_Montgomery_daily_*.csv | head -3
```

Press Enter. You'll see something like:

```
-rw-r--r-- ... Jul  4 06:38 /Users/ryanhawker/Desktop/SiftStack/output/OH_Montgomery_daily_20260704_063800.csv
-rw-r--r-- ... Jul  3 06:24 /Users/ryanhawker/Desktop/SiftStack/output/OH_Montgomery_daily_20260703_062400.csv
```

**What you're looking for:** the top file's date should be **today**. If it is, Pass 1 ran successfully — skip to Step 2.

**If the top file is older than today** (Pass 1 didn't run), do this:
1. Check Slack `#h3-homebuyers-ftm` — did anything post from the bot this morning?
2. If no Slack post either, run Pass 1 manually. Paste this in Terminal:
   ```bash
   bash /Users/ryanhawker/Desktop/SiftStack/scripts/run_ohio_daily_via_chrome.sh --two-pass
   ```
   This takes 30-40 minutes. When it finishes, come back for Step 2.
3. If the manual run also fails, ping Ryan.

---

### Step 2: Confirm the day's list is in DataSift (~2 min)

Pass 1 automatically uploads to DataSift. Confirm it landed.

1. Open a web browser → go to **https://app.reisift.io** → log in.
2. Once in, click **"Records"** in the left sidebar (looks like a list icon).
3. In the **Lists** filter dropdown, look for **"H3 Montgomery Courthouse Data"**.
   - If you see today's date next to it, or a "recently updated" indicator, Pass 1 successfully uploaded.
   - You should also see the records tagged `Courthouse Data` and today's date (e.g., `2026-07`).

**If the list isn't there or looks empty:**
- Wait 5 more minutes (upload sometimes takes a moment)
- If still empty after 10 min, ping Ryan

---

### Step 3: Wait for DataSift Skip Trace to finish (~30-45 min)

After Pass 1 uploads, DataSift automatically kicks off two things:
- **Property Enrichment** (fills in Zestimate, beds, baths, equity — takes ~5-10 min)
- **Skip Trace** (finds phones + emails — takes ~30-45 min)

You need to wait for **Skip Trace** to complete before doing Pass 2.

**How to check if it's done:**

1. In DataSift, click **"Activity"** in the top right (bell icon or "Activity" text).
2. Look for the most recent **"Skip Trace"** entry.
3. Its status should say **"Complete"** or ✅ (green checkmark).

**If Skip Trace status is:**
- **"Processing" / "In Progress"** → wait, check back in 15 min
- **"Complete"** → proceed to Step 4
- **"Failed"** → ping Ryan, don't proceed

**Alternative check** — go back to Records, filter to "H3 Montgomery Courthouse Data". Every record that got skip-traced will have a tag like **`skip_traced_2026-07`**. If most records have this tag, skip trace is done.

**While you wait:** you can do other work. Don't stress about the exact minute.

---

### Step 4: Export the enriched CSV from DataSift (~3 min)

Once Skip Trace is done, download the day's records with all the new phones.

1. In DataSift, go to **Records** (left sidebar).
2. Click the **Lists** filter → select **"H3 Montgomery Courthouse Data"** (this narrows to today's records + prior days').
3. **Optional but recommended:** filter to only today's records:
   - Look for a **Tags** filter → add tag `2026-07` (or whatever month it is)
   - Or add tag `skip_traced_2026-07` to filter only records DataSift processed
4. In the top-right of the record list, click the **checkbox** in the header to **select all records**.
5. Click **"Manage"** (button near top-right) → click **"Export"** from the dropdown.
6. A dialog opens asking what to export. Choose:
   - **"All fields"** or **"Phone Enrichment CSV"** (either is fine — full-field is safer)
7. Click **Export** or **Download**.
8. The CSV downloads to your Downloads folder. It'll be named something like `DataSift_Export_20260704_074512.csv`.

**Save the file location** — you'll need the full path in the next step.

---

### Step 5: Run Pass 2 (~5 min actual, ~2 min wait for Tracerfy)

Now the Terminal command that finishes the workflow.

**Open Terminal** if it's not already open.

**Paste this command** — but replace the path in quotes with the path to the CSV you just downloaded:

```bash
cd /Users/ryanhawker/Desktop/SiftStack && TRESTLE_ENABLED=1 PYTHONPATH=src .venv/bin/python -u src/ohio_pass2.py --csv "/Users/ryanhawker/Downloads/DataSift_Export_20260704_074512.csv"
```

**How to get the CSV path exactly right:**
- In Finder, right-click the downloaded CSV → hold **Option** key → click **"Copy [filename] as Pathname"**
- Paste that path between the double quotes in the command

Press Enter. You'll see output like:

```
2026-07-04 07:52:14 INFO Pass 2: loaded 32 rows from /Users/ryanhawker/Downloads/DataSift_Export_...
2026-07-04 07:52:14 INFO Pass 2 miss detection: 24/32 rows have phones from DataSift, 8 rows are misses
2026-07-04 07:52:14 INFO Pass 2: submitting 8 miss row(s) to Tracerfy Advanced batch (~$0.32)
...
2026-07-04 07:53:20 INFO Tracerfy advanced batch complete: 3 matched, cumulative cost $0.32
2026-07-04 07:53:22 INFO Trestle: scored 98 phones, applied 87 tier tags (cost $1.47)
2026-07-04 07:53:22 INFO Pass 2 complete: wrote final dial list → output/dial_list_20260704_075322.csv

============================================================
PASS 2 SUMMARY
============================================================
  total_rows: 32
  datasift_hits: 24
  datasift_misses: 8
  tracerfy_recovered: 3
  tracerfy_phones_added: 12
  tracerfy_emails_added: 5
  trestle_phones_scored: 98
  trestle_tier_tags_applied: 87
  output_path: output/dial_list_20260704_075322.csv
```

**What to note down** (to report back to Ryan):
- `total_rows` — how many records the dial team is getting today
- `datasift_hits` — how many DataSift already had phones for
- `tracerfy_recovered` — how many extras Tracerfy pulled (this is where the two-pass workflow shines)
- `output_path` — where the final file is

**If the command errors out:**
- Screenshot the error and send to Ryan
- Common causes: wrong CSV path, DataSift export was empty, Tracerfy token expired

---

### Step 6: Deliver the final dial list (~2 min)

Find the file `output/dial_list_YYYYMMDD_HHMMSS.csv` — the exact filename is printed at the end of Step 5.

**Full path** on the Mac:
```
/Users/ryanhawker/Desktop/SiftStack/output/dial_list_20260704_075322.csv
```
(replace date/time with what Pass 2 printed)

**How to open it to verify:**
1. In Finder, navigate to `Desktop → SiftStack → output`
2. Find the newest `dial_list_*.csv`
3. Double-click to open in Excel or Numbers

**What a good dial list looks like:**
- Rows have Owner First Name + Last Name filled in
- Phone 1, Phone 2, etc. columns have numbers
- Phone Tags columns show **"Dial First"**, **"Dial Second"**, **"Dial Third"**, **"Dial Fourth"**, or **"Drop"**
- Some rows will have no phones at all (that's normal — some properties are hard to trace)

**Deliver to the dial team:**
- Upload to the shared Google Drive folder (or wherever the dial team gets their daily list)
- Include the summary numbers from Step 5 in the message

**Rename the file** for clarity before delivering:
- Change `dial_list_20260704_075322.csv` to `Montgomery_Dial_List_2026-07-04.csv`

---

## Part 4 — What each phone tier means (for the dial team's context)

Trestle scores every phone 0-100 and puts it in one of five tiers:

| Tier | Score | Meaning |
|---|---|---|
| **Dial First** | 81-100 | Best phones. Highest confidence line is active and belongs to the person. Call these first. |
| **Dial Second** | 61-80 | Good phones. Slight uncertainty but reliable. |
| **Dial Third** | 41-60 | Marginal. Worth trying but expect voicemail more often. |
| **Dial Fourth** | 21-40 | Low confidence — old number or shared line. |
| **Drop** | 0-20 | Don't dial. Almost certainly a wrong number or disconnected. |

The dial team should work Dial First → Second → Third → Fourth. Skip anything tagged Drop.

---

## Part 5 — Troubleshooting

### You saw a red ✗ post in `#h3-homebuyers-ftm` this morning

The 6 AM cron ran but the orchestrator crashed partway through. The Slack message will show:
- The exit code (any non-zero means failure)
- The Mac hostname it ran on
- Paths to the Chrome log and the orchestrator log
- Top 4 common causes

Check the causes in order:
1. **Chrome didn't launch** — kill any stale Chrome (`pkill -f siftstack-chrome-profile`) and re-run Command B from Part 8
2. **reCAPTCHA v3 blocked** — Chrome profile may have gotten poisoned. Kill scraper-Chrome and let the fresh-launch path retry
3. **DataSift login expired** — session cookies aged out. This one requires you to log in manually to DataSift in a browser on Ryan's Mac (which refreshes cookies for the automation)
4. **API token expired** (Tracerfy 401 / Trestle 402) — ping Ryan; he'll refresh the token in `.env`

For anything not on this shortlist, ping Ryan and paste the red ✗ message.

### Chrome window keeps popping up during Pass 1
- Normal. It's the scraper Chrome instance running off-screen. Don't close it.
- If a Chrome window is IN THE WAY on screen, drag it off to the side. Don't kill Chrome.

### "The CSV path doesn't exist" error in Pass 2
- Double-check the path between the quotes
- The path should start with `/Users/ryanhawker/Downloads/` — NOT `~/Downloads`
- Filename should end with `.csv`
- If the path has spaces in it (e.g. `DataSift Export`), keep it in quotes

### Pass 2 says "0 misses" — everyone got phones from DataSift
- Great news! No Tracerfy spend today. Just run through anyway — Trestle still needs to score everyone.

### Pass 2 says "0 hits from DataSift" — no one got phones
- Something is off. Either:
  - DataSift's skip trace didn't actually run — check Activity tab again
  - You exported before skip trace completed
- Wait 30 more minutes, re-export, try again.

### "Tracerfy API 401 Unauthorized"
- Tracerfy's API token expired. Ping Ryan to refresh it. Skip today's Tracerfy step (DataSift's phones are still usable — you can deliver the DataSift export as-is to the dial team).

### "Trestle rate limit" or "TRESTLE_ENABLED not set"
- Add `TRESTLE_ENABLED=1` to the beginning of the command (already in the paste-ready command above)

### DataSift shows old list with same tag as today
- DataSift dedups by property address. If a property from a prior day is re-scraped, it may not appear as "new" today. That's expected behavior — the dial team already got that record.

### Nothing works, I'm lost
- Ping Ryan
- Include: (1) which step you're on, (2) what error message, (3) screenshot if possible

---

## Part 6 — Weekly summary (for Ryan)

Once a week, send Ryan these numbers over Slack/email:

- Total records delivered to dial team this week
- Total Tracerfy recoveries (rows DataSift missed that Tracerfy filled in)
- Any days you had to run Pass 1 manually (i.e., cron missed)
- Any DataSift issues (Skip Trace failures, weird UI behavior)

Simple format:

```
Week of 2026-07-04:
- Records delivered: 217 (avg 31/day)
- Tracerfy recoveries: 24 (avg 3.4/day)
- Manual Pass 1 runs: 0
- DataSift issues: none
```

---

## Part 7 — When to escalate to Ryan (don't try to fix these yourself)

- 6 AM cron missed AND manual Pass 1 also fails
- DataSift Skip Trace stuck "In Progress" for over 2 hours
- Pass 2 errors that repeat after retrying
- Any API cost that looks unusually high (>$5 for Pass 2 in a day)
- Chrome broken / won't launch
- Tracerfy or Trestle API keys expired

For everything else, use the troubleshooting section above.

---

## Part 8 — Quick reference (bookmark this section)

**Daily routine (three commands total):**

1. Check cron ran:
```bash
ls -lt /Users/ryanhawker/Desktop/SiftStack/output/OH_Montgomery_daily_*.csv | head -1
```

2. If cron didn't run:
```bash
bash /Users/ryanhawker/Desktop/SiftStack/scripts/run_ohio_daily_via_chrome.sh --two-pass
```

3. Pass 2 after DataSift export:
```bash
cd /Users/ryanhawker/Desktop/SiftStack && TRESTLE_ENABLED=1 PYTHONPATH=src .venv/bin/python -u src/ohio_pass2.py --csv "PATH_TO_DATASIFT_CSV_HERE"
```

**Key file locations:**
- Pass 1 daily CSVs: `/Users/ryanhawker/Desktop/SiftStack/output/OH_Montgomery_daily_*.csv`
- Pass 2 final dial lists: `/Users/ryanhawker/Desktop/SiftStack/output/dial_list_*.csv`
- DataSift login: https://app.reisift.io

**Time estimate:**
- Step 1-2 (verify): 5 min
- Step 3 (wait): 30-45 min (do other work)
- Step 4-6 (export + Pass 2 + deliver): 10 min
- **Total active work: ~15-20 min/day**
