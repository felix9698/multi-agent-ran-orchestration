#!/usr/bin/env python3
"""Operator-started fixed-command helper for the four PC1 root actions this lab needs.

The operator starts this once, with sudo, in a normal PC1 terminal:

    sudo python3 experiment_results/ota-20260911/pc1_fixed_command_helper.py

It then listens on a private unix socket and runs ONLY the fixed operations in
ALLOWED. Each is a fixed argv list; no part of any command comes from the client,
no shell string is ever evaluated, and unknown names are refused. The socket is
chowned to the invoking (non-root) user and left mode 0600, so only that user can
connect. Ctrl+C stops it. Every request and result is appended to the log file.

This is operator-granted automation of actions the operator already performs by
hand. It is not a general privilege escalation: it cannot run anything else.
"""
import datetime
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time

EXP = Path(__file__).resolve().parent
GNB1_START = EXP / 'recovery/known-good-38prb/start_gnb1_when_absent.sh'
# An AF_UNIX path is capped near 108 bytes, so the socket lives in a short
# root-owned directory; the long scratchpad path is fine for a plain log file.
SOCKET = Path('/run/aic-helper/pc1.sock')
LOG = Path('/tmp/claude-1000/-home-ran-node1-agentic-ran-coordinator-based-on-ORAN/'
           '04937d58-04ff-4a1f-995c-5a0b8c910d31/scratchpad/pc1-root-helper.log')
GNB1_N3 = '192.168.70.129'


def gnb1_leader():
    """The single nr-softmodem session leader, or None."""
    rows = subprocess.run(['ps', '-C', 'nr-softmodem', '-o', 'pid=,pgid='],
                          capture_output=True, text=True).stdout.split('\n')
    leaders = [int(r.split()[0]) for r in rows
               if len(r.split()) == 2 and r.split()[0] == r.split()[1]]
    return leaders[0] if len(leaders) == 1 else None


def op_status():
    out = subprocess.run(['ps', '-C', 'nr-softmodem', '-o', 'pid=,etimes=,args='],
                         capture_output=True, text=True)
    return {'leader': gnb1_leader(), 'ps': out.stdout[:2000]}


def op_stop():
    pid = gnb1_leader()
    if pid is None:
        return {'stopped': False, 'reason': 'no single nr-softmodem session leader'}
    os.killpg(pid, signal.SIGINT)
    end = time.time() + 30
    while gnb1_leader() is not None and time.time() < end:
        time.sleep(0.5)
    return {'stopped': gnb1_leader() is None, 'signalledPgid': pid}


def op_start():
    if gnb1_leader() is not None:
        return {'started': False, 'reason': 'a nr-softmodem is already running'}
    if not GNB1_START.is_file():
        return {'started': False, 'reason': 'start script missing: ' + str(GNB1_START)}
    out = subprocess.run(['bash', str(GNB1_START)], capture_output=True, text=True, timeout=180)
    return {'started': gnb1_leader() is not None, 'exit': out.returncode,
            'stdoutTail': out.stdout[-1500:], 'stderrTail': out.stderr[-800:]}


def op_n3_capture():
    out = subprocess.run(['timeout', '12', 'tcpdump', '-ni', 'any', '-c', '40', '-q',
                          'udp port 2152 and host ' + GNB1_N3],
                         capture_output=True, text=True)
    return {'exit': out.returncode, 'lines': out.stdout.splitlines()[:40],
            'stderrTail': out.stderr[-400:]}


ALLOWED = {'gnb1-status': op_status, 'gnb1-stop': op_stop,
           'gnb1-start': op_start, 'n3-capture': op_n3_capture}


def record(entry):
    with LOG.open('a') as stream:
        stream.write(json.dumps(entry) + '\n')


def main():
    if os.geteuid() != 0 or not os.environ.get('SUDO_UID'):
        raise SystemExit('RUN_WITH_SUDO_IN_A_NORMAL_PC1_TERMINAL')
    uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
    directory = SOCKET.parent
    directory.mkdir(mode=0o755, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise SystemExit('UNSAFE_SOCKET_DIRECTORY: ' + str(directory))
    if SOCKET.exists():
        SOCKET.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET))
    os.chown(SOCKET, uid, gid)
    os.chmod(SOCKET, 0o600)
    LOG.touch()
    os.chown(LOG, uid, gid)
    server.listen(4)
    print('PC1_ROOT_HELPER_READY ' + json.dumps(
        {'socket': str(SOCKET), 'log': str(LOG), 'allowed': sorted(ALLOWED),
         'stopWith': 'Ctrl+C'}), flush=True)
    try:
        while True:
            connection, _ = server.accept()
            with connection:
                name = connection.recv(256).decode('utf-8', 'replace').strip()
                at = datetime.datetime.now().astimezone().isoformat()
                if name not in ALLOWED:
                    result = {'ok': False, 'refused': 'UNKNOWN_OPERATION'}
                else:
                    try:
                        result = {'ok': True, 'result': ALLOWED[name]()}
                    except Exception as exc:           # report, never crash the helper
                        result = {'ok': False, 'error': type(exc).__name__, 'detail': str(exc)[:300]}
                record({'at': at, 'operation': name if name in ALLOWED else '<refused>',
                        'ok': result.get('ok')})
                print(at + ' ' + (name if name in ALLOWED else '<refused>') +
                      ' ok=' + str(result.get('ok')), flush=True)
                connection.sendall(json.dumps(result).encode('utf-8'))
    except KeyboardInterrupt:
        print('PC1_ROOT_HELPER_STOPPED', flush=True)
    finally:
        server.close()
        if SOCKET.exists():
            SOCKET.unlink()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
