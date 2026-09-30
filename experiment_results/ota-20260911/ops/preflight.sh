#!/usr/bin/env bash
# 무선이나 RIC 을 보기 전에 60초 안에 전체 스택을 훑는다 (2026-09-21).
#
# 왜: 2026-09-21 에 UE 가 하나도 안 붙는 것을 다섯 시간 팠다.  PRACH 분포, CFO,
# 안테나, SCTP 멀티호밍, 기준 클럭을 차례로 의심하고 전부 반증했다.  정답은
# `docker ps -a` 한 줄(`mysql  Exited 12 hours ago`)과 UE 로그 한 줄
# (`Registration Reject: Illegal_UE`)에 처음부터 있었다.  아래를 먼저 돌려라.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '\n== %s\n' "$*"; }

say "1. 꺼져 있는 컨테이너 (여기서 끝나는 경우가 많다)"
docker ps -a --filter status=exited --format '   {{.Names}}  {{.Status}}' | grep -vE 'before-epoch|-old-|weeks ago' || echo '   없음'

say "2. 컨테이너 재시작 정책 (no 면 한 번 꺼지면 영원히 꺼져 있다)"
for c in mysql oai-nrf oai-udr oai-udm oai-ausf oai-amf oai-smf oai-upf oai-ext-dn; do
  p=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c" 2>/dev/null)
  [ "$p" = "unless-stopped" ] || [ "$p" = "always" ] || printf '   %-12s %s  ← 고쳐라\n' "$c" "${p:-없음}"
done

say "3. 서비스"
for s in aic-keeper aic-v31-episodes; do printf '   %-20s %s\n' "$s" "$(systemctl --user is-active $s)"; done
[ -f "$HERE/overnight/NO_EPISODES" ] && echo "   (NO_EPISODES 보류 파일이 있다)"
[ -f "$HERE/overnight/NO_UE_AUTO_RESTART" ] && echo "   (NO_UE_AUTO_RESTART 보류 파일이 있다 — UE 는 판 러너가 붙인다)"

say "4. UE 가 거절당하고 있나 (NAS 는 무선보다 먼저 본다)"
for h in ue1 ue2 ue3; do
  printf '   %-4s ' "$h"
  ssh -o BatchMode=yes -o ConnectTimeout=6 "$h" '
    L=$(ls -t ~/ota-fixed38-*.log 2>/dev/null | head -1)
    printf "tun=%s " "$(ip -4 -o addr show up dev oaitun_ue1 2>/dev/null | awk "{print \$4}" | head -1)"
    [ -n "$L" ] && grep -aoE "Registration Reject cause: [A-Za-z_]+|Registration accept" "$L" | tail -1' 2>/dev/null
  echo
done

say "5. 사슬"
for c in oran-aic-nearrt-ric oran-aic-kpm-gate oran-aic-a1p-producer; do
  printf '   %-24s %s\n' "$c" "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)"
done
for nb in 3584 2816; do
  printf '   KPM 30초 nb %s: %s\n' "$nb" "$(docker logs --since 30s oran-aic-kpm-gate 2>&1 | grep -ac "\"nb_id\":$nb")"
done

say "6. 판이 실제로 쌓이고 있나 (놀고 있는지 드러나는 곳)"
n=$(ls -d "$HERE"/../formal38guarded-*/ 2>/dev/null | wc -l)
newest=$(ls -dt "$HERE"/../formal38guarded-*/ 2>/dev/null | head -1)
printf '   판 %s개, 마지막 %s\n' "$n" "$(stat -c %y "$newest" 2>/dev/null | cut -c1-16)"
echo

say "=== 프로듀서 포트 (2026-09-21 추가: 이게 빠져서 판이 1초 만에 즉사했다) ==="
# R1 조종 프로듀서가 죽어 있으면 부착 게이트는 통과하고 판은 뜨지만
# `DEPENDENCY_PREFLIGHT_REFUSED:R1Error` 로 1.2초 만에 죽는다. 증상이 "UE 가 안 붙는다"
# 와 전혀 다르게 보여서 UE 를 몇 시간 뒤지게 만든다.
for hp in "192.168.50.1 18443 R1조종" "192.168.50.1 9444 A1P" "192.168.50.1 9445 캠페인5액션"; do
  set -- $hp
  if timeout 4 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; then
    say "  $3 ($1:$2) 열림"
  else
    say "  ** $3 ($1:$2) 닫힘 — 판이 preflight 에서 즉사한다 **"
  fi
done
