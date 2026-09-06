#!/usr/bin/env bash
# app-sleep-disable.sh — Guide step 22. RUN INSIDE imsg01's OWN SESSION (not clarioinc's).
# Disables App Nap / App Sleep for Messages.app, then reads it back. Re-login or reboot afterwards
# and repeat the idle tests (t16). Only apply if the unmodified baseline (step 17) failed.
set -euo pipefail
[[ "$(whoami)" == "imsg01" ]] || { echo "must run as imsg01 (current user: $(whoami))" >&2; exit 1; }
defaults write com.apple.iChat NSAppSleepDisabled -bool YES
echo -n "NSAppSleepDisabled = "; defaults read com.apple.iChat NSAppSleepDisabled
echo "applied $(date -u +%Y-%m-%dT%H:%M:%SZ). Restart Messages (or re-login) for it to take effect."
