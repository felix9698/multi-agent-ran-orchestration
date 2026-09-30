#!/usr/bin/env bash
# 러너가 판을 세우는 중이면 하드웨어를 건드리지 못하게 막는다 (2026-09-21).
#
# 왜: 2026-09-21 에 나는 아무것도 "멈추지" 않았는데도 판이 한 번도 못 떴다.  진단하느라
# `force_reattach` 를 열 번, gNB 재기동을 여덟 번 돌렸는데, 러너는 **세 UE 가 동시에 20 초**
# 버텨야 판을 시작한다.  내 진단이 매번 그 시계를 0 으로 되돌렸다.  서비스를 멈추는 것만
# 중단이 아니다 -- 러너와 같은 하드웨어를 뺏는 것도 중단이다.
#
#   bash ops/bedguard.sh                 # 지금 건드려도 되나 (종료코드 0=가능, 1=불가)
#   bash ops/bedguard.sh -- <명령...>    # 가능할 때만 실행
#
# 읽기 전용 측정(로그 grep, 카운터 읽기, docker logs)은 이 가드를 쓸 필요가 없다.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$HERE/overnight/episode-busy.lock"

holder=""
if [ -f "$LOCK" ]; then
  pid=$(awk '{print $1}' "$LOCK" 2>/dev/null)
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    holder="$pid $(tr -d '\n' < "$LOCK" | cut -c1-40)"
  fi
fi

if [ -n "$holder" ]; then
  echo "거부: 러너가 베드를 쓰는 중이다 ($holder)" >&2
  echo "      하드웨어를 건드리면 세 UE 동시 부착 시계가 0 으로 돌아간다." >&2
  echo "      읽기 전용으로 재거나, 판이 끝나기를 기다려라." >&2
  exit 1
fi

[ "${1:-}" = "--" ] || { echo "베드 가용 (러너가 락을 잡고 있지 않다)"; exit 0; }
shift
exec "$@"
