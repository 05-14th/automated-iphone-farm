#!/usr/bin/env bash
# Unload + remove the webhook listener LaunchAgent for the current user. No sudo.
set -euo pipefail
LABEL="com.clario.bb-webhook-listener"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || launchctl unload -w "$PLIST_DST" 2>/dev/null || true
rm -f "$PLIST_DST"
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  echo "WARN: $LABEL still loaded" >&2; exit 1
fi
echo "removed $LABEL ($PLIST_DST)"
