# GitHub Actions Deploy — SiftStack Ohio Daily

Runs the daily Montgomery pipeline on GitHub's infrastructure at 6 AM
Eastern. Free within GH Actions limits (2000 min/month private, unlimited
public); no separate scheduling service needed.

Same limitation as Apify: **GH Actions runners use Azure datacenter IPs,
which the pro.mcohio.org reCAPTCHA v3 policy is likely to flag.** The
D.1 guardrail (commit `0a9f1b6`) will surface it clearly — you get a
Slack `:x:` failure ping instead of silent 0-record CSVs. If FC-scrape
fails on GH Actions, the fallback is a residential proxy (no free tier
option) or accepting FC data loss until portal policy changes.

Probate + sheriff sale (different domains) run cleanly regardless.

## Step 1 — Fork the upstream repo

You cloned `tyvhb/SiftStack` directly, so your `origin` remote points
at the upstream. All 20+ commits you've made on this machine (D.1
guardrail, D.2 solver, Apify manifest, Ohio backfill fixes, etc.) live
only on your laptop right now. First step: create your own fork so you
have somewhere to push them.

### Option A — via `gh` CLI (fastest, ~2 min)

```bash
# Authenticate the gh CLI (opens a browser, GitHub OAuth flow)
gh auth login
#   1. Where do you use GitHub?           → GitHub.com
#   2. What is your preferred protocol?   → HTTPS
#   3. Authenticate with your credentials → Login with a web browser
#      (follow the browser prompts; paste the one-time code)

# Fork tyvhb/SiftStack into your account.
# Passing --remote=false so it does NOT touch our local git remotes —
# we do that manually in Step 2 to keep your local commits safe.
gh repo fork tyvhb/SiftStack --remote=false --clone=false

# Confirm your fork exists:
gh repo view <your-username>/SiftStack --json url,defaultBranchRef
```

### Option B — via GitHub website (if you don't want to authenticate the CLI)

1. Open <https://github.com/tyvhb/SiftStack> in your browser.
2. Click **Fork** in the top-right.
3. Choose your account as the destination.
4. Uncheck "Copy the main branch only" if you want all branches.
5. Click **Create fork**. Note the URL of your fork
   (e.g. `https://github.com/lilhawk915/SiftStack`).

## Step 2 — Point your local `origin` at your fork

Right now your `origin` still points at the upstream. This command
rewires it to your fork so `git push` sends code to your own copy.
Replace `YOUR-USERNAME` with your GitHub handle.

```bash
cd ~/Desktop/SiftStack

# Rename the existing origin → upstream so you can still pull upstream changes later
git remote rename origin upstream

# Add your fork as the new origin
git remote add origin https://github.com/YOUR-USERNAME/SiftStack.git

# Confirm — should show: upstream → tyvhb/SiftStack, origin → YOUR-USERNAME/SiftStack
git remote -v
```

## Step 3 — Push your local commits to the fork

```bash
# Set up main to track your fork's main and push everything
git push -u origin main
```

If GitHub asks you to authenticate, use either:
- **HTTPS + Personal Access Token** — Settings → Developer settings → Personal access tokens → Fine-grained → generate token with `repo` scope. Paste when prompted for password.
- **`gh auth setup-git`** — configures git to use your gh auth automatically. Simplest if you already ran `gh auth login`.

Confirm the push worked:

```bash
# Should show your commits on the fork
gh repo view --web
```

## Step 4 — Store secrets in your fork

GitHub Actions reads secrets from repo settings. Set each of these — the
workflow references them as `${{ secrets.NAME }}`. Grab the values from
your local `~/.env` file.

### Option A — via `gh` CLI

```bash
cd ~/Desktop/SiftStack

# Load your .env and set each key as a repo secret
# (--repo defaults to origin, which is now your fork)
while IFS='=' read -r key value; do
  # Skip comments + blank lines
  [[ -z "$key" || "$key" == \#* ]] && continue
  case "$key" in
    CAPTCHA_API_KEY|ANTHROPIC_API_KEY|SMARTY_AUTH_ID|SMARTY_AUTH_TOKEN|\
    TRACERFY_API_KEY|TRESTLE_API_KEY|DATASIFT_EMAIL|DATASIFT_PASSWORD|\
    SLACK_BOT_TOKEN|SLACK_WEBHOOK_URL)
      # Strip surrounding quotes if present
      value="${value%\"}"; value="${value#\"}"
      echo "Setting $key"
      echo -n "$value" | gh secret set "$key"
      ;;
  esac
done < .env

# Confirm secrets are set
gh secret list
```

### Option B — via GitHub website

Repo → Settings → Secrets and variables → Actions → **New repository
secret**. Add each of:

Required:
- `CAPTCHA_API_KEY`
- `ANTHROPIC_API_KEY`
- `SLACK_BOT_TOKEN` (starts with `xoxb-`)

Recommended:
- `TRACERFY_API_KEY`
- `TRESTLE_API_KEY`

Optional (used only if enabled):
- `SMARTY_AUTH_ID`, `SMARTY_AUTH_TOKEN`
- `DATASIFT_EMAIL`, `DATASIFT_PASSWORD`
- `SLACK_WEBHOOK_URL` (fallback if bot token isn't set)

## Step 5 — (Optional) Override reCAPTCHA v3 config as repo Variables

If DevTools inspection reveals the site's real `action` string, or you
want to bump the min_score, set these as GH Actions Variables (NOT
secrets — they're safe to expose):

```bash
gh variable set PRO_MCOHIO_RECAPTCHA_V3_ACTION -b "search"
gh variable set PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE -b "0.7"
```

Skip this step if you're OK with the workflow defaults (`submit` +
`0.3`).

## Step 6 — First manual run

The workflow is set to run at 6 AM ET automatically, but you should
kick off a manual test first.

### Via `gh` CLI

```bash
gh workflow run "Ohio Daily Scrape"

# Watch it run
gh run watch
```

### Via GitHub website

Actions tab → "Ohio Daily Scrape" (left sidebar) → **Run workflow**
button (right side) → leave inputs blank for a normal run → Run.

## Step 7 — Inspect the result

- **Success**: the workflow's own Slack post lands in `#h3-homebuyers-ftm`
  from the orchestrator, plus a CSV file is available under the run's
  **Artifacts** section for 30 days.
- **Failure**: the workflow posts `:x: *SiftStack daily FAILED*` to
  `#h3-homebuyers-ftm` with a link to the run's logs.

Most likely failure on first run: `RecaptchaBlockedError` — the D.1
guardrail firing because GH Actions' IP hit the reCAPTCHA v3 wall.
Options:
1. Try changing the `PRO_MCOHIO_RECAPTCHA_V3_ACTION` variable to another
   likely value (`search`, `verify`) and re-run.
2. Bump `PRO_MCOHIO_RECAPTCHA_V3_MIN_SCORE` to `0.7` or `0.9`.
3. Add a residential proxy (paid; no free option on GH Actions — you
   pipe requests through a service like Bright Data + set an env var).
4. Move to Apify with residential proxy add-on (`docs/apify_deploy.md`).

## Cron timing across daylight saving

The workflow uses `0 10 * * *` (10:00 UTC), which is 6 AM Eastern
Daylight Time. When the US clock falls back in November, this fires at
5 AM Eastern Standard Time. Two options:

- **Leave it** — a 5 AM fire in winter is fine; the courthouse portal
  hasn't posted the day's filings yet either way.
- **Add a second schedule** — put `0 11 * * *` behind an OR in the `on:
  schedule:` block during winter months. Not recommended (adds
  complexity for a 1-hour difference).

## Cost math

| Compute | Public repo | Private repo |
|---|---|---|
| Per daily 25-min run | Free | 25 min |
| Monthly (30 runs) | Free | 750 min |
| Free tier | Unlimited | 2000 min/mo |
| Overage rate | N/A | $0.008/min |

If your fork is public: **$0/mo forever** for the compute. If private,
you're well within the free tier at ~1000 min/mo headroom.

Not included: Slack API calls (free), 2Captcha ($~0.10/mo), OnBase
Vision ($~1-2/mo), Tracerfy + Trestle ($~0.50/mo total).

## Rollback plan

If GH Actions fails to run OR the FC scrape hits the reCAPTCHA block
persistently, fall back to the launchd cron on your MacBook:

```bash
# The plists are still in place
launchctl load ~/Library/LaunchAgents/com.siftstack.ohio-daily.plist

# Disable the GH Actions schedule (edit .github/workflows/ohio-daily.yml)
# — comment out the `- cron: '0 10 * * *'` line under `on: schedule:`,
# leaving only workflow_dispatch for manual triggers.
```
