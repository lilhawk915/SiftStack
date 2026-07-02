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

REPO_ROOT="/Users/ryanhawker/Desktop/SiftStack"
CHROME_APP="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT=9222
CDP_URL="http://localhost:${CDP_PORT}"
SCRAPER_PROFILE="$HOME/.siftstack-chrome-profile"
CHROME_LOG="$REPO_ROOT/logs/chrome_cdp.log"

mkdir -p "$REPO_ROOT/logs"

# 1. Fast path: is the dedicated scraper Chrome already listening?
if curl -s --max-time 2 "${CDP_URL}/json/version" >/dev/null 2>&1; then
    echo "Reusing existing scraper Chrome on port ${CDP_PORT}."
else
    echo "No Chrome on debug port ${CDP_PORT}. Launching off-screen..."

    # Edge case: stale scraper-Chrome process still holding the profile
    # lock but not the port (crashed mid-session). Nuke only processes
    # tied to OUR dedicated profile — never the operator's daily-driver
    # Chrome. `pkill -f` matches on the full command line so the
    # --user-data-dir path filter is safe.
    if pgrep -f "siftstack-chrome-profile" >/dev/null 2>&1; then
        echo "Cleaning up stale scraper-Chrome process(es)..."
        pkill -f "siftstack-chrome-profile" || true
        sleep 1
    fi

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

exec .venv/bin/python -u src/ohio_orchestrator.py daily "$@"
