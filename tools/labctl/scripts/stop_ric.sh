#!/usr/bin/env bash
set -euo pipefail
pid=$(pgrep -o -x nearRT-RIC || true)
if [[ -z "$pid" ]]; then
  printf 'RIC_ALREADY_STOPPED\n'
  exit 0
fi
sudo -n kill -TERM "$pid"
for _ in $(seq 1 30); do
  if ! sudo -n kill -0 "$pid" 2>/dev/null; then
    printf 'RIC_STOPPED pid=%s\n' "$pid"
    exit 0
  fi
  sleep 1
done
printf 'RIC_TERM_TIMEOUT pid=%s manual_action_required=true\n' "$pid" >&2
exit 2
