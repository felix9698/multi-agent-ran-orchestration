#!/usr/bin/env python3
"""Operator start-only lane: existing formal 38-PRB profile, no config edits."""
import datetime
import hashlib
import json
import os
import pwd
import re
import subprocess
import time
from pathlib import Path

ROOT = Path('/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN')
BASE = ROOT/'experiment_results/ota-20260911/recovery/known-good-38prb'
BIN = Path('/opt/ran-lab/controller/oai-build-campaign5/cmake_targets/ran_build_campaign5/build/nr-softmodem')
CONF = BASE/'gnb1.serving.conf'
NEIGHBOUR = BASE/'neighbour.conf'
EXPECTED = {
    BIN: 'f1f51d49cc5162a15592058b1c977e5599a90fb3ed00c133b1748e052733aa22',
    CONF: '1e719798594070a71cd0cdc531499dccb6d7f11eb83f87c46cdd2f5371f99869',
    NEIGHBOUR: 'ee92259702164c20b7a2d07a08d769b4e76b8424815bddc6aad6665a636fcb36',
}

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo python3 in a normal PC1 terminal; no privilege bypass is attempted.')
    for path, sha in EXPECTED.items():
        if digest(path) != sha:
            raise SystemExit('ABORT: inspected startup input changed: '+str(path))
    processes = subprocess.run(['ps','-C','nr-softmodem','-o','pid=,pgid=,stat='],
                               capture_output=True,text=True,check=False).stdout.strip()
    if processes:
        print('GNB_PROCESS_ALREADY_PRESENT: no process stopped or replaced\n'+processes)
        return
    # The device must not be opened while an existing gNB owns it.
    probe = subprocess.run(['timeout','40','/usr/local/bin/uhd_usrp_probe',
                            '--args','addr=192.168.40.4'],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if probe.returncode:
        raise SystemExit('ABORT: X310 probe failed; no gNB start or blind retry.')
    owner = pwd.getpwnam('ran-node1')
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    log = Path('/opt/ran-lab/controller')/('gnb1-formal38-start-'+stamp+'.log')
    fd = os.open(log, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    os.fchown(fd, owner.pw_uid, owner.pw_gid)
    env = os.environ.copy()
    env.update(OAI_RC_STYLE2_BASELINE_BOOTSTRAP='1:-:1:100:0', LD_LIBRARY_PATH=str(BIN.parent))
    p = subprocess.Popen([str(BIN),'-O',str(CONF),'--telnetsrv',
                          '--log_config.global_log_options','level,nocolor,time'],
                         cwd=BASE,env=env,stdin=subprocess.DEVNULL,
                         stdout=fd,stderr=fd,start_new_session=True)
    os.close(fd)
    markers = ['RU 0 RF started','Received NGSetupResponse from AMF',
               'E2 SETUP RESPONSE rx','Actual RX frequency: 3.400320GHz',
               'Actual TX frequency: 3.400320GHz']
    deadline = time.monotonic()+75
    ready = False
    while time.monotonic()<deadline:
        text = log.read_text(errors='replace')
        if p.poll() is not None:
            break
        if all(marker in text for marker in markers):
            ready = True
            break
        time.sleep(0.2)
    result = dict(captured_at=datetime.datetime.now().astimezone().isoformat(),
                  pid=p.pid,log=str(log),ready=ready,process_exit=p.poll(),
                  config=str(CONF),config_sha256=EXPECTED[CONF],
                  binary_sha256=EXPECTED[BIN],carrier_hz=3400320000,prb=38,
                  configuration_edited=False)
    receipt = ROOT/'experiment_results/ota-20260911'/('gnb1-startonly-'+stamp+'.json')
    out = os.open(receipt, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    os.fchown(out, owner.pw_uid, owner.pw_gid)
    os.write(out,(json.dumps(result,indent=2)+'\n').encode())
    os.close(out)
    print(json.dumps(result))
    if not ready:
        text = log.read_text(errors='replace')
        errors = [line[:400] for line in text.splitlines()
                  if re.search(r'cannot open include|No USRP|socket closed|Assertion|ERROR_CODE_',line)]
        print('\n'.join(errors[-5:]))
        raise SystemExit('GNB_NOT_READY: retained log; no automatic restart.')
    print('GNB1_FORMAL38_READY pid='+str(p.pid)+' receipt='+str(receipt))

if __name__ == '__main__':
    main()
