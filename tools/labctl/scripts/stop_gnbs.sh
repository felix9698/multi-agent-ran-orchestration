#!/usr/bin/env bash
set -euo pipefail
pid1=$(pgrep -o -x nr-softmodem || true)
: "${LABCTL_GNB2_HOST:?set LABCTL_GNB2_HOST to the configured gNB2 SSH target}"
pid2=$(ssh -o BatchMode=yes "$LABCTL_GNB2_HOST" pgrep -o -x nr-softmodem || true)
if [[ -n "$pid2" ]]; then ssh -o BatchMode=yes "$LABCTL_GNB2_HOST" sudo -n kill -TERM "$pid2"; fi
if [[ -n "$pid1" ]]; then sudo -n kill -TERM "$pid1"; fi
for _ in $(seq 1 30); do
  local_alive=false
  remote_alive=false
  [[ -n "$pid1" ]] && sudo -n kill -0 "$pid1" 2>/dev/null && local_alive=true
  [[ -n "$pid2" ]] && ssh -o BatchMode=yes "$LABCTL_GNB2_HOST" sudo -n kill -0 "$pid2" 2>/dev/null && remote_alive=true
  if [[ "$local_alive" = false && "$remote_alive" = false ]]; then
    printf 'GNBS_STOPPED\n'
    exit 0
  fi
  sleep 1
done
printf 'GNB_TERM_TIMEOUT manual_action_required=true\n' >&2
exit 2
