#!/usr/bin/env bash
set -euo pipefail
found=0
for process in tcpdump tshark nr-softmodem nr-uesoftmodem nearRT-RIC; do
  if pgrep -x "$process" >/dev/null; then
    printf 'STALE_OR_ACTIVE_PROCESS host=pc1 process=%s\n' "$process" >&2
    found=1
  fi
done
exit "$found"
