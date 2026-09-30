#!/usr/bin/env bash
# Allow PC2 (and other LAN hosts) to reach the OAI 5G core containers on PC1.
# Idempotent. Re-run after every core (re)start - docker recreates its
# nftables rules each time the demo-oai network is created.
#
# Installed to /usr/local/sbin/oai-cn-forward.sh and invoked automatically by
# the coordinator's SystemController after starting the core (NOPASSWD sudoers).
#
# Root cause handled here:
#   1. FORWARD (ip filter, policy drop) - allow LAN <-> CN via DOCKER-USER
#   2. docker 29 adds per-container drops in `table ip raw` PREROUTING
#      (priority raw, runs BEFORE filter) that block any non-bridge host from
#      reaching a container IP. We insert an accept for the LAN subnet ahead
#      of those drops.

set -u
CN_SUBNET="192.168.70.128/26"
# LAN subnets allowed to reach the core:
#   192.168.50.0/24 = direct PC1<->PC2 wired backhaul (gNB2 NGAP/GTP) - primary
#   192.168.0.0/24  = WiFi LAN (fallback / control plane)
LAN_SUBNETS="192.168.50.0/24 192.168.0.0/24"
BR="demo-oai"
TAG="oai-cn-forward"

sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1

# 1. ip filter DOCKER-USER: accept both directions (survives core restarts,
#    docker never flushes DOCKER-USER). Add only if missing.
add_docker_user() {
    local spec="$1"
    nft list chain ip filter DOCKER-USER 2>/dev/null | grep -qF "$spec" || \
        nft insert rule ip filter DOCKER-USER $spec counter accept 2>/dev/null
}
if nft list chain ip filter DOCKER-USER >/dev/null 2>&1; then
    for lan in $LAN_SUBNETS; do
        add_docker_user "ip saddr $CN_SUBNET ip daddr $lan"
        add_docker_user "ip saddr $lan ip daddr $CN_SUBNET"
    done
fi

# 2. docker 29 raw-table direct-ingress protection: remove our previous
#    tagged rule(s), then insert a fresh accept at the top of PREROUTING.
if nft list chain ip raw PREROUTING >/dev/null 2>&1; then
    for h in $(nft -a list chain ip raw PREROUTING 2>/dev/null \
               | awk -v t="$TAG" '$0 ~ t {for(i=1;i<=NF;i++) if($i=="handle") print $(i+1)}'); do
        nft delete rule ip raw PREROUTING handle "$h" 2>/dev/null
    done
    for lan in $LAN_SUBNETS; do
        nft insert rule ip raw PREROUTING iifname != "$BR" \
            ip saddr $lan ip daddr $CN_SUBNET counter accept \
            comment \"$TAG\" 2>/dev/null
    done
fi

echo "$TAG applied (LAN {$LAN_SUBNETS} <-> CN $CN_SUBNET)"
