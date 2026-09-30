#!/bin/bash
# 2026-09-25 22:2x (owner): restart both gNBs every 5 blocks, at the block boundary, to clear the
# context/uid state that re-attaches pile up (ue2 fell to 1.8 Mbps today until a power cycle).
# While the last slot of block b (with (b+1) % 5 == 0) runs, hold PAUSE; after it ends restart both
# gNBs (keeper rebuilds the chain and re-pins), wait for three UEs in KPM, release PAUSE.  The next
# block then measures its reference on the refreshed cells.  Never touches a board.
O="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; LEDGER="$O/overnight/bed-changes.log"
DONE="$O/overnight/gnb-refresh-done"
# 2026-09-30 v2: the progress file was hard-wired to blocks18-v47, so the refresh never fired after
# 09-26 18:18 (v5.x campaigns) while gnb1 degraded cell-wide twice on 09-30.  The campaign is read
# from the episodes unit every loop; the last slot is the plan row length - 1; DONE is campaign:block.
CONF="$HOME/.config/systemd/user/aic-v31-episodes.service.d/campaign.conf"
say(){ echo "$(date +%T) $*"; }
board(){ for p in $(pgrep -f "ops/run_episode\.py"); do tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q "^python3 " && return 0; done; return 1; }
held(){ q=$(awk '{print $1}' "$O/overnight/episode-busy.lock" 2>/dev/null); [ -n "$q" ] && kill -0 "$q" 2>/dev/null; }
maint(){ pgrep -f "core_echo_reset\.sh|preventive_maintenance\.sh --now" >/dev/null; }
while true; do
  C=$(sed -n 's/^Environment=AIC_CAMPAIGN=//p' "$CONF" | tail -1)
  PROG="$O/overnight/$C.progress.json"; PLAN="$O/overnight/$C.plan.json"
  read b s last < <(python3 -c "import json;x=json.load(open('$PROG'));p=json.load(open('$PLAN'))['plan'];print(x['block'],x['slot'],len(p[0])-1)" 2>/dev/null)
  if [ -n "$b" ] && [ "$s" = "$last" ] && [ $(( (b + 1) % 5 )) = 0 ] && board && ! grep -qx "$C:$b" "$DONE" 2>/dev/null; then
    touch "$O/overnight/PAUSE"; say "block $b last slot running; PAUSE set for the gNB refresh"
    while board || held; do sleep 5; done
    systemctl --user stop aic-keeper
    for p in $(pgrep -x nr-softmodem); do kill -TERM "$p"; done; ssh enb2 'sudo -n pkill -TERM -x nr-softmodem'
    for i in $(seq 1 30); do pgrep -x nr-softmodem >/dev/null || ssh enb2 pgrep -x nr-softmodem >/dev/null || break; sleep 2; done
    sleep 15; systemctl --user start aic-keeper; say "gNBs down; keeper rebuilding the chain"
    # 2026-09-26: the old check matched the current minute on watch lines written just before the
    # restart, so PAUSE was released within the same second (blocks 19, 24).  Count only lines
    # stamped after the gNBs were stopped, with an address and a KPM cell.
    since=$(date +%T)
    for i in $(seq 1 72); do [ "$(tail -3 "$O/overnight/datapath-watch.log" | awk -v t="$since" '$1>t && /kpm=\[[0-9]/ && $3!="ip="' | wc -l)" -ge 3 ] && break; sleep 10; done
    echo "$C:$b" >> "$DONE"
    echo "$(date '+%F %H:%M') 5블록 주기 gNB 재기동: $C 블록 $b 끝, 블록 $((b + 1)) 기준 측정 전 (gnb_refresh_every5_v2.sh)." >> "$LEDGER"
    while maint; do sleep 10; done
    rm -f "$O/overnight/PAUSE"; say "gNBs refreshed after block $b; PAUSE removed"
  fi
  sleep 30
done
