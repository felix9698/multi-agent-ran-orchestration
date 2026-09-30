#!/bin/bash
# Restart a gNB from its campaign restart script (operator sudo; RT threads).
# Usage: sudo -E bash scripts/hardware/restart_gnb.sh {gnb1|gnb2}
#
# This is a thin dispatcher onto the lab's own restart script (which kills the
# old softmodem, resets the X310, and starts the 38-PRB campaign conf with
# real-time priority). The actual script path is an env var so no absolute lab
# path is baked into the repository.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/env.sh" ] && source "$HERE/env.sh" || source "$HERE/env.sh.example"

which="${1:-}"
case "$which" in
  gnb1) restart_script="${HW_GNB1_RESTART:?set HW_GNB1_RESTART}" ;;
  gnb2) restart_script="${HW_GNB2_RESTART:?set HW_GNB2_RESTART}" ;;
  *)
    echo "usage: $0 {gnb1|gnb2}"; exit 2 ;;
esac

[ -x "$restart_script" ] || { echo "not executable: $restart_script" >&2; exit 1; }
process="${HW_GNB_SOFTMODEM_PROCESS:-nr-softmodem}"
old_pid="$(sudo -n pgrep -x "$process" | head -n 1 || true)"
if [ -n "$old_pid" ]; then
  echo "stopping existing $which $process PID $old_pid with sudo ..."
  sudo -n pkill -x "$process"
  for _ in $(seq 1 15); do
    sudo -n pgrep -x "$process" >/dev/null || break
    sleep 1
  done
  sudo -n pgrep -x "$process" >/dev/null && { echo "old $process survived sudo pkill" >&2; exit 1; }
fi

# A released X310 remains unavailable briefly.  Do not turn the first
# "socket closed" failure into an E2 association churn event.
echo "waiting 15 seconds for USRP release before starting $which ..."
sleep 15
echo "restarting $which via $restart_script ..."
bash "$restart_script"
new_pid="$(sudo -n pgrep -x "$process" | head -n 1 || true)"
[ -n "$new_pid" ] || { echo "$which did not leave a running $process" >&2; exit 1; }
[ "$new_pid" != "$old_pid" ] || { echo "$which still has the old $process PID $old_pid" >&2; exit 1; }
echo "GNB_RESTARTED name=$which old_pid=${old_pid:-none} new_pid=$new_pid"
