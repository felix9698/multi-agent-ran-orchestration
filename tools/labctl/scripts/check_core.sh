#!/usr/bin/env bash
set -euo pipefail
: "${LABCTL_CORE_COMPOSE_FILE:?set LABCTL_CORE_COMPOSE_FILE to the deployed compose file}"
compose=$LABCTL_CORE_COMPOSE_FILE
test -r "$compose"
running=$(docker compose -f "$compose" ps --status running --quiet | wc -l)
test "$running" -gt 0
printf 'CORE_READY running_containers=%s\n' "$running"
