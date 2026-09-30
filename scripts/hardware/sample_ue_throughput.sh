#!/usr/bin/env bash
# Sample per-UE delivered DL/UL throughput from the UE tun rx/tx byte counters.
#
# The KPM DRB.UEThpDl counter reads 0 on this deployment even while real
# downlink data is flowing (verified 2026-09-06: oaitun_ue1 rx_bytes advancing
# ~6 Mbps while DRB.UEThpDl reported 0). So the honest per-UE throughput comes
# from the UE's own tun counter differenced over time -- exactly what
# CLAUDE.md says to do. This streams one ssh loop per UE that prints
# "unix_ms rx_bytes tx_bytes" once a second, and turns the differences into a
# CSV the Agent board plotter overlays with the trials.
#
# Usage:
#   scripts/hardware/sample_ue_throughput.sh <seconds> <out.csv> <ue_host:amf_id> [<ue_host:amf_id> ...]
# e.g.
#   scripts/hardware/sample_ue_throughput.sh 120 /tmp/thp.csv ue1:28 ue2:29
#
# Output CSV columns: unix_ms,amf_ue_ngap_id,dl_mbps,ul_mbps
set -euo pipefail

SECONDS_TOTAL="${1:?seconds}"
OUT="${2:?output csv}"
shift 2
[ "$#" -ge 1 ] || { echo "name at least one <ue_host:amf_id>" >&2; exit 2; }

IFACE="${HW_UE_TUN_IFACE:-oaitun_ue1}"
RAW_DIR="$(mktemp -d)"
trap 'rm -rf "$RAW_DIR"' EXIT

pids=()
for spec in "$@"; do
  host="${spec%%:*}"; amf="${spec##*:}"
  [ -n "$host" ] && [ -n "$amf" ] || { echo "bad spec $spec (want host:amf)" >&2; exit 2; }
  # One remote loop: print the tun byte counters once a second, stamped by the
  # UE's own clock, for the whole window. The UE hosts share this session's
  # login, so no password is needed here.
  ssh -o ConnectTimeout=8 -o BatchMode=yes "$host" \
    "for i in \$(seq 1 $SECONDS_TOTAL); do \
       printf '%s %s %s\n' \"\$(date +%s%3N)\" \
         \"\$(cat /sys/class/net/$IFACE/statistics/rx_bytes 2>/dev/null || echo 0)\" \
         \"\$(cat /sys/class/net/$IFACE/statistics/tx_bytes 2>/dev/null || echo 0)\"; \
       sleep 1; \
     done" > "$RAW_DIR/$amf.raw" 2>/dev/null &
  pids+=("$!")
done
for pid in "${pids[@]}"; do wait "$pid" || true; done

python3 - "$OUT" "$RAW_DIR" "$@" <<'PY'
import sys
out, raw_dir = sys.argv[1], sys.argv[2]
specs = sys.argv[3:]
rows = []
for spec in specs:
    amf = spec.split(":", 1)[1]
    prev = None
    try:
        lines = open(f"{raw_dir}/{amf}.raw", encoding="utf-8").read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            ms, rx, tx = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if prev is not None:
            dt = (ms - prev[0]) / 1000.0
            if dt > 0:
                dl = (rx - prev[1]) * 8.0 / dt / 1e6
                ul = (tx - prev[2]) * 8.0 / dt / 1e6
                rows.append((ms, amf, max(0.0, dl), max(0.0, ul)))
        prev = (ms, rx, tx)
rows.sort()
with open(out, "w", encoding="utf-8") as handle:
    handle.write("unix_ms,amf_ue_ngap_id,dl_mbps,ul_mbps\n")
    for ms, amf, dl, ul in rows:
        handle.write(f"{ms},{amf},{dl:.4f},{ul:.4f}\n")
print(f"wrote {len(rows)} samples to {out}")
PY
