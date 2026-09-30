#!/bin/bash
# Clear the AMF's stale implicit-deregistration timers (116 min echo that releases live UEs) between
# attempts.  User-approved 2026-09-15 14:4x for oai-amf; SMF and UPF restart with it because an AMF-only
# restart left dangling SM contexts that exhausted the SMF IP pool (5GSM cause 0x1a, 2026-09-15 ~10:25/20:2x).
# The PAUSE switch is held only from the end of the running attempt until the core and three UEs are back.
set -u
O=$(cd "$(dirname "$0")" && pwd)
LOG=$O/overnight/run-forever.log; L=$O/overnight/v31-ledger.jsonl; F=/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/a1-live-kpm.jsonl
note(){ python3 -c "import json,time,sys;open('$L','a').write(json.dumps({'at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'note':'core-echo-reset','cause':sys.argv[1]})+'\n')" "$1"; }
held(){ p=$(awk '{print $1}' $O/overnight/episode-busy.lock 2>/dev/null); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
touch $O/overnight/PAUSE; echo "$(date +%T) PAUSE set; waiting for the running attempt to end"
while held || { sleep 20; held; }; do sleep 5; done; echo "$(date +%T) no attempt running"  # free 20 s apart: not the gap between a reference and its board
# Hold the busy lock over the container restarts (Codex 2026-09-27): run_episode and reference_dl refuse
# a live holder, so a board admitted in the gap cannot start under a core reset.  Released before the
# UE wait -- the keeper defers UE restarts while the lock is held.
echo "$$ core-reset" > $O/overnight/episode-busy.lock
release(){ [ "$(awk '{print $1}' $O/overnight/episode-busy.lock 2>/dev/null)" = "$$" ] && rm -f $O/overnight/episode-busy.lock; }
trap release EXIT
for c in oai-upf oai-smf oai-amf; do echo "$(date +%T) restart $c"; docker restart -t 20 $c >/dev/null; sleep 8; done
for i in $(seq 1 36); do s=$(docker inspect -f '{{.State.Health.Status}}' oai-amf 2>/dev/null); [ "$s" = healthy ] && break; sleep 5; done
echo "$(date +%T) amf health=$s smf=$(docker inspect -f '{{.State.Health.Status}}' oai-smf) upf=$(docker inspect -f '{{.State.Health.Status}}' oai-upf)"
for i in $(seq 1 36); do n=$(docker logs --since 5m oai-amf 2>&1 | grep -ci "NG Setup Response\|NGSetupResponse"); [ "$n" -ge 2 ] && break; sleep 5; done
echo "$(date +%T) NG setup lines=$n"
release
t0=$(date +%s); ok=0
while [ $(( $(date +%s) - t0 )) -lt 600 ]; do
  ok=$(tail -c 600000 $F | python3 -c "
import sys,json,time
now=time.time(); ues=set()
for l in sys.stdin:
    try: r=json.loads(l)
    except Exception: continue
    if r.get('recv_unix_us',0)/1e6>now-5:
        for u in r.get('ues',[]): ues.add((u.get('amf_ue_ngap_id'),r.get('nb_id')))
print(len(ues))")
  [ "$ok" -ge 3 ] && break; sleep 15
done
echo "$(date +%T) fresh UEs in KPM: $ok"
rm -f $O/overnight/PAUSE; echo "$(date +%T) PAUSE removed"
note "oai-upf, oai-smf, oai-amf restarted between attempts to clear AMF implicit-deregistration echo timers (21:29-21:47 KST 13 IMPLICIT_DEREGISTRATION releases incl. ue2 60c7 at 21:37:12 -> attempt 93 refusal); amf health=$s, NG setup lines=$n, fresh UEs in KPM=$ok; PAUSE held only for this"
