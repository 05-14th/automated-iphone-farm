#!/usr/bin/env bash
# post-reboot-check.sh — Guide step 21 (and 37 reboot runbook): health checklist after a reboot.
# Checks internet, imsg01 GUI session + Messages.app, BlueBubbles process, port 12341, bb.sh ping/info,
# webhook listener health. With --run-tests also runs steps 13/14/15 (keep outbound paused until this passes).
# Usage: post-reboot-check.sh [--run-tests] [new-chat-address]
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
RUN_TESTS=0; NEW_ADDR=""
while [[ $# -gt 0 ]]; do case "$1" in --run-tests) RUN_TESTS=1; shift;; *) NEW_ADDR="$1"; shift;; esac; done
BB_PORT="$(echo "$BB_URL" | sed -E 's#.*:([0-9]+)/?$#\1#')"; [[ "$BB_PORT" =~ ^[0-9]+$ ]] || BB_PORT=12341

fails=0
check() {  # check <name> <command...>
  local name="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then printf '  [%sOK%s]   %s\n' "$c_green" "$c_off" "$name"
  else
    printf '  [%sFAIL%s] %s\n' "$c_red" "$c_off" "$name"
    [[ -n "$out" ]] && echo "$out" | head -5 | sed 's/^/         /'
    fails=$((fails+1))
  fi
}

echo "Post-reboot checklist  ($(iso_now), uptime:$(uptime | sed 's/.*up/ /;s/,.*//'))"
check "internet reachable (ping 1.1.1.1)"            ping -c1 -t3 1.1.1.1
check "Apple reachable (apple.com:443)"               bash -c 'nc -z -G 5 apple.com 443'
check "imsg01 has a GUI session (who)"                 bash -c 'who | grep -q "^imsg01 .*console"'
check "imsg01 Messages.app running"                    bash -c "ps -axo user,comm | grep -qE '^imsg01 +(.*/)?Messages$'"
check "BlueBubbles process under imsg01"               bash -c "ps -axo user,comm | grep -iE '^imsg01 +.*bluebubbles' | grep -qv grep"
check "port $BB_PORT listening on 127.0.0.1"           bash -c "nc -z 127.0.0.1 $BB_PORT"
check "bb.sh ping"                                     "$BB" ping
check "bb.sh info (auth ok)"                           "$BB" info
check "webhook registered for listener ($LISTENER_URL)" bash -c "'$BB' webhooks | jq -e --arg u '$LISTENER_URL' '[.data[]? | select(.url|startswith(\$u))] | length > 0' >/dev/null"
check "webhook listener /health"                       curl -sS --max-time 3 "$LISTENER_URL/health"
check "listener LaunchAgent loaded"                    bash -c 'launchctl print gui/$(id -u)/com.clario.bb-webhook-listener >/dev/null'
check "clarioinc Messages.app running (tester)"        pgrep -xq Messages

echo
if "$BB" info >/dev/null 2>&1; then
  "$BB" info | jq -r '.data | "  server: v\(.server_version // "?")  macOS \(.os_version // "?")  private_api=\(.private_api // "?")  helper_connected=\(.helper_connected // "?")  proxy=\(.proxy_service // "?")"' 2>/dev/null || true
fi
echo "  imsg01 processes: $(ps -axo user,comm | awk '$1=="imsg01"{print $2}' | xargs -n1 basename 2>/dev/null | sort | uniq -c | sort -rn | head -5 | tr '\n' ';')"
echo
if [[ $fails -eq 0 ]]; then echo "${c_green}HEALTH: OK${c_off}"; else echo "${c_red}HEALTH: $fails FAILED${c_off} — keep outbound paused"; fi

if [[ $RUN_TESTS -eq 1 ]]; then
  if [[ $fails -ne 0 ]]; then echo "skipping tests because health checks failed"; exit 1; fi
  echo; echo "Running functional tests 13/14/15..."
  "$TESTS_DIR/run-all.sh" ${NEW_ADDR:+"$NEW_ADDR"}; exit $?
fi
[[ $fails -eq 0 ]]
