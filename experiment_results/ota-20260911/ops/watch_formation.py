#!/usr/bin/env python3
"""Sample, every 10 s, what the formation window actually does to the UEs.

Three attempts died with ``pfWeight@<id> ATTRIBUTION_UNAVAILABLE`` after the
model calls, which means the pinned amfUeNgapId had stopped being attributed by
the time the axis was verified.  Every explanation offered so far was refuted by
a later measurement, so this stops explaining and records the two quantities
that separate the remaining candidates:

* **tun rx bytes per UE** -- if downlink is NOT flowing during formation, the
  UEs are idle, and an idle UE is released by the network (the AMF's implicit
  de-registration timer, and the gNB's own inactivity release).  That is a
  cause we can fix.  If downlink IS flowing, idleness is ruled out and the
  release has another reason.
* **the attributed identities** -- so the exact tick a UE leaves the KPM stream
  is on the record next to the byte counters, rather than inferred afterwards
  from a refusal message.

Read-only: it pings nothing, starts nothing and restarts nothing.  It must not
become a third thing operating the bed.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HOSTS = ("ue1", "ue2", "ue3")
KPM = ("/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/"
       "a1-live-kpm.jsonl")


def rx_bytes(host: str):
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host,
         "cat /sys/class/net/oaitun_ue1/statistics/rx_bytes 2>/dev/null"],
        capture_output=True, text=True, timeout=15).stdout.strip()
    try:
        return int(out)
    except ValueError:
        return None


def attributed(window_s: float = 12.0):
    cutoff = (time.time() - window_s) * 1e6
    seen: dict = {}
    try:
        lines = open(KPM, errors="replace").readlines()[-3000:]
    except OSError:
        return seen
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (row.get("recv_unix_us") or 0) < cutoff:
            continue
        for ue in (row.get("ues") or ()):
            identifier = ue.get("amf_ue_ngap_id")
            if identifier is not None:
                seen.setdefault(row.get("nb_id"), set()).add(identifier)
    return {nb: sorted(ids) for nb, ids in seen.items()}


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("formation-watch.jsonl")
    limit = float(sys.argv[2]) if len(sys.argv) > 2 else 1200.0
    previous = {host: None for host in HOSTS}
    end = time.monotonic() + limit
    with out.open("a") as handle:
        while time.monotonic() < end:
            row = {"at": time.strftime("%H:%M:%S"), "attributed": attributed()}
            for host in HOSTS:
                now = rx_bytes(host)
                was = previous[host]
                row[host] = {
                    "rx": now,
                    # Mbit/s over the sample, which is the number that decides
                    # whether this UE is idle.  ``None`` means the interface is
                    # gone, which is a different thing from zero and is kept so.
                    "mbps": (None if now is None or was is None
                             else round((now - was) * 8 / 1e6 / 10.0, 2)),
                }
                previous[host] = now
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            print(json.dumps(row), flush=True)
            time.sleep(10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
