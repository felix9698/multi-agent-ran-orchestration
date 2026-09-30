#!/bin/bash
# Bring up the near-RT RIC (non-root) with the E2 connection witness enabled.
# Verified procedure: RIC first, single fresh witness, so a restarted gNB's E2
# SETUP is not refused by a stale add_reg_e2_node. Run on PC1 by the operator's
# own account (no sudo needed for the RIC).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/env.sh" ] && source "$HERE/env.sh" || source "$HERE/env.sh.example"

: "${HW_RIC_BIN:?set HW_RIC_BIN}" "${HW_RIC_CONF:?set HW_RIC_CONF}"
: "${HW_RUNTIME_DIR:?set HW_RUNTIME_DIR}" "${HW_WITNESS:?set HW_WITNESS}"

for p in $(pgrep -x nearRT-RIC); do kill -9 "$p" 2>/dev/null; done
sleep 3
rm -f "$HW_WITNESS" "$HW_WITNESS.counter" "$HW_WITNESS.lock" "$HW_WITNESS.stale"
cd "$HW_RUNTIME_DIR"
FLEXRIC_E2_CONNECTION_WITNESS_PATH="$HW_WITNESS" \
  nohup "$HW_RIC_BIN" -c "$HW_RIC_CONF" -p "$HW_RUNTIME_DIR/sm/" \
  > "$HW_RUNTIME_DIR/ric.log" 2>&1 &
sleep 6
if pgrep -x nearRT-RIC >/dev/null; then
  echo "RIC_STARTED pid=$(pgrep -x nearRT-RIC | head -1)"
  tail -2 "$HW_RUNTIME_DIR/ric.log"
else
  echo "RIC_START_FAILED"; tail -8 "$HW_RUNTIME_DIR/ric.log"; exit 1
fi
