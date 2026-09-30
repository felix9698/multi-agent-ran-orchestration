#!/usr/bin/env python3
"""Stop every UE, let the core release every session, then start every UE.

Why not one UE at a time (force_reattach.py): on 2026-09-15 stop->start per
host, right after an SMF/UPF restart, left half-overlapping PDU sessions in the
core.  gnb1's first-generation tunnels (gNB TEIDs 0xc3fcd468, 0x67cee5e9) stayed
in the UPF with the downlink pointed at gnb2, which dropped them as "unknown
TEID", while the second generation (UPF TEIDs 0xa, 0xe) was never established
and every uplink came back as a GTP error indication.  Both gnb1 UEs had 100%
downlink loss while attached, in-sync and at 21 dB.

Stopping all first gives the AMF and SMF a moment with no registered UE, so the
next registration establishes fresh sessions instead of updating stale ones.
Reuses force_reattach.py's own stop and start code unchanged.
"""
import importlib.util, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('fr_codes', HERE / 'force_reattach.py')
source = (HERE / 'force_reattach.py').read_text()
# take only the definitions, not the module-level per-host loop
cut = source.index('\nfor spec in sys.argv[1:]')
namespace = {'__file__': str(HERE / 'force_reattach.py'), '__name__': 'fr_codes'}
exec(compile(source[:cut], 'force_reattach.py', 'exec'), namespace)
ssh, stop_code, start_code, password = (namespace[k] for k in
                                        ('ssh', 'stop_code', 'start_code', 'password'))
CELL_OF, CARRIER = namespace['CELL_OF'], namespace['CARRIER']
hosts = sys.argv[1:] or ['ue1', 'ue2', 'ue3']
settle = float(__import__('os').environ.get('AIC_CYCLE_SETTLE_S', '30'))

for host in hosts:
    r = ssh(host, ['sudo', '-S', '-p', '', 'python3', '-c', stop_code], timeout=120, stdin=password + '\n')
    print(f'  {host} STOP  rc={r.returncode}', flush=True)
print(f'all stopped; letting the core release every session for {settle:.0f}s', flush=True)
time.sleep(settle)
for host in hosts:
    cell = CELL_OF[host]
    r = ssh(host, ['sudo', '-S', '-p', '', 'env', 'AIC_UE_NO_SCAN=1', 'python3', '-c',
                   start_code, host, cell], timeout=200, stdin=password + '\n')
    print(f'  {host} START rc={r.returncode} cell={cell}', flush=True)
    time.sleep(8)   # serial attach: parallel RA on one cell is its own failure mode
