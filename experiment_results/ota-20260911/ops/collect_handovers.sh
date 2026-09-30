#!/usr/bin/env bash
# AMF 가 남기는 핸드오버 사건을 **보존**한다.  아무것도 바꾸지 않는 순수 관측이다.
#
# 왜: `servingCell` 되읽기는 셀 변경 시행의 관측을 자주 잃는다(2026-09-17: 조종이
# 요청한 12건 중 9건이 관측 없음).  그런데 AMF 는 `Handover Request Ack` 을
# 놓치지 않는다 -- 10:47 의 실제 핸드오버는 AMF 로그로만 증명됐다.  그리고
# **AMF 로그 보존은 약 6시간**이라 그 증거는 곧 사라진다.  그래서 파일로 받아 둔다.
#
# 가입자 식별자(SUPI/IMSI)는 **지운다** -- 증거에 남길 이유가 없다.
set -u
OPS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$OPS/overnight/handover-events.log"
exec >>"$OUT" 2>&1
# 이 스크립트를 죽여도 `docker logs -f` 자식은 살아남아 같은 파일에 계속 쓴다.
# 2026-09-17 에 실제로 그랬다 -- 부모만 죽였더니 고아가 남아 **모든 줄이 두 번**
# 찍혔고, 그 중복을 "AMF 가 두 번 로그한다" 로 오해할 뻔했다.  프로세스 그룹째 끝낸다.
trap 'kill 0' EXIT INT TERM
echo "=== 수집 시작 $(date '+%F %T %Z') (AMF 로그는 UTC+2, KST = +7h) ==="
# `docker logs -f` 는 컨테이너가 재시작하면 **끝난다**.  그대로 두면 수집기가
# 조용히 죽고 아무도 모른다 -- 오늘 판정기가 사라진 스크래치패드를 가리키던 것과
# 같은 종류의 구멍이다.  그래서 다시 붙는다.
while true; do
  docker logs -f --since 1m oai-amf 2>&1 \
    | grep --line-buffered -aE "Handover Required|Handover Request Ack|Handover Notify|Path Switch|UE Context Release Complete ran_ue" \
    | sed -u -E 's/imsi-[0-9]+/imsi-<가림>/g; s/supi-[0-9]+/supi-<가림>/g; s/SUPI [^)]*/SUPI <가림>/g'
  echo "--- 스트림이 끊겼다 $(date '+%F %T') · 10초 뒤 다시 붙는다 ---"
  sleep 10
done
