#!/usr/bin/env bash
set -euo pipefail
: "${LABCTL_RIC_ROOT:?set LABCTL_RIC_ROOT to the deployed RIC runtime}"
binary=$LABCTL_RIC_ROOT/artifacts/flexric/bin/nearRT-RIC
expected=9808ae2c21a5d0e6e99232c6b911eea8dd56d28d37f9aa671d07325e52665f4f
test "$(sha256sum "$binary" | awk '{print $1}')" = "$expected"
pid=$(pgrep -o -x nearRT-RIC)
test -n "$pid"
printf 'RIC_READY pid=%s\n' "$pid"
