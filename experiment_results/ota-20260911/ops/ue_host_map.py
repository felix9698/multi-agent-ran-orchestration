#!/usr/bin/env python3
"""Write a live profile copy whose liveConsole.ueHosts maps amfUeNgapId -> host.

Attach order does not track the physical machines: a UE that re-attaches gets a
new amfUeNgapId, and the ids can end up in any order relative to the hosts.  The
core knows the truth, so the mapping is joined here from the AMF's UE table
(IMSI -> amfUeNgapId) and each machine's own profile (host -> IMSI).  No IMSI is
stored: each is read live, over ssh, from the machine that owns it.

usage: ue_host_map.py <profile-in> <profile-out> <host> [<host> ...]
"""
import json, re, subprocess, sys

profile_in, profile_out, hosts = sys.argv[1], sys.argv[2], sys.argv[3:]

log_result = subprocess.run(["docker", "logs", "--tail", "4000", "oai-amf"],
                            capture_output=True, text=True)
amf_log = log_result.stdout + log_result.stderr
amf_of_imsi = {}
for line in amf_log.splitlines():
    cells = [c.strip() for c in line.split("|")]
    ids = [c for c in cells if re.fullmatch(r"\d{15}", c)]
    hexes = [c for c in cells if re.fullmatch(r"0x[0-9A-Fa-f]+", c)]
    if ids and hexes:
        amf_of_imsi[ids[0]] = int(hexes[-1], 16)

mapping = {}
for host in hosts:
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
         "grep -hoE 'imsi *= *\"[0-9]+\"' ~/ai-ran-stage/runtime/phase-b/nr-ue.conf 2>/dev/null | head -1"],
        capture_output=True, text=True).stdout
    found = re.search(r"(\d{15})", out)
    if not found:
        print(f"  {host}: IMSI unreadable", file=sys.stderr); continue
    amf = amf_of_imsi.get(found.group(1))
    if amf is None:
        print(f"  {host}: not in the AMF table", file=sys.stderr); continue
    mapping[str(amf)] = host

if set(mapping.values()) != set(hosts):
    raise SystemExit("incomplete host identity mapping; profile not overwritten")

doc = json.load(open(profile_in))
doc.setdefault("liveConsole", {})["ueHosts"] = mapping
json.dump(doc, open(profile_out, "w"), indent=2)
print(json.dumps(mapping, sort_keys=True))
