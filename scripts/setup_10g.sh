#!/usr/bin/env bash
# 10GbE SFP+ setup for USRP X310 (run on PC1 and PC2 after installing SFP modules)
#
# What it does:
#  1. Kernel network buffer tuning (required for 61.44 MSps streaming)
#  2. Configures the chosen NIC with candidate host IPs for all default
#     X310 port addressings (HG: 1G=192.168.10.2 / 10G=192.168.20.2,
#     XG: 192.168.30.2 / 192.168.40.2) with MTU 9000
#  3. Detects the USRP via uhd_find_devices
#  4. Updates sdr_addrs in this host's gnb.sa.band78.fr1.*PRB.pc*.conf files
#  5. Sets CPU governor to performance
#
# Usage: sudo ./setup_10g.sh <10G-interface-name>
#        (find the interface with: ip -br link show)

set -u

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run with sudo"; exit 1
fi
if [ $# -lt 1 ]; then
    echo "Usage: sudo $0 <10G-interface-name>"
    echo "Interfaces on this host:"
    ip -br link show
    exit 1
fi

IFACE="$1"
OAI_CONF_DIR_CANDIDATES=(
    "/opt/ran-lab/controller/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF"
    "/opt/ran-lab/gnb2/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF"
)

echo "== 1. Kernel buffer tuning =="
sysctl -w net.core.rmem_max=33554432
sysctl -w net.core.wmem_max=33554432
sysctl -w net.core.rmem_default=33554432
sysctl -w net.core.wmem_default=33554432
grep -q "usrp-10g" /etc/sysctl.d/99-usrp-10g.conf 2>/dev/null || cat > /etc/sysctl.d/99-usrp-10g.conf <<'EOF'
# usrp-10g: buffers for USRP X310 10GbE streaming
net.core.rmem_max=33554432
net.core.wmem_max=33554432
net.core.rmem_default=33554432
net.core.wmem_default=33554432
EOF
echo "   persisted to /etc/sysctl.d/99-usrp-10g.conf"

echo "== 2. NIC setup ($IFACE, MTU 9000, candidate subnets) =="
# NetworkManager must not touch the USRP link. `nmcli dev set ... managed no`
# alone is NOT enough: it is runtime-only, and while NM still owns the device
# it runs DHCP on it, and when DHCP times out (there is no DHCP server on a
# USRP link) NM drives the device to "disconnected" and FLUSHES every manual
# `ip addr` we added -> the gNB then loses the radio mid-run (observed
# 2026-07-27: "dhcp4 (ens1) ... failed -> disconnected", gNB died with
# late/underflow). Mark it unmanaged persistently via a conf.d drop-in.
if command -v nmcli >/dev/null; then
    nmcli dev set "$IFACE" managed no 2>/dev/null || true
    if [ -d /etc/NetworkManager/conf.d ]; then
        cat > "/etc/NetworkManager/conf.d/99-usrp-$IFACE.conf" <<EOF
# USRP 10G link: never managed by NetworkManager (no DHCP, no IP flush).
[keyfile]
unmanaged-devices=interface-name:$IFACE
EOF
        nmcli general reload 2>/dev/null || systemctl reload NetworkManager 2>/dev/null || true
        echo "   $IFACE marked NM-unmanaged persistently (conf.d drop-in)"
    fi
fi
ip link set "$IFACE" up
ip link set "$IFACE" mtu 9000 || echo "WARNING: MTU 9000 failed - check NIC/SFP jumbo frame support"
# X310 default host subnets: SFP0 1G=192.168.10.x; SFP1 10G(HG)=192.168.40.x;
# XG images use 192.168.20.x / 192.168.30.x. Cover all.
for HOSTIP in 192.168.10.1 192.168.20.1 192.168.30.1 192.168.40.1; do
    ip addr replace "$HOSTIP/24" dev "$IFACE"
done
sleep 3

echo "== 3. USRP detection =="
FOUND_ADDR=$(uhd_find_devices 2>/dev/null | grep -oE "addr: [0-9.]+" | awk '{print $2}' | head -1)
if [ -z "$FOUND_ADDR" ]; then
    echo "ERROR: no USRP found. Check SFP module seating, cable, and that the X310 is powered."
    echo "       Diagnose with: uhd_find_devices"
    exit 1
fi
echo "   USRP found at: $FOUND_ADDR"

SUBNET=$(echo "$FOUND_ADDR" | cut -d. -f1-3)
echo "== 4. Keeping only host IP ${SUBNET}.1 on $IFACE =="
for HOSTIP in 192.168.10.1 192.168.20.1 192.168.30.1 192.168.40.1; do
    if [ "$(echo "$HOSTIP" | cut -d. -f1-3)" != "$SUBNET" ]; then
        ip addr del "$HOSTIP/24" dev "$IFACE" 2>/dev/null
    fi
done

echo "== 5. Updating sdr_addrs in gNB confs on this host =="
for DIR in "${OAI_CONF_DIR_CANDIDATES[@]}"; do
    [ -d "$DIR" ] || continue
    for CONF in "$DIR"/gnb.sa.band78.fr1.*PRB.pc*.conf; do
        [ -f "$CONF" ] || continue
        sed -i "s|sdr_addrs      = \"addr=[0-9.]*\"|sdr_addrs      = \"addr=${FOUND_ADDR}\"|" "$CONF"
        echo "   $(basename "$CONF") -> addr=${FOUND_ADDR}"
    done
done

echo "== 6. CPU governor -> performance + disable deep C-states =="
if command -v cpupower >/dev/null; then
    cpupower frequency-set -g performance >/dev/null && echo "   governor: performance"
    # Deep C-states add wake-up latency -> USRP TX 'late'/underflow. Disabling
    # them keeps the OAI real-time threads responsive (verified: eliminates
    # steady-state 'L' at 24 and 106 PRB). Runtime only; for a permanent fix add
    # 'processor.max_cstate=1 intel_idle.max_cstate=1' to the kernel cmdline.
    cpupower idle-set -D 0 >/dev/null 2>&1 && echo "   deep C-states disabled"
else
    for GOV in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo performance > "$GOV" 2>/dev/null
    done
    echo "   governor: performance (sysfs)"
fi

echo
echo "== 7. Verification =="
uhd_usrp_probe --args "addr=${FOUND_ADDR}" 2>&1 | grep -E "X3|FPGA|10 ?Gig|link" | head -5
echo
echo "DONE. USRP at ${FOUND_ADDR}, host IP ${SUBNET}.1, MTU 9000."
echo "NOTE: If uhd_usrp_probe reports an FPGA image mismatch, flash with:"
echo "  uhd_image_loader --args \"type=x300,addr=${FOUND_ADDR}\" && power-cycle the X310"
