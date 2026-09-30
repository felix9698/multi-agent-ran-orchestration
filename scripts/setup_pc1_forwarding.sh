#!/usr/bin/env bash
# PC1: allow gNB2 (PC2, 192.168.0.51) to reach the 5G core docker network.
# Docker sets the FORWARD policy to DROP; NGAP/GTP-U from PC2 traverses the
# FORWARD chain into the demo-oai bridge and needs an explicit accept.
# Run with sudo AFTER the core has been started at least once (bridge exists).

set -u
if [ "$(id -u)" -ne 0 ]; then echo "ERROR: run with sudo"; exit 1; fi

sysctl -w net.ipv4.ip_forward=1

# Idempotent insert into DOCKER-USER
add_rule() {
    iptables -C DOCKER-USER "$@" 2>/dev/null || iptables -I DOCKER-USER "$@"
}
add_rule -s 192.168.0.0/24 -d 192.168.70.128/26 -j ACCEPT
add_rule -s 192.168.70.128/26 -d 192.168.0.0/24 -j ACCEPT

echo "DOCKER-USER rules:"
iptables -L DOCKER-USER -n --line-numbers | head -6
echo
echo "DONE. NOTE: iptables rules are not persistent across reboots."
echo "Re-run this script after reboot, or install iptables-persistent."
