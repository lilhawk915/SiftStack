#!/bin/bash
# Ohio daily orchestrator via a dedicated headless Chrome (BUG-04 mitigation).
#
# pro.mcohio.org deployed reCAPTCHA v3 + IP-based proxy detection on
# 2026-07-01. None of the free-or-cheap deployment paths (GH Actions,
# Apify, IPRoyal residential/mobile) can reach the site's foreclosure
# results anymore — the anti-bot layer blocks scraping-service IP
# ranges at the network level. The workable path is running the
# scraper INSIDE a Chrome instance on the operator's home network:
# residential IP + real Chrome binary → organic v3 score.
#
# Since 2026-07-02 this Chrome runs:
#   * headed but off-screen (--window-position=-3000,-3000). We tried
#     --headless=new first — v3 flagged it (score_too_low) even with
#     UA spoofing and --disable-blink-features=AutomationControlled;
#     pro.mcohio.org's v3 config appears to use WebGL/client-hints or
#     other deeper signals that trip headless. Off-screen headed
#     restores the organic v3 score (verified 2026-07-02 with a
#     2026-06-29 → 2026-06-30 backfill returning 61 rows / 12 cases,
#     matching the pre-block baseline) at the cost of a Chrome Dock
#     icon during the run.
#   * in a DEDICATED persistent profile (not the operator's default
#     Chrome), so cookies + v3 reputation accumulate across runs
#     without touching the operator's daily-driver browser
#   * on a dedicated debug port (9222) — no collision with any Chrome
#     the operator has open
#
# This script:
#   1. Reuses the headless Chrome if the debug port already responds
#      (fast path: cookies/reputation stay hot across same-day retries).
#   2. Otherwise launches a fresh headless Chrome on port 9222 using
#      the dedicated profile.
#   3. Sets CHROME_CDP_URL so MontgomeryScraper's connect_over_cdp
#      path activates.
#   4. Runs the standard Ohio daily orchestrator.
#
# The scraper Chrome stays running after the scrape completes; the
# next cron run reuses it. This is intentional — a warm profile
# scores higher on v3 than a cold restart every time.

set -e

REPO_ROOT="/Users/ryanhawker/SiftStack"
CHROME_APP="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT=9222
CDP_URL="http://localhost:${CDP_PORT}"
SCRAPER_PROFILE="$HOME/.siftstack-chrome-profile"
CHROME_LOG="$REPO_ROOT/logs/chrome_cdp.log"

mkdir -p "$REPO_ROOT/logs"

# 1. Force-restart Chrome on every run. Previously we tried to REUSE
#    an existing Chrome if port 9222 was already responding — that
#    saves ~5s of startup and keeps the profile warm for v3 scoring.
#    BUT: Chrome's CDP session degrades after ~24 hours of uptime.
#    Playwright's connect_over_cdp handshake times out against the
#    degraded session even though /json/version responds normally.
#    Verified 2026-07-06: cron reused a 20-hour-old Chrome, FC + sheriff
#    scrapers both failed with connect_over_cdp timeouts, 0 records
#    shipped. Fresh Chrome on every run is worth the 5s cost.
#
#    v3 scoring reputation lives in the profile dir (~/.siftstack-chrome-profile)
#    which persists across restarts, so we don't lose any warmth.
#
# Kill only the SPECIFIC scraper Chrome PID whose PARENT is init/launchd
# (that's the top-level app process; children die with it when it exits).
#
# History: an earlier version used `pkill -f "siftstack-chrome-profile"`
# which pattern-matched every process (parent + all helpers) carrying
# that string in its command line. That cascaded through Chrome's
# internal app-process manager and killed the operator's daily-driver
# Chrome too — verified 2026-07-06 when a manual kickstart during
# operator use closed all their Chrome windows.
#
# Surgical fix: find the scraper Chrome's PARENT process (PPID == 1,
# i.e. spawned by launchd/init and not a subprocess) and TERM it.
# Killing the parent triggers Chrome's normal shutdown, which cascades
# to ITS children only — not to any other Chrome instance the user
# has open under a different profile.
SCRAPER_PARENTS=$(pgrep -P 1 -f "siftstack-chrome-profile" || true)
if [ -n "$SCRAPER_PARENTS" ]; then
    echo "Killing scraper Chrome parent PID(s): $SCRAPER_PARENTS"
    for pid in $SCRAPER_PARENTS; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 2
fi
echo "Launching fresh Chrome off-screen..."
# Single unconditional-launch path — same as the "else" branch below.
# (The reuse fast-path was removed in the same edit.)
if true; then

    # Launch. Key flags:
    #   --window-position=-3000,-3000  render off-screen so the operator
    #                            never sees the window. Verified with
    #                            pro.mcohio.org's v3 (headless failed;
    #                            off-screen headed passed). AppKit still
    #                            renders the window fully, so Playwright
    #                            visibility checks work.
    #   --window-size=1920,1080  match a real desktop viewport (v3 uses
    #                            client-hint viewport in scoring).
    #   --user-data-dir=...      dedicated persistent profile
    #   --remote-debugging-port  the CDP endpoint MontgomeryScraper
    #                            attaches to
    #   --no-first-run / --no-default-browser-check  suppress the
    #                            welcome flows a fresh profile would
    #                            otherwise open
    "$CHROME_APP" \
        --remote-debugging-port="$CDP_PORT" \
        --user-data-dir="$SCRAPER_PROFILE" \
        --window-position=-3000,-3000 \
        --window-size=1920,1080 \
        --no-first-run \
        --no-default-browser-check \
        > "$CHROME_LOG" 2>&1 &
    CHROME_PID=$!
    echo "Launched Chrome PID=$CHROME_PID → $CHROME_LOG"

    # Wait for port bind. Chrome usually binds within 2-3s cold; give
    # 10s max before giving up.
    for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        if curl -s --max-time 1 "${CDP_URL}/json/version" >/dev/null 2>&1; then
            echo "Chrome debug port ready after ${i}s"
            break
        fi
        if [ "$i" -eq 10 ]; then
            echo "ERROR: Chrome debug port never came up. Aborting."
            echo "  Check $CHROME_LOG for launch errors."
            exit 1
        fi
    done
fi

# 2. Confirm the port responds + log which Chrome build we got
CHROME_VERSION=$(curl -s --max-time 2 "${CDP_URL}/json/version" | python3 -c "import json, sys; print(json.load(sys.stdin).get('Browser','?'))")
echo "Chrome ready: $CHROME_VERSION"

# 3. Run the orchestrator with CDP env var set
cd "$REPO_ROOT"
export CHROME_CDP_URL="$CDP_URL"
export PRO_MCOHIO_RECAPTCHA_V3_ACTION="genSearch"
export SHERIFF_NEW_ONLY=1
export ONBASE_ENABLED=1
export TRACERFY_ENABLED=1
export TRESTLE_ENABLED=1

# Load .env into environment
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Run the orchestrator. On failure, POST to Slack so silent misses
# become visible. Success is already announced by slack_poster.py
# inside the orchestrator (posts the CSV to #h3-homebuyers-ftm),
# so this only fires the red-X path.
if .venv/bin/python -u src/ohio_orchestrator.py daily "$@"; then
    echo "Orchestrator exited 0 (success). Slack success post already fired from inside."
    exit 0
fi

ORCH_EXIT=$?
echo "Orchestrator FAILED with exit code $ORCH_EXIT"

# Failure notification. Same channel as success posts (#h3-homebuyers-ftm,
# ID C0B1ZPMMMUK) so operator sees both in one place. Uses SLACK_BOT_TOKEN
# already exported above via .env.
if [ -n "$SLACK_BOT_TOKEN" ]; then
    HOSTNAME_SHORT=$(hostname -s 2>/dev/null || echo "mac")
    LOG_TAIL=$(tail -20 "$CHROME_LOG" 2>/dev/null | tail -c 1500 || echo "(no chrome log)")
    curl -s -X POST https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        -H "Content-type: application/json; charset=utf-8" \
        --data @- <<EOF
{
  "channel": "C0B1ZPMMMUK",
  "text": ":x: *SiftStack local Mac cron FAILED* — orchestrator exit code $ORCH_EXIT on host $HOSTNAME_SHORT at $(date '+%Y-%m-%d %H:%M %Z'). Check logs at $REPO_ROOT/logs/ohio_daily.err and Chrome log at $CHROME_LOG. Common causes: (1) Chrome not launched — check the fast-path curl in the script; (2) reCAPTCHA v3 blocked headless — Chrome profile may be poisoned, kill scraper-Chrome and retry; (3) DataSift login expired — refresh cookies; (4) API token expired (Tracerfy/Trestle 401). Manual retry: \`bash $0\`."
}
EOF
    echo "Slack failure notification sent."
else
    echo "SLACK_BOT_TOKEN not set — skipping failure notification."
fi

exit $ORCH_EXIT
