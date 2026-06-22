# Ohio Orchestrator — Cron Wiring

The Ohio pipeline runs from three production cron slots that NEVER
mix records between DataSift lists.

| When | Mode | Source types | Counties | DataSift list |
|---|---|---|---|---|
| Daily 6:00 AM ET | `daily` | foreclosure + probate + sheriff_sale | Montgomery | **H3 Montgomery Courthouse Data** |
| Monday 6:00 AM ET | `weekly` | foreclosure + probate + sheriff_sale | Butler, Clark, Clermont, Greene, Miami, Warren | **H3 SW Ohio Courthouse Data** |
| Every 3 months | `quarterly` | tax_delinquent (+ parcel→address enrichment) | **Montgomery only** | H3 Montgomery Courthouse Data |

Daily + weekly cover fresh court activity (high cadence, fast scrape).
Quarterly handles the slow-changing tax-delinquent feed with
expensive parcel→address enrichment (~15 min at concurrency=5).
**Montgomery-only** — the iasWorld lookup is mcrealestate.org-
specific, so other counties' tax_delinquent feeds are skipped to
avoid producing records without addresses.

The county→list routing is enforced in
[`src/ohio_destination_lists.py`](../src/ohio_destination_lists.py)
and locked by [`tests/test_ohio_destination_lists.py`](../tests/test_ohio_destination_lists.py)
(30 tests).

## CLI

```bash
# Daily Montgomery → H3 Montgomery Courthouse Data
python src/ohio_orchestrator.py daily

# Weekly other-6 → H3 SW Ohio Courthouse Data
python src/ohio_orchestrator.py weekly

# Quarterly tax_delinquent → both lists (per-county routing)
python src/ohio_orchestrator.py quarterly

# Operator escape hatches:
python src/ohio_orchestrator.py daily --dry-run        # print plan, no scrape
python src/ohio_orchestrator.py daily --no-upload      # scrape + CSV, no DataSift
python src/ohio_orchestrator.py quarterly --no-upload  # spot-check before pushing
python src/ohio_orchestrator.py weekly --headed        # visible browser (debug)
python src/ohio_orchestrator.py weekly -v              # DEBUG-level logging
```

## Cron wiring

### macOS — `launchd`

Two `.plist` files in `~/Library/LaunchAgents/`:

`com.siftstack.ohio-daily.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.siftstack.ohio-daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/ryanhawker/Desktop/SiftStack/.venv/bin/python</string>
    <string>/Users/ryanhawker/Desktop/SiftStack/src/ohio_orchestrator.py</string>
    <string>daily</string>
  </array>
  <key>WorkingDirectory</key>
    <string>/Users/ryanhawker/Desktop/SiftStack</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>6</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
    <string>/Users/ryanhawker/Desktop/SiftStack/logs/ohio_daily.log</string>
  <key>StandardErrorPath</key>
    <string>/Users/ryanhawker/Desktop/SiftStack/logs/ohio_daily.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TZ</key><string>America/New_York</string>
    <key>DATASIFT_EMAIL</key><string>YOUR_EMAIL</string>
    <key>DATASIFT_PASSWORD</key><string>YOUR_PASSWORD</string>
  </dict>
</dict>
</plist>
```

`com.siftstack.ohio-weekly.plist`: same shape with these changes:
- `Label` = `com.siftstack.ohio-weekly`
- Third element of `ProgramArguments` = `weekly`
- Replace `StartCalendarInterval` with:
  ```xml
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>1</integer> <!-- Monday -->
    <key>Hour</key><integer>6</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  ```

`com.siftstack.ohio-quarterly.plist`: same shape, fires four times
a year. `launchd` doesn't have a native "every 3 months" trigger,
so use an array of `StartCalendarInterval` entries — one per
quarter:
- `Label` = `com.siftstack.ohio-quarterly`
- Third element of `ProgramArguments` = `quarterly`
- Replace `StartCalendarInterval` with:
  ```xml
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Month</key><integer>1</integer><key>Day</key><integer>15</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Month</key><integer>4</integer><key>Day</key><integer>15</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Month</key><integer>7</integer><key>Day</key><integer>15</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Month</key><integer>10</integer><key>Day</key><integer>15</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  ```
  (Pick whatever calendar days you prefer — Jan 15 / Apr 15 / Jul 15 /
  Oct 15 keeps each quarter slightly past the tax-due deadlines.)

Load them:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.siftstack.ohio-daily.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.siftstack.ohio-weekly.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.siftstack.ohio-quarterly.plist
```

### Linux — `cron`

```cron
# /etc/cron.d/siftstack-ohio
TZ=America/New_York

# Daily Montgomery
0 6 * * *   ryanhawker   cd /opt/siftstack && /opt/siftstack/.venv/bin/python src/ohio_orchestrator.py daily

# Weekly other-6 (Monday)
0 6 * * 1   ryanhawker   cd /opt/siftstack && /opt/siftstack/.venv/bin/python src/ohio_orchestrator.py weekly

# Quarterly tax_delinquent (Jan / Apr / Jul / Oct, 15th at 6 AM)
0 6 15 1,4,7,10 *   ryanhawker   cd /opt/siftstack && /opt/siftstack/.venv/bin/python src/ohio_orchestrator.py quarterly
```

### `systemd` timer (preferred on modern Linux)

`/etc/systemd/system/siftstack-ohio-daily.service`:
```ini
[Unit]
Description=SiftStack Ohio — daily Montgomery pull
After=network-online.target

[Service]
Type=oneshot
User=ryanhawker
WorkingDirectory=/opt/siftstack
ExecStart=/opt/siftstack/.venv/bin/python src/ohio_orchestrator.py daily
EnvironmentFile=/opt/siftstack/.env
```

`/etc/systemd/system/siftstack-ohio-daily.timer`:
```ini
[Unit]
Description=SiftStack Ohio daily — 6 AM ET

[Timer]
OnCalendar=*-*-* 06:00:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
```

Same shape for `siftstack-ohio-weekly.{service,timer}` with
`OnCalendar=Mon *-*-* 06:00:00 America/New_York` in the timer.

Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now siftstack-ohio-daily.timer
sudo systemctl enable --now siftstack-ohio-weekly.timer
```

## Operator checklist

Before the first production cron firing:

- [ ] Smoke run both modes with `--dry-run` to confirm the
      county→list plan
- [ ] Smoke run both modes with `--no-upload` to confirm scrape
      succeeds + CSV is well-formed
- [ ] Check `output/OH_Montgomery_daily_*.csv` and
      `output/OH_SW_Ohio_weekly_*.csv` have records, county column
      matches the bucket, `Lists` column has the right value
- [ ] Manually upload one CSV via the DataSift wizard to verify
      list mapping + tag stacking work
- [ ] Activate the cron / launchd / systemd timer
- [ ] Schedule a Slack-notify hook on the orchestrator's exit code
      so a failure pages the on-call

## Cross-contamination guard

The routing is enforced at multiple layers — if any of these were
violated, the test suite would fail before deploy:

1. **`destination_list_for_county()`** raises `ValueError` on unknown
   counties — no silent fallback to the wrong list.
2. **`split_by_destination_list()`** buckets EVERY notice by its
   county; the orchestrator never bypasses this.
3. **`run_daily()` plan**: never includes a non-Montgomery county.
4. **`run_weekly()` plan**: never includes Montgomery.
5. **Tests**: `test_dry_run_never_lets_montgomery_into_the_weekly_bucket`
   + `test_run_daily_never_writes_sw_ohio_csv` lock both directions.

If a future operator passes `--counties Montgomery,Butler` to a
hand-rolled CLI (not the daily/weekly modes), `split_by_destination_list`
will still bucket them into 2 lists with no cross-contamination, but
that's an off-script invocation — production should always go through
`daily` or `weekly`.
