#!/bin/bash
# Restart gNB1 on a given PRB profile. Needs local sudo on PC1, so the
# operator runs it from a normal terminal:
#   cd ~/agentic_intent_coordinator && ./scripts/start_gnb1.sh 24
#
# 24 is the recommended profile: the RPi5 UEs cannot decode 51 PRB grants in
# time, which starves PUCCH, and OAI scores the resulting DTX as NACK -- that
# pins DL MCS at 0. See docs/mcs0_csi_rootcause.md.
#
# The conf is DEPLOYED from the repo on every run. SystemController only
# references the path under the OAI tree, it never copies anything there, so
# without this step edits to configs/oai/ never reach the radio.
PRB="${1:-24}"
cd "$(dirname "$0")/.." || exit 1

CONF_SRC="configs/oai/gnb.sa.band78.fr1.${PRB}PRB.pc1.conf"
CONF_DST="$HOME/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/"
[ -f "$CONF_SRC" ] || { echo "no such profile: $CONF_SRC" >&2; exit 1; }
cp -v "$CONF_SRC" "$CONF_DST" || exit 1

python3 - "$PRB" <<'PY'
import sys, time
sys.path.insert(0, '.')
from executor.system_controller import SystemController

prb = int(sys.argv[1])
sc = SystemController()
sc.set_prb_config(prb)
sc.on_log = lambda c, m: print(f"  ({c}) {m}", flush=True)
sc.stop_component("gnb1", async_stop=False)
print("waiting 15s for the USRP to release the radio...")
time.sleep(15)
print(f"gnb1 start ({prb} PRB):", sc.start_component("gnb1", async_start=False))
time.sleep(30)
PY

echo "--- verification ---"
pgrep -x nr-softmodem >/dev/null && echo "gnb1: ALIVE" || echo "gnb1: DEAD"
grep -aoE "Actual TX sample rate: [0-9.]+MSps" /tmp/gnb1.log | tail -1
grep -aoE "N_RB_DL [0-9]+" /tmp/gnb1.log | tail -1
grep -aoE "Checking for USRP with args [^ ]+" /tmp/gnb1.log | tail -1
