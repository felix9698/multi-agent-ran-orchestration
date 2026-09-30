#!/usr/bin/env bash
# 세 방식 각 10판.  2026-09-23 오너 지시.
#
# **round-robin 으로 섞는다.**  10판씩 몰아서 돌리면 밤새 베드가 드리프트할 때 그
# 드리프트가 방식 차이로 둔갑한다(09-20 의 재부팅 한 번이 네 층을 동시에 깼다).
# 순서를 섞으면 드리프트가 세 방식에 고르게 실린다.
#
# **판이 실제로 시작했을 때만(`STARTED_EPISODE`) 할당량을 센다.**  preflight 거절은
# 방식과 무관하므로 세면 한 방식만 손해를 본다.
#
# **원장의 method 는 실제로 실행된 것을 적는다.**  run_forever.sh 는 "three-agent" 를
# 박아 두어, 다른 방식을 돌리면 기록이 거짓말을 한다
# ([[condition-labels-are-not-evidence-of-what-ran]]).
set -u
SP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREF=${AIC_CASE_PREF:-P1} LOAD=${AIC_CASE_LOAD:-10}
CAMPAIGN=${AIC_CAMPAIGN:-methods10-20260923}
TARGET=${AIC_PER_METHOD:-10}
# **검증 안 된 것을 먼저.**  단일모델 두 방식은 09-20 까지 3-인텐트 코퍼스에서만
# 돌았고 7-인텐트 v4.4 에서는 한 번도 돈 적이 없다.  three-agent 는 수백 판 실적이
# 있으므로 뒤로 보낸다 -- 깨져 있다면 첫 판에서 드러나야지 밤을 다 쓰고 드러나면 안 된다.
METHODS=(internal-monolith basic-monolith three-agent)
STATE="$SP/overnight/$CAMPAIGN.progress.json"
COUNTER="$SP/overnight/next-attempt.txt"
LEDGER="$SP/overnight/v31-ledger.jsonl"

[ -f "$STATE" ] || printf '{"three-agent":0,"internal-monolith":0,"basic-monolith":0}\n' > "$STATE"

done_for() { python3 -c "import json,sys; print(json.load(open('$STATE')).get(sys.argv[1],0))" "$1"; }
bump()     { python3 -c "
import json,sys
p='$STATE'; d=json.load(open(p)); d[sys.argv[1]]=d.get(sys.argv[1],0)+1
open(p,'w').write(json.dumps(d))" "$1"; }

while true; do
  [ -f "$SP/overnight/PAUSE" ] && { sleep 30; continue; }

  # 가장 적게 돈 방식을 고른다 -- 동률이면 선언 순서.  이것이 round-robin 이고,
  # 판이 거절돼도 순서가 밀리지 않는다.
  NEXT=''; LOW=99999
  for m in "${METHODS[@]}"; do
    n=$(done_for "$m")
    if [ "$n" -lt "$TARGET" ] && [ "$n" -lt "$LOW" ]; then LOW=$n; NEXT=$m; fi
  done
  if [ -z "$NEXT" ]; then
    # **10판은 최소선이지 정지선이 아니다** (오너: "계속 판 기록 쌓다가").
    # 목표를 채웠으면 이정표만 남기고 균등하게 계속 돈다 -- 가장 적게 돈 방식을
    # 고르는 규칙은 그대로라 표본은 계속 균형을 유지한다.
    [ -f "$SP/overnight/$CAMPAIGN.DONE" ] || {
      echo "$(date +%H:%M:%S) CAMPAIGN $CAMPAIGN 목표 달성 :: $(cat "$STATE") -- 계속 돈다"
      cp "$STATE" "$SP/overnight/$CAMPAIGN.DONE"
    }
    NEXT=''; LOW=99999
    for m in "${METHODS[@]}"; do
      n=$(done_for "$m")
      if [ "$n" -lt "$LOW" ]; then LOW=$n; NEXT=$m; fi
    done
  fi

  i=$(cat "$COUNTER"); echo $((i+1)) > "$COUNTER"
  LOG="$SP/overnight/v31-$PREF-L$LOAD-$NEXT-attempt$i.log"
  AIC_METHOD="$NEXT" bash "$SP/run_case.sh" "$PREF" "$LOAD" > "$LOG" 2>&1; rc=$?

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

  [ "$STATUS" = "STARTED_EPISODE" ] && bump "$NEXT"

  python3 - "$LEDGER" "$PREF-L$LOAD" "$i" "$DIR" "$STATUS" "$REASON" "$rc" "$TERM_" "$NEXT" "$CAMPAIGN" <<'PY'
import json, sys, time
path, case, i, d, status, reason, rc, term, method, campaign = sys.argv[1:]
row = {"at": time.strftime('%Y-%m-%dT%H:%M:%S%z'), "version": "v3.1-select10-existing3", "case": case,
       "method": method, "campaign": campaign, "attempt": int(i), "dir": d,
       "submissionStatus": status, "episodeStarted": status == "STARTED_EPISODE",
       "refusal": reason or None, "outerExit": int(rc),
       "episodeTermination": json.loads(term) if term else None,
       "loggedBy": "run_methods_campaign.sh"}
open(path, 'a').write(json.dumps(row) + '\n')
PY
  echo "$(date +%H:%M:%S) ATTEMPT $i [$NEXT] $STATUS :: ${REASON:-$TERM_} :: 진행 $(cat "$STATE")"
  sleep 5
done
