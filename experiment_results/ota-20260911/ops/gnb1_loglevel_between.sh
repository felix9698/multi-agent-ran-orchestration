#!/bin/bash
# Restart gNB1 between attempts on its conf with PDCP/GTPU/SDAP back at info (the 09-14 serving conf had
# them at debug: 84% of gNB1 log lines, 7x gnb2's log volume, 12x its late-TX count).  Same binary, env and
# readiness gates as keeper.restart_gnb1; keeper stopped across the restart and re-pin (2026-09-15 rule).
set -u
O=$(cd "$(dirname "$0")" && pwd)
L=$O/overnight/v31-ledger.jsonl
note(){ python3 -c "import json,time,sys;open('$L','a').write(json.dumps({'at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'note':'gnb1-loglevel-restart','cause':sys.argv[1]},ensure_ascii=False)+'\n')" "$1"; }
held(){ p=$(awk '{print $1}' $O/overnight/episode-busy.lock 2>/dev/null); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
touch $O/overnight/PAUSE; echo "$(date +%T) PAUSE set; waiting for the running attempt to end"
while held; do sleep 5; done
systemctl --user stop aic-keeper; echo "$(date +%T) keeper stopped"
python3 $O/restart_gnb1_n3.py; rc=$?; echo "$(date +%T) gnb1 restart rc=$rc"
if [ $rc -eq 0 ]; then
  # repin_a1p.sh resolves deployment/... against the repo root: run it from there (22:14 rc=1 from ops/).
  RL=$O/overnight/repin-$(date +%H%M).log; (cd /opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN && bash scripts/hardware/repin_a1p.sh) > $RL 2>&1; pr=$?; echo "$(date +%T) repin rc=$pr"; tail -n 2 $RL
  for h in ue1 ue3; do AIC_REATTACH_USB_RESET=1 python3 $O/force_reattach.py $h 2>&1 | tail -3; done
else pr=skipped; fi
systemctl --user start aic-keeper; echo "$(date +%T) keeper started"
rm -f $O/overnight/PAUSE; echo "$(date +%T) PAUSE removed"
note "gNB1 restarted between attempts with pdcp/gtpu/sdap log level debug->info only (conf backup gnb1.serving.n3-140.conf.bak-debuglog-*); restart rc=$rc, repin rc=$pr, ue1/ue3 re-attached through force_reattach with USB reset"
