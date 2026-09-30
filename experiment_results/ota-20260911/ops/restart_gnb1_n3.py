#!/usr/bin/env python3
"""Restart gNB1 with its N3 (GTP-U) on 192.168.70.140 instead of .129.

gnb2's GTP-U reaches the UPF through PC1 (192.168.50.2 -> 192.168.50.1 ->
demo-oai), so at the UPF both gNBs appear from 192.168.70.129:2152.  Whichever
flow claims that conntrack tuple first owns the downlink: on 2026-09-15 every
gnb1 UE downlink was delivered to gnb2 and dropped there as "unknown TEID",
with both gnb1 UEs attached, in-sync and at 21 dB.  A distinct N3 address for
gnb1 removes the shared tuple; no root is needed, only gnb1's own restart.
Same binary, env and readiness gates as keeper.restart_gnb1.
"""
import os, signal, subprocess, sys, time
from pathlib import Path
EXP = Path(__file__).resolve().parent.parent
BIN = Path('/opt/ran-lab/controller/oai-build-campaign5/cmake_targets/ran_build_campaign5/build/nr-softmodem')
ROOT = EXP / 'recovery' / 'known-good-38prb'
CONF = ROOT / 'gnb1.serving.n3-140.conf'

def pids():
    found = []
    for d in Path('/proc').iterdir():
        if d.name.isdigit():
            try:
                argv = (d / 'cmdline').read_bytes().split(b'\0')
            except OSError:
                continue
            if argv and argv[0] == str(BIN).encode():
                found.append(int(d.name))
    return found

for pid in pids():
    os.kill(pid, signal.SIGTERM)
for _ in range(30):
    if not pids(): break
    time.sleep(2)
if pids():
    sys.exit(f'STOP_FAILED {pids()}')
print('gnb1 stopped', flush=True)
for _ in range(6):
    if subprocess.run(['timeout', '50', '/usr/local/bin/uhd_usrp_probe', '--args', 'addr=192.168.40.4'],
                      capture_output=True).returncode == 0:
        break
    time.sleep(5)
else:
    sys.exit('USRP_NOT_ENUMERATING')
stamp = time.strftime('%Y%m%dT%H%M%S')
out = Path(f'/opt/ran-lab/controller/gnb1-loop38-n3140-{stamp}.log')
env = dict(os.environ, OAI_RC_STYLE2_BASELINE_BOOTSTRAP='1:-:1:100:0', LD_LIBRARY_PATH=str(BIN.parent))
with out.open('wb') as handle:
    subprocess.Popen([str(BIN), '-O', str(CONF), '--telnetsrv', '--log_config.global_log_options',
                      'level,nocolor,time'], cwd=str(ROOT), env=env, stdout=handle,
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
print('gnb1 started, log', out, flush=True)
gates = ('RU 0 RF started', 'Received NGSetupResponse from AMF', 'E2 SETUP RESPONSE rx',
         'Actual TX frequency: 3.400320GHz')
for _ in range(90):
    time.sleep(2)
    text = out.read_text('utf8', 'ignore')
    missing = [g for g in gates if g not in text]
    if not missing:
        print('READY all gates', flush=True); sys.exit(0)
    if not pids():
        sys.exit('EXITED ' + text[-600:])
sys.exit(f'NOT_READY missing={missing}')
