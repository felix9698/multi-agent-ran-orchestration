#!/usr/bin/env bash
set -euo pipefail
: "${LABCTL_RIC_ROOT:?set LABCTL_RIC_ROOT to the deployed RIC runtime}"
: "${LABCTL_RUN_ROOT:?set LABCTL_RUN_ROOT to a writable lab run directory}"
root=$LABCTL_RIC_ROOT
binary=$root/artifacts/flexric/bin/nearRT-RIC
config=$root/runtime/flexric/flexric.conf
service_models=$root/artifacts/flexric/lib/service-models
expected=9808ae2c21a5d0e6e99232c6b911eea8dd56d28d37f9aa671d07325e52665f4f
test "$(sha256sum "$binary" | awk '{print $1}')" = "$expected"
test -r "$config"
test -d "$service_models"
! pgrep -x nearRT-RIC >/dev/null
stamp=$(TZ=Asia/Seoul date +%Y%m%dT%H%M%SKST)
run=$LABCTL_RUN_ROOT/labctl-ric-$stamp
mkdir -p "$run"
sudo -n -b env SM_DIR="$service_models" "$binary" -c "$config" >"$run/nearRT-RIC.log" 2>&1
for _ in $(seq 1 30); do
  pid=$(pgrep -o -x nearRT-RIC || true)
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
    printf 'RIC_STARTED pid=%s run=%s\n' "$pid" "$run"
    exit 0
  fi
  sleep 1
done
printf 'RIC_START_TIMEOUT run=%s\n' "$run" >&2
exit 2
