#!/usr/bin/env python3
"""Read-only data-path watcher: every 15 s, one line per UE along the whole path.

UE tun address -> AMF UE NGAP id -> KPM cell membership (union over 10 s: gnb2 reports
one UE per indication) -> gNB MAC counters (UL/DL DTX deltas) -> DL bytes on the tun.
Lines that break the chain carry ALERT so a reader can grep for them.  Touches nothing.
2026-09-24: owner — "판 돌 때 데이터 경로 계속 확인해"; a board ran with ue3 re-registered
and ue1 losing 20-30 % of its PUSCH while nobody looked.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keeper  # noqa: E402

OUT = Path(__file__).resolve().parent / 'overnight' / 'datapath-watch.log'
PERIOD_S = 15
# The placement lives in conductor.INITIAL_CELLS; a private copy here said ue3=gnb2 after the
# 09-25 switch to placement B and flagged every sample 'wrong-cell'.  Read it as text, like
# read_placement.py does (importing conductor runs its epoch-pin helper).
import re as _re
from pathlib import Path as _Path
_NB = {'gnb1': 3584, 'gnb2': 2816}
_src = (_Path(__file__).with_name('conductor.py')).read_text(encoding='utf-8')
CELL_OF = {ue: _NB[cell] for ue, cell in _re.findall(
    r"'(ue[123])'\s*:\s*'(gnb[12])'", _re.search(r"INITIAL_CELLS\s*=\s*\{([^}]*)\}", _src).group(1))}
assert set(CELL_OF) == {'ue1', 'ue2', 'ue3'}, CELL_OF


def sh(host, cmd, timeout=10):
    try:
        return keeper.ssh(host, ['sh', '-c', cmd], timeout=timeout).stdout.strip()
    except Exception:  # noqa: BLE001 - an unreachable host is itself a finding
        return ''


def kpm_members(window_s=10):
    now = time.time()
    seen = {}
    for line in keeper.KPM_JSONL.read_bytes()[-600000:].decode('utf8', 'ignore').splitlines()[1:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get('event') == 'kpm_indication' and now - rec['recv_unix_us'] / 1e6 < window_s:
            for ue in rec.get('ues') or ():
                seen.setdefault(rec['nb_id'], set()).add(ue.get('amf_ue_ngap_id'))
    return seen


def mac_counters():
    """rnti -> dict of cumulative counters from both gNBs' MAC stats."""
    gnb2 = sh('enb2', "grep -a -E 'UE [0-9a-f]{4}: (dl|ul)sch_rounds' /tmp/nrMAC_stats.log", 12)
    log1 = subprocess.run('ls -t /opt/ran-lab/controller/gnb1-loop38-*.log | head -1', shell=True,
                          capture_output=True, text=True).stdout.strip()
    gnb1 = subprocess.run(f"tail -300 {log1} | sed 's/\\x1b\\[[0-9;]*m//g' | "
                          "grep -a -E 'UE [0-9a-f]{4}: (dl|ul)sch_rounds'", shell=True,
                          capture_output=True, text=True).stdout
    out = {}
    for line in (gnb2 + '\n' + gnb1).splitlines():
        m = re.match(r'UE ([0-9a-f]{4}): (dl|ul)sch_rounds (\d+)/', line)
        if not m:
            continue
        rnti, kind, first = m.group(1), m.group(2), int(m.group(3))
        dtx = re.search(r'(pucch0|ulsch)_DTX (\d+)', line)
        out.setdefault(rnti, {})[kind] = (first, int(dtx.group(2)) if dtx else 0)
    return out


def main():
    prev_ip, prev_mac, prev_churn = {}, {}, {}
    while True:
        at = time.strftime('%H:%M:%S')
        kpm, amf, mac = kpm_members(), keeper._amf_of_host(), mac_counters()
        lines = []
        for host in keeper.HOSTS:
            ip = sh(host, 'ip -4 -o addr show up dev oaitun_ue1 | awk "{print \\$4}"')
            rnti = sh(host, 'L=$(ls -t ~/ota-fixed38-*.log | head -1); '
                            'grep -a -o "RNTI [0-9a-f]\\{4\\} stats" $L | tail -1 | cut -d" " -f2')
            b0 = sh(host, 'cat /sys/class/net/oaitun_ue1/statistics/rx_bytes')
            time.sleep(1)
            b1 = sh(host, 'cat /sys/class/net/oaitun_ue1/statistics/rx_bytes')
            dl = round((int(b1) - int(b0)) * 8 / 1e6, 2) if b0.isdigit() and b1.isdigit() else None
            cells = [nb for nb, ids in kpm.items() if amf.get(host) in ids]
            alerts = []
            # One echo per UE per cycle (not a flood): the whole user plane, UL and DL.
            ping_ok = ip and '1 received' in sh(
                host, 'ping -I oaitun_ue1 -c 1 -W 2 192.168.70.135 2>&1 | grep received', 8)
            if ip and not ping_ok:
                alerts.append('ping-lost')
            # Reconnect churn: counters since the previous cycle in the UE's current log.
            churn = sh(host, 'L=$(ls -t ~/ota-fixed38-*.log | head -1); echo $L '
                             '$(grep -a -c "Found RAR" $L) $(grep -a -c "RRC moved into IDLE" $L) '
                             '$(grep -a -c "Registration Accept" $L)').split()
            if len(churn) == 4:
                was = prev_churn.get(host)
                if was and was[0] == churn[0]:
                    ra, idle, reg = (int(churn[i]) - int(was[i]) for i in (1, 2, 3))
                    if idle or reg or ra > 2:
                        alerts.append(f'reconnect ra+{ra} idle+{idle} reg+{reg}')
                elif was:
                    alerts.append('softmodem-restarted')
                prev_churn[host] = churn
            if not ip:
                alerts.append('no-address')
            if host in prev_ip and ip and prev_ip[host] != ip:
                alerts.append(f'address-changed {prev_ip[host]}->{ip}')
            if not cells:
                alerts.append('not-in-KPM')
            elif CELL_OF[host] not in cells:
                alerts.append(f'wrong-cell {cells}')
            if ip and rnti and rnti not in mac:
                alerts.append(f'no-gNB-context {rnti} (tun address may be stale)')
            dtx = ''
            cur, old = mac.get(rnti, {}), prev_mac.get(rnti, {})
            for kind in ('ul', 'dl'):
                if kind in cur and kind in old and cur[kind][0] > old[kind][0]:
                    rounds = cur[kind][0] - old[kind][0]
                    pct = 100 * (cur[kind][1] - old[kind][1]) // rounds
                    dtx += f' {kind}DTX={pct}%/{rounds}'
                    if kind == 'ul' and rounds > 200 and pct >= 10:
                        alerts.append(f'ul-dtx {pct}%')
            prev_ip[host] = ip
            lines.append(f'{at} {host} ip={ip} rnti={rnti} amf={amf.get(host)} kpm={cells} '
                         f'dl={dl}Mbps ping={"ok" if ping_ok else "LOST"}{dtx}' +(f' ALERT {";".join(alerts)}' if alerts else ''))
        prev_mac = mac
        with OUT.open('a') as fh:
            fh.write('\n'.join(lines) + '\n')
        time.sleep(PERIOD_S)


if __name__ == '__main__':
    main()
