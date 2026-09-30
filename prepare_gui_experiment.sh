#!/usr/bin/env bash
set -euo pipefail

readonly USER_CONFIRMATION="YES"
readonly RF_APPROVAL="GNB_USRP_ON_AND_RF_READY"
readonly PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "$PROJECT_ROOT/bin/labctl" ]]; then
  readonly LABCTL="$PROJECT_ROOT/bin/labctl"
else
  printf '오류: 이 프로젝트 내부의 bin/labctl을 찾을 수 없습니다.\n' >&2
  exit 1
fi

printf '\n[1/4] 장비 상태를 확인합니다.\n'
"$LABCTL" status

printf '\n[2/4] SSH, 권한 및 기동 조건을 점검합니다.\n'
"$LABCTL" preflight

printf '\n[3/4] 실제로 수행될 준비 작업을 미리 확인합니다.\n'
"$LABCTL" prepare

printf '\n[안전 확인]\n'
printf 'gNB1과 gNB2의 USRP 전원을 켜고 RF 케이블·안테나·감쇠기 연결을 확인하십시오.\n'
printf '준비가 끝났으면 다음 문구를 정확히 입력하십시오.\n  %s\n> ' "$USER_CONFIRMATION"
IFS= read -r confirmation

if [[ "$confirmation" != "$USER_CONFIRMATION" ]]; then
  printf '\n취소했습니다. 실제 장비 기동 명령은 실행하지 않았습니다.\n' >&2
  exit 2
fi

printf '\n[4/4] GUI 실험에 필요한 Core/RIC/gNB/UE 준비를 실행합니다.\n'
result=$("$LABCTL" prepare --execute --rf-approval "$RF_APPROVAL")
printf '%s\n' "$result"

if ! printf '%s\n' "$result" | python3 -c 'import json,sys; data=json.load(sys.stdin); raise SystemExit(0 if data.get("disposition") == "COMPLETED" else 1)'; then
  printf '\n오류: 준비 명령이 COMPLETED로 끝나지 않았습니다. 위 결과와 labctl 로그를 확인하십시오.\n' >&2
  exit 1
fi

printf '\n준비 완료: 이제 GUI Live 실험을 시작해도 됩니다. GUI에서 Live binding과 Preflight를 진행하십시오.\n'
