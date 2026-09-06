#!/usr/bin/env bash
# Install + load the webhook listener LaunchAgent for the current user (clarioinc). No sudo.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="com.clario.bb-webhook-listener"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -f "$REPO_ROOT/.env" ]]; then set -a; source "$REPO_ROOT/.env"; set +a; fi
PORT="${WEBHOOK_PORT:-12391}"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
[[ -x "$NODE_BIN" ]] || { echo "node not found on PATH; set NODE_BIN=/path/to/node" >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$REPO_ROOT/logs"
sed -e "s|__NODE__|$NODE_BIN|g" -e "s|__REPO__|$REPO_ROOT|g" -e "s|__PORT__|$PORT|g" \
  "$SCRIPT_DIR/webhook-listener.plist" > "$PLIST_DST"
plutil -lint "$PLIST_DST"

UID_NUM="$(id -u)"
# Unload a previous copy if present (ignore errors), then bootstrap.
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
if launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"; then
  echo "bootstrapped $LABEL"
else
  echo "bootstrap failed; falling back to launchctl load" >&2
  launchctl load -w "$PLIST_DST"
fi
launchctl kickstart -k "gui/$UID_NUM/$LABEL" 2>/dev/null || true

sleep 1
echo "--- launchctl status"
launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -E 'state|pid|last exit' || launchctl list | grep "$LABEL" || true
echo "--- health"
for _ in 1 2 3 4 5; do
  if curl -sS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null; then echo; echo "OK: listener healthy on 127.0.0.1:$PORT"; exit 0; fi
  sleep 1
done
echo "WARN: listener not answering on 127.0.0.1:$PORT yet — check $REPO_ROOT/logs/listener.err.log" >&2
exit 1
