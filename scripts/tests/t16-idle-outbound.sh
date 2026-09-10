#!/bin/bash
# Guide step 16 adapted: idle reliability via outbound sends at 30/60/120 min.
# Inbound was proven separately; this measures whether Messages+BlueBubbles survive idle.
cd "$(dirname "$0")/../.." || exit 1
source .env
REPORT=logs/idle-report.md
MINS="${1:-30,60,120}"
IFS=',' read -ra STEPS <<< "$MINS"
mkdir -p logs
{ echo "# Idle reliability report (guide step 16)"; echo; echo "Started: $(date -u +%FT%TZ)"; echo "Mode: outbound send via BlueBubbles -> $TEST_RECIPIENT"; echo; 
  echo "| label | scheduled | sent ok | err | echo latency ms | events | duplicate |"; echo "|---|---|---|---|---|---|---|"; } > $REPORT

prev=0
for m in "${STEPS[@]}"; do
  delta=$(( m - prev )); prev=$m
  sleep $(( delta * 60 ))
  LABEL="IDLE-${m}-$(date +%s)"
  T0=$(python3 -c 'import time;print(int(time.time()*1000))')
  RESP=$(curl -sS -m 120 -X POST "$BB_URL/api/v1/message/text?password=$BB_PASSWORD" \
    -H "Content-Type: application/json" \
    -d "{\"chatGuid\":\"any;-;$TEST_RECIPIENT\",\"tempGuid\":\"idle-$(date +%s)\",\"message\":\"$LABEL\",\"method\":\"apple-script\"}" 2>&1)
  GUID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('guid',''))" 2>/dev/null)
  ERR=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('error','?'))" 2>/dev/null)
  OK=$([ -n "$GUID" ] && echo yes || echo no)
  sleep 20
  # reconcile: does the message exist in provider state regardless of API answer?
  FOUND=$(curl -sS -m 30 -X POST "$BB_URL/api/v1/message/query?password=$BB_PASSWORD" -H "Content-Type: application/json" \
    -d '{"limit":20,"offset":0,"sort":"DESC"}' 2>/dev/null | python3 -c "
import sys,json
rows=json.load(sys.stdin).get('data',[])
m=[r for r in rows if r.get('text')=='$LABEL']
print(m[0].get('guid') if m else '')" 2>/dev/null)
  [ -z "$GUID" ] && GUID="$FOUND"
  EV=$(grep -c "$LABEL" logs/events.jsonl 2>/dev/null || echo 0)
  NEW=$(python3 -c "
import json
n=0;lat=''
for l in open('logs/events.jsonl'):
    e=json.loads(l)
    if e.get('text')=='$LABEL' and e.get('type')=='new-message':
        n+=1
        if not lat:
            import datetime
            r=datetime.datetime.fromisoformat(e['receivedAt'].replace('Z','+00:00')).timestamp()*1000
            lat=str(int(r-$T0))
print(n, lat or 'n/a')" 2>/dev/null)
  NCOUNT=$(echo $NEW | cut -d' ' -f1); LAT=$(echo $NEW | cut -d' ' -f2)
  DUP=$([ "${NCOUNT:-0}" -gt 1 ] && echo YES || echo no)
  echo "| $LABEL | T+${m}m | $OK | $ERR | $LAT | $EV | $DUP |" >> $REPORT
done
{ echo; echo "Finished: $(date -u +%FT%TZ)"; } >> $REPORT
echo "IDLE SERIES COMPLETE -> $REPORT"
cat $REPORT
