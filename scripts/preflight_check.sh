#!/usr/bin/env bash
# End-to-end preflight check - run on PC1 before an experiment session.
# Verifies every dependency the GUI "Start All" flow relies on.

set -u
SSH_OPTS="-o ConnectTimeout=4 -o StrictHostKeyChecking=no -o BatchMode=yes"
PASS=0; FAIL=0
ok()   { echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "=== PC1 (local) ==="
[ -x /opt/ran-lab/controller/openairinterface5g/cmake_targets/ran_build/build/nr-softmodem ] \
    && ok "nr-softmodem build" || bad "nr-softmodem build missing"
[ -f /opt/ran-lab/controller/openairinterface5g/cmake_targets/ran_build/build/libtelnetsrv_ci.so ] \
    && ok "libtelnetsrv_ci.so (rfatt patch)" || bad "libtelnetsrv_ci.so missing - run: ./build_oai --build-lib telnetsrv"
sudo -n /opt/ran-lab/controller/openairinterface5g/cmake_targets/ran_build/build/nr-softmodem --help >/dev/null 2>&1 \
    && ok "sudo NOPASSWD nr-softmodem" || bad "sudo NOPASSWD nr-softmodem"
command -v tmux >/dev/null && ok "tmux" || bad "tmux"
command -v docker >/dev/null && ok "docker" || bad "docker"
[ "$(sysctl -n net.core.wmem_max)" -ge 33554432 ] 2>/dev/null \
    && ok "net.core.wmem_max >= 32MB" || bad "net.core.wmem_max too small - run setup_10g.sh"

echo "=== USRP (PC1) ==="
ADDR=$(grep -oE 'addr=[0-9.]+' /opt/ran-lab/controller/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.fr1.24PRB.pc1.conf | head -1 | cut -d= -f2)
if timeout 8 uhd_find_devices 2>/dev/null | grep -q "$ADDR"; then
    ok "X310 reachable at $ADDR"
else
    bad "X310 NOT reachable at $ADDR (conf sdr_addrs vs actual - run setup_10g.sh after SFP swap)"
fi

echo "=== 5G Core (PC1 docker) ==="
CORE_UP=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -cE '^oai-(amf|smf|upf)$')
if [ "$CORE_UP" -ge 3 ]; then
    ok "core containers running ($CORE_UP/3)"
    docker exec oai-ext-dn which iperf3 >/dev/null 2>&1 \
        && ok "iperf3 in oai-ext-dn" || bad "iperf3 missing in oai-ext-dn"
else
    echo "  [INFO] core not running (start via GUI) - skipping ext-dn check"
fi
sysctl -n net.ipv4.ip_forward | grep -q 1 && ok "ip_forward=1" || bad "ip_forward=0 (PC2 NG traffic needs it)"

echo "=== PC2 (gNB2) ==="
if ssh $SSH_OPTS ran-node2@192.168.0.51 true 2>/dev/null; then
    ok "SSH PC2"
    ssh $SSH_OPTS ran-node2@192.168.0.51 \
        "[ -f /opt/ran-lab/gnb2/openairinterface5g/cmake_targets/ran_build/build/libtelnetsrv_ci.so ]" \
        && ok "PC2 libtelnetsrv_ci.so" || bad "PC2 telnetsrv not built - run deploy_pc2.sh"
    ssh $SSH_OPTS ran-node2@192.168.0.51 \
        "[ -f /opt/ran-lab/gnb2/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.fr1.24PRB.pc2.conf ] && grep -q 0xe01 /opt/ran-lab/gnb2/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.fr1.24PRB.pc2.conf" \
        && ok "PC2 corrected confs deployed" || bad "PC2 confs stale - run deploy_pc2.sh"
    ssh $SSH_OPTS ran-node2@192.168.0.51 "ip route get 192.168.70.132 2>/dev/null | grep -q 192.168.0.50" \
        && ok "PC2 route to core via PC1" || bad "PC2 route missing - run deploy_pc2.sh step 3"
else
    bad "SSH PC2 (192.168.0.51) unreachable"
fi

echo "=== UEs (RPi) ==="
for i in 1 2; do
    HOST="192.168.0.5$((i+1))"; USER="lics-ue$i"
    if ssh $SSH_OPTS "$USER@$HOST" true 2>/dev/null; then
        ok "SSH $USER@$HOST"
        ssh $SSH_OPTS "$USER@$HOST" \
            "[ -x /home/$USER/openairinterface5g/cmake_targets/ran_build/build/nr-uesoftmodem ]" \
            && ok "ue$i nr-uesoftmodem build" || bad "ue$i nr-uesoftmodem missing"
        ssh $SSH_OPTS "$USER@$HOST" "command -v iperf3 >/dev/null" \
            && ok "ue$i iperf3" || bad "ue$i iperf3 missing (sudo apt install iperf3)"
        ssh $SSH_OPTS "$USER@$HOST" \
            "sudo -n /home/$USER/openairinterface5g/cmake_targets/ran_build/build/nr-uesoftmodem --help >/dev/null 2>&1" \
            && ok "ue$i sudo NOPASSWD" || bad "ue$i sudo NOPASSWD not configured"
        ssh $SSH_OPTS "$USER@$HOST" "lsusb 2>/dev/null | grep -qi 'Ettus\|B200'" \
            && ok "ue$i B206mini on USB" || bad "ue$i B206mini not detected on USB"
    else
        bad "SSH $USER@$HOST unreachable"
    fi
done

echo
echo "=== Result: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && echo "READY for experiments." || echo "Fix the FAIL items before starting."
exit "$FAIL"
