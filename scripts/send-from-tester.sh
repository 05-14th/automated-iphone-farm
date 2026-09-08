#!/usr/bin/env bash
# send-from-tester.sh <recipient> <text>
# Sends an iMessage from THIS macOS user's Messages.app (clarioinc = consenting tester) to <recipient>
# via AppleScript. Tries the modern `participant` API (macOS 14+) first, falls back to `buddy`.
# Prints:  T0=<ISO ms>  T0_MS=<epoch ms>  API=<participant|buddy>
set -euo pipefail
[[ $# -ge 2 ]] || { echo "usage: $(basename "$0") <recipient-handle> <text>" >&2; exit 2; }
recipient="$1"; text="$2"

iso_now() { perl -MTime::HiRes=time -MPOSIX=strftime -e '$t=time; printf "%s.%03dZ\n", strftime("%Y-%m-%dT%H:%M:%S", gmtime($t)), ($t-int($t))*1000'; }
now_ms()  { perl -MTime::HiRes=time -e 'printf "%d\n", time*1000'; }

T0="$(iso_now)"; T0_MS="$(now_ms)"

api="$(osascript - "$recipient" "$text" <<'APPLESCRIPT'
on run argv
  set theRecipient to item 1 of argv
  set theText to item 2 of argv
  tell application "Messages"
    -- macOS 14+ (Sonoma): accounts/participants
    try
      set theAccount to 1st account whose service type = iMessage and enabled = true
      set theTarget to participant theRecipient of theAccount
      send theText to theTarget
      return "participant"
    on error errMsg1 number errNum1
      -- Older API: services/buddies
      try
        set theService to 1st service whose service type = iMessage
        set theBuddy to buddy theRecipient of theService
        send theText to theBuddy
        return "buddy"
      on error errMsg2 number errNum2
        error "participant API failed (" & errNum1 & ": " & errMsg1 & "); buddy API failed (" & errNum2 & ": " & errMsg2 & ")"
      end try
    end try
  end tell
end run
APPLESCRIPT
)"

echo "T0=$T0"
echo "T0_MS=$T0_MS"
echo "API=$api"
echo "RECIPIENT=$recipient"
