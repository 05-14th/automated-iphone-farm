#!/usr/bin/env bash
# t16-idle.sh — Guide step 16: IDLE-30 / IDLE-60 / IDLE-120. Sleeps 30 min, runs inbound test
# labeled IDLE-30; sleeps 30 more, IDLE-60; sleeps 60 more, IDLE-120. Each stage must pass with no
# duplicates. Writes logs/idle-report.md.
# Usage: t16-idle.sh [--minutes 30,30,60]   (e.g. --minutes 1,2,3 for a dry run -> IDLE-1, IDLE-3, IDLE-6)
# NOTE: do NOT run under caffeinate — the point is to observe the unmodified idle baseline (step 17).
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
MINUTES="30,30,60"
while [[ $# -gt 0 ]]; do case "$1" in --minutes) MINUTES="$2"; shift 2;; *) shift;; esac; done
REPORT="$LOG_DIR/idle-report.md"
T15="$TESTS_DIR/t15-inbound.sh"

require_env SENDER01_EMAIL || exit 1
require_listener || exit 1

IFS=',' read -r -a STAGES <<< "$MINUTES"
START="$(iso_now)"
{
  echo "# Idle reliability report (guide step 16)"
  echo
  echo "- started: $START"
  echo "- stages (sleep minutes): $MINUTES"
  echo "- host: $(hostname)  user: $(whoami)"
  echo
  echo "| stage | slept (min) | cumulative (min) | T0 | dateCreated | receivedAt | latency ms | dup check | result |"
  echo "|---|---|---|---|---|---|---|---|---|"
} > "$REPORT"

cum=0; overall=0
for s in "${STAGES[@]}"; do
  cum=$(( cum + s ))
  LBL="IDLE-$cum"
  info "sleeping ${s} min before $LBL (do not touch the Mac)..."
  sleep $(( s * 60 ))
  info "running inbound test $LBL"
  OUT="$("$T15" --label "$LBL" --timeout 180 2>&1)" && rc=0 || rc=$?
  echo "$OUT"
  R="$(echo "$OUT" | grep '^RESULT ' | tail -1)"
  t0="$(echo "$R" | sed -n 's/.* t0=\([^ ]*\).*/\1/p')"; dc="$(echo "$R" | sed -n 's/.* dateCreated=\([^ ]*\).*/\1/p')"
  ra="$(echo "$R" | sed -n 's/.* receivedAt=\([^ ]*\).*/\1/p')"; lat="$(echo "$R" | sed -n 's/.* latency_ms=\([^ ]*\).*/\1/p')"
  DUPS="$(duplicate_guids "$START")"
  dupcell="none"; [[ -z "$DUPS" ]] || { dupcell="DUP: $(echo "$DUPS" | tr '\n' ' ')"; rc=1; }
  res="PASS"; [[ $rc -eq 0 ]] || { res="FAIL"; overall=1; }
  echo "| $LBL | $s | $cum | ${t0:-n/a} | ${dc:-n/a} | ${ra:-n/a} | ${lat:-n/a} | $dupcell | $res |" >> "$REPORT"
done

{
  echo
  echo "- finished: $(iso_now)"
  echo "- overall: $([[ $overall -eq 0 ]] && echo PASS || echo FAIL)"
  echo
  echo "Requirements (guide step 16): no losses, no duplicates, no manual wake-up, acceptable latency."
} >> "$REPORT"
echo; cat "$REPORT"
if [[ $overall -eq 0 ]]; then result "t16-idle" PASS "$MINUTES"; else result "t16-idle" FAIL "$MINUTES"; exit 1; fi
