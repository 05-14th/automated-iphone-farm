#!/usr/bin/env bash
# messages-keepalive.sh — Guide step 23. Run by the com.imsg.messages-keepalive LaunchAgent every 300s
# inside imsg01's session. If Messages.app is not running, relaunch it in the background without
# stealing focus (open -gja). Logs to ~/Library/Logs/messages-keepalive.log.
LOG="$HOME/Library/Logs/messages-keepalive.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
if pgrep -x Messages >/dev/null 2>&1; then
  echo "$(ts) ok Messages running (pid $(pgrep -x Messages | head -1))" >> "$LOG"
else
  echo "$(ts) MISSING Messages not running -> open -gja Messages" >> "$LOG"
  if open -gja Messages; then
    sleep 5
    echo "$(ts) relaunched: pid $(pgrep -x Messages | head -1 || echo none)" >> "$LOG"
  else
    echo "$(ts) ERROR open -gja Messages failed" >> "$LOG"
  fi
fi
# keep the log from growing unbounded (~5000 lines)
if [[ $(wc -l < "$LOG") -gt 6000 ]]; then tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi
