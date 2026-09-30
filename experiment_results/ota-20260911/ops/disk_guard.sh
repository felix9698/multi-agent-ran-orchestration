#!/bin/bash
# 2026-09-25 22:3x (owner): logs are the forensic record, so nothing is deleted.  When a disk passes
# 70% the oldest plain-text logs are gzip-compressed in place (about 10x) until it is back under.
# PC1: gNB loop logs; UEs: softmodem logs.  Docker container logs are reported, not touched.
LEDGER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/overnight/bed-changes.log"
LIMIT=70
use(){ df --output=pcent / | tail -1 | tr -dc 0-9; }
if [ "$(use)" -ge $LIMIT ]; then
  for f in $(ls -tr /opt/ran-lab/controller/gnb1-loop38-*.log 2>/dev/null | head -n -2); do
    [ "$(use)" -lt $LIMIT ] && break; gzip -9 "$f" && echo "$(date '+%F %H:%M') disk_guard: PC1 $(use)% -> gzip $f" >> "$LEDGER"
  done
  [ "$(use)" -ge $LIMIT ] && echo "$(date '+%F %H:%M') disk_guard: PC1 still $(use)% after gNB logs; docker logs (UPF) need a look" >> "$LEDGER"
fi
for h in ue1 ue2 ue3 enb2; do
  ssh -o ConnectTimeout=5 $h "u=\$(df --output=pcent / | tail -1 | tr -dc 0-9); [ \$u -ge $LIMIT ] || exit 0
    for f in \$(ls -tr ~/ota-fixed38-*.log /tmp/gnb2-probe-*.log 2>/dev/null | head -n -2); do
      [ \$(df --output=pcent / | tail -1 | tr -dc 0-9) -lt $LIMIT ] && break; gzip -9 \"\$f\" && echo \$f; done" 2>/dev/null \
    | sed "s|^|$(date '+%F %H:%M') disk_guard: $h gzip |" >> "$LEDGER"
done
