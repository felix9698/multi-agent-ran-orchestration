#!/usr/bin/env bash
# RIC → gNB1 → gNB2 → KPM gate, verifying each step before the next (2026-09-20).
#
# Why a script: the RIC aborts when a witness publish fails, every abort drops both
# gNBs (gnb2 never re-registers by itself), every gNB restart bumps the connection
# epoch, and a stale epoch makes the KPM gate's stream useless to the preflight.
# Doing this by hand means one of the four is always a step behind.  This retries
# the whole chain instead of leaving it half-built.
set -u
OPS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RIC=oran-aic-nearrt-ric
GATE=oran-aic-kpm-gate

# **active 인 것만** 센다.  재등록한 노드는 witness 에 옛 항목이 남을 수 있고,
# 그것까지 세면 붙지 않은 셀을 붙었다고 읽는다 (keeper 의 `_live_epochs()` 와 같은 기준).
witness() {
  docker exec "$RIC" cat /run/ai-ran/flexric-connection-witness.json 2>/dev/null \
    | python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit
print(",".join(str(c["globalE2NodeId"]["nbId"]) for c in d.get("connections") or []
               if c.get("active") and isinstance(c.get("connectionEpoch"), int)))'
}

ric_alive() { [ "$(docker inspect -f '{{.State.Running}}' $RIC 2>/dev/null)" = true ]; }

# `import keeper` 를 cwd 에 맡기면 레포 루트에서 부를 때 엉뚱한 경로를 뒤져, 기지국을
# 한 번도 안 띄운 채 세 번 시도를 헛돈다.  스크립트 자신의 위치로 고정한다.
start_gnb() {   # $1 = gnb1|gnb2
  AIC_OPS="$OPS" python3 - "$1" <<'PY'
import os, sys
sys.path.insert(0, os.environ['AIC_OPS'])
import keeper as k
(k.restart_gnb1 if sys.argv[1] == 'gnb1' else k.restart_gnb2)()
PY
}

stop_gnbs() {   # RIC 보다 먼저 두 기지국을 내린다 (2026-09-21, 코드와 패킷으로 확인).
                # 죽은 게이트가 남긴 구독은 **기지국 안에** 살아 있어, 기지국이 계속
                # 그 번호로 지시를 올린다(RIC 로그의 "no xApp associated" 폭주).  그걸
                # 처리하느라 RIC 의 E2 SETUP 응답이 3초를 넘기면, 에이전트의 3초 주기
                # 타이머가 먼저 울리고 응답이 pending 을 비운 뒤 그 타이머 이벤트가
                # 디스패치되어 e2_agent.c:225 의 assert(bi_map_size == 1) 가 깨진다
                # -- 기지국이 기동 중 즉사한다.  게다가 그렇게 반쯤 붙었다 죽은 노드는
                # RIC 의 노드->소켓 맵에 죽은 포트를 남겨, 그 노드로 가는 구독이
                # 영영 응답을 못 받는다(캡처: RIC:36421 -> gnb2:58100 INIT / ABORT).
                # 둘 다 내렸다 올리면 낡은 구독도 낡은 소켓도 남지 않는다.
  pkill -TERM -x nr-softmodem 2>/dev/null
  ssh -o BatchMode=yes -o ConnectTimeout=8 enb2 'sudo -n pkill -TERM -x nr-softmodem' >/dev/null 2>&1
  for i in $(seq 1 15); do
    here=$(pgrep -cx nr-softmodem 2>/dev/null || echo 0)
    there=$(ssh -o BatchMode=yes -o ConnectTimeout=8 enb2 'pgrep -cx nr-softmodem' 2>/dev/null || echo 0)
    [ "$here" = 0 ] && [ "$there" = 0 ] && break
    sleep 2
  done
  sleep 15          # USRP 해제
}

for attempt in 1 2 3; do
  echo "== 시도 $attempt  $(date +%T)"
  docker stop "$GATE" >/dev/null 2>&1
  stop_gnbs                       # RIC 보다 먼저 -- 위 주석 참조
  docker restart "$RIC" >/dev/null 2>&1
  sleep 20
  ric_alive || { echo "   RIC 이 안 뜬다"; continue; }

  start_gnb gnb1; sleep 10
  echo "   gnb1 뒤 witness: [$(witness)]  RIC alive=$(ric_alive && echo yes || echo no)"
  ric_alive || continue

  start_gnb gnb2; sleep 12
  nodes="$(witness)"
  echo "   gnb2 뒤 witness: [$nodes]  RIC alive=$(ric_alive && echo yes || echo no)"
  case "$nodes" in
    *3584*2816*|*2816*3584*) ;;
    *) echo "   두 노드가 다 붙지 않았다 -- 다시"; continue ;;
  esac

  # mtime 은 **한 셀만** 보고해도 전진한다 -- 2026-09-21 에 9445 가 한 셀만 물고 있던 것과
  # 같은 함정이다.  두 nbId 가 **둘 다** 보이는지 본다.  단, 꼬리를 그냥 읽으면
  # **재기동 전의 옛 줄**이 두 셀을 다 갖고 있어 통과한다 -- 게이트를 띄운 시점의
  # 파일 크기를 적어 두고 **그 뒤에 덧붙은 바이트만** 읽는다.
  KPM=/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/a1-live-kpm.jsonl
  mark=$(stat -c %s "$KPM" 2>/dev/null || echo 0)
  docker start "$GATE" >/dev/null 2>&1
  sleep 35
  seen=""
  for i in $(seq 1 10); do
    sleep 5
    now=$(stat -c %s "$KPM" 2>/dev/null || echo 0)
    [ "$now" -lt "$mark" ] && mark=0          # 회전됐으면 처음부터
    seen=$(tail -c +$((mark + 1)) "$KPM" 2>/dev/null | python3 -c 'import sys,json
ids=set()
for line in sys.stdin:
    try: ids.add(int(json.loads(line).get("nb_id")))
    except Exception: pass
print(",".join(str(i) for i in sorted(ids)))')
    case ",$seen," in *,3584,*) case ",$seen," in *,2816,*) break;; esac;; esac
  done
  case ",$seen," in *,3584,*) ;; *) echo "   KPM 에 3584 가 없다 [$seen] -- 다시"; continue;; esac
  case ",$seen," in *,2816,*) ;; *) echo "   KPM 에 2816 이 없다 [$seen] -- 다시"; continue;; esac
  echo "   KPM 두 셀 확인 [$seen].  witness=[$(witness)]"
  echo "READY"
  exit 0
done
echo "FAILED: 세 번 시도했지만 사슬이 서지 않았다"
exit 1
