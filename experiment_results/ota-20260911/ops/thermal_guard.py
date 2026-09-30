#!/usr/bin/env python3
"""Thermal guard: every 30 s read the hwmon sensors of PC1, enb2 and ue1-3.

Warn level  -> PAUSE (no new board; a running board finishes).
Critical    -> PAUSE, stop the keeper (so nothing restarts), and terminate the softmodem on the
               overheated host.  Nothing resumes by itself.
An unreadable sensor decides nothing (logged).  USRP-internal temperatures are not readable while
the radios are in use, so they are not covered.
2026-09-24 owner: "각 기기 부분의 온도가 이상값보다 높으면 중단하는거도 만들어줘".
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAUSE = HERE / 'overnight' / 'PAUSE'
LOG = HERE / 'overnight' / 'thermal-guard.jsonl'
HOSTS = ('localhost', 'enb2', 'ue1', 'ue2', 'ue3')
#: hwmon name -> (warn, critical) in degrees C.  Sensors not listed (acpitz, iwlwifi) are ignored.
LIMITS = {'k10temp': (90, 95), 'nvme': (80, 85), 'ens1': (95, 105), 'amdgpu': (90, 100)}
READ = ('for z in /sys/class/hwmon/hwmon*; do n=$(cat $z/name 2>/dev/null); '
        'for t in $z/temp*_input; do [ -f $t ] && echo "$n $(cat $t)"; done; done')
SOFTMODEM = {'localhost': 'nr-softmodem', 'enb2': 'nr-softmodem'}   # UEs: nr-uesoftmodem


def read(host):
    """[(sensor, celsius)] or None when the host could not be read."""
    cmd = ['sh', '-c', READ] if host == 'localhost' else \
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', host, READ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except subprocess.TimeoutExpired:
        return None
    rows = []
    for line in out.splitlines():
        name, _, milli = line.partition(' ')
        if milli.strip().lstrip('-').isdigit():
            rows.append((name, int(milli) / 1000))
    return rows or None


def judge(rows):
    """'critical' | 'warn' | None, with the offending readings."""
    level, bad = None, []
    for name, c in rows:
        warn, crit = LIMITS.get(name, (None, None))
        if crit is not None and c >= crit:
            level = 'critical'
            bad.append((name, c))
        elif warn is not None and c >= warn:
            level = level or 'warn'
            bad.append((name, c))
    return level, bad


def log(**kw):
    with LOG.open('a') as f:
        f.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), **kw}, ensure_ascii=False) + '\n')


def stop_host(host):
    subprocess.run(['systemctl', '--user', 'stop', 'aic-keeper'], timeout=60)
    if host in SOFTMODEM:        # gNBs: gnb1 is ours, enb2 has sudo -n
        kill = f'sudo -n pkill -TERM -x {SOFTMODEM[host]} || pkill -TERM -x {SOFTMODEM[host]}'
        cmd = ['sh', '-c', kill] if host == 'localhost' else ['ssh', '-o', 'BatchMode=yes', host, kill]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).returncode
    # UEs run as root; stop them the way the keeper does (its password lane and stop code).
    sys.path.insert(0, str(HERE))
    import keeper
    source = (keeper.WINDOW / 'execute_once.py').read_text()
    index = source.index('STOP_CODE = """') + len('STOP_CODE = """')
    stop_code = source[index:source.index('"""', index)]
    return keeper.ssh(host, ['sudo', '-S', '-p', '', 'python3', '-c', stop_code],
                      timeout=90, stdin=keeper._ue_password() + '\n').returncode


def cycle():
    for host in HOSTS:
        rows = read(host)
        if rows is None:
            log(event='THERMAL_UNREADABLE', host=host)
            continue
        level, bad = judge(rows)
        if level is None:
            continue
        PAUSE.touch()
        if level == 'warn':
            log(event='THERMAL_WARN_PAUSED', host=host, readings=bad)
        else:
            rc = stop_host(host)
            log(event='THERMAL_CRITICAL_STOPPED', host=host, readings=bad, killRc=rc)


def main():
    if '--once' in sys.argv:
        for host in HOSTS:
            rows = read(host)
            print(host, judge(rows) if rows else 'unreadable', rows)
        return
    log(event='THERMAL_GUARD_START', limits=LIMITS)
    while True:
        try:
            cycle()
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not end the guard
            log(event='THERMAL_GUARD_CYCLE_ERROR', error=f'{type(exc).__name__}: {exc}'[:200])
        time.sleep(30)


if __name__ == '__main__':
    main()
