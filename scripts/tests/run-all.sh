#!/usr/bin/env bash
# run-all.sh — runs the functional acceptance tests (guide steps 13, 14, 15) and prints a summary table.
# Usage: run-all.sh [new-chat-address]   (defaults to NEW_CHAT_ADDRESS env, then TEST_RECIPIENT)
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
NEW_ADDR="${1:-${NEW_CHAT_ADDRESS:-${TEST_RECIPIENT:-}}}"
export RESULTS_FILE="$LOG_DIR/run-all.$(date +%Y%m%d-%H%M%S).results"
: > "$RESULTS_FILE"

run() { echo; echo "=============== $1 ==============="; shift; "$@" || true; }
run "13 existing chat" "$TESTS_DIR/t13-existing-chat.sh"
if [[ -n "$NEW_ADDR" ]]; then
  run "14 new chat ($NEW_ADDR)" "$TESTS_DIR/t14-new-chat.sh" "$NEW_ADDR"
else
  echo "14 new chat: SKIPPED (no address)"; echo "t14-new-chat|SKIP|no address" >> "$RESULTS_FILE"
fi
run "15 inbound" "$TESTS_DIR/t15-inbound.sh"

echo
echo "================ SUMMARY ================"
printf '%-28s %-6s %s\n' "test" "result" "note"
printf '%-28s %-6s %s\n' "----" "------" "----"
fails=0
while IFS='|' read -r n r note; do
  [[ -n "$n" ]] || continue
  printf '%-28s %-6s %s\n' "$n" "$r" "$note"
  [[ "$r" == FAIL ]] && fails=$((fails+1))
done < "$RESULTS_FILE"
echo "results file: $RESULTS_FILE"
if [[ $fails -eq 0 ]]; then echo "ALL PASS"; exit 0; else echo "$fails FAILED"; exit 1; fi
