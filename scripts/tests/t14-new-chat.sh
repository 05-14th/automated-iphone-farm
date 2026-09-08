#!/usr/bin/env bash
# t14-new-chat.sh <address> — Guide step 14: create a brand-new conversation via POST /api/v1/chat/new
# with a labeled message; assert the chat exists and contains exactly one message with the label.
# On HTTP 500 / AppleScript error prints the "NEW-CHAT FAILURE — see guide step 25" note.
# Usage: t14-new-chat.sh <address> [--timeout 90] [--settle 5]
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
ADDR=""; TIMEOUT=90; SETTLE=5
while [[ $# -gt 0 ]]; do case "$1" in --timeout) TIMEOUT="$2"; shift 2;; --settle) SETTLE="$2"; shift 2;; *) ADDR="$1"; shift;; esac; done
[[ -n "$ADDR" ]] || { echo "usage: $(basename "$0") <address> [--timeout N]" >&2; exit 2; }

NAME="t14-new-chat"
require_env BB_PASSWORD || { result "$NAME" FAIL "env"; exit 1; }
require_bb || { result "$NAME" FAIL "bluebubbles down"; exit 1; }
require_listener || warn "listener down — webhook assertions will be skipped"

LABEL="$(make_label NEWCHAT)"
info "label: $LABEL  address: $ADDR"
EXISTING="$(bb_find_chat "$ADDR" || true)"
[[ -z "$EXISTING" ]] || warn "a 1:1 chat with $ADDR already exists ($EXISTING); the server may reuse it instead of creating a new one"

SINCE="$(iso_now)"; T0="$(now_ms)"
ERRFILE="$(mktemp)"
if ! RESP="$("$BB" new-chat "$ADDR" "$LABEL" 2>"$ERRFILE")"; then
  ERRTXT="$(cat "$ERRFILE")"; rm -f "$ERRFILE"
  echo "$ERRTXT" >&2
  if echo "$ERRTXT" | grep -qiE 'HTTP 5[0-9][0-9]|applescript|osascript|Failed to create chat|Can.t get'; then
    cat >&2 <<EOF
${c_red}NEW-CHAT FAILURE — see guide step 25${c_off}
  POST /api/v1/chat/new (method=apple-script) returned a server/AppleScript error.
  If existing-chat replies (t13) and inbound (t15) pass but this consistently fails, that is the
  "new-chat-only failure" branch: evaluate the audited upstream fix in a source build with SIP ON,
  and record version, git commit, diff, build date, macOS build and SIP state before going further.
EOF
  fi
  result "$NAME" FAIL "chat/new error"; exit 1
fi
rm -f "$ERRFILE"

CHAT_GUID="$(echo "$RESP" | jq -r '.data.guid // empty')"
MSG_GUID="$(echo "$RESP" | jq -r '.data.messages[0].guid // empty')"
info "created chat: ${CHAT_GUID:-<none>}  message guid: ${MSG_GUID:-<none>}  latency $(latency_ms "$T0" "$(now_ms)") ms"
[[ -n "$CHAT_GUID" ]] || { fail "response carried no chat guid"; echo "$RESP" | jq . ; result "$NAME" FAIL "no chat guid"; exit 1; }

# Verify chat is fetchable with participants
if ! CHAT="$("$BB" chat "$CHAT_GUID")"; then result "$NAME" FAIL "GET chat failed"; exit 1; fi
echo "  participants: $(echo "$CHAT" | jq -c '[.data.participants[]?.address]')"

if curl -sS --max-time 3 "$LISTENER_URL/health" >/dev/null 2>&1; then
  if EV="$(wait_for_event "$LABEL" "$TIMEOUT" new-message "$SINCE")"; then
    info "outbound echo received: $(echo "$EV" | jq -c '.[0] | {guid, chatGuid, isFromMe, handle}')"
  else
    warn "no new-message webhook with label within ${TIMEOUT}s"
  fi
fi

sleep "$SETTLE"
ok=0
info "checks:"
assert_count "$(count_messages_with_text "$CHAT_GUID" "$LABEL")" 1 "messages in new chat with label" || ok=1
if curl -sS --max-time 3 "$LISTENER_URL/health" >/dev/null 2>&1; then
  assert_count "$(count_events_by_text "$LABEL" new-message "$SINCE")" 1 "new-message events with label" || ok=1
fi
ROW="$("$BB" messages "$CHAT_GUID" 20 | jq -c --arg l "$LABEL" '.[] | select((.text//"")|contains($l)) | {guid,isFromMe,handle,dateCreated,dateDelivered,error}' | head -1)"
echo "  message row: ${ROW:-<none>}"
[[ -n "$ROW" && "$(echo "$ROW" | jq -r '.error // 0')" == "0" ]] || { echo "  FAIL message missing or has error code"; ok=1; }
echo "  chat guid: $CHAT_GUID"

if [[ $ok -eq 0 ]]; then result "$NAME" PASS "$LABEL"; else result "$NAME" FAIL "$LABEL"; exit 1; fi
