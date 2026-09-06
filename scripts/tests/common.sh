#!/usr/bin/env bash
# common.sh — shared helpers for the acceptance tests. Source it; don't execute it.
#   source "$(dirname "$0")/common.sh"
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TESTS_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"
BB="$SCRIPTS_DIR/bb.sh"
SEND_FROM_TESTER="$SCRIPTS_DIR/send-from-tester.sh"
ENV_FILE="${BB_ENV_FILE:-$REPO_ROOT/.env}"

# Load KEY=VALUE lines from .env without overriding variables already set in the environment.
load_env() {
  local f="$1" line k v
  [[ -f "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    k="${BASH_REMATCH[2]}"; v="${BASH_REMATCH[3]}"
    v="${v%"${v##*[![:space:]]}"}"
    if [[ "$v" =~ ^\"(.*)\"$ ]] || [[ "$v" =~ ^\'(.*)\'$ ]]; then v="${BASH_REMATCH[1]}"; fi
    [[ -n "${!k+x}" ]] || export "$k=$v"
  done < "$f"
}
load_env "$ENV_FILE"
BB_URL="${BB_URL:-http://127.0.0.1:12341}"
WEBHOOK_PORT="${WEBHOOK_PORT:-12391}"
LISTENER_URL="${LISTENER_URL:-http://127.0.0.1:$WEBHOOK_PORT}"
LOG_DIR="${LOG_DIR:-$REPO_ROOT/logs}"
EVENTS_FILE="${EVENTS_FILE:-$LOG_DIR/events.jsonl}"
mkdir -p "$LOG_DIR"

# ---------- output ----------
c_green=$'\033[32m'; c_red=$'\033[31m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
info() { echo "${c_dim}[$(iso_now)]${c_off} $*"; }
warn() { echo "${c_yel}WARN:${c_off} $*" >&2; }
pass() { echo "${c_green}PASS${c_off} $*"; }
fail() { echo "${c_red}FAIL${c_off} $*" >&2; }

# ---------- time ----------
iso_now() { perl -MTime::HiRes=time -MPOSIX=strftime -e '$t=time; printf "%s.%03dZ\n", strftime("%Y-%m-%dT%H:%M:%S", gmtime($t)), ($t-int($t))*1000'; }
now_ms()  { perl -MTime::HiRes=time -e 'printf "%d\n", time*1000'; }
iso_to_ms() { node -e 'const d=Date.parse(process.argv[1]); if(Number.isNaN(d)){process.exit(1)} console.log(d)' "$1"; }
ms_to_iso() { node -e 'console.log(new Date(Number(process.argv[1])).toISOString())' "$1"; }
latency_ms() { echo $(( $2 - $1 )); }   # latency_ms <t0_ms> <t1_ms>

# ---------- env checks ----------
require_env() {
  local missing=0
  for v in "$@"; do
    if [[ -z "${!v:-}" ]]; then echo "missing required env var: $v (set it in $ENV_FILE)" >&2; missing=1; fi
  done
  [[ $missing -eq 0 ]]
}
require_listener() {
  curl -sS --max-time 3 "$LISTENER_URL/health" >/dev/null 2>&1 || { fail "webhook listener not reachable at $LISTENER_URL (run scripts/listener-install.sh or node scripts/webhook-listener.mjs)"; return 1; }
}
require_bb() {
  "$BB" ping >/dev/null 2>&1 || { fail "BlueBubbles not reachable at $BB_URL (bb.sh ping failed)"; return 1; }
}

# ---------- labels ----------
# make_label <name> -> T-<name>-<epoch>-<rand>
make_label() { echo "T-$1-$(date +%s)-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 4)"; }

# ---------- event queries ----------
# Prefer the listener's /events endpoint; fall back to reading the JSONL directly.
urlenc() { jq -rn --arg v "$1" '$v|@uri'; }
# events_query <textSubstr> <type> <sinceIso> <guid>  (any may be "") -> JSON array, oldest first
events_query() {
  local text="${1:-}" type="${2:-}" since="${3:-}" guid="${4:-}" out
  local qs="text=$(urlenc "$text")&type=$(urlenc "$type")&since=$(urlenc "$since")&guid=$(urlenc "$guid")"
  if out="$(curl -sS --max-time 5 "$LISTENER_URL/events?$qs" 2>/dev/null)" && [[ -n "$out" ]]; then
    printf '%s' "$out"; return 0
  fi
  # Fallback: filter the JSONL file directly with the same semantics
  [[ -f "$EVENTS_FILE" ]] || { echo '[]'; return 0; }
  jq -s --arg t "$text" --arg ty "$type" --arg s "$since" --arg g "$guid" '
    map(select(($t=="" or ((.text//"")|contains($t)))
           and ($ty=="" or .type==$ty)
           and ($g=="" or .guid==$g)
           and ($s=="" or (.receivedAt >= $s))))' "$EVENTS_FILE"
}

# count_events_by_text <label> [type] [sinceIso]
count_events_by_text() { events_query "$1" "${2:-}" "${3:-}" "" | jq 'length'; }
# count_events_by_guid <guid> [type]
count_events_by_guid() { events_query "" "${2:-}" "" "$1" | jq 'length'; }

# wait_for_event <label> [timeout_s] [type] [sinceIso] -> prints JSON array once >=1 match; returns 1 on timeout
wait_for_event() {
  local label="$1" timeout="${2:-90}" type="${3:-}" since="${4:-}"
  local start; start="$(now_ms)"
  local deadline=$(( start + timeout*1000 ))
  local res
  while :; do
    res="$(events_query "$label" "$type" "$since" "")"
    if [[ "$(echo "$res" | jq 'length')" -ge 1 ]]; then printf '%s' "$res"; return 0; fi
    [[ "$(now_ms)" -lt "$deadline" ]] || { echo '[]'; return 1; }
    sleep 1
  done
}

# assert_count <actual> <expected> <what>  -> echoes ok/fail line; returns 1 on mismatch
assert_count() {
  local actual="$1" expected="$2" what="$3"
  if [[ "$actual" == "$expected" ]]; then echo "  ok   $what = $actual"; return 0
  else echo "  FAIL $what = $actual (expected $expected)"; return 1; fi
}

# count_messages_with_text <chatGuid> <label> [limit]
count_messages_with_text() { "$BB" messages "$1" "${3:-100}" | jq --arg l "$2" '[.[] | select((.text//"")|contains($l))] | length'; }

# bb_find_chat <address> -> guid (empty if none)
bb_find_chat() { "$BB" find-chat "$1"; }

# duplicate_guids [sinceIso] -> lists guids that have >1 new-message event (should be empty)
duplicate_guids() {
  events_query "" new-message "${1:-}" "" | jq -r 'group_by(.guid) | map(select(length>1)) | .[] | "\(.[0].guid) x\(length)"'
}

# result <name> <PASS|FAIL> [note]  -> appends to $RESULTS_FILE if set, prints line
result() {
  local line="$1|$2|${3:-}"
  [[ -n "${RESULTS_FILE:-}" ]] && echo "$line" >> "$RESULTS_FILE"
  if [[ "$2" == PASS ]]; then pass "$1 ${3:-}"; else fail "$1 ${3:-}"; fi
}
