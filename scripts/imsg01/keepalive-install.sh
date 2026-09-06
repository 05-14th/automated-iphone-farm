#!/usr/bin/env bash
# keepalive-install.sh — RUN INSIDE imsg01's OWN SESSION. Copies messages-keepalive.sh into
# ~/Library/Application Support/imsg-keepalive/, renders the LaunchAgent plist and loads it (no sudo).
set -euo pipefail
[[ "$(whoami)" == "imsg01" ]] || { echo "must run as imsg01 (current user: $(whoami))" >&2; exit 1; }
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.imsg.messages-keepalive"
APP_DIR="$HOME/Library/Application Support/imsg-keepalive"
SCRIPT_DST="$APP_DIR/messages-keepalive.sh"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$APP_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$SRC_DIR/messages-keepalive.sh" "$SCRIPT_DST"; chmod 755 "$SCRIPT_DST"
sed -e "s|__SCRIPT__|$SCRIPT_DST|g" -e "s|__HOME__|$HOME|g" "$SRC_DIR/$LABEL.plist" > "$PLIST_DST"
plutil -lint "$PLIST_DST"

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST" || launchctl load -w "$PLIST_DST"
launchctl kickstart "gui/$UID_NUM/$LABEL" 2>/dev/null || true
sleep 2
launchctl print "gui/$UID_NUM/$LABEL" | grep -E 'state|last exit' || true
echo "--- last log lines"; tail -n 3 "$HOME/Library/Logs/messages-keepalive.log" 2>/dev/null || echo "(no log yet)"
echo "installed $LABEL (every 300s). Uninstall with keepalive-uninstall.sh"
