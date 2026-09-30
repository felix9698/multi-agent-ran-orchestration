#!/bin/bash
# Attach a UE at a given PRB profile, loading B2xx firmware first if needed.
# Usage: bash scripts/hardware/attach_ue.sh <ue-host> [prb]     (default prb=38)
#
# Runs over SSH to the UE host; the nrue launcher itself needs sudo there, so
# this uses `ssh -t` and the operator types the password. B2xx firmware is
# loaded with uhd_usrp_probe (no sudo) before the launcher, because a
# power-cycled B2xx reports a fake serial / 480 Mbps until firmware is loaded.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/env.sh" ] && source "$HERE/env.sh" || source "$HERE/env.sh.example"

host="${1:?usage: $0 <ue-host> [prb]}"
prb="${2:-38}"
UHD="${HW_UHD_PREFIX:-/opt/uhd-4.9.0.0}"
case "$prb" in
  24) launcher="${HW_UE_PRB24_LAUNCHER:-ai-ran-nrue-start-gnb1-prb24}" ;;
  *) launcher="${HW_UE_GENERIC_LAUNCHER:-ai-ran-nrue-start-prb} $prb" ;;
esac

echo "loading B2xx firmware on $host (no sudo) ..."
ssh -o ConnectTimeout=10 "$host" \
  "env LD_LIBRARY_PATH=$UHD/lib UHD_IMAGES_DIR=$UHD/share/uhd/images \
   timeout 30 $UHD/bin/uhd_usrp_probe 2>&1 | grep -iE 'Operating over USB|serial:' | head -2" \
  || echo "  (probe returned non-zero; continuing)"

echo "stopping any old nrue on $host (operator sudo) ..."
ssh -t "$host" 'sudo pkill -x nr-uesoftmodem || true'
echo "starting nrue on $host at ${prb} PRB with ${launcher} (operator sudo) ..."
# ssh -t keeps sudo's password prompt interactive; no password is stored or
# forwarded by this script.
ssh -t "$host" "sudo $launcher"

sleep 8
ip=$(ssh -o ConnectTimeout=6 "$host" \
  'ip -4 -o addr show up dev oaitun_ue1 2>/dev/null | awk "{print \$4}" | cut -d/ -f1')
if [ -n "$ip" ]; then
  echo "UE_ATTACHED host=$host ip=$ip"
  # prime the uplink once, or DL reads 0
  if ssh -o ConnectTimeout=6 "$host" \
    "ping -I oaitun_ue1 -c 2 -W 2 ${HW_UE_PRIME_TARGET} >/dev/null 2>&1"; then
    echo "UL_PRIME host=$host target=$HW_UE_PRIME_TARGET result=PASS"
  else
    echo "UL_PRIME host=$host target=$HW_UE_PRIME_TARGET result=FAIL" >&2
    exit 1
  fi
else
  echo "UE_NOT_ATTACHED host=$host (check SSB/SIB1 sync; warm the USRP)"
  exit 1
fi
