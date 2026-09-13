#!/bin/bash
# Guide step 18: Wi-Fi interruption and automatic recovery.
# SAFETY: Wi-Fi is re-enabled by trap on ANY exit path.
cd "$(dirname "$0")/../.." || exit 1
source .env
IF=en1
restore(){ networksetup -setairportpower $IF on 2>/dev/null; }
trap restore EXIT INT TERM

log(){ echo "[$(date -u +%H:%M:%S)] $*"; }
log "BEFORE: wifi=$(networksetup -getairportpower $IF | awk '{print $NF}') bb=$(curl -sf -m5 "$BB_URL/api/v1/ping?password=$BB_PASSWORD" >/dev/null && echo ok || echo fail)"

log "Wi-Fi OFF"
networksetup -setairportpower $IF off
sleep 5
log "route check (expect no default): $(route -n get default 2>&1 | grep -c interface)"

# attempt a send while offline - should fail or queue
L_OFF="WIFI-DOWN-$(date +%s)"
log "attempting send while offline: $L_OFF"
curl -sS -m 25 -X POST "$BB_URL/api/v1/message/text?password=$BB_PASSWORD" -H "Content-Type: application/json" \
  -d "{\"chatGuid\":\"any;-;$TEST_RECIPIENT\",\"tempGuid\":\"wifioff-$(date +%s)\",\"message\":\"$L_OFF\",\"method\":\"apple-script\"}" 2>&1 | head -c 150
echo
sleep 15

log "Wi-Fi ON"
networksetup -setairportpower $IF on
for i in $(seq 1 40); do
  ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 && { log "network back after ~$((i*2))s"; break; }
  sleep 2
done
sleep 10

log "AFTER: bb=$(curl -sf -m10 "$BB_URL/api/v1/ping?password=$BB_PASSWORD" >/dev/null && echo ok || echo fail)"
L_REC="WIFI-RECOVERY-$(date +%s)"
log "recovery send: $L_REC"
RESP=$(curl -sS -m 90 -X POST "$BB_URL/api/v1/message/text?password=$BB_PASSWORD" -H "Content-Type: application/json" \
  -d "{\"chatGuid\":\"any;-;$TEST_RECIPIENT\",\"tempGuid\":\"wifirec-$(date +%s)\",\"message\":\"$L_REC\",\"method\":\"apple-script\"}" 2>&1)
echo "$RESP" | head -c 200; echo
sleep 20

python3 - "$L_OFF" "$L_REC" <<'PY'
import json,sys,collections
off,rec=sys.argv[1],sys.argv[2]
rows=[json.loads(l) for l in open('logs/events.jsonl')]
for lbl in (off,rec):
    m=[r for r in rows if r.get('text')==lbl]
    c=collections.Counter(r.get('type') for r in m)
    guids={r.get('guid') for r in m}
    print(f"{lbl}: events={len(m)} types={dict(c)} distinct_guids={len(guids)} duplicate={'YES' if c.get('new-message',0)>1 else 'no'}")
PY
log "Messages/BlueBubbles restart needed? bb_pid=$(ssh -o BatchMode=yes imsg01 'pgrep -x BlueBubbles' 2>/dev/null | tr -d '\n')"
