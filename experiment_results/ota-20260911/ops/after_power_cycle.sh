#!/usr/bin/env bash
# 2026-09-23 감사: 폐기.  gNB 를 내리지 않은 채 RIC 을 재시작하고(e2_agent pend_event assert·죽은 소켓 맵)
# KPM 을 jsonl mtime 으로 판정한다.  현행 절차: docs/runbooks/usrp-power-on-startup.md
if [ "${AIC_ALLOW_OLD_POWER_CYCLE:-}" != 1 ]; then
  echo "after_power_cycle.sh 는 폐기됐다 — docs/runbooks/usrp-power-on-startup.md 를 따르라" >&2; exit 2
fi
# 전원을 껐다 켠 뒤 판이 돌 때까지: 순서대로 세우고, 각 단계를 검증한다 (2026-09-21).
#
# 왜 스크립트인가: 2026-09-20/21 두 번의 전원 재투입에서 같은 네 가지를 매번 손으로
# 찾아 고쳤다 -- 코어 컨테이너, 양쪽 X310 링크, PC1 의 무선 공유기, 그리고 RIC/기지국/
# 게이트의 기동 순서.  손으로 하면 늘 한 단계가 빠지고, 빠진 단계는 세 단계 뒤에
# 엉뚱한 증상으로 나타난다(KPM 정지, LEASE_EXPIRED, 게이트 assert).
#
# sudo 가 필요한 한 줄(PC1 의 ens1)만 사람이 실행한다.  나머지는 전부 여기서 한다.
#
#     bash ops/after_power_cycle.sh            # 세우기
#     bash ops/after_power_cycle.sh --check    # 현재 상태만 본다
set -u
OPS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXP="$(cd "$OPS/.." && pwd)"
REPO="$(cd "$EXP/../.." && pwd)"
KPM=/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/a1-live-kpm.jsonl
CHECK_ONLY=false
[ "${1:-}" = "--check" ] && CHECK_ONLY=true

say() { printf '%s %s\n' "$(date +%T)" "$*"; }
witness() {
  docker exec oran-aic-nearrt-ric cat /run/ai-ran/flexric-connection-witness.json 2>/dev/null \
    | python3 -c 'import sys,json
try: d=json.load(sys.stdin); print(",".join(str(c["globalE2NodeId"]["nbId"]) for c in d.get("connections") or []))
except Exception: print("")'
}
kpm_flows() {                     # KPM 파일이 자라는가
  local a b; a=$(stat -c %Y "$KPM" 2>/dev/null || echo 0); sleep 15
  b=$(stat -c %Y "$KPM" 2>/dev/null || echo 0); [ "$b" -gt "$a" ]
}
gnb() { python3 -c "import sys;sys.path.insert(0,'$OPS');import keeper as k;k.restart_$1()" >/dev/null 2>&1; }

# -- 1. 코어 -----------------------------------------------------------------
core_up() {
  local missing=0
  for c in oai-nrf oai-udr oai-udm oai-ausf oai-amf oai-smf oai-upf oai-ext-dn; do
    [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || missing=1
  done
  return $missing
}

# -- 2. 링크 -----------------------------------------------------------------
pc1_usrp_link() { ip -br addr show ens1 2>/dev/null | grep -q '192\.168\.40\.1'; }
pc1_wifi_ok()   { ip -br addr show wlp109s0 2>/dev/null | grep -q '192\.168\.0\.50'; }

if $CHECK_ONLY; then
  say "코어: $(core_up && echo 정상 || echo 내려감)"
  say "PC1 X310 링크: $(pc1_usrp_link && echo 정상 || echo 없음)"
  say "PC1 무선: $(pc1_wifi_ok && echo 정상 || echo '다른 공유기/끊김')"
  say "enb2: $(ssh -o BatchMode=yes -o ConnectTimeout=6 enb2 'ip -br addr show ens1 | grep -q 192.168.30.1 && echo 정상 || echo 없음' 2>/dev/null || echo '접속 불가')"
  say "E2 등록: [$(witness)]"
  say "KPM: $(kpm_flows && echo 흐름 || echo 정지)"
  exit 0
fi

say "== 1. 코어"
if core_up; then say "   이미 떠 있다"; else
  for c in oai-nrf oai-udr oai-udm oai-ausf oai-amf oai-smf oai-upf oai-ext-dn; do
    docker start "$c" >/dev/null 2>&1
  done
  sleep 20
  core_up && say "   기동 완료" || { say "   코어가 서지 않는다 -- docker logs oai-amf 를 보라"; exit 1; }
fi

say "== 2. PC1 무선"
if pc1_wifi_ok; then say "   정상"; else
  nmcli con up LICS5G >/dev/null 2>&1; sleep 8
  pc1_wifi_ok && say "   LICS5G 로 전환했다" || say "   전환 실패 -- UE 에 닿지 못한다"
fi

say "== 3. X310 링크"
if pc1_usrp_link; then say "   PC1 정상"; else
  cat <<'NEED'
   PC1 의 ens1 이 내려가 있다.  이 한 줄만 직접 실행해 달라(sudo 가 필요하다):

     sudo ip link set ens1 up && sudo ip link set ens1 mtu 9000 && sudo ip addr replace 192.168.40.1/24 dev ens1

NEED
  exit 2
fi
ssh -o BatchMode=yes -o ConnectTimeout=6 enb2 '
  ip -br addr show ens1 | grep -q 192.168.30.1 || {
    sudo -n ip addr add 192.168.30.1/24 dev ens1 2>/dev/null
    sudo -n ip addr add 192.168.40.1/24 dev ens1 2>/dev/null
    sudo -n ip link set ens1 mtu 9000 up; }
  ip route | grep -q 192.168.70.128 || sudo -n ip route add 192.168.70.128/26 via 192.168.50.1 dev enp108s0
' >/dev/null 2>&1 && say "   enb2 정상" || say "   enb2 링크 설정 실패"

say "== 4. RIC → gNB2 → gNB1 → 게이트"
systemctl --user stop aic-keeper >/dev/null 2>&1      # 순서를 흔들지 못하게
for attempt in 1 2 3 4; do
  say "   시도 $attempt"
  docker stop oran-aic-kpm-gate >/dev/null 2>&1
  docker restart oran-aic-nearrt-ric >/dev/null 2>&1; sleep 25
  gnb gnb2; sleep 12; say "      gnb2 뒤: [$(witness)]"
  gnb gnb1; sleep 12; nodes="$(witness)"; say "      gnb1 뒤: [$nodes]"
  case "$nodes" in *3584*2816*|*2816*3584*) ;; *) continue ;; esac
  docker start oran-aic-kpm-gate >/dev/null 2>&1; sleep 40
  if kpm_flows; then say "   KPM 흐름 확인 · E2=[$(witness)]"; break; fi
  say "      KPM 이 흐르지 않는다 -- 다시"
  [ "$attempt" = 4 ] && { say "사슬이 서지 않았다"; exit 3; }
done

say "== 5. 재핀"
if bash "$REPO/scripts/hardware/repin_a1p.sh" >/tmp/repin-$$.log 2>&1; then
  say "   $(tail -1 /tmp/repin-$$.log)"
else
  say "   재핀 실패 -- /tmp/repin-$$.log"; exit 4
fi

say "== 6. keeper"
systemctl --user start aic-keeper
say "완료.  UE 가 붙으면 판을 돌릴 수 있다:"
say "  AIC_ARMS=\"three-agent internal-monolith basic-monolith\" setsid nohup bash ops/run_loop_v4.sh 400 > ops/overnight/loop-\$(date +%H%M%S).log 2>&1 &"
