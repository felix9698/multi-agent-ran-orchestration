#!/usr/bin/env bash
# 전원 재투입 뒤 기동 -- 순서 고정 (docs/runbooks/usrp-power-on-startup.md 2.5단계, 오너 2026-09-28 지적).
#   전부 정지 -> 코어 UPF->SMF->AMF -> bring_up_oran.sh(RIC->gnb1->gnb2->KPM 게이트, keeper 끈 채)
#   -> witness epoch 60초 안정 -> repin_a1p.sh -> keeper(UE 3대) -> 세 UE 부착 확인 -> 보류 해제·판 재개.
# 한 단계라도 통과 못 하면 보류(PAUSE+NO_EPISODES)를 걸어 둔 채 멈춘다.
set -u
OPS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$OPS/../../.." && pwd)"
LEDGER="$OPS/overnight/bed-changes.log"
say(){ echo "$(date +%T) $*"; }
fail(){ say "FAILED: $* (보류 유지)"; exit 1; }
epochs(){ docker exec oran-aic-nearrt-ric cat /run/ai-ran/flexric-connection-witness.json 2>/dev/null | python3 -c \
  "import json,sys;d=json.load(sys.stdin);print(sorted((c['globalE2NodeId']['nbId'],c['connectionEpoch']) for c in d['connections'] if c.get('active')))"; }

touch "$OPS/overnight/PAUSE" "$OPS/overnight/NO_EPISODES" || fail "보류 파일"
systemctl --user stop aic-v31-episodes aic-keeper
for u in aic-v31-episodes aic-keeper; do systemctl --user is-active -q $u && fail "$u 가 안 멈춘다"; done
say "1/7 UE·gNB 정지"
AIC_OPS="$OPS" python3 - <<'PY' || fail "UE 정지"
import os, sys
sys.path.insert(0, os.environ['AIC_OPS'])
import keeper
pw = keeper._ue_password()
src = (keeper.WINDOW / 'execute_once.py').read_text()
i = src.index('STOP_CODE = """') + len('STOP_CODE = """')
code = src[i:src.index('"""', i)]
for h in ('ue1', 'ue2', 'ue3'):
    rc = keeper.ssh(h, ['sudo', '-S', '-p', '', 'python3', '-c', code], timeout=90, stdin=pw + '\n').returncode
    print(h, 'stop rc', rc)
    if rc != 0:
        sys.exit(1)
PY
pkill -TERM -x nr-softmodem 2>/dev/null; ssh enb2 'sudo -n pkill -TERM -x nr-softmodem' 2>/dev/null
for i in $(seq 1 30); do [ "$(pgrep -cx nr-softmodem)" = 0 ] && [ "$(ssh enb2 pgrep -cx nr-softmodem)" = 0 ] && break; sleep 2; done
# 09-30 06:52: gnb1 이 SIGTERM 에 반쯤 내려간 채(L1 스레드 소멸, main do_poll) 멈췄다 -> 내가 띄운 것이니 KILL
[ "$(pgrep -cx nr-softmodem)" = 0 ] || { say "gnb1 SIGTERM 무응답 -> SIGKILL"; pkill -KILL -x nr-softmodem; sleep 3; }
[ "$(pgrep -cx nr-softmodem)" = 0 ] || fail "gnb1 가 안 내려간다"
[ "$(ssh -o ConnectTimeout=8 enb2 pgrep -cx nr-softmodem 2>/dev/null)" = 0 ] || fail "gnb2 가 안 내려간다(또는 enb2 무응답)"
sleep 15   # USRP 해제

say "2/7 코어 UPF->SMF->AMF"
for c in oai-upf oai-smf oai-amf; do docker restart -t 20 $c >/dev/null || fail "$c 재시작"; sleep 8; done
for i in $(seq 1 30); do
  ok=1; for c in oai-upf oai-smf oai-amf; do [ "$(docker inspect -f '{{.State.Health.Status}}' $c 2>/dev/null)" = healthy ] || ok=0; done
  [ $ok = 1 ] && break; sleep 5
done
[ $ok = 1 ] || fail "코어 healthy 아님"
docker exec oai-upf ip -br addr show tun0 | grep -q UP || fail "UPF tun0"

say "3/7 RIC->gnb1->gnb2->KPM 게이트"
bash "$OPS/bring_up_oran.sh" | tail -3 | tee -a "$OPS/overnight/bring-up-oran.last" | grep -q READY || fail "bring_up_oran.sh READY 아님"

say "4/7 epoch 60초 안정"
a=$(epochs); sleep 65; b=$(epochs)
[ "$a" = "$b" ] || fail "epoch 불안정 ($a / $b)"
echo "$b" | grep -qE "^\[\(2816, [0-9]+\), \(3584, [0-9]+\)\]$" || fail "두 노드 active·정수 epoch 아님 ($b)"

say "5/7 재핀 $b"
(cd "$REPO" && bash scripts/hardware/repin_a1p.sh >/dev/null 2>&1) || fail "repin_a1p.sh"

say "6/7 keeper -> UE 3대"
systemctl --user reset-failed aic-keeper 2>/dev/null; systemctl --user start aic-keeper || fail "keeper 기동"
T0=$(date +%H:%M:%S); up=0
for i in $(seq 1 90); do
  t=$(tail -3 "$OPS/overnight/datapath-watch.log")
  # (Codex) 이 스크립트가 keeper 를 켠 뒤에 찍힌 줄만 (datapath-watch 줄은 HH:MM:SS 로 시작, 날짜 없음)
  if [ "$(echo "$t" | head -1 | cut -c1-8)" \> "$T0" ] && echo "$t" | grep -q "ue1 ip=12.*kpm=\[2816\]" \
     && echo "$t" | grep -q "ue2 ip=12.*kpm=\[3584\]" && echo "$t" | grep -q "ue3 ip=12.*kpm=\[3584\]"; then up=1; break; fi
  sleep 10
done
[ $up = 1 ] || fail "UE 3대 부착 안 됨 (15분)"

say "7/7 보류 해제·판 재개"
rm -f "$OPS/overnight/PAUSE" "$OPS/overnight/NO_EPISODES"
systemctl --user restart aic-v31-episodes && systemctl --user is-active -q aic-v31-episodes \
  || { touch "$OPS/overnight/PAUSE"; fail "판 서비스 재시작"; }
echo "$(date '+%F %H:%M') power_on_bringup.sh 완료: 코어->RIC->gnb1->gnb2->게이트->재핀($b)->UE 3대->판 재개." >> "$LEDGER"
say "READY"
