#!/usr/bin/env bash
set -euo pipefail

: "${LABCTL_CORE_COMPOSE_FILE:?set LABCTL_CORE_COMPOSE_FILE to the deployed compose file}"
docker compose -f "$LABCTL_CORE_COMPOSE_FILE" down
