#!/usr/bin/env bash
# t18-wifi.sh --yes — Guide step 18: Wi-Fi interruption. Turns Wi-Fi (en1) OFF, waits 20s, sends an
# inbound message labeled WIFI-DOWN from the tester (expected to queue/fail — recorded only), turns
# Wi-Fi back ON, waits for the network and BlueBubbles, then runs the inbound test WIFI-RECOVERY and
# asserts single delivery with no duplicates of anything.
# Usage: t18-wifi.sh --yes [--iface en1] [--down-seconds 20]
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
YES=0; IFACE="en1"; DOWN=20
while [[ $# -gt 0 ]]; do case "$1" in --yes) YES=1; shift;; --iface) IFACE="$2"; shift 2;; --down-seconds) DOWN="$2"; shift 2;; *) shift;; esac; done
T15="$TESTS_DIR/t15-inbound.sh"

DEFAULT_IF="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
echo "${c_red}=================================================================================="
echo " WARNING: this test turns Wi-Fi ($IFACE) OFF for ~${DOWN}s."
echo " Default route is currently on: ${DEFAULT_IF:-unknown}."
if [[ "$DEFAULT_IF" == "$IFACE" ]]; then
  echo " THAT IS THE WI-FI INTERFACE — ALL connectivity (SSH, VNC, remote Claude sessions) WILL DROP."
fi
echo " Run it from a LOCAL terminal (or tmux) — not over SSH."
echo "==================================================================================${c_off}"
[[ $YES -eq 1 ]] || { echo "refusing to run without --yes" >&2; exit 2; }
require_env SENDER01_EMAIL || exit 1
require_listener || exit 1

restore_wifi() { networksetup -setairportpower "$IFACE" on >/dev/null 2>&1 || true; }
trap restore_wifi EXIT

START="$(iso_now)"
DOWN_LABEL="$(make_label WIFI-DOWN)"
info "Wi-Fi OFF ($IFACE)"
networksetup -setairportpower "$IFACE" off || { fail "networksetup off failed (needs an admin user?)"; exit 1; }
sleep "$DOWN"
info "sending $DOWN_LABEL from tester while offline (expected to queue or fail)"
DOWN_OUT="$("$SEND_FROM_TESTER" "$SENDER01_EMAIL" "$DOWN_LABEL" 2>&1)" && DOWN_RC=0 || DOWN_RC=$?
echo "$DOWN_OUT" | sed 's/^/    /'
info "tester send while offline exit=$DOWN_RC (recorded, not asserted)"

info "Wi-Fi ON ($IFACE)"
networksetup -setairportpower "$IFACE" on
info "waiting for network (ping 1.1.1.1)..."
NET_T0="$(now_ms)"
for _ in $(seq 1 120); do ping -c1 -t2 1.1.1.1 >/dev/null 2>&1 && break; sleep 1; done
ping -c1 -t2 1.1.1.1 >/dev/null 2>&1 || { fail "network did not come back within 120s"; exit 1; }
info "network up after $(latency_ms "$NET_T0" "$(now_ms)") ms"
for _ in $(seq 1 60); do "$BB" ping >/dev/null 2>&1 && break; sleep 1; done
"$BB" ping >/dev/null 2>&1 && info "BlueBubbles reachable" || warn "BlueBubbles still not answering ping"
sleep 10   # give Messages/iMessage a moment to reconnect and flush queued traffic

info "running inbound test WIFI-RECOVERY"
"$T15" --label WIFI-RECOVERY --timeout 180 && rc=0 || rc=$?

ok=$rc
info "post-recovery checks:"
DC="$(count_events_by_text "$DOWN_LABEL" new-message "$START")"
echo "  info WIFI-DOWN message ($DOWN_LABEL) new-message events: $DC (0 = never delivered, 1 = delivered after recovery)"
[[ "$DC" -le 1 ]] || { echo "  FAIL WIFI-DOWN delivered $DC times"; ok=1; }
DUPS="$(duplicate_guids "$START")"
[[ -z "$DUPS" ]] && echo "  ok   no duplicate guids since $START" || { echo "  FAIL duplicate guids: $DUPS"; ok=1; }
echo "  note: guide requires no manual Messages restart — record whether one was needed."

if [[ $ok -eq 0 ]]; then result "t18-wifi" PASS; else result "t18-wifi" FAIL; exit 1; fi
