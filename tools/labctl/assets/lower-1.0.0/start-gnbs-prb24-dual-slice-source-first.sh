#!/usr/bin/env bash
set -euo pipefail

: "${LABCTL_GNB1_RUNTIME_ROOT:?set LABCTL_GNB1_RUNTIME_ROOT to the deployed gNB1 runtime}"
: "${LABCTL_GNB2_RUNTIME_ROOT:?set LABCTL_GNB2_RUNTIME_ROOT to the deployed gNB2 runtime}"
: "${LABCTL_GNB2_HOST:?set LABCTL_GNB2_HOST to the configured gNB2 SSH target}"
: "${LABCTL_RUN_ROOT:?set LABCTL_RUN_ROOT to a writable lab run directory}"
b1=$LABCTL_GNB1_RUNTIME_ROOT
b2=$LABCTL_GNB2_RUNTIME_ROOT
c1=$b1/runtime/oai/gnb1.dual-slice-prb24.conf
c2=$b2/runtime/oai/gnb2.dual-slice-prb24.conf
n1=$b1/runtime/oai/neighbour.conf
n2=$b2/runtime/oai/neighbour.conf
stamp=$(TZ=Asia/Seoul date +%Y%m%dT%H%M%SKST)
run1=$LABCTL_RUN_ROOT/phase-b-gnb1-dual-slice-prb24-source-$stamp
run2=$LABCTL_RUN_ROOT/phase-b-gnb2-dual-slice-prb24-target-$stamp

! pgrep -x nr-softmodem >/dev/null
! ssh "$LABCTL_GNB2_HOST" pgrep -x nr-softmodem >/dev/null
test "$(sha256sum "$c1" | awk '{print $1}')" = ee7ebb64ad3c7c080f674150bfd4ad65a5853f5823106dac6d7e93a5ef0551bc
test "$(sha256sum "$n1" | awk '{print $1}')" = ccf2b337510f8286a3d00384613074c1368b9f429ecdd814205a090e9f1aa56b
test "$(ssh "$LABCTL_GNB2_HOST" sha256sum "$c2" | awk '{print $1}')" = e6a10c9afc9e5cf435c531de0dd70eb2bc3f4b9de7aa765f8015fa0d52891491
test "$(ssh "$LABCTL_GNB2_HOST" sha256sum "$n2" | awk '{print $1}')" = ccf2b337510f8286a3d00384613074c1368b9f429ecdd814205a090e9f1aa56b

mkdir -p "$run1"
sudo -n -b env LD_LIBRARY_PATH="$b1/artifacts/oai/lib" \
  "$b1/artifacts/oai/bin/nr-softmodem" -O "$c1" \
  --log_config.global_log_options level,nocolor,time >"$run1/gnb1.log" 2>&1
sleep 15
pid1=$(pgrep -o -x nr-softmodem || true)
[[ "$pid1" =~ ^[1-9][0-9]*$ ]]

for _ in $(seq 1 90); do
  sudo -n kill -0 "$pid1" 2>/dev/null
  count=$(sudo -n jq '[.connections[] | select(.active == true and .globalE2NodeId.nbId == 3584)] | length' /run/ai-ran/flexric-connection-witness.json)
  [[ "$count" = 1 ]] && break
  sleep 1
done
test "$(sudo -n jq '[.connections[] | select(.active == true and .globalE2NodeId.nbId == 3584)] | length' /run/ai-ran/flexric-connection-witness.json)" = 1

ssh "$LABCTL_GNB2_HOST" "mkdir -p '$run2'; sudo -n -b env LD_LIBRARY_PATH='$b2/artifacts/oai/lib' \
  '$b2/artifacts/oai/bin/nr-softmodem' -O '$c2' \
  --log_config.global_log_options level,nocolor,time >'$run2/gnb2.log' 2>&1"
sleep 15
pid2=$(ssh "$LABCTL_GNB2_HOST" pgrep -o -x nr-softmodem | tail -1 || true)
[[ "$pid2" =~ ^[1-9][0-9]*$ ]]

for _ in $(seq 1 90); do
  sudo -n kill -0 "$pid1" 2>/dev/null
  ssh "$LABCTL_GNB2_HOST" sudo -n kill -0 "$pid2" 2>/dev/null
  count=$(sudo -n jq '[.connections[] | select(.active == true)] | length' /run/ai-ran/flexric-connection-witness.json)
  ids=$(sudo -n jq -r '[.connections[] | select(.active == true) | .globalE2NodeId.nbId] | sort | join(",")' /run/ai-ran/flexric-connection-witness.json)
  if [[ "$count" = 2 && "$ids" = 2816,3584 ]]; then
    printf 'GNB_DUAL_SLICE_PRB24_READY gnb1_pid=%s gnb2_pid=%s\nGNB1_RUN=%s\nGNB2_RUN=%s\n' \
      "$pid1" "$pid2" "$run1" "$run2"
    exit 0
  fi
  sleep 1
done
printf 'GNB_DUAL_SLICE_PRB24_E2_NOT_READY\n' >&2
exit 2
