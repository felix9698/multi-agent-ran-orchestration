#!/usr/bin/env bash
set -euo pipefail
pid1=$(pgrep -o -x nr-softmodem || true)
: "${LABCTL_GNB2_HOST:?set LABCTL_GNB2_HOST to the configured gNB2 SSH target}"
pid2=$(ssh -o BatchMode=yes "$LABCTL_GNB2_HOST" pgrep -o -x nr-softmodem || true)
[[ "$pid1" =~ ^[1-9][0-9]*$ ]]
[[ "$pid2" =~ ^[1-9][0-9]*$ ]]
# The connection witness is root-owned 0600.  Read it without host sudo (this
# session has NoNewPrivs on PC1) by going through the RIC container, which runs
# as root with the witness mounted; fall back to sudo where that is available.
witness_json() {
  sudo -n cat /run/ai-ran/flexric-connection-witness.json 2>/dev/null && return 0
  docker exec oran-aic-nearrt-ric cat /run/ai-ran/flexric-connection-witness.json 2>/dev/null && return 0
  return 1
}
ids=$(witness_json | python3 -c 'import sys,json; d=json.load(sys.stdin); print(",".join(str(x) for x in sorted(c["globalE2NodeId"]["nbId"] for c in d["connections"] if c.get("active"))))')
test "$ids" = 2816,3584
printf 'GNBS_READY gnb1_pid=%s gnb2_pid=%s e2_ids=%s\n' "$pid1" "$pid2" "$ids"
