#!/usr/bin/env bash
# t15-inbound.sh — Guide step 15: send a labeled message FROM clarioinc's Messages.app (the tester)
# TO SENDER01_EMAIL, then wait for BlueBubbles' new-message webhook carrying that label.
# Prints T0 (tester send), dateCreated (Messages.app arrival, from payload), receivedAt (BlueBubbles
# detection at the listener) and latencies; asserts exactly one event.
# Usage: t15-inbound.sh [--label NAME] [--timeout 120] [--settle 5]
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
LBL_NAME="INBOUND"; TIMEOUT=120; SETTLE=5
while [[ $# -gt 0 ]]; do case "$1" in --label) LBL_NAME="$2"; shift 2;; --timeout) TIMEOUT="$2"; shift 2;; --settle) SETTLE="$2"; shift 2;; *) shift;; esac; done

NAME="t15-inbound[$LBL_NAME]"
require_env SENDER01_EMAIL || { result "$NAME" FAIL "env"; exit 1; }
require_listener || { result "$NAME" FAIL "listener down"; exit 1; }
require_bb || warn "bb.sh ping failed — continuing (webhook may still arrive)"

LABEL="$(make_label "$LBL_NAME")"
info "label: $LABEL  -> $SENDER01_EMAIL"
SINCE="$(iso_now)"

if ! OUT="$("$SEND_FROM_TESTER" "$SENDER01_EMAIL" "$LABEL")"; then
  fail "send-from-tester.sh failed: $OUT"; result "$NAME" FAIL "tester send"; exit 1
fi
T0_ISO="$(echo "$OUT" | sed -n 's/^T0=//p')"; T0_MS="$(echo "$OUT" | sed -n 's/^T0_MS=//p')"
info "tester sent via $(echo "$OUT" | sed -n 's/^API=//p') API at $T0_ISO"

info "waiting up to ${TIMEOUT}s for inbound new-message webhook..."
if ! EV="$(wait_for_event "$LABEL" "$TIMEOUT" new-message "$SINCE")"; then
  fail "no new-message webhook with label within ${TIMEOUT}s"
  echo "  (check: is the message visible in imsg01's Messages.app? is the webhook registered? bb.sh webhooks)"
  result "$NAME" FAIL "timeout"; exit 1
fi
E="$(echo "$EV" | jq -c '.[0]')"
RECV_ISO="$(echo "$E" | jq -r .receivedAt)"; RECV_MS="$(iso_to_ms "$RECV_ISO")"
DC_MS="$(echo "$E" | jq -r '.dateCreated // empty')"
if [[ -n "$DC_MS" ]]; then DC_ISO="$(ms_to_iso "$DC_MS")"; else DC_ISO="n/a"; fi
GUID="$(echo "$E" | jq -r .guid)"

echo
echo "  T0 (tester send):            $T0_ISO"
echo "  dateCreated (Messages.app):  $DC_ISO"
echo "  receivedAt (webhook):        $RECV_ISO"
[[ -n "$DC_MS" ]] && echo "  latency send->Messages:      $(latency_ms "$T0_MS" "$DC_MS") ms"
echo "  latency send->webhook:       $(latency_ms "$T0_MS" "$RECV_MS") ms"
echo "  guid=$GUID isFromMe=$(echo "$E" | jq -r .isFromMe) handle=$(echo "$E" | jq -r .handle) chat=$(echo "$E" | jq -r .chatGuid)"
echo

sleep "$SETTLE"
ok=0
info "checks:"
assert_count "$(count_events_by_text "$LABEL" new-message "$SINCE")" 1 "new-message events with label" || ok=1
assert_count "$(count_events_by_guid "$GUID" new-message)" 1 "new-message events for guid" || ok=1
[[ "$(echo "$E" | jq -r .isFromMe)" == "false" ]] && echo "  ok   isFromMe=false" || { echo "  FAIL isFromMe should be false for inbound"; ok=1; }
DUPS="$(duplicate_guids "$SINCE")"
[[ -z "$DUPS" ]] && echo "  ok   no duplicate guids since $SINCE" || { echo "  FAIL duplicate guids: $DUPS"; ok=1; }

# machine-readable line for t16/t18 reports
echo "RESULT label=$LABEL t0=$T0_ISO dateCreated=$DC_ISO receivedAt=$RECV_ISO latency_ms=$(latency_ms "$T0_MS" "$RECV_MS") guid=$GUID"
if [[ $ok -eq 0 ]]; then result "$NAME" PASS "$LABEL latency=$(latency_ms "$T0_MS" "$RECV_MS")ms"; else result "$NAME" FAIL "$LABEL"; exit 1; fi
