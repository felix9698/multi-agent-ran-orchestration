#!/usr/bin/env bash
# Deploy gNB2 artifacts to PC2 (run on PC1 when PC2 is powered on)
#
#  1. Copies the corrected pc2 gNB confs (gNB_ID 0xe01, PCI 1, PLMN 208/95,
#     NG interface = 192.168.0.51) to PC2's OAI tree
#  2. Copies the patched telnetsrv_ci.c (adds the "ci rfatt" runtime TX
#     attenuation command) and rebuilds the telnetsrv libs on PC2
#  3. Installs the route to the 5G core docker network via PC1
#  4. Preflight-checks sudoers/tmux on PC2

set -u

PC2="ran-node2@192.168.0.51"
PC1_OAI="/opt/ran-lab/controller/openairinterface5g"
PC2_OAI="/opt/ran-lab/gnb2/openairinterface5g"
CONF_REL="targets/PROJECTS/GENERIC-NR-5GC/CONF"
SSH_OPTS="-o ConnectTimeout=5 -o StrictHostKeyChecking=no"

echo "== 0. PC2 reachability =="
ssh $SSH_OPTS "$PC2" "echo PC2 OK: \$(hostname)" || { echo "ERROR: PC2 unreachable"; exit 1; }

echo "== 1. Copying pc2 gNB confs =="
for PRB in 24 51 106; do
    scp $SSH_OPTS "$PC1_OAI/$CONF_REL/gnb.sa.band78.fr1.${PRB}PRB.pc2.conf" \
        "$PC2:$PC2_OAI/$CONF_REL/" || exit 1
done
echo "   done"

echo "== 2. Deploying telnetsrv rfatt patch and rebuilding on PC2 =="
scp $SSH_OPTS "$PC1_OAI/common/utils/telnetsrv/telnetsrv_ci.c" \
    "$PC2:$PC2_OAI/common/utils/telnetsrv/telnetsrv_ci.c" || exit 1
echo "   rebuilding telnetsrv libs on PC2 (takes a few minutes)..."
ssh $SSH_OPTS "$PC2" "cd $PC2_OAI/cmake_targets && ./build_oai --build-lib telnetsrv 2>&1 | tail -3"
ssh $SSH_OPTS "$PC2" "ls $PC2_OAI/cmake_targets/ran_build/build/libtelnetsrv_ci.so" \
    && echo "   libtelnetsrv_ci.so OK" || echo "   ERROR: telnetsrv build failed on PC2"

echo "== 3. Route to 5G core (192.168.70.128/26 via PC1) =="
echo "   (may prompt for PC2 sudo password)"
ssh -t $SSH_OPTS "$PC2" "sudo ip route replace 192.168.70.128/26 via 192.168.0.50 && ip route get 192.168.70.132"
echo "   NOTE: this route is not persistent. To persist, add to PC2 netplan:"
echo "     routes: [{to: 192.168.70.128/26, via: 192.168.0.50}]"

echo "== 4. Preflight checks on PC2 =="
ssh $SSH_OPTS "$PC2" "
  command -v tmux >/dev/null && echo '  tmux: OK' || echo '  tmux: MISSING';
  sudo -n $PC2_OAI/cmake_targets/ran_build/build/nr-softmodem --help >/dev/null 2>&1 \
    && echo '  sudo NOPASSWD nr-softmodem: OK' \
    || echo '  sudo NOPASSWD nr-softmodem: NOT CONFIGURED (add to /etc/sudoers.d/)';
  sudo -n /usr/bin/pkill --help >/dev/null 2>&1 \
    && echo '  sudo NOPASSWD pkill: OK' || echo '  sudo NOPASSWD pkill: NOT CONFIGURED'
"

echo
echo "DONE. After the 10G SFP swap on PC2, also run there:"
echo "  sudo ./setup_10g.sh <PC2-10G-interface>"
