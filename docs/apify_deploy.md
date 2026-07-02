# Apify Deploy Guide — SiftStack Ohio Orchestrator

Wiring up to Apify replaces the launchd cron on your MacBook with a
managed daily run + monitoring + logs UI. Primary reason to move: the
2026-07-01 pro.mcohio.org reCAPTCHA v3 block on your home IP. Apify
gives you clean IPs (and residential proxy add-on if the free tier's
datacenter proxies also get flagged).

## Prerequisites (one-time)

```bash
# Install Apify CLI
npm install -g apify-cli

# Log in (opens browser to Apify Console for OAuth)
apify login
```

## First deploy (from repo root)

```bash
cd ~/Desktop/SiftStack

# Push the actor definition + build the Docker image on Apify's side
apify push
```

This picks up `.actor/actor.json` (name: `siftstack-ohio-orchestrator`)
and the `Dockerfile`. Wait ~3-5 min for the build to complete.

## Set secrets in Apify Console

Once the build finishes, Apify Console shows the Actor. Go to
**Actor → Source → Input** and populate the secret fields from your
local `.env`:

Required (Ohio-specific):
- `captcha_api_key` — from `CAPTCHA_API_KEY`
- `anthropic_api_key` — from `ANTHROPIC_API_KEY`

Recommended:
- `tracerfy_api_key` — from `TRACERFY_API_KEY`
- `trestle_api_key` — from `TRESTLE_API_KEY`
- `slack_bot_token` — from `SLACK_BOT_TOKEN` (starts with `xoxb-`)

Optional:
- `smarty_auth_id` / `smarty_auth_token`
- `datasift_email` / `datasift_password` (only if `upload_datasift=true`)

Apify's "Secret Store" encrypts these at rest and injects them at run time.
Never commit them to `input.json`.

## First test run (manual)

In Apify Console → Actor page → **Start** button. Use these input values:

```json
{
    "mode": "daily",
    "dry_run": false,
    "sheriff_new_only": true,
    "onbase_enabled": true,
    "tracerfy_enabled": true,
    "trestle_enabled": true
}
```

Wait ~25 min for the run to complete. Check:
1. **Log tab** — should show `Sheriff sale: emitted N new` and no
   `reCAPTCHA blocked` errors
2. **Storage → Key-value store → run_summary** — should show records > 0
3. **#h3-homebuyers-ftm Slack channel** — CSV should post

## If the run hits the reCAPTCHA v3 block

The Actor's D.1 guardrail will surface it clearly in the logs:
`reCAPTCHA blocked at https://pro.mcohio.org/ — reason=score_too_low`

Options in order of increasing cost:

1. **Bump min score in the input** — set `pro_mcohio_recaptcha_v3_min_score`
   to `0.7` or `0.9`. Costs $0 extra.
2. **Capture the real action string** — open pro.mcohio.org in a real
   Chrome incognito on your home wifi, DevTools → Network → filter
   `recaptcha`, run a MORTGAGE FORECLOSURE search, find the `act=` query
   param on the `reload` request. Set `pro_mcohio_recaptcha_v3_action`
   to that value. Costs $0 extra.
3. **Enable Apify Residential Proxy add-on** — $8-12/mo. In Actor
   Settings → Proxy configuration, enable Residential Proxy. Add
   `usApify=true` and `groups=RESIDENTIAL`. Also add:
   ```
   PROXY_CONFIG_URL=http://groups-RESIDENTIAL:${APIFY_PROXY_PASSWORD}@proxy.apify.com:8000
   ```
   to the Actor input. This solves the IP-fingerprint issue structurally
   — 2Captcha may not even be needed.

## Schedule the daily run

Once one manual run succeeds:

1. Apify Console → **Schedules** → **Create Schedule**
2. Name: `siftstack-ohio-daily`
3. Cron: `0 10 * * *` (6 AM ET = 10 AM UTC, adjusted for daylight saving)
   or `0 11 * * *` in winter — Apify runs in UTC
4. Actor: `siftstack-ohio-orchestrator`
5. Input: same JSON as your test run
6. Save

## Retire the launchd cron

Only after 3-5 successful daily Apify runs. Preserve the local plist
for now as a fallback:

```bash
launchctl unload ~/Library/LaunchAgents/com.siftstack.ohio-daily.plist
mv ~/Library/LaunchAgents/com.siftstack.ohio-daily.plist \
   ~/Library/LaunchAgents/com.siftstack.ohio-daily.plist.retired-YYYYMMDD
```

## Cost expectation

| Item | Cost/mo |
|---|---|
| Actor compute (~25 min × 30 runs × 2GB) | $7-8 (over the $5 free tier) |
| Datacenter proxy (bundled) | $0 |
| Residential proxy add-on (if needed) | +$8-12 |
| 2Captcha (only if residential proxy isn't enough) | ~$0.10 |
| **Total, best case** | **$7-8/mo (Personal plan required)** |
| **Total, likely case** | **$15-20/mo** |

The Apify guide's "~$5/mo" number was optimistic — real production
Ohio pipelines land in the $7-20 range depending on proxy needs.

## Local dev + test cycle

```bash
# Simulate the Actor locally (reads ~/.actor/input.json — gitignored)
apify run --purge

# Iterate the manifest without pushing
apify validate-schema
```

For a local input JSON that mimics production:

```json
{
    "mode": "daily",
    "captcha_api_key": "YOUR_KEY",
    "anthropic_api_key": "YOUR_KEY",
    "tracerfy_api_key": "YOUR_KEY",
    "trestle_api_key": "YOUR_KEY",
    "slack_bot_token": "xoxb-...",
    "onbase_enabled": true,
    "tracerfy_enabled": true,
    "trestle_enabled": true,
    "dry_run": false
}
```

Save as `input.json` (already gitignored) at repo root.
