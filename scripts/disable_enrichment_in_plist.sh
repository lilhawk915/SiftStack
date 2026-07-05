#!/usr/bin/env bash
# Disable the Tracerfy + Trestle enrichment phases in the SiftStack launchd jobs
# by flipping their env flags to 0, then reload the agents. The orchestrator
# code is left untouched (dormant fallback) — only the env gating changes.
#
# Run on your Mac:   bash ~/Desktop/SiftStack/disable_enrichment_in_plist.sh
set -euo pipefail

UID_NUM="$(id -u)"

flip() {
  local plist="$1" label="$2"
  [ -f "$plist" ] || { echo "skip (not found): $plist"; return 0; }
  echo "== $label =="
  for VAR in TRACERFY_ENABLED TRESTLE_ENABLED; do
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:$VAR 0" "$plist" 2>/dev/null \
      || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:$VAR string 0" "$plist"
  done
  echo "  EnvironmentVariables now:"
  /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables" "$plist" | sed 's/^/    /'
  # Reload the agent so launchd picks up the new env.
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$plist"
  echo "  reloaded $label"
  echo
}

flip "$HOME/Library/LaunchAgents/com.siftstack.ohio-daily.plist"  com.siftstack.ohio-daily
# Uncomment if your weekly job also had the flags set:
# flip "$HOME/Library/LaunchAgents/com.siftstack.ohio-weekly.plist" com.siftstack.ohio-weekly

echo "Done. Verify the live env launchd will use:"
launchctl print "gui/$UID_NUM/com.siftstack.ohio-daily" | grep -iA8 'environment' || true
