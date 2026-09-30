#!/usr/bin/env bash
# 세 방식을 **블록 단위**로 비교한다.  2026-09-23 결정 §5.2.
#
# 한 블록 = 연속된 세 판, 방식마다 하나.  6가지 방식 순서 순열을 **각 3회**, 즉
# 18블록 54판.  블록 목록은 기록된 시드로 섞는다.
#
# **왜 순열인가**: 이 베드의 셀 총량은 몇 시간 주기로 약 5 Mbps 진동한다(v4.4 전수
# 이동 중앙값 11.0~15.9).  방식마다 판이 도는 시각이 다르면 그 변동이 방식 차이로
# 둔갑한다.  `run_methods_campaign.sh` 의 "가장 적게 돈 방식" 규칙은 균형은 맞추지만
# 순서를 고정하지 않아, 한 방식이 늘 하루의 같은 부분에 놓일 수 있다.
#
# **통계적 검정력 보장이 아니다** -- 초기 측정 배분이다.  어느 방식이 앞선다고
# 캠페인을 멈추거나 늘리지 마라.  중단된 블록은 불완전으로 기록하고 세지 않는다.
set -u
# 2026-09-23 감사: 상태 파일이 잘리면 read_state 가 빈 값을 주고, 빈 방식 이름이
# run_case.sh 에서 three-agent 기본값으로 떨어져 **다른 방식의 자리에서** 판이 돌았다.
# 상태·계획은 tmp+mv 로 원자적으로 쓰고, 읽은 값이 말이 안 되면 추측하지 않고 멈춘다.
# (set -e 는 쓰지 않는다: rc=$? 수집·빈 grep 이 루프를 끊는다.)
die() { echo "$(date +%H:%M:%S) CAMPAIGN_STATE_INVALID :: $*" >&2; exit 78; }
is_int() { case "$1" in ''|*[!0-9]*) return 1;; *) return 0;; esac; }
SP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREF=${AIC_CASE_PREF:-P1} LOAD=${AIC_CASE_LOAD:-10}
CAMPAIGN=${AIC_CAMPAIGN:-blocks18-v46}
BLOCKS=${AIC_BLOCKS:-18}
SEED=${AIC_BLOCK_SEED:-20260923}
PLAN="$SP/overnight/$CAMPAIGN.plan.json"
STATE="$SP/overnight/$CAMPAIGN.progress.json"
COUNTER="$SP/overnight/next-attempt.txt"
LEDGER="$SP/overnight/v31-ledger.jsonl"

# 계획은 한 번만 만들고 그 뒤로는 읽는다 -- 재기동해도 같은 순서를 잇는다.
[ -f "$PLAN" ] || python3 - "$PLAN" "$BLOCKS" "$SEED" <<'PY'
import itertools, json, random, sys
path, blocks, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
methods = ("three-agent", "internal-monolith", "basic-monolith")
perms = list(itertools.permutations(methods))          # 6
if blocks % len(perms):
    raise SystemExit(f"blocks {blocks} is not a multiple of {len(perms)} permutations")
plan = [list(p) for p in perms for _ in range(blocks // len(perms))]
random.Random(seed).shuffle(plan)
import os
json.dump({"seed": seed, "blocks": blocks, "permutations": len(perms),
           "note": "each of the 6 method orders appears blocks/6 times; the block "
                   "list is shuffled with the recorded seed before execution",
           "plan": plan}, open(path + ".tmp", "w"), indent=1)
os.replace(path + ".tmp", path)
print(f"wrote {path}: {len(plan)} blocks")
PY
[ -f "$PLAN" ] || die "no plan at $PLAN"

[ -f "$STATE" ] || { printf '{"block":0,"slot":0,"completedBlocks":0,"incompleteBlocks":[]}\n' > "$STATE.tmp" \
                      && mv "$STATE.tmp" "$STATE"; }

# 결정 §5.2 "Report any infrastructure interruption and incomplete block."
# `incompleteBlocks` 는 만들어만 두고 한 번도 쓰이지 않았다.  러너가 **시작할 때마다**
# 어디서 이어 가는지 적는다: 첫 줄이 캠페인 시작이고, 그 뒤의 줄은 전부 중단
# (서비스 재시작·재부팅) 뒤 재개다.  슬롯이 0 이 아닌 곳에서 재개했다면 그 블록은
# 시간 간격을 두고 이어진 것이라 `incompleteBlocks` 에 표시한다 -- 판은 버리지 않는다.
python3 - "$STATE" <<'STATE_PY'
import json, sys, time
path = sys.argv[1]
state = json.load(open(path))
entry = {"at": time.strftime('%Y-%m-%dT%H:%M:%S%z'), "kind": "runner-start",
         "block": state["block"], "slot": state["slot"]}
starts = state.setdefault("runnerStarts", [])
if starts and state["slot"] != 0:
    entry["kind"] = "resumed-mid-block"
    if state["block"] not in state["incompleteBlocks"]:
        state["incompleteBlocks"].append(state["block"])
elif starts:
    entry["kind"] = "resumed-at-block-boundary"
starts.append(entry)
import os
json.dump(state, open(path + '.tmp', 'w'))
os.replace(path + '.tmp', path)
STATE_PY
[ $? -eq 0 ] || die "cannot read or update $STATE; fix or restore it by hand -- not guessing the position"

read_state() { python3 -c "import json;d=json.load(open('$STATE'));print(d['$1'])"; }
plan_at()    { python3 -c "import json;print(json.load(open('$PLAN'))['plan'][$1][$2])"; }
plan_len()   { python3 -c "import json;print(len(json.load(open('$PLAN'))['plan']))"; }

# A runner start is an interruption too: treat it as leaving a PAUSE so a half-run block restarts
# (Codex review of bdc31dbbe -- stop while PAUSEd, remove PAUSE, start used to skip the helper).
PAUSED=1
while true; do
  # owner 2026-09-26: a PAUSE cuts at the running board; leaving it restarts a half-run block
  # with a new reference (restart_block_after_pause.py) instead of resuming it an hour later.
  [ -f "$SP/overnight/PAUSE" ] && { PAUSED=1; sleep 30; continue; }
  if [ "$PAUSED" = 1 ]; then
    PAUSED=0
    RESTART=$(python3 "$SP/restart_block_after_pause.py" "$CAMPAIGN" "$STATE" "$SP/overnight") \
      || die "restart_block_after_pause.py failed; the block position was not changed"
    echo "$(date +%H:%M:%S) PAUSE lifted :: $RESTART"
  fi
  BLOCK=$(read_state block); SLOT=$(read_state slot); TOTAL=$(plan_len)
  is_int "$BLOCK" && is_int "$SLOT" && is_int "$TOTAL" \
    || die "block='$BLOCK' slot='$SLOT' total='$TOTAL' from $STATE / $PLAN"
  if [ "$BLOCK" -ge "$TOTAL" ]; then
    echo "$(date +%H:%M:%S) CAMPAIGN $CAMPAIGN COMPLETE :: $TOTAL blocks"
    touch "$SP/overnight/$CAMPAIGN.DONE"; sleep 300; continue
  fi
  NEXT=$(plan_at "$BLOCK" "$SLOT")
  case "$NEXT" in three-agent|internal-monolith|basic-monolith) ;;
    *) die "plan[$BLOCK][$SLOT] gave method '$NEXT'";; esac

  # **블록의 첫 판 전에** 선언된 시작 설정과 부하를 되돌리고 확인한다.  판 사이의
  # 복원은 러너 자신의 준비 단계가 하지만, 블록 경계에서는 그 확인을 기록에 남긴다.
  if [ "$SLOT" -eq 0 ]; then
    echo "$(date +%H:%M:%S) BLOCK $BLOCK/$TOTAL order=$(python3 -c "
import json;print(','.join(json.load(open('$PLAN'))['plan'][$BLOCK]))")"
  fi

  # v4.7 (.orca/drops/V47_FINAL_PLAN.md 2.1-2.2): before a block's first board, measure the
  # reference DL, generate the block corpus from it and freeze both for the whole block.
  # A failed or non-positive reference starts no board; it is measured again.
  BLOCK_ENV="$SP/overnight/$CAMPAIGN.block$BLOCK.env"
  # 2026-09-28 (Codex on 48c2b72): the maintenance daemon polls every 5 min and missed block
  # boundaries; it now leaves CORE_RESET_WANTED and the runner resets the core here, before the
  # block's reference, where a reset discards no finished board.
  if [ ! -s "$BLOCK_ENV" ] && [ -f "$SP/overnight/CORE_RESET_WANTED" ]; then
    echo "$(date +%H:%M:%S) BLOCK $BLOCK core reset at the block boundary (CORE_RESET_WANTED)"
    bash "$SP/core_echo_reset.sh" >> "$SP/overnight/preventive-maintenance.log" 2>&1
    rm -f "$SP/overnight/CORE_RESET_WANTED"; touch "$SP/overnight/last-core-reset" "$SP/overnight/last-ue-refresh"
    continue
  fi
  if [ "${AIC_V47:-0}" = 1 ] && [ ! -s "$BLOCK_ENV" ]; then
    REF="$SP/overnight/$CAMPAIGN.block$BLOCK.reference.json"
    if python3 "$SP/reference_dl.py" --out "$REF" > "$REF.log" 2>&1 \
       && python3 "$SP/make_v47_corpus.py" "$REF" > "$BLOCK_ENV.tmp" 2>>"$REF.log" \
       && grep -q '^export AIC_BLOCK_PILOT=' "$BLOCK_ENV.tmp"; then
      mv "$BLOCK_ENV.tmp" "$BLOCK_ENV"
      echo "$(date +%H:%M:%S) BLOCK $BLOCK reference $(python3 -c "import json;print(json.load(open('$REF'))['reference'])") corpus $(cut -d' ' -f2 "$BLOCK_ENV")"
    else
      rm -f "$BLOCK_ENV.tmp"
      echo "$(date +%H:%M:%S) BLOCK $BLOCK reference DL failed (see $REF.log); retry in 120 s"
      sleep 120; continue
    fi
  fi
  # 2026-09-27 09:40: maintenance set PAUSE while the reference ran, saw no lock in the gap after
  # it and reset the core as board 777 started -- the board went ahead without looking at PAUSE.
  [ -f "$SP/overnight/PAUSE" ] && continue
  # keeper flagged a UE to restart between boards (KEEPER_GAP): give it a lock-free gap, 90 s at most.
  for _ in $(seq 1 18); do [ -f "$SP/overnight/KEEPER_GAP" ] || break; sleep 5; done
  [ -f "$SP/overnight/PAUSE" ] && continue   # (Codex) a PAUSE set during that wait still holds
  if [ "${AIC_V47:-0}" = 1 ]; then
    # shellcheck disable=SC1090
    source "$BLOCK_ENV"; export AIC_BLOCK_PILOT AIC_BLOCK_INTENTS_SHA AIC_BLOCK_MANIFEST_SHA
    export AIC_T_CAP=9
  fi

  i=$(cat "$COUNTER"); is_int "$i" || die "attempt counter $COUNTER reads '$i'"
  echo $((i+1)) > "$COUNTER.tmp" && mv "$COUNTER.tmp" "$COUNTER"
  LOG="$SP/overnight/$CAMPAIGN-b${BLOCK}s${SLOT}-$NEXT-attempt$i.log"
  # Handover outcomes of this board from both gNB logs (plan V47 section 4: counted in the
  # campaign, no separate soak).  Never fatal: a missing log records null, the board runs.
  HO_L1=$(ls -t /opt/ran-lab/controller/gnb1-loop38-*.log 2>/dev/null | head -1); HO_N1=$(wc -l < "$HO_L1" 2>/dev/null || echo 0)
  HO_L2=$(ssh -o ConnectTimeout=5 enb2 'ls -t /tmp/gnb2-probe-*.log | head -1' 2>/dev/null)
  HO_N2=$(ssh -o ConnectTimeout=5 enb2 "wc -l < $HO_L2" 2>/dev/null || echo 0)
  AIC_METHOD="$NEXT" bash "$SP/run_case.sh" "$PREF" "$LOAD" > "$LOG" 2>&1; rc=$?
  {
    HO1=$(tail -n +"$((HO_N1+1))" "$HO_L1" 2>/dev/null > "/tmp/aic-ho-$CAMPAIGN-gnb1.log" && bash "$SP/ho_outcome.sh" "/tmp/aic-ho-$CAMPAIGN-gnb1.log")
    scp -q -o ConnectTimeout=5 "$SP/ho_outcome.sh" enb2:/tmp/ho_outcome.sh 2>/dev/null
    HO2=$(ssh -o ConnectTimeout=5 enb2 "tail -n +$((HO_N2+1)) $HO_L2 > /tmp/aic-ho-gnb2.log && bash /tmp/ho_outcome.sh /tmp/aic-ho-gnb2.log" 2>/dev/null)
    python3 - "$SP/overnight/$CAMPAIGN-handovers.jsonl" "$i" "$NEXT" "$BLOCK" "$SLOT" "$HO1" "$HO2" <<'PY'
import json, re, sys, time
path, attempt, method, block, slot, g1, g2 = sys.argv[1:]
def tally(text):
    rows = [dict((k, int(v)) for k, v in re.findall(r'(\w+)=(\d+)', line)) for line in text.splitlines() if 'ue=' in line]
    return {'triggered': len(rows), 'completed': sum(1 for r in rows if r.get('complete', 0) > 0),
            'integrityFail': sum(r.get('integrityFail', 0) for r in rows),
            'ongoingRefusals': sum(r.get('ongoingRefusals', 0) for r in rows)}
open(path, 'a').write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'attempt': int(attempt),
    'method': method, 'block': int(block), 'slot': int(slot),
    'gnb1': tally(g1), 'gnb2': tally(g2)}) + '\n')
PY
  } || echo "$(date +%H:%M:%S) handover tally for attempt $i failed (board result unaffected)"

  DIR=$(grep -o 'formal38guarded-[0-9T]*-[0-9a-f]*' "$LOG" | tail -1)
  STATUS=$(grep -o '"submissionStatus": "[A-Z_]*"' "$LOG" | tail -1 | cut -d'"' -f4)
  TERM_=$(python3 -c "
import json,glob,sys
f=glob.glob(sys.argv[1]+'/evidence/*-episode.json')
if f:
    e=json.load(open(f[0])); t=e.get('termination') or {}
    print(json.dumps({'reason':t.get('reason'),'kernelTermination':t.get('kernelTermination')}))" \
    "$SP/../$DIR" 2>/dev/null)

  python3 - "$LEDGER" "$STATE" "$PREF-L$LOAD" "$i" "$DIR" "$STATUS" "$rc" "$TERM_" \
           "$NEXT" "$CAMPAIGN" "$BLOCK" "$SLOT" "$TOTAL" <<'PY'
import json, sys, time
(path, state_path, case, i, d, status, rc, term,
 method, campaign, block, slot, total) = sys.argv[1:]
block, slot, total = int(block), int(slot), int(total)
started = status == "STARTED_EPISODE"
open(path, 'a').write(json.dumps({
    "at": time.strftime('%Y-%m-%dT%H:%M:%S%z'), "version": "v4.6-blocks",
    "case": case, "method": method, "campaign": campaign,
    "block": block, "slot": slot, "attempt": int(i), "dir": d,
    "submissionStatus": status, "episodeStarted": started,
    "outerExit": int(rc), "episodeTermination": json.loads(term) if term else None,
    "loggedBy": "run_blocks_campaign.sh"}) + '\n')
state = json.load(open(state_path))
# **한 슬롯은 판이 실제로 시작했을 때만 넘어간다.**  preflight 거절은 방식과 무관하니
# 그 자리를 다시 시도한다 -- 안 그러면 거절이 잦은 시각의 방식만 표본을 잃는다.
if started:
    state['slot'] = slot + 1
    if state['slot'] >= 3:
        state['slot'] = 0
        state['block'] = block + 1
        state['completedBlocks'] = state.get('completedBlocks', 0) + 1
import os
json.dump(state, open(state_path + '.tmp', 'w'))
os.replace(state_path + '.tmp', state_path)
PY
  [ $? -eq 0 ] || die "could not record attempt $i into $STATE"
  echo "$(date +%H:%M:%S) ATTEMPT $i [b$BLOCK s$SLOT $NEXT] $STATUS :: ${TERM_}"
  sleep 5
done
