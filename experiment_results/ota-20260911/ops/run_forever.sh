#!/usr/bin/env bash
# Runs the frozen v3.1 case attempt after attempt and never stops on an outcome.
# Analysis and fixes happen beside it; each attempt imports the code as it is at launch.
# systemd (aic-v31-episodes.service) restarts this script if it dies.
set -u
SP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREF=${AIC_CASE_PREF:-P1} LOAD=${AIC_CASE_LOAD:-8}
COUNTER="$SP/overnight/next-attempt.txt"
[ -f "$COUNTER" ] || echo 28 > "$COUNTER"
while true; do
  [ -f "$SP/overnight/PAUSE" ] && { sleep 30; continue; }   # operator/assistant maintenance switch
  i=$(cat "$COUNTER"); echo $((i+1)) > "$COUNTER"
  LOG="$SP/overnight/v31-$PREF-L$LOAD-attempt$i.log"
  bash "$SP/run_case.sh" "$PREF" "$LOAD" > "$LOG" 2>&1; rc=$?
  DIR=$(grep -o 'formal38guarded-[0-9T]*-[0-9a-f]*' "$LOG" | tail -1)
  STATUS=$(grep -o '"submissionStatus": "[A-Z_]*"' "$LOG" | tail -1 | cut -d'"' -f4)
  REASON=$(grep -A1 'refused before anything was submitted' "$SP/../$DIR/live-sitting.stdout" 2>/dev/null | tail -1 | sed 's/^ *//')
  [ -z "$REASON" ] && REASON=$(python3 -c "import json,sys; print((json.load(open(sys.argv[1])).get('failure') or {}).get('code',''))" "$SP/../$DIR/exit.json" 2>/dev/null)
  TERM_=$(python3 -c "
import json,glob,sys
f=glob.glob(sys.argv[1]+'/evidence/*-episode.json')
if f:
    e=json.load(open(f[0])); t=e.get('termination') or {}; c=e.get('completion') or {}
    print(json.dumps({'reason':t.get('reason'),'kernelTermination':t.get('kernelTermination'),'unresolved':c.get('unresolved')}))" "$SP/../$DIR" 2>/dev/null)
  python3 - "$SP/overnight/v31-ledger.jsonl" "$PREF-L$LOAD" "$i" "$DIR" "$STATUS" "$REASON" "$rc" "$TERM_" <<'PY'
import json, sys, time
path, case, i, d, status, reason, rc, term = sys.argv[1:]
row = {"at": time.strftime('%Y-%m-%dT%H:%M:%S%z'), "version": "v3.1-select10-existing3", "case": case,
       "method": "three-agent", "attempt": int(i), "dir": d, "submissionStatus": status,
       "episodeStarted": status == "STARTED_EPISODE", "refusal": reason or None, "outerExit": int(rc),
       "episodeTermination": json.loads(term) if term else None, "loggedBy": "run_forever.sh"}
open(path, 'a').write(json.dumps(row) + '\n')
PY
  echo "$(date +%H:%M:%S) ATTEMPT $i $STATUS :: ${REASON:-$TERM_}"
  sleep 5
done
