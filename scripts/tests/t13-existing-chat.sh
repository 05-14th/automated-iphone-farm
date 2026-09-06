#!/usr/bin/env bash
# t13-existing-chat.sh — Guide step 13: send a labeled reply through BlueBubbles into the existing
# 1:1 chat with TEST_RECIPIENT; wait for the outbound echo webhook; assert exactly one message with
# that label exists in the chat and exactly one new-message event carries its GUID.
# Usage: t13-existing-chat.sh [--timeout 90] [--settle 5]
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
TIMEOUT=90; SETTLE=5
while [[ $# -gt 0 ]]; do case "$1" in --timeout) TIMEOUT="$2"; shift 2;; --settle) SETTLE="$2"; shift 2;; *) shift;; esac; done

NAME="t13-existing-chat"
require_env BB_PASSWORD TEST_RECIPIENT || { result "$NAME" FAIL "env"; exit 1; }
require_bb || { result "$NAME" FAIL "bluebubbles down"; exit 1; }
require_listener || { result "$NAME" FAIL "listener down"; exit 1; }

LABEL="$(make_label EXIST)"
info "label: $LABEL"

CHAT_GUID="$(bb_find_chat "$TEST_RECIPIENT" || true)"
if [[ -z "$CHAT_GUID" ]]; then
  fail "no existing 1:1 chat with $TEST_RECIPIENT found (send one native message first — guide step 9 — or run t14)"
  result "$NAME" FAIL "no chat"; exit 1
fi
info "chat: $CHAT_GUID"

SINCE="$(iso_now)"; T0="$(now_ms)"
if ! RESP="$("$BB" send "$CHAT_GUID" "$LABEL")"; then
  result "$NAME" FAIL "POST /message/text failed"; exit 1
fi
MSG_GUID="$(echo "$RESP" | jq -r '.data.guid // empty')"
TEMP_GUID="$(echo "$RESP" | jq -r '.tempGuid')"
ERR="$(echo "$RESP" | jq -r '.data.error // 0')"
info "sent: guid=${MSG_GUID:-?} tempGuid=$TEMP_GUID error=$ERR"
[[ "$ERR" == "0" || "$ERR" == "null" ]] || warn "server reported message error code $ERR"

info "waiting up to ${TIMEOUT}s for outbound echo (new-message with label)..."
if EV="$(wait_for_event "$LABEL" "$TIMEOUT" new-message "$SINCE")"; then
  T1="$(iso_to_ms "$(echo "$EV" | jq -r '.[0].receivedAt')")"
  info "echo received: latency $(latency_ms "$T0" "$T1") ms  isFromMe=$(echo "$EV" | jq -r '.[0].isFromMe') handle=$(echo "$EV" | jq -r '.[0].handle') guid=$(echo "$EV" | jq -r '.[0].guid')"
  [[ -n "$MSG_GUID" ]] || MSG_GUID="$(echo "$EV" | jq -r '.[0].guid')"
else
  warn "no outbound echo webhook within ${TIMEOUT}s (BlueBubbles may not emit new-message for isFromMe on apple-script sends — check bb.sh messages below)"
fi

sleep "$SETTLE"   # let any duplicates arrive before counting
ok=0
info "checks:"
assert_count "$(count_messages_with_text "$CHAT_GUID" "$LABEL")" 1 "messages in chat with label" || ok=1
assert_count "$(count_events_by_text "$LABEL" new-message "$SINCE")" 1 "new-message events with label" || ok=1
if [[ -n "$MSG_GUID" ]]; then
  assert_count "$(count_events_by_guid "$MSG_GUID" new-message)" 1 "new-message events for guid $MSG_GUID" || ok=1
  echo "  info updated-message events for guid: $(count_events_by_guid "$MSG_GUID" updated-message)"
fi
ROW="$("$BB" messages "$CHAT_GUID" 50 | jq -c --arg l "$LABEL" '.[] | select((.text//"")|contains($l)) | {guid,isFromMe,handle,dateCreated,dateDelivered,error}' | head -1)"
echo "  message row: ${ROW:-<none>}"
if [[ -n "$ROW" ]]; then
  [[ "$(echo "$ROW" | jq -r .isFromMe)" == "true" ]] && echo "  ok   isFromMe=true" || { echo "  FAIL isFromMe!=true"; ok=1; }
fi
echo "  identity check: confirm in Messages.app that the message shows as sent from ${SENDER01_EMAIL:-SENDER01_EMAIL (unset)}"

if [[ $ok -eq 0 ]]; then result "$NAME" PASS "$LABEL"; else result "$NAME" FAIL "$LABEL"; exit 1; fi
