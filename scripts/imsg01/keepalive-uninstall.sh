#!/usr/bin/env bash
# keepalive-uninstall.sh — RUN INSIDE imsg01's OWN SESSION. Unloads and removes the keepalive LaunchAgent.
set -euo pipefail
LABEL="com.imsg.messages-keepalive"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || launchctl unload -w "$PLIST_DST" 2>/dev/null || true
rm -f "$PLIST_DST"
rm -rf "$HOME/Library/Application Support/imsg-keepalive"
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then echo "WARN: still loaded" >&2; exit 1; fi
echo "removed $LABEL (log kept at ~/Library/Logs/messages-keepalive.log)"
