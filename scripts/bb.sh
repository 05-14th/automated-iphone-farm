#!/usr/bin/env bash
# bb.sh — thin curl/jq wrapper around the BlueBubbles Server REST API (v1).
#
# Routes verified against bluebubbles-server/packages/server/src/server/api/http/api/v1/httpRoutes.ts:
#   GET  /api/v1/ping                     GET  /api/v1/server/info
#   POST /api/v1/chat/query               GET  /api/v1/chat/:guid?with=participants,lastmessage
#   GET  /api/v1/chat/:guid/message       POST /api/v1/chat/new
#   POST /api/v1/message/text             GET|POST /api/v1/webhook   DELETE /api/v1/webhook/:id
# Auth: authMiddleware accepts ?password= (also ?guid= / ?token=) on every request.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
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
BB_PASSWORD="${BB_PASSWORD:-}"
BB_TIMEOUT="${BB_TIMEOUT:-30}"

for dep in curl jq; do
  command -v "$dep" >/dev/null 2>&1 || { echo "bb.sh: missing dependency: $dep" >&2; exit 2; }
done

usage() {
  cat <<EOF
Usage: $(basename "$0") <command> [args]

  ping                          GET  /api/v1/ping
  info                          GET  /api/v1/server/info
  chats [limit]                 POST /api/v1/chat/query (participants + last message, newest first)
  chat <chatGuid>               GET  /api/v1/chat/<guid>?with=participants,lastmessage
  find-chat <address>           Find 1:1 chat guid whose participant address matches (case-insensitive)
  send <chatGuid> <text>        POST /api/v1/message/text (method=apple-script, client tempGuid)
  new-chat <address> <text>     POST /api/v1/chat/new (addresses=[address], service=iMessage, method=apple-script)
  messages <chatGuid> [limit]   GET  /api/v1/chat/<guid>/message newest first (guid/text/dateCreated/isFromMe/handle)
  messages-raw <chatGuid> [n]   Same, but full server payload
  webhooks                      GET  /api/v1/webhook
  webhook-add <url> [events]    POST /api/v1/webhook  (events default: new-message,updated-message,server-update; pass '*' for all)
  webhook-del <id>              DELETE /api/v1/webhook/<id>
  raw <METHOD> <path> [json]    Escape hatch, e.g. raw GET server/info

Env (.env): BB_URL=$BB_URL  BB_PASSWORD=$( [[ -n "$BB_PASSWORD" ]] && echo '(set)' || echo '(EMPTY!)' )
EOF
}

urlenc() { jq -rn --arg v "$1" '$v|@uri'; }
tempguid() { echo "temp-$(uuidgen | tr '[:upper:]' '[:lower:]')"; }

# _req METHOD PATH [JSON_BODY]  -> prints response body on success; on HTTP>=400 prints status + body to stderr, returns 1
_req() {
  local method="$1" path="$2" body="${3:-}"
  local sep='?'; [[ "$path" == *\?* ]] && sep='&'
  local url="${BB_URL%/}/api/v1/${path}${sep}password=$(urlenc "$BB_PASSWORD")"
  local args=(-sS --max-time "$BB_TIMEOUT" -X "$method" -H 'Accept: application/json' -w $'\n%{http_code}')
  if [[ -n "$body" ]]; then args+=(-H 'Content-Type: application/json' --data "$body"); fi
  local out status resp
  if ! out="$(curl "${args[@]}" "$url" 2>&1)"; then
    out="${out%$'\n'000}"
    echo "bb.sh: curl failed ($method /api/v1/$path): ${out//$'\n'/ }" >&2
    return 1
  fi
  status="${out##*$'\n'}"
  resp="${out%$'\n'*}"
  if [[ "$status" -ge 400 || "$status" -lt 200 ]]; then
    echo "bb.sh: HTTP $status on $method /api/v1/$path" >&2
    echo "$resp" | jq . 2>/dev/null >&2 || echo "$resp" >&2
    return 1
  fi
  printf '%s' "$resp"
}

pretty() { jq . 2>/dev/null || cat; }

cmd="${1:-}"; shift || true
case "$cmd" in
  ping)   _req GET ping | pretty ;;
  info)   _req GET server/info | pretty ;;
  chats)
    limit="${1:-25}"
    _req POST chat/query "$(jq -cn --argjson l "$limit" '{limit:$l, offset:0, with:["participants","lastMessage"], sort:"lastmessage"}')" \
      | jq '.data |= map({guid, chatIdentifier, displayName, style, participants: [.participants[]?.address], lastMessage: {text: .lastMessage?.text, dateCreated: .lastMessage?.dateCreated, isFromMe: .lastMessage?.isFromMe}})' ;;
  chat)
    [[ $# -ge 1 ]] || { usage; exit 2; }
    _req GET "chat/$(urlenc "$1")?with=participants,lastmessage" | pretty ;;
  find-chat)
    [[ $# -ge 1 ]] || { usage; exit 2; }
    addr="$(echo "$1" | tr '[:upper:]' '[:lower:]')"
    _req POST chat/query "$(jq -cn '{limit:1000, offset:0, with:["participants"]}')" \
      | jq -r --arg a "$addr" '
          [.data[] | select((.participants|length)==1) | select((.participants[0].address|ascii_downcase)==$a)]
          | sort_by(.guid | startswith("iMessage") | not)   # prefer iMessage chats over SMS
          | .[0].guid // empty' ;;
  send)
    [[ $# -ge 2 ]] || { usage; exit 2; }
    tg="$(tempguid)"
    _req POST message/text "$(jq -cn --arg c "$1" --arg m "$2" --arg t "$tg" '{chatGuid:$c, tempGuid:$t, message:$m, method:"apple-script"}')" \
      | jq --arg t "$tg" '. + {tempGuid:$t}' ;;
  new-chat)
    [[ $# -ge 2 ]] || { usage; exit 2; }
    tg="$(tempguid)"
    _req POST chat/new "$(jq -cn --arg a "$1" --arg m "$2" --arg t "$tg" '{addresses:[$a], message:$m, method:"apple-script", service:"iMessage", tempGuid:$t}')" \
      | jq --arg t "$tg" '. + {tempGuid:$t}' ;;
  messages)
    [[ $# -ge 1 ]] || { usage; exit 2; }
    limit="${2:-25}"
    _req GET "chat/$(urlenc "$1")/message?limit=${limit}&offset=0&sort=DESC" \
      | jq '.data | map({guid, text, dateCreated, isFromMe, handle: (.handle.address // null), dateDelivered, dateRead, error})' ;;
  messages-raw)
    [[ $# -ge 1 ]] || { usage; exit 2; }
    limit="${2:-25}"
    _req GET "chat/$(urlenc "$1")/message?limit=${limit}&offset=0&sort=DESC" | pretty ;;
  webhooks) _req GET webhook | pretty ;;
  webhook-add)
    [[ $# -ge 1 ]] || { usage; exit 2; }
    events_csv="${2:-new-message,updated-message,server-update}"
    events_json="$(jq -cn --arg e "$events_csv" '$e | split(",") | map(gsub("^\\s+|\\s+$";""))')"
    _req POST webhook "$(jq -cn --arg u "$1" --argjson e "$events_json" '{url:$u, events:$e}')" | pretty ;;
  webhook-del)
    [[ $# -ge 1 ]] || { usage; exit 2; }
    _req DELETE "webhook/$(urlenc "$1")" | pretty ;;
  raw)
    [[ $# -ge 2 ]] || { usage; exit 2; }
    _req "$1" "$2" "${3:-}" | pretty ;;
  -h|--help|help|"") usage ;;
  *) echo "bb.sh: unknown command: $cmd" >&2; usage >&2; exit 2 ;;
esac
