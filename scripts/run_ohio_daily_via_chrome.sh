#!/bin/bash
# Ohio daily orchestrator via operator's local Chrome (BUG-04 mitigation).
#
# pro.mcohio.org deployed reCAPTCHA v3 + IP-based proxy detection on
# 2026-07-01. None of the free-or-cheap deployment paths (GH Actions,
# Apify, IPRoyal residential/mobile) can reach the site's foreclosure
# results anymore — the anti-bot layer blocks scraping-service IP
# ranges at the network level. The workable path is running the
# scraper INSIDE the operator's own daily-driver Chrome instance:
# residential IP + real browsing history + organic v3 score.
#
# This script:
#   1. Ensures Chrome is running with --remote-debugging-port=9222.
#      If not running, launches it in the background using the
#      operator's default Chrome profile. If running WITHOUT the
#      debug port, does nothing (we don't want to interrupt an
#      active browsing session by restarting Chrome).
#   2. Sets CHROME_CDP_URL so MontgomeryScraper's connect_over_cdp
#      path activates.
#   3. Runs the standard Ohio daily orchestrator.
#
# The Chrome process stays running after the scrape completes so the
# operator can keep using it normally.

set -e

REPO_ROOT="/Users/ryanhawker/Desktop/SiftStack"
CHROME_APP="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT=9222
CDP_URL="http://localhost:${CDP_PORT}"

# 1. Is Chrome already running with the debug port?
if ! curl -s --max-time 2 "${CDP_URL}/json/version" >/dev/null 2>&1; then
    echo "Chrome not listening on debug port ${CDP_PORT}. Launching..."
    if pgrep -x "Google Chrome" >/dev/null; then
        echo "WARNING: Chrome is already running WITHOUT a debug port."
        echo "  Not restarting to avoid interrupting an active session."
        echo "  Manual action: fully quit Chrome (Cmd+Q), then re-run this script."
        echo "  OR: start Chrome once with:  '$CHROME_APP' --remote-debugging-port=$CDP_PORT &"
        exit 2
    fi
    # Fresh launch with debug port + normal profile
    "$CHROME_APP" \
        --remote-debugging-port="$CDP_PORT" \
        --restore-last-session \
        > /dev/null 2>&1 &

    # Give Chrome ~5s to bind the port
    for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        if curl -s --max-time 1 "${CDP_URL}/json/version" >/dev/null 2>&1; then
            echo "Chrome debug port ready after ${i}s"
            break
        fi
        if [ "$i" -eq 10 ]; then
            echo "ERROR: Chrome debug port never came up. Aborting."
            exit 1
        fi
    done
fi

# 2. Confirm the port responds
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

exec .venv/bin/python -u src/ohio_orchestrator.py daily
