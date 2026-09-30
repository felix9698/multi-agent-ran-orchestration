#!/usr/bin/env python3
"""Deterministic deployment keeper: no model, no judgement, only rules.

Everything in here is something that was done by hand on 2026-09-13 while the
campaign kept falling over.  Each rule states the evidence it fires on, the
action it takes, and the budget that stops it from making things worse -- the
two faults that hurt most that day were an unrationed restart loop that drained
the DNN address pool, and a `docker rm -f` on the KPM gate that segfaulted the
near-RT RIC.

Rules, checked every CADENCE seconds:

  UE   no softmodem process, or no UE-originated downlink for FAIL_STREAK
       consecutive probes  ->  stop + start that UE on its cell
  gNB2 no softmodem process, or `got 0 from USRP` in the live log  ->  restart
       gNB2 on enb2
  gNB1 same two conditions  ->  restart gNB1 locally.  This used to be an alert
       only, on the premise that stopping gNB1 needs PC1-local sudo.  That is
       false for a gNB1 this account started -- the process is ours, SIGTERM
       works, and 38 PRB comes up without RT priority (verified 2026-09-14).
       A gNB1 the operator started under sudo still becomes an alert.
  RIC  container not running  ->  docker start
  gate either E2 node's KPM older than GATE_MAX_AGE  ->  docker stop, rm, run

Budgets are per target per hour.  When one is exhausted the keeper stops acting
on that target and writes an ALERT line naming the smallest operator action.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import re
import subprocess
import sys
import time
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _ue_password():
    """The UE sudo password, from outside the repository tree.

    ``AIC_UE_PASSWORD_FILE`` when set, else ``~/.config/aic/uepw`` (0600).  It used
    to sit beside this script in a session-scoped /tmp scratchpad, which a reboot
    deletes -- and a secret must not live in the repository either.
    """
    import os
    candidates = [os.environ.get('AIC_UE_PASSWORD_FILE'),
                  str(Path.home() / '.config' / 'aic' / 'uepw'), str(HERE / '.uepw')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).read_text().rstrip('\n')
    raise SystemExit('no UE password file: set AIC_UE_PASSWORD_FILE or create ~/.config/aic/uepw')


OUT = HERE / 'overnight'
LOG = OUT / 'keeper.jsonl'
LOCK = OUT / 'keeper.lock'
READY = OUT / 'deployment-ready.json'      # the keeper's verdict, the conductor's input
BUSY = OUT / 'episode-busy.lock'           # set by the conductor while a trial runs
EXP = Path('/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/'
           'experiment_results/ota-20260911')
#: Follows the conductor's window (2026-09-14 05:00).  The keeper reads
#: ``execute_once.py`` from here for the UE stop code, so pointing at the old
#: window would stop UEs with a different script than the one running them.
WINDOW = EXP / 'window-20260914T050000'
#: The deployment binding the launch preflight reads; resolved the same way
#: tools.liveconsole.profile.load_live_deployment resolves it, so the file the
#: keeper re-pins is the file the conductor will open.
BINDING = Path('/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/'
               'deployment/assurance-live-binding.1.0.0.json')

HOSTS = ('ue1', 'ue2', 'ue3')
EXT_DN_ADDR = '192.168.70.135'   # 러너의 게이트가 도달성을 재는 그 주소
# The lone UE on a cell receives that cell's whole capacity: at 10 Mbps per UE
# the solitary ue2 measured 9.888 Mbps against 6.9/6.7 for the pair sharing
# gnb1, and it is that 45% surplus that kills a board on the sample-overflow
# assert (nr-ue.c:982).  Counted deaths per start: ue1 273/851, ue2 157/520,
# ue3 0/489 -- so the solitary slot goes to ue3, the only board that has
# never died of it, and the two fragile ones share gnb1.  Placement is not
# fixed by the case; both cells stay occupied, which is what it requires.
# Reverted 2026-09-14 21:55.  Putting the robust board (ue3, 0 overflow deaths
# in 489 starts) alone on gnb2 was sound in principle -- the lone UE takes the
# whole cell and that surplus is what kills a board -- but the bed refuted it:
# ue3 would not attach to gnb2 at all (process alive, no tun, repeated USB
# resets and re-attaches), while it had carried gnb1 all day.  The documented
# placement is the one that actually attaches.
# 2026-09-16 13:48: ue2 and gNB2 fail only as a PAIR, so ue3 takes the solitary cell.
# Controlled, keeper stopped and PAUSE held, same board, same UE-originated ping:
#   ue2 on gNB2  20/20 lost, UL BLER 1.00000, ulsch_DTX 48502, 19,615 SRB1 RETX exhaustions
#   ue2 on gNB1  0/20 lost, ulsch_errors 0, ulsch_DTX 11, BLER 0.00133 at SNR 16.5 dB
#   ue3 on gNB2  0/20 lost  <- the cell is fine with the other board
# So neither the cell nor the board is broken; only that combination is. This is also the
# placement the overflow counts argued for (ue3 0 deaths in 489 starts takes the lone slot);
# it was reverted on 2026-09-14 only because ue3 would not attach to gNB2 then, and today it does.
# 2026-09-16 16:38, 세 번째 배치 교정 — 15:50 의 것을 되돌린다.
# 15:50 은 "gNB2 는 ue1 하고만 맞는다" 는 결론에 근거했는데 그 결론은 같은 날 16:0x 에 철회했다
# (같은 시퀀스에서 ue2 가 gNB1 에서 300 중 149 를, ue3 이 300 중 300 을 잃었다 — 판정에 쓴 핑이
# 하향 제어 고갈에 오염돼 있었다). 근거가 사라졌고 결과도 나빴다: 15:50 이후 여섯 판 연속
# (135·136·137·138·139·140) 에피소드가 하나도 시작하지 못했고, 138·139·140 은 모두
# EXC_TAGGEDECHOERROR:ue1:sample 이었다.
#
# ue1 @ gNB2 실측 (16:31): 264 초에 `Initial sync successful` 235 회 — 260 ms 마다 한 번.
#   같은 시각 ue2 @ gNB1 728 초에 6 회, ue3 @ gNB1 194 초에 1 회.
#   주파수 수렴 실패가 아니다 (보정 후 잔차 -44/-91/6/32/8/-28 Hz). 매번 재튜닝하고 되돌아온다.
# 셀별 절대 오프셋: gNB1 은 어느 보드가 붙어도 약 10 Hz, gNB2 는 어느 보드가 붙어도 1.0~1.5 kHz
#   (ue1 중앙 1021~1075 가 소프트모뎀 로그 6 개에 걸쳐 안정, ue3 의 gNB2 시절 1473/1503).
#   즉 오차는 보드가 아니라 셀에 붙어 있다. 두 gNB 다 `GPS not locked` 라 GPS 는 차이가 아니고,
#   gNB2 의 X310(192.168.30.2) 자유발진 LC_XO 가 0.3~0.45 ppm 어긋난 개체다. 원장 참조.
#
# 되돌리는 배치는 오늘 **유일하게 완주한 두 판**(131 T0_SUCCESS, 134 T0_SUCCESS)의 배치다.
# gNB2 에 아무도 없으면 게이트가 FRESH_TWO_CELL_KPM_REQUIRED 로 막히므로 한 UE 는 거기 있어야 하고,
# 완주 실적이 있는 쪽을 고른다. 이건 실험 조건이 아니다 — 인텐트·부하 L·정책·액션 후보는 그대로다.
CELL_OF = {'ue1': 'gnb2', 'ue2': 'gnb1', 'ue3': 'gnb1'}  # 2026-09-28 05:3x back from option B; placement B of 2026-09-25
# 2026-09-17 05:2x: ue1 takes the gnb2 slot.  Measured on that cell, same attenuation,
# minutes apart: ue1 reads ulsch_DTX 0, BLER 0.00000, SNR 16.4 where ue2 read DTX 12,548,
# BLER 0.037-0.242 and SNR 14.5.  ue2's uplink there was collapsing mid-episode -- eight
# UL failure timer expiries, 43% of trials losing the deadline cohort outright, and one
# episode that never left the gate -- and the deadline intent is a ROUND TRIP, so a weak
# uplink destroys it while downlink goodput still looks fine.  ue1 carries a goodput
# intent only, so the weaker cell now holds the UE whose intent is least sensitive to it.
# 2026-09-17 03:4x, measured both ways round: on gnb2 at 0 dB attenuation ue3 gets its
# preamble detected and its Msg3 through (RA 2, PUSCH with TC_RNTI 2) and then stalls at
# RRCSetupComplete 0, while ue2 needs the 10 dB to complete at all.  On gnb2 at 10 dB the
# opposite: ue2 attaches, holds in-sync at PH 10 dB and passes the gate's own 1200-byte
# probe at 0% loss, and ue3 cannot even find the SSB (`synch Failed`).  So the cell's
# attenuation and the UE that can live on it are one choice, not two, and this is the
# pair that produced a complete episode meeting every target.
# 2026-09-17 02:5x: ue2 takes the gnb2 slot, not ue3.  gnb2's downlink is hot relative to
# its uplink, and a UE derives its UL power from DL pathloss, so on gnb2 a near UE
# under-transmits and its Msg3 never lands: at att_tx 0 the cell detected 58 preambles,
# answered all 58 with Msg2 and received ONE Msg3 (`RA failed at state WAIT_Msg3`).
# att_tx 10 fixes that for ue2 -- RA 3/3, in-sync PH 10 dB (was 17), the gate's own
# 1200-byte probe at 0% loss and 12.368 Mbps downlink -- but ue3 sits far enough out that
# the same 10 dB (and 6 dB) leaves it at `synch Failed`, never finding the SSB at all.
# The two UEs want opposite things from this cell, so each goes where it works.  Both
# cells stay occupied, which is what the case requires; v4 pins servingCell anyway
# (run_case_v4.sh sets AIC_CELLS=''), so no attempt hands a UE across.
# 2026-09-17 02:0x: the "gNB2 receive chain is broken" note that stood here is WITHDRAWN.
# Measured on gnb2 with ue3 attached and a reverse iperf3 downlink: 72.6 MB in 60 s =
# 9.9 Mbps, LCID 4 TX climbing 13.2 MB -> 87.1 MB, matching this cell's historical
# single-UE figure.  Two mistakes produced the false verdict, and both are mine:
#   1. SNR compared across different grant widths.  gnb2 was read at NPRB 22-26 and gnb1
#      at NPRB 5-7; a UE spreads a fixed transmit power over its grant, so 10*log10 of
#      the ratio (6-7 dB) must come off first.  Every gnb2 reading then normalises to
#      14.0-14.4 dB -- identical before and after the antenna swap, i.e. nothing changed.
#   2. Every "downlink is 0" came from a load that never left oai-ext-dn: its iperf3
#      server still held an earlier test ("the server is busy running a test"), so
#      ext-dn eth0 tx incremented 0.00 MB.  Read the SENDER's counter before calling a
#      path dead.
# A cell-swap diagnostic overrides this without editing the default, e.g.
# AIC_KEEPER_CELLS=ue1:gnb2,ue2:gnb1,ue3:gnb1 (2026-09-15 test: does UL failure follow the cell or the UE?).
import os as _os
if _os.environ.get('AIC_KEEPER_CELLS'):
    CELL_OF = dict(pair.split(':') for pair in _os.environ['AIC_KEEPER_CELLS'].split(','))
    assert set(CELL_OF) == {'ue1', 'ue2', 'ue3'} and set(CELL_OF.values()) <= {'gnb1', 'gnb2'}, CELL_OF
CELL_NB = {'gnb1': 3584, 'gnb2': 2816}

# The shape the case requires, stated here rather than inferred from HOSTS.
# `ready` used to mean "every host I happen to watch is healthy", so narrowing
# HOSTS silently narrowed the readiness bar: on 2026-09-13 dropping one UE from
# supervision let the conductor start an episode the case could not hold, and it
# ended in CONTROLLER_REFUSED / JOINT_UP_TUN_NOT_OBSERVED after 605 s.
CASE_UES = ('ue1', 'ue2', 'ue3')
CASE_CELLS = (3584, 2816)

CADENCE = 10.0
FAIL_STREAK = 3           # consecutive bad probes before a UE is restarted
EXT_DN = '192.168.70.135' # the one DL traffic source; UEs ping it to prove the bearer
GATE_MAX_AGE = 8.0        # seconds; the conductor uses the same bound
GATE_STALE_STREAK = 2     # cycles in a row a gap must last before the gate is restarted
_gate_stale_streak = 0
BUDGET_PER_HOUR = 24        # was 6, which was aimed at the wrong cause
#: The storm this cap was added for came from judging idle UEs dead: the gNB
#: releases an unloaded UE after ~15 s, and the old probe supplied no traffic.
#: The keeper now carries load every cycle, so that cause is gone, and the
#: pathology that remains -- a restart that hands back the same address with
#: the same dead downlink -- is caught by _futile, which stops after two.  An
#: hourly cap on top of that blocks legitimate recovery: on 2026-09-14 ue2 sat
#: at 0/6 while the campaign waited on 'not attached: ue2'.

_stop = False
_events: dict[str, list[float]] = {}
_streak: dict[str, int] = {h: 0 for h in HOSTS}
_busy_streak: dict[str, int] = {h: 0 for h in HOSTS}
_last_address: dict[str, str] = {}
_futile: dict[str, int] = {}


def _sigterm(_signum, _frame):
    global _stop
    _stop = True


def log(event: str, **fields) -> None:
    record = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': event}
    record.update(fields)
    with LOG.open('a') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    print(json.dumps(record, ensure_ascii=False), flush=True)


BUDGET_FILE = OUT / 'keeper-budget.json'


def budget(target: str) -> bool:
    """True when *target* may still be acted on this hour.

    Held on disk, not in memory: restarting the keeper to pick up a config
    change used to hand it a fresh allowance, which is exactly how the
    2026-09-13 restart storm got past a 6-per-hour limit that was working.
    """
    now = time.time()
    try:
        book = json.loads(BUDGET_FILE.read_text())
    except (OSError, ValueError):
        book = {}
    seen = [t for t in book.get(target, []) if now - t < 3600.0]
    if len(seen) >= BUDGET_PER_HOUR:
        book[target] = seen
        try:
            BUDGET_FILE.write_text(json.dumps(book))
        except OSError:
            pass
        _events[target] = seen
        return False
    seen.append(now)
    book[target] = seen
    try:
        BUDGET_FILE.write_text(json.dumps(book))
    except OSError:
        pass
    _events[target] = seen
    return True


def run(args, timeout=60, stdin=None):
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, input=stdin)


def ssh(host, args, timeout=60, stdin=None):
    return run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
                host, shlex.join(args)], timeout=timeout, stdin=stdin)


# -- UE ---------------------------------------------------------------------

def ue_address(host: str) -> str:
    try:
        r = ssh(host, ['ip', '-4', '-o', 'addr', 'show', 'up', 'dev', 'oaitun_ue1'],
                timeout=20)
    except Exception:  # noqa: BLE001 - an unreachable host is simply not ready
        return ''
    parts = (r.stdout or '').split()
    return parts[3] if len(parts) > 3 else ''


def ue_alive(host: str) -> bool:
    try:
        r = ssh(host, ['ps', '-C', 'nr-uesoftmodem', '-o', 'pid='], timeout=20)
    except Exception:  # noqa: BLE001
        return False
    return bool((r.stdout or '').strip())


def downlink(host: str, address: str) -> bool:
    """Judge the downlink with UE-ORIGINATED traffic.

    This probe used to run `docker exec oai-ext-dn ping <ue>`, i.e. downlink the
    ext-DN starts on its own.  That direction does not reliably traverse this
    testbed: on 2026-09-14 a UE measured at 10.388 Mbps downlink failed it, and
    over 24 h the check passed 27 times against 2,303 failures while driving 493
    UE restarts.  A health check that is almost always wrong is worse than none,
    because each wrong answer tears down a working bearer.

    A ping the UE sends itself is solicited, so the replies are ext-DN-originated
    downlink over the same bearer -- the DL-traffic-from-ext-dn rule still holds.
    Both polarities were verified the same evening: ue2 45/45 through gnb2,
    ue1 0/45 through gnb1.  It also primes the uplink, which an idle bearer needs
    before downlink flows at all.
    """
    if not address:
        return False
    for attempt in (0, 1):
        try:
            r = ssh(host, ['timeout', '16', 'ping', '-I', 'oaitun_ue1',
                           '-c', '20', '-i', '0.3', '-s', '1200', '-W', '1',
                           '-w', '14', EXT_DN], timeout=35)
        except Exception:  # noqa: BLE001
            return False
        if r.returncode == 0:
            return True
        # A first burst can land entirely inside a re-attach.  One retry costs
        # 16 s and is far cheaper than a wrong restart.
        if attempt == 0:
            time.sleep(3)
    return False


def usb_wedged(host: str) -> bool:
    """True when the last start died on a wedged USB endpoint.

    The softmodem gets as far as 'setting rx channel 0' -- device detected,
    register loopback passed, clock locked -- and then throws
    uhd::io_error 'usb rxN transfer status: LIBUSB_TRANSFER_ERROR'.  That is the
    USB transport, not RF and not the cell, and restarting the process alone
    never clears it: ue1 died this way six times in a row on 2026-09-13.
    """
    try:
        r = ssh(host, ['sh', '-c',
                       'L=$(ls -t /home/*/ota-fixed38-%s-*.log 2>/dev/null | head -1); '
                       '[ -n "$L" ] && tail -5 "$L"' % host], timeout=25)
    except Exception:  # noqa: BLE001
        return False
    return 'LIBUSB_TRANSFER_ERROR' in (r.stdout or '')


def usb_reset(host: str) -> bool:
    """Reset the USRP's USB endpoint from userspace.

    /dev/bus/usb/<bus>/<dev> is mode 0666, so this needs no root -- which
    matters because the UE hosts have no passwordless sudo.
    """
    code = (
        'import fcntl, os, glob\n'
        'node = None\n'
        'for d in glob.glob("/sys/bus/usb/devices/*/"):\n'
        '    try:\n'
        '        if open(d + "idVendor").read().strip() != "2500":\n'
        '            continue\n'
        '        b = int(open(d + "busnum").read()); n = int(open(d + "devnum").read())\n'
        '    except OSError:\n'
        '        continue\n'
        '    node = "/dev/bus/usb/%03d/%03d" % (b, n)\n'
        'if node is None:\n'
        '    print("NO_ETTUS_DEVICE")\n'
        'else:\n'
        '    fd = os.open(node, os.O_WRONLY)\n'
        '    try:\n'
        '        fcntl.ioctl(fd, 0x5514, 0)\n'          # USBDEVFS_RESET
        '        print("RESET_OK " + node)\n'
        '    finally:\n'
        '        os.close(fd)\n'
    )
    try:
        r = ssh(host, ['python3', '-c', code], timeout=40)
    except Exception as exc:  # noqa: BLE001
        log('USB_RESET_FAILED', host=host, error=str(exc)[:120])
        return False
    ok = 'RESET_OK' in (r.stdout or '')
    log('USB_RESET', host=host, ok=ok, detail=(r.stdout or r.stderr or '').strip()[:120])
    if ok:
        time.sleep(5)
    return ok


def sweep_stale_workload() -> None:
    """Clear workload processes left behind between episodes.

    The runner refuses to start on top of a workload it did not create
    (`EXISTING_WORKLOAD_PROCESSES:<host>:preflight`), which is right -- a probe
    someone left running would otherwise be measured as the episode's own
    traffic.  But it means one manual calibration can burn every subsequent
    episode: on 2026-09-14 a hand-run echo probe on ue1 cost three in a row.

    Only safe while no episode holds the hardware, so this is called on the
    not-busy path.  Sources belonging to a live episode are never touched
    because the busy interlock is checked first.
    """
    # 2026-09-23 감사: 주 루프의 busy 는 주기 첫머리 값이다 -- 쓸기 직전에 다시 본다.
    if episode_running():
        log('SWEEP_SKIPPED_BUSY')
        return
    for host in HOSTS:
        try:
            # Count first, so the ledger says whether this ever actually fires.
            # A sweep that silently does nothing is indistinguishable from one
            # that was never reached, and that ambiguity cost an hour today.
            before = ssh(host, ['sh', '-c',
                                'pgrep -fc "(tagged_echo|flow_goodput)\\.py" || true'],
                         timeout=20)
            count = int((before.stdout or '0').strip() or 0)
            if not count:
                continue
            ssh(host, ['sh', '-c',
                       'pkill -f "/tmp/aic-flow-.*/(tagged_echo|flow_goodput)\\.py" ; '
                       'pkill -f "^python3 /tmp/(tagged_echo|flow_goodput)\\.py" ; '
                       'exit 0'], timeout=20)
            after = ssh(host, ['sh', '-c',
                               'pgrep -fc "(tagged_echo|flow_goodput)\\.py" || true'],
                        timeout=20)
            left = int((after.stdout or '0').strip() or 0)
            log('SWEPT_STALE_WORKLOAD', host=host, before=count, after=left)
        except Exception as exc:  # noqa: BLE001 - the ledger carries the reason
            log('SWEEP_FAILED', host=host, error=f'{type(exc).__name__}: {exc}'[:120])
    # 짝이 되는 송신기와 에코 서버는 `oai-ext-dn` 안에서 돌고 root 소유라 호스트에서
    # 죽일 수 없다.  2026-09-22 에 UE 쪽만 치우고 여기를 빼먹어 여섯 개가 남았다.
    try:
        run(['docker', 'exec', 'oai-ext-dn', 'sh', '-c',
             'ps -eo pid=,args= | grep -E "(flow_goodput|tagged_echo)\\.py" '
             '| grep -v grep | awk "{print \\$1}" | xargs -r kill -9; exit 0'],
            timeout=30)
    except Exception as exc:  # noqa: BLE001
        log('SWEEP_FAILED', host='oai-ext-dn', error=f'{type(exc).__name__}: {exc}'[:120])


#: 판이 제어 적용 단계에서 무한히 멎는다.  2026-09-22 08:0x 와 08:2x 두 번 모두 stdout
#: 마지막 줄이 `aiming at T.. x C..` 였고, 구동기는 소켓을 하나도 열지 않은 채 `do_select`
#: 로 대기했다 -- 네트워크가 아니라 외부 명령(ssh 등)의 파이프를 기다린 것이다.  44분과
#: 14분을 그렇게 버렸다.  디렉터리 mtime 은 다른 파일 때문에 신선해 보이므로 **stdout 의
#: 갱신 시각**이 유일한 판별자다.
#: 2026-09-22 09:28 정정: 12분은 **너무 짧았다**.  멎은 줄 알았던 시팅을 재 보니
#: CPU 72.9%, 누적 CPU 5분01초/경과 412초 -- 막힌 게 아니라 계산 중이었고, 내 감시가
#: 정상적으로 느린 판을 죽이고 있었다.  v4.2 는 목표 공간이 커져 카탈로그가 192~1152 로
#: 널뛴다.  진짜 무한 대기만 잡도록 넉넉히 둔다 -- 판 하나가 40분 침묵하면 그건 계산이
#: 아니다.  판별을 CPU 로 하지 않는 이유: 한 표본으로는 '지금 잠깐 쉬는 중'과 구분이
#: 안 되고, 회수는 파괴적이므로 보수적인 쪽이 옳다.
# 2026-09-22: 40분은 JCS 성능 결함(`oran/contract/jcs.py`, 17.9배 느렸다) 때문에 올린
# 값이었다.  그 결함을 고친 뒤로는 정상 판이 시행마다 `live-sitting.stdout` 에 쓰므로
# 40분 침묵은 고장이다 -- 그날 판 하나가 30.8분을 한 시행에서 멎은 채 아무것도 못 쓰고
# 죽었고, 그 사이 캠페인은 서 있었다.  거두기 전에 **스택을 먼저 찍는다**(아래).
STALLED_BOARD_IDLE_S = 15 * 60


#: 놀린 UE 는 15초 무부하면 RRC Release 를 받는다.  판이 도는 동안은 판이 부하를 걸지만,
#: 판과 판 사이·게이트가 UE 를 되붙이는 동안에는 아무도 부하를 걸지 않는다.  그래서
#: 하나를 살리는 사이 다른 하나가 유휴로 빠지고, 게이트가 요구하는 "셋이 동시에" 가
#: 영영 안 열린다 -- 2026-09-22 16:2x 에 매 순간 둘만 살아 있고 대상만 바뀌었다.
#: keeper 의 `idle-after-release` 증거가 이것을 그대로 적고 있었다.
#: 한 번에 몇 바이트만 보내면 된다: 목적은 타이머를 되돌리는 것뿐이다.
#: 상향이 **들리는데 복호만 안 되는** UE 컨텍스트를 해제하기까지 몇 표본을 기다릴지.
#: 2026-09-22 실측 UE b1d1: `ulsch_errors 25292/25342 · BLER 1.00000 · ulsch_DTX 140`
#: 이 791 표본 내내 지속됐다.  같은 셀의 b700 은 같은 순간 BLER 0.00000 이었으므로
#: 셀이 아니라 그 **컨텍스트**가 망가진 것이다.
UNDECODABLE_SAMPLES = 12
UNDECODABLE_BLER = 0.95
#: 같은 RNTI 를 이 시간 안에 두 번 해제하지 않는다 (재부착 여유).
UNDECODABLE_COOLDOWN_S = 120
_undecodable_last: dict = {}


def _gnb_telnet(command: str, port: int = 9091, timeout: float = 8.0) -> str:
    """패치된 softmodem 콘솔에 한 줄 보내고 받은 것을 돌려준다."""
    import socket as _s
    try:
        with _s.create_connection(('127.0.0.1', port), timeout=timeout) as c:
            c.settimeout(timeout)
            time.sleep(0.3)
            c.sendall((command + '\n').encode())
            time.sleep(1.2)
            out = b''
            try:
                while True:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    out += chunk
                    if len(out) > 65536:
                        break
            except OSError:
                pass
            return out.decode('utf-8', 'replace')
    except OSError as exc:
        return f'TELNET_ERROR:{type(exc).__name__}'


def undecodable_contexts(text: str, samples: int = UNDECODABLE_SAMPLES) -> list:
    """마지막 *samples* 표본이 전부 BLER>=임계인 RNTI.

    OAI 는 이 상태를 **스스로 끝내지 못한다**: gNB 쪽에는 RLF 콜백이 등록돼 있지 않고
    (`nr_rlc_set_rlf_handler` 는 `RRC/NR_UE/rrc_UE.c` 에서만 불린다), UL 실패 경로는
    `pusch_consecutive_dtx_cnt >= pusch_FailureThres` 즉 **DTX** 를 세는데 이 UE 는
    들리기는 하므로 DTX 가 쌓이지 않는다.  그래서 해제되지 않고 영원히 남는다.
    """
    import re as _re
    rows = _re.findall(
        r'UE ([0-9a-f]{4}): ulsch_rounds [0-9]+[^,]*, ulsch_errors ([0-9]+), '
        r'ulsch_DTX ([0-9]+), BLER ([0-9.]+)', text)
    per: dict = {}
    for rnti, errors, dtx, bler in rows:
        per.setdefault(rnti, []).append((int(errors), int(dtx), float(bler)))
    stuck = []
    for rnti, series in per.items():
        tail = series[-samples:]
        if len(tail) < samples:
            continue
        if all(b >= UNDECODABLE_BLER for _e, _d, b in tail) and tail[-1][0] > tail[0][0]:
            stuck.append((rnti, tail[-1][0], tail[-1][1]))
    return stuck


def release_undecodable_ue_contexts() -> None:
    """복호 불능으로 굳은 컨텍스트를 끊어 UE 가 다시 붙게 한다.

    2026-09-22: 이 상태의 UE 는 goodput 0 인데도 '부착됨' 으로 보여, 판이 그 UE 를
    측정 대상으로 계속 붙들고 있었다.  판당 재등록 40~155 회와 시행 24 분의 정체가
    여기서 나온다.  `ci force_ue_release <rnti>` 로 끊으면 `Remove NR rnti` 가 찍히고
    UE 가 새로 부착한다 (실측 확인).
    """
    # 2026-09-22 사고: 이 함수가 **이미 제거된 RNTI** 에 `ci force_ue_release` 를 걸어
    # gNB1 을 죽였다 (19:41:17, 로그의 마지막 줄이 그 명령).  RNTI 는 해제 뒤에도 로그
    # 꼬리에 한동안 남으므로, 꼬리에 보인다는 것만으로 살아 있다는 뜻이 아니다.
    # 기본적으로 **꺼 둔다**: 켜려면 `AIC_RELEASE_UNDECODABLE=1` 을 주고, 그때도
    # 해제 직전에 gNB 가 그 RNTI 를 **지금도 갱신하고 있는지** 확인한다.
    if os.environ.get('AIC_RELEASE_UNDECODABLE') != '1':
        return
    text = _gnb1_tail(400000)
    if not text:
        return
    now = time.time()
    for rnti, errors, dtx in undecodable_contexts(text):
        if now - _undecodable_last.get(rnti, 0) < UNDECODABLE_COOLDOWN_S:
            continue
        # 살아 있음의 증거: 짧은 꼬리(최근 표본)에도 그 RNTI 가 있어야 한다.
        if rnti not in [r for r, _e, _d in undecodable_contexts(_gnb1_tail(40000), samples=4)]:
            continue
        _undecodable_last[rnti] = now
        reply = _gnb_telnet(f'ci force_ue_release {rnti}')
        log('UE_CONTEXT_UNDECODABLE_RELEASED', rnti=rnti, ulschErrors=errors,
            ulschDtx=dtx, reply=reply.strip()[-160:])
        # 한 주기에 하나만.  그리고 gNB 가 살아남았는지 확인하고 아니면 더는 안 한다.
        time.sleep(3)
        if 'TELNET_ERROR' in _gnb_telnet('ci rfatt'):
            log('UE_RELEASE_KNOB_DISABLED', reason='gNB did not answer after release')
            os.environ['AIC_RELEASE_UNDECODABLE'] = '0'
        return


def keep_ues_warm() -> None:
    """주소를 가진 UE 에 최소 상향 트래픽을 흘려 유휴 릴리즈를 막는다.

    판이 돌고 있으면 건드리지 않는다 -- 그때는 판이 부하원이고, 여기서 끼어들면
    측정이 오염된다 ([[measure-the-bed-without-touching-it]]).
    """
    if episode_running():
        return
    for host in HOSTS:
        try:
            ssh(host, ['sh', '-c',
                       'ip -4 -o addr show up dev oaitun_ue1 >/dev/null 2>&1 || exit 0; '
                       'timeout 4 ping -I oaitun_ue1 -c 2 -i 0.3 -W 1 %s >/dev/null 2>&1; '
                       'exit 0' % EXT_DN_ADDR], timeout=20)
        except Exception as exc:  # noqa: BLE001 - 원장이 이유를 들고 있다
            log('KEEPALIVE_FAILED', host=host, error=f'{type(exc).__name__}: {exc}'[:120])


def reap_stalled_board() -> None:
    """stdout 이 오래 멎은 판의 구동기를 회수한다.  무한 대기 -> 판 하나 손실로 바꾼다.

    보수적으로 잠갔다: 구동기가 실제로 살아 있어야 하고, stdout 이
    ``STALLED_BOARD_IDLE_S`` 넘게 조용해야 한다.  정상 판은 쉬지 않고 쓰므로
    (측정·프롬프트·시행이 전부 여기로 간다) 12분 침묵은 정상 동작이 아니다.
    """
    boards = sorted(HERE.parent.glob('formal38guarded-*'),
                    key=lambda b: b.stat().st_mtime, reverse=True)
    if not boards:
        return
    board = boards[0]
    if (board / 'exit.json').exists():
        return                                   # 이미 끝난 판이다
    out = board / 'live-sitting.stdout'
    try:
        idle = time.time() - out.stat().st_mtime
    except OSError:
        return
    if idle < STALLED_BOARD_IDLE_S:
        return
    # 2026-09-22 09:0x: 이름만으로 구동기를 고르면 **갓 뜬 판의 구동기까지 죽인다**.
    # 멎은 판은 mtime 이 안 오르니 계속 "최신" 으로 잡히고, 그 사이 시작된 시도
    # 275~278 이 연달아 즉사했다.  구동기는 자기 판의 `live-sitting.stdout` 을 열고
    # 있으므로 그 fd 로 짝지어야 이 판의 것만 잡는다.
    pids = []
    target = str(out.resolve())
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        fds = entry / 'fd'
        try:
            opened = [os.path.realpath(str(fd)) for fd in fds.iterdir()]
        except OSError:
            continue
        if target in opened:
            pids.append(int(entry.name))
    if not pids:
        return
    try:
        tail = out.read_text('utf8', 'ignore')[-400:].strip().splitlines()[-1][:160]
    except (OSError, IndexError):
        tail = ''
    # 2026-09-22: 거두기 **전에** 스택을 찍는다.  판은 SIGUSR1 에 전 스레드 스택을
    # `live-sitting.stdout` 으로 내놓는다(atomic_formal_run_guarded 상단 등록).  이걸
    # 안 하면 정체의 원인이 프로세스와 함께 사라져, 그날처럼 로그로 추측만 하게 된다.
    for pid in pids:
        try:
            os.kill(pid, signal.SIGUSR1)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(3)
    try:
        dumped = out.read_text('utf8', 'ignore')[-4000:]
    except OSError:
        dumped = ''
    log('BOARD_STALLED_STACK', board=board.name,
        tail=[ln for ln in dumped.splitlines() if 'File "' in ln][-14:])
    log('BOARD_STALLED_REAPED', board=board.name, idleMin=round(idle / 60),
        pids=pids, lastLine=tail)
    for pid in pids:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    time.sleep(8)
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    try:
        BUSY.unlink()
    except OSError:
        pass
    # 표식이 없으면 이 판이 계속 "최신 미종료" 로 잡혀 다음 판을 또 죽인다.
    try:
        (board / 'exit.json').write_text(json.dumps(
            {'submissionStatus': 'REAPED_STALLED_BY_KEEPER',
             'failure': {'code': 'BOARD_STALLED', 'idleMin': round(idle / 60),
                         'lastLine': tail}}) + '\n')
    except OSError:
        pass


#: 부하 프로세스는 자기가 속한 판을 `--session-id` 로 들고 다닌다.  살아 있는 판의 id
#: 하나만 빼고 죽이면, busy 여부를 물을 필요 없이 안전하다.
_REAP = r"""
import os, signal, sys
keep = set(a for a in sys.argv[1].split(',') if a)
killed = 0
if not keep:
    # fail-closed: 지킬 판을 하나도 모르면 아무것도 죽이지 않는다.  빈 인자에 죽이던
    # 예전 판본은 **시작 중인 판**의 부하를 잡았다 (2026-09-23 00:38·00:39, 아래 참조).
    print(0)
    raise SystemExit
for entry in os.listdir('/proc'):
    if not entry.isdecimal() or int(entry) == os.getpid():
        continue
    try:
        with open('/proc/' + entry + '/cmdline', 'rb') as handle:
            argv = handle.read().decode('utf-8', 'replace').split('\0')
    except OSError:
        continue
    if not any(os.path.basename(a) in ('flow_goodput.py', 'tagged_echo.py')
               for a in argv if a):
        continue
    sid = argv[argv.index('--session-id') + 1] if '--session-id' in argv else ''
    if sid in keep:
        continue
    try:
        os.kill(int(entry), signal.SIGKILL)
        killed += 1
    except OSError:
        pass
print(killed)
"""


def live_board_id() -> str:
    """지금 돌고 있는 판의 id.  **구동기가 살아 있어야 살아 있는 판이다.**

    2026-09-22: `exit.json` 이 없다는 것만으로 판단했더니, 중단된 판(끊겨서 exit.json 을
    못 남긴 판)이 mtime 최신인 채 영원히 "살아 있는 판" 으로 잡혔다.  그 판의 고아 부하가
    보호되어 뒤따르는 시도가 전부
    `EXISTING_WORKLOAD_PROCESSES:ue1:preflight` 로 거절됐다 -- 최근 40판의 **35%** 가 이것이고
    항상 세 판씩 뭉쳐 났다(중단 -> 3판 거절 -> 손으로 치움 -> 한 판 -> 반복).

    판정은 파일이 아니라 프로세스로 한다: 구동기는 자기 판의 `live-sitting.stdout` 을 연다.
    """
    boards = sorted(HERE.parent.glob('formal38guarded-*'),
                    key=lambda d: d.stat().st_mtime, reverse=True)
    for board in boards[:3]:
        if (board / 'exit.json').exists():
            continue
        target = str((board / 'live-sitting.stdout').resolve())
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                opened = [os.path.realpath(str(fd)) for fd in (entry / 'fd').iterdir()]
            except OSError:
                continue
            if target in opened:
                return board.name
    return ''


#: 판 하나가 시작되고 첫 시행까지 걸리는 시간보다 넉넉하고, 중단된 판이 이 안에 다시
#: 뭔가를 쓸 일은 없는 폭.  판이 도는 동안은 `live-sitting.stdout` 이 계속 늘어난다.
BOARD_ALIVE_GRACE_S = 180.0


def unfinished_board_ids(depth: int = 6) -> list:
    """부하를 지켜 줄 판들 — **끝나지 않았고 아직 살아 있는** 판.

    2026-09-23 00:38·00:39, 이 회수가 UE 마다 2개·ext-dn 6개를 죽였고 7초 뒤 판이
    `SOURCE_SUPERVISOR_EXITED_OR_CHANGED:ue1:sample` 로 거절됐다.  두 번 연달아 그랬다.
    원인이 둘이었다.  첫째, :func:`live_board_id` 는 구동기가 `live-sitting.stdout` 을
    **연 뒤**에만 판을 보는데 러너는 그보다 먼저 preflight 에서 부하원을 띄운다 --
    시작 중인 판이 안 잡히는 창이 있다.  둘째, 그때 `keep` 이 빈 문자열이 되고 `_REAP` 의
    `if sid and sid == keep` 은 빈 값에 아무것도 건너뛰지 않아 **모르면 전부 죽였다**.

    그래서 `exit.json` 유무로만 갈랐더니 **반대쪽으로 넘어갔다** (01:13·01:14,
    `EXISTING_WORKLOAD_PROCESSES:ue1:preflight` 연속): 끊겨서 `exit.json` 을 못 남긴 판의
    고아 부하가 영영 보호되고, 뒤따르는 시도가 전부 거절된다.  원래 주석이 경고하던
    바로 그 고장이다.

    그래서 판정이 두 개다 -- **끝나지 않았고**(`exit.json` 없음) **아직 살아 있다**
    (그 판의 파일이 :data:`BOARD_ALIVE_GRACE_S` 안에 갱신됐다).  시작 중인 판은 방금
    디렉터리를 만들었으니 통과하고, 도는 판은 stdout 이 계속 늘어나니 통과하고,
    중단된 판은 아무것도 안 쓰므로 유예가 지나면 회수 대상이 된다.
    """
    now = time.time()
    boards = sorted(HERE.parent.glob('formal38guarded-*'),
                    key=lambda d: d.stat().st_mtime, reverse=True)
    keep = []
    for board in boards[:depth]:
        if (board / 'exit.json').exists():
            continue
        touched = board.stat().st_mtime
        stdout = board / 'live-sitting.stdout'
        if stdout.exists():
            touched = max(touched, stdout.stat().st_mtime)
        if now - touched <= BOARD_ALIVE_GRACE_S:
            keep.append(board.name)
    return keep


def reap_finished_board_workload() -> None:
    """끝난 판이 남긴 부하만 치운다.

    2026-09-22 04:03-04:15, 구동기가 사라진 판이 ue1 에 부하 둘을 남겼고 그 뒤 열두 판이
    연달아 `EXISTING_WORKLOAD_PROCESSES:ue1:preflight` 로 거절됐다.  판이 1분마다 뜨고
    지는 동안 busy 락이 거의 늘 잡혀 있어 `not busy` 아래의 `sweep_stale_workload()` 는
    영영 오지 않았다 -- 실패한 판들이 스스로를 못 고치게 막고 있었다.

    거기서 "거절이 났으면 busy 라도 치우자" 로 갔다가 **도는 판의 부하를 죽였다**
    (04:13:38, ue1 before=6).  거절 기록은 어느 판의 것인지 말해 주지 않기 때문이다.
    부하 자신은 말해 준다 -- `--session-id` 를 들고 다닌다.  그래서 살아 있는 판의 id 만
    빼고 죽인다.  busy 여부를 묻지 않아도 안전하고, 교착도 생기지 않는다.

    짝이 되는 송신기·에코 서버는 `oai-ext-dn` 안에서 root 로 돌아 호스트에서 못 죽이므로
    컨테이너 안에서도 같은 판정을 돌린다.
    """
    keep = ','.join(unfinished_board_ids())
    if not keep:
        log('REAP_DEFERRED_NO_UNFINISHED_BOARD')
        return
    for host in HOSTS:
        try:
            r = ssh(host, ['python3', '-c', _REAP, keep], timeout=30)
            n = int((r.stdout or '0').strip().splitlines()[-1] or 0)
        except Exception as exc:  # noqa: BLE001 - 원장이 이유를 들고 있다
            log('REAP_FAILED', host=host, error=f'{type(exc).__name__}: {exc}'[:120])
            continue
        if n:
            log('REAPED_FINISHED_WORKLOAD', host=host, killed=n, kept=keep or None)
    try:
        r = run(['docker', 'exec', 'oai-ext-dn', 'python3', '-c', _REAP, keep], timeout=30)
        n = int((r.stdout or '0').strip().splitlines()[-1] or 0)
    except Exception as exc:  # noqa: BLE001
        log('REAP_FAILED', host='oai-ext-dn', error=f'{type(exc).__name__}: {exc}'[:120])
        return
    if n:
        log('REAPED_FINISHED_WORKLOAD', host='oai-ext-dn', killed=n, kept=keep or None)


HOLD = Path(__file__).resolve().parent / 'overnight' / 'NO_UE_AUTO_RESTART'


def ue_auto_restart_held() -> bool:
    """Operator switch (2026-09-15 20:1x): automatic UE restarts were feeding the
    drop churn -- restarted attached UEs leave stale AMF contexts that release a
    live UE ~116 min later, and a restart cannot fix a core-side or radio cause.
    While overnight/NO_UE_AUTO_RESTART exists, nothing restarts a UE on its own;
    drops are analysed by cause first.

    Exception (2026-09-15 20:5x): a UE whose cell already released its current RNTI but which
    missed the RRC Release (stale tun, no data, never heals) is restarted -- evidence-based,
    see ue_zombie.py."""
    return HOLD.exists()


def zombie_release_evidence(host: str):
    """The released RNTI when the gNB released this UE's current context, else None."""
    try:
        import keeper as _k
        import ue_zombie
        return ue_zombie.zombie(host, _k.CELL_OF[host], _k.ssh, _k.gnb1_log)
    except Exception:  # noqa: BLE001 - no evidence means no restart
        return None


def rnti_held_by_no_gnb(host: str) -> bool:
    """True only when the UE's current RNTI is read and neither gNB's MAC stats list it.

    Any read that fails answers False (keep the handover grace) -- a missing file must
    not take a UE away mid-board.
    """
    try:
        # 2026-09-24 22:08: the stats line lags a handover -- ue2 was restarted mid-trial
        # because its last stats line still named the source cell's RNTI.  A
        # reconfigurationWithSync after the last stats line means the RNTI is stale: no verdict.
        last = (ssh(host, ['sh', '-c', 'L=$(ls -t ~/ota-fixed38-*.log | head -1); '
                           'grep -a -o -E "RNTI [0-9a-f]{4} stats|Processing reconfigurationWithSync" '
                           '"$L" | tail -1'],
                    timeout=10).stdout or '').split()
        if len(last) < 3 or last[0] != 'RNTI':
            return False
        rnti = last[1]
        gnb2 = ssh('enb2', ['sh', '-c', 'cat /tmp/nrMAC_stats.log'], timeout=12)
        log1 = gnb1_log()
        if gnb2.returncode != 0 or not gnb2.stdout or log1 is None:
            return False
        # 2026-09-24 22:33: 400 KB of gnb1 log is hundreds of 1.28 s stats blocks, so a
        # retired RNTI still read as "held" (ue3).  Judge on the last few blocks only.
        blocks = (run(['tail', '-c', '400000', str(log1)], timeout=10).stdout or '').split('Frame.Slot')
        if len(blocks) < 5:
            return False
        tail1 = 'Frame.Slot'.join(blocks[-4:])
        return f'UE {rnti}:' not in gnb2.stdout and f'UE {rnti}:' not in tail1
    except Exception:  # noqa: BLE001 - unknown means keep the grace
        return False


def episode_in_sitting() -> bool:
    """True once the runner has left its readiness gate and is inside the sitting.

    The gate repairs UEs itself (run_episode.restart_ue) while it runs, so recovering
    one here as well would stop and start the same board twice; the lock says which
    phase holds it.
    """
    try:
        return BUSY.read_text().split()[1] == 'cli'
    except (OSError, IndexError):
        return False


#: How long a frozen bearer may be treated as a handover in flight.  A steering
#: trial writes, holds and rolls back inside ~40 s, so anything past this is the
#: UE, not the move (2026-09-21).
BEARER_FROZEN_GRACE_S = 90.0

#: When each host's current freeze was first seen under the busy lock.
_frozen_since: dict = {}


def recover_during_episode(host: str, healthy: bool) -> bool:
    """While an episode holds the bed, restart only a UE that is provably dead.

    2026-09-16 (docs/design/ue-identity-continuity.md): the sitting now waits for a
    dropped UE and rebinds to it, so a UE stuck where it never heals by itself -- its
    cell released the RNTI it still holds, idle after a release, or no softmodem at
    all -- must be brought back during the episode, not after it.  Nothing else is
    done under the busy lock: no streak-only restart, no sweep, no binding change.
    Returns True when a restart was issued.
    """
    if healthy or not episode_in_sitting():
        _busy_streak[host] = 0
        # 2026-09-23 감사: 건강하거나 판 밖이면 동결 첫 목격도 잊는다.  남겨 두면 다음 판의
        # 핸드오버 첫 목격이 곧바로 TOO_LONG(heldS 6196 등)이 되어 멀쩡한 UE 를 재시작했다.
        _frozen_since.pop(host, None)
        return False
    _busy_streak[host] += 1
    # A UE already under the handover grace is re-judged every cycle.  Waiting another
    # FAIL_STREAK cycles (3 x 60-80 s) put the restart past the board's 300 s wait cap:
    # board 474 (2026-09-24) ended HARDWARE_UNAVAILABLE at 02:49:40 while ue1, dropped at
    # 02:44:36 and first seen at 02:46:36, was still inside the keeper's recheck gap.
    if _busy_streak[host] < FAIL_STREAK and host not in _frozen_since:
        return False
    _busy_streak[host] = 0
    evidence = zombie_release_evidence(host)
    if evidence is None:
        _frozen_since.pop(host, None)
        return False
    # 2026-09-20: a handover *is* a frozen bearer for a few seconds -- the UE has
    # moved cell and the core has not switched the downlink path yet.  Five boards
    # in a row were lost to this: the steering trial handed ue1 to the other cell,
    # this gate read `bearer-frozen`, restarted a healthy UE mid-trial, and the
    # rollback's readback then found nothing and locked the trial down.  Under the
    # busy lock the Kernel owns the UE: only evidence that cannot be a handover
    # justifies taking it away (an absent softmodem, an idle-after-release, an RA
    # loop, a released RNTI).  A bearer that is still frozen after the board is
    # repaired between boards, where no verdict can be polluted.
    # 2026-09-24 21:14-21:20: ue1's context was gone from gnb2 yet this path waited the
    # handover grace and the board ran 5.5 min without it.  A handover leaves the UE's
    # current RNTI on the other cell; an RNTI neither gNB holds cannot be a move.
    if evidence == 'bearer-frozen' and rnti_held_by_no_gnb(host):
        log('UE_CONTEXT_GONE_DURING_EPISODE', host=host,
            note='no gNB holds the RNTI the UE is using: not a handover, no grace')
        evidence = 'context-gone'
    if evidence == 'bearer-frozen':
        # A handover's freeze clears within seconds -- the trial's own rollback
        # restores the UE.  A freeze that outlives that window is the real thing,
        # and leaving it alone costs the whole board: 2026-09-21 00:24-00:50 ue1
        # stayed frozen for 25 minutes and three boards died on LEASE_EXPIRED
        # while composition waited for a UE nobody was allowed to repair.
        first = _frozen_since.setdefault(host, time.monotonic())
        held_s = time.monotonic() - first
        if held_s < BEARER_FROZEN_GRACE_S:
            log('UE_BEARER_FROZEN_DURING_EPISODE', host=host, heldS=round(held_s),
                note='not restarting yet: a handover looks exactly like this')
            return False
        _frozen_since.pop(host, None)
        log('UE_BEARER_FROZEN_TOO_LONG', host=host, heldS=round(held_s),
            note='past the handover window; this is a frozen bearer, not a move')
    else:
        _frozen_since.pop(host, None)
    log('UE_ZOMBIE_RESTART_DURING_EPISODE', host=host, evidence=evidence)
    restart_ue(host, evidence=evidence)
    return True


# 2026-09-23 감사: force_reattach.py 와 **한 벌**이다 (그쪽이 여기서 가져간다).  keeper 의
# restart_ue 에는 authorized 토글도 반송파 되읽기도 없어, 같은 UE 를 두 경로가 다르게 고쳤다.
#
# One rung above USBDEVFS_RESET: writing 0 then 1 to the device's ``authorized``
# makes the kernel drop and re-enumerate it, which recovers a B200 whose
# descriptors survive a reset but whose radio does not.  2026-09-19 01:08-01:38:
# the gate reset ue1's device fifteen times and the UE still could not acquire
# SSB, and at 01:42 the authorized toggle attached it at once
# ([[usb-authorized-toggle-unwedges-a-usrp]]).
AUTHORIZED_CODE = """
import os, time
d = None
for name in sorted(os.listdir('/sys/bus/usb/devices')):
    path = '/sys/bus/usb/devices/' + name + '/'
    try:
        if open(path + 'idVendor').read().strip() == '2500':
            d = path
            break
    except OSError:
        continue
if d is None:
    print('NO_ETTUS_DEVICE')
else:
    open(d + 'authorized', 'w').write('0')
    time.sleep(3)
    open(d + 'authorized', 'w').write('1')
    time.sleep(5)
    print('AUTHORIZED_TOGGLED ' + d + ' now=' + open(d + 'authorized').read().strip())
"""

# 2026-09-22 15:0x: gnb1 을 3400.32 -> 3349.92 MHz 로 옮겼다(협대역 간섭).  UE 기동 인자
# `-C` 가 이 값이어야 그 셀에 붙는다 -- 되읽어 확인한다.
CARRIER = {'gnb1': 3349920000, 'gnb2': 3319680000}

#: 실행 중인 UE softmodem 이 실제로 받은 `-C` 값을 되읽는 명령.
CARRIER_READBACK = ('for p in $(ps -C nr-uesoftmodem -o pid= --no-headers); do '
                    'tr "\\0" "\\n" < /proc/$p/cmdline | grep -A1 -x -- "-C" | tail -1; '
                    'break; done')


def restart_ue(host: str, evidence: str | None = None) -> None:
    # 2026-09-23 감사: busy 는 주기 첫머리에 한 번 읽혀 60~75 초 재사용됐다.  그 사이 러너가
    # 락을 잡았으면, 증거 없는 재시작(주 루프의 streak·중복 주소 경로)은 여기서 멈춘다.
    # 증거 있는 재시작은 recover_during_episode 가 sitting 단계에서만 부른다.
    if episode_running() and (evidence is None or not episode_in_sitting()):
        log('UE_RESTART_ABORTED_BUSY', host=host, evidence=evidence,
            note='an episode took the lock after this cycle read it')
        return
    if ue_auto_restart_held():
        rnti = evidence if evidence is not None else zombie_release_evidence(host)
        if rnti is None:
            log('UE_RESTART_HELD', host=host)
            return
        log('UE_ZOMBIE_RESTART', host=host, releasedRnti=rnti)
        # The evidence we just FOUND has to reach the USB branch below, which
        # promises "an evidence restart always resets".  It never did: only a
        # caller that passed `evidence=` reached it, so the streak path -- the
        # one that restarts a UE nobody is watching -- reset the process and
        # left the endpoint alone.  2026-09-18 is the whole story in the log:
        # 18:26 and 18:40 show UE_ZOMBIE_RESTART{session-released} on ue3 with
        # no USB_RESET and no recovery, the futile guard then wrote ue3 off at
        # 18:49 ("restarting it changes nothing"), and four further streaks
        # were skipped in silence -- until 19:03, when the runner's own gate
        # did STOP -> usb RESET_OK -> START and ue3 came back on the *same*
        # address 12.1.1.194.  The guard's premise was false because the thing
        # it called futile had never actually been tried.
        evidence = rnti
    if not budget('ue:' + host):
        log('ALERT_BUDGET_EXHAUSTED', target=host,
            action=f'{host} has been restarted {BUDGET_PER_HOUR} times this hour and is '
                   f'still not carrying a downlink; check its antenna and USB cable, '
                   f'then delete {LOCK.name} to resume automatic recovery')
        return
    password = _ue_password()
    source = (WINDOW / 'execute_once.py').read_text()
    index = source.index('STOP_CODE = """') + len('STOP_CODE = """')
    stop_code = source[index:source.index('"""', index)]
    start_code = (EXP / 'start_fixed38_ue_when_stopped.py').read_text()
    r = ssh(host, ['sudo', '-S', '-p', '', 'python3', '-c', stop_code],
            timeout=90, stdin=password + '\n')
    log('UE_STOP', host=host, rc=r.returncode)
    # 2026-09-23 감사: rc 를 버렸다.  멈추지 못한 UE 위에 새 softmodem 을 올리면 장치가 둘에게
    # 잡힌다 -- 멈춤이 실패하면 여기서 끝낸다(force_reattach 와 같은 판정).
    if r.returncode != 0:
        log('UE_RESTART_FAILED', host=host, stage='stop', rc=r.returncode,
            err=(r.stderr or '').strip()[:160])
        return
    time.sleep(4)
    # A wedged USB endpoint survives the process restart, so clear it while the
    # softmodem is down -- otherwise every further start burns budget for
    # nothing.
    # An evidence restart always resets: re-attaching without it lost PUCCH twice on
    # 2026-09-15 (force_reattach sets AIC_REATTACH_USB_RESET=1 for the same reason).
    if evidence is not None or usb_wedged(host):
        usb_reset(host)
    # 2026-09-23 감사: run_episode.restart_ue 처럼 이 시간 두 번째 재시작이면 authorized
    # 토글까지 올린다 (09-19: 리셋 15번에도 못 붙던 ue1 이 토글 한 번에 붙었다).
    if len(_events.get('ue:' + host, [])) >= 2:
        t = ssh(host, ['sudo', '-S', '-p', '', 'python3', '-c', AUTHORIZED_CODE],
                timeout=120, stdin=password + '\n')
        log('UE_USB_AUTHORIZED_TOGGLE', host=host, rc=t.returncode,
            out=(t.stdout or t.stderr or '').strip()[:140])
        time.sleep(8)
    # Without AIC_UE_NO_SCAN the start script passes --ue-scan-carrier and the
    # UE picks its own cell, so CELL_OF becomes an intention rather than a
    # fact: on 2026-09-13 ue2 was started for gnb1 and joined gnb2 anyway,
    # leaving gnb1 with no UE at all.
    cell = CELL_OF[host]
    r = ssh(host, ['sudo', '-S', '-p', '', 'env', 'AIC_UE_NO_SCAN=1',
                   'python3', '-c', start_code,
                   host, cell], timeout=170, stdin=password + '\n')
    log('UE_START', host=host, cell=cell, rc=r.returncode,
        err=(r.stderr or '').strip()[:160])
    if r.returncode != 0:
        log('UE_RESTART_FAILED', host=host, stage='start', rc=r.returncode)
        return
    # 2026-09-23 감사: force_reattach 처럼 프로세스가 실제로 받은 반송파를 되읽는다.
    # 요청한 셀만 적으면 일어나지 않은 배치를 보고한다(force_reattach 주석).
    time.sleep(6)
    try:
        got = (ssh(host, ['bash', '-c', CARRIER_READBACK], timeout=30).stdout or '').strip()
    except Exception:  # noqa: BLE001 - 못 읽은 것도 불일치로 적는다
        got = ''
    if got != str(CARRIER[cell]):
        log('UE_CARRIER_MISMATCH', host=host, cell=cell, wanted=CARRIER[cell], got=got or None)


# -- gNB --------------------------------------------------------------------

def gnb2_stalled() -> bool:
    """True when gNB2 needs restarting: no process at all, or a live process
    whose radio is streaming zeros.  The first case matters because a start that
    hits 'No USRP Device Found' segfaults and leaves nothing running, which the
    zero-sample test alone never notices."""
    global _gnb2_unreadable
    try:
        r = ssh('enb2', ['bash', '-c',
                         'n=$(ps -C nr-softmodem -o pid= | wc -l); '
                         'f=$(ls -t /tmp/gnb2*.log | head -1); '
                         'z=$(tail -n 400 "$f" | grep -c "got 0 from USRP"); '
                         'echo "$n $z"'], timeout=30)
        alive, zeros = int((r.stdout or '').split()[0]), int((r.stdout or '').split()[1])
        # 2026-09-23 감사: ssh 실패(rc 255, 빈 출력)를 예전엔 '0 0' 으로 채워 "프로세스 없음"
        # 으로 읽었다.  출력이 두 수가 아니면 못 읽은 것이다.
    except Exception:  # noqa: BLE001
        # 2026-09-23 감사: 못 읽음을 '정상(False)' 으로 읽지 않는다 -- 형제
        # ue_zombie.cell_is_down 은 두 번 못 읽으면 셀이 죽은 것으로 본다.  한 번의 깜빡임에
        # 재기동하지 않도록 연속 GNB2_UNREADABLE_LIMIT 회일 때만 참이다.
        _gnb2_unreadable += 1
        log('GNB2_UNREADABLE', streak=_gnb2_unreadable, limit=GNB2_UNREADABLE_LIMIT)
        if _gnb2_unreadable >= GNB2_UNREADABLE_LIMIT:
            _gnb2_unreadable = 0
            return True
        return False
    _gnb2_unreadable = 0
    if alive == 0:
        log('GNB2_PROCESS_ABSENT')
        return True
    return zeros > 0


#: gnb2 상태를 연속 이만큼 못 읽으면 재기동 대상으로 본다 (2026-09-23).  주기 20~75 초.
GNB2_UNREADABLE_LIMIT = 3
_gnb2_unreadable = 0


GNB1_BIN = Path('/opt/ran-lab/controller/oai-build-campaign5/cmake_targets/'
                'ran_build_campaign5/build/nr-softmodem')
#: `--telnetsrv` 의 플러그인은 campaign5 빌드가 아니라 actionspace 빌드에 있다.
#: 2026-09-21: 이 경로가 빠져 있어 gNB1 이 `libtelnetsrv.so: cannot open shared
#: object file` 로 텔넷을 못 올렸고, **전력 축(`ci rfatt`)이 통째로 죽어 있었다**.
#: 포트 9091 이 안 열리는 것이 유일한 증상이라 판 쪽에서는 보이지 않는다.
GNB1_TELNET_LIB_DIR = Path('/opt/ran-lab/controller/oai-build-campaign5/cmake_targets/'
                           'ran_build_actionspace/build')
GNB1_ROOT = EXP / 'recovery' / 'known-good-38prb'
# N3 on 192.168.70.140, not .129: gnb2's GTP-U is forwarded through PC1 and
# reaches the UPF from .129:2152 too, so a shared N3 address let whichever
# flow claimed the conntrack tuple first take gnb1's downlink (2026-09-15).
GNB1_CONF = GNB1_ROOT / 'gnb1.serving.n3-140.conf'


#: gnb1 기동 로그 자리 (2026-09-23: 시험이 홈에 파일을 만들지 않도록 상수로 뺐다).
GNB1_LOG_DIR = Path('/opt/ran-lab/controller')


def gnb1_log() -> Path | None:
    """Newest gNB1 log across BOTH naming conventions.  Globbing only
    /tmp/gnb1-probe-* silently unwatched gNB1 the moment it was started under the
    gnb1-loop38-* name, which is what happened on 2026-09-14: the keeper kept
    reading a file frozen at 15:41 while gNB1 was restarted three times."""
    found = list(Path('/tmp').glob('gnb1-probe-*.log'))
    found += list(GNB1_LOG_DIR.glob('gnb1-loop38*.log'))
    found = [p for p in found if p.is_file()]
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def _gnb1_tail(nbytes: int = 200000) -> str:
    path = gnb1_log()
    if path is None:
        return ''
    try:
        with path.open('rb') as handle:
            handle.seek(max(0, path.stat().st_size - nbytes))
            return handle.read().decode('utf8', 'ignore')
    except OSError:
        return ''


def gnb1_pids() -> list[int]:
    """PIDs of nr-softmodem processes running OUR gNB1 config, read from
    /proc/<pid>/cmdline with NUL separators -- `pkill -f` would match this
    keeper's own command line."""
    out = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if not argv or not argv[0].endswith(b'nr-softmodem'):
            continue
        # Match the conf this keeper launches: a hard-coded older name made every
        # gNB1 on the renamed N3 conf look absent (2026-09-15, 50 false restarts).
        if any(a.endswith(str(GNB1_CONF).encode()) for a in argv):
            out.append(int(entry.name))
    return out


def gnb1_stalled() -> bool:
    """True when gNB1 needs restarting: no process at all, or a live process
    whose radio is streaming zeros.  Mirrors gnb2_stalled()."""
    if not gnb1_pids():
        log('GNB1_PROCESS_ABSENT')
        return True
    return 'got 0 from USRP' in _gnb1_tail(20000)


#: 판이 도는 중에 gNB 재기동을 미룰 수 있는 주기 수.  주 루프가 20초이므로 약 1분이다.
#: 무한히 미루면 진짜 RF 고장을 영영 못 고치고(2026-09-21 에 그걸 걱정해 가드를 껐다가
#: 더 큰 위험을 열었다), 아예 안 미루면 일시적 오판 하나가 세 UE 를 다 날린다.
GNB_RESTART_DEFER_CYCLES = 3
_gnb_restart_deferred: "dict[str, int]" = {}
#: 그 셀을 마지막으로 미룬 사유.  컨텍스트 판정(excess·harq)이 미룬 횟수는 그 셀이 깨끗하게
#: 보이면 0 으로 돌아간다(2026-09-24 판 769: 09:50 대의 미룸 3회가 판 내내 남아 있다가
#: 09:59 조종 직후의 일시적 초과 1개에 곧장 강제 재기동이 걸렸다).  'radio' 는 되돌리지
#: 않는다 -- 죽은 RF 는 컨텍스트가 0 이라 늘 깨끗해 보인다.
_gnb_restart_deferred_cause: "dict[str, str]" = {}


def _clear_context_deferral(cell: str) -> None:
    if _gnb_restart_deferred_cause.get(cell) in ('excess', 'harq'):
        _gnb_restart_deferred.pop(cell, None)
        _gnb_restart_deferred_cause.pop(cell, None)


def _defer_gnb_restart(target: str, reason: str = 'radio') -> bool:
    """판이 하드웨어를 쥐고 있으면 재기동을 미룬다 -- 다만 한도가 있다.

    2026-09-21: gNB 재기동 한 번이 두 셀을 내리고 세 UE 의 부착 시계를 0 으로 돌린다.
    러너는 세 UE 가 동시에 버텨야 판을 시작하므로, 판 도중의 재기동은 그 판을 확실히
    죽인다.  그러나 `gnb1_stalled()` 이 참이면 RF 가 이미 죽은 것이라 그 판은 어차피
    성공할 수 없다 -- 그래서 짧게만(약 1분) 미루고 그 뒤에는 고친다.

    점유 판정은 `rebuild_chain()` 과 **같은 `episode_in_sitting()`** 을 쓴다.  판정이 둘이면
    언젠가 갈린다.
    """
    # 2026-09-24: 판 **안**(`cli`)일 때만 미룬다.  판 전 UE 대기(`manual-attempt`)는 gNB 가
    # 고쳐지길 기다리는 중이라, 거기서 미루면 러너와 keeper 가 서로를 기다린다(02:52~03:16,
    # gnb1 excess 3 인데 재기동이 24분 밀렸다).  러너는 UE 대기 뒤 관문을 다시 본다.
    if not episode_in_sitting():
        _gnb_restart_deferred.pop(target, None)
        return False
    seen = _gnb_restart_deferred.get(target, 0) + 1
    _gnb_restart_deferred[target] = seen
    _gnb_restart_deferred_cause[target] = reason
    if seen <= GNB_RESTART_DEFER_CYCLES:
        log('GNB_RESTART_DEFERRED', target=target, cycle=seen, cause=reason,
            reason='an episode holds the hardware; a restart would reset all three UEs')
        return True
    # 2026-09-23 감사: 사유를 실제 사유로 적는다 -- 좀비 컨텍스트 재기동도 "radio is down" 으로 찍혔다.
    log('GNB_RESTART_FORCED', target=target, cycles=seen, cause=reason,
        reason=('the radio is down and waiting longer cannot make this episode succeed'
                if reason == 'radio' else
                f'{reason}: the cell cannot serve this episode as it is'))
    _gnb_restart_deferred.pop(target, None)
    return False


def _gnb1_expected_tx_banner() -> str:
    """기동 성공 판정에 쓸 TX 주파수 문자열을 **conf 에서** 만든다.

    2026-09-22: 게이트에 `3.400320GHz` 가 박혀 있었는데 그날 반송파를 3349.92 MHz 로
    옮겼다.  그러면 gNB 가 정상 기동해도 게이트가 영원히 안 맞아 실패로 본다.
    """
    try:
        text = GNB1_CONF.read_text('utf8', 'ignore')
        import re as _re
        m = _re.search(r'absoluteFrequencySSB\s*=\s*([0-9]+)', text)
        if m:
            mhz = 3000 + 0.015 * (int(m.group(1)) - 600000)
            return 'Actual TX frequency: %.6fGHz' % (mhz / 1000.0)
    except OSError:
        pass
    return 'Actual TX frequency:'


#: 한 셀에 실제로 배치된 UE 수보다 이만큼 많은 RNTI 가 **최근 창에서 계속 통계를
#: 내고 있으면** 그 gNB 는 죽은 컨텍스트를 들고 있는 것이다.  gNB 는 이 상태를 스스로
#: 끝내지 못한다 -- RLF 콜백이 gNB 쪽에 등록돼 있지 않고(`nr_rlc_set_rlf_handler` 는
#: `RRC/NR_UE/rrc_UE.c` 에서만 불린다), UL 실패 경로는 `pusch_consecutive_dtx_cnt >=
#: pusch_FailureThres` 즉 **DTX** 를 세는데 이 좀비는 들리기는 하므로 DTX 가 안 쌓인다.
#: 2026-09-22 실측: 21:00 재기동 뒤 **1시간 20분** 만에 다시 쌓였고, 그때 gNB 가 살아
#: 있는 UE 에게 `NPRB 23` 을 주기 시작해 그 UE 의 `ulsch_DTX` 가 199배로 뛰었다.
STALE_RNTI_MARGIN = 1
#: 좀비 판정에 쓸 로그 꼬리 크기(바이트).  통계 줄은 UE 당 초당 한 줄꼴이다.
STALE_RNTI_TAIL_BYTES = 300000
#: 판이 도는 중이어도 이만큼 연속으로 감지되면 고친다.  좀비가 붙은 판은 ue3 가
#: 1 Mbps 로 떨어져 어차피 쓸 수 없으므로, 무한정 미루면 캠페인 전체가 무의미해진다.
STALE_PATIENCE = 3
_stale_seen: dict = {}
_stale_reason: dict = {}   # 연속 횟수가 어느 사유의 연속인지 (excess | harq)


#: 4차 재전송 비율이 이 값을 넘으면 그 UE 의 HARQ 결합이 깨져 있다.
#: 2026-09-22 실측(같은 셀·같은 순간): 정상 0.00086 · 망가진 것 0.0210 -- 24배.
#: 기전은 `nr_ue_procedures.c:952` -- gNB 가 같은 HARQ 프로세스에 다른 TBS 를 지시하면
#: UE 가 `new_data_indicator = true` 로 **HARQ 버퍼를 버려** 결합이 불가능해진다.
#: OAI 는 이 상태를 스스로 못 끝낸다: RLF 콜백은 gNB 쪽에 미등록이고, UL 실패 경로는
#: `pusch_consecutive_dtx_cnt` 즉 **연속** DTX 를 세는데 이 좀비는 가끔 PDU 를 보내
#: 카운터를 0 으로 되돌린다(`gNB_scheduler_ulsch.c:1016`) -- 그래서 누적 DTX 가 1,684
#: 여도 연속 100 을 한 번도 못 채운다.  실제로 gnb2 는 RLF 368 건에 `Remove NR rnti`
#: 가 3 건뿐이었다.
HARQ_DEPTH_BAD = 0.015   # 2026-09-25 18:1x: 0.005 flagged a healthy ue2 (17 Mbps, MCS 22) at 0.005-0.0118 and re-attached it at every board boundary; re-attach churn is what degraded ue2 today. Broken was 0.021.
#: 이 비율을 판단하기에 충분한 1차 전송 수.
HARQ_DEPTH_MIN_ROUNDS = 5000
#: 같은 UE 를 HARQ 로 다시 붙인 뒤 이 안에 또 걸리면 재부착은 소용없다(2026-09-24).  23:5x 30분 -> 10분:
#: ue1 의 재발은 이득(112) 탓이었고, 그 뒤 본 나쁜 기동 회차(ue2 23:25, ue3 23:23)는 다시 띄우면 나았다.
HARQ_REATTACH_COOLDOWN_S = 600.0
_harq_reattached_at: dict = {}

# 2026-09-27 00:1x: a UE can come up in a bad state that every other check passes (attached, DL
# flowing, KPM present).  ue3 on gnb1 ran MCS 6-8 at RSRP -95 while ue2 on the same cell ran 25;
# one restart with a USB reset brought it to MCS 18 and 9.85 of 10 Mbps.  Judged only while
# loaded, acted on only at a board boundary, once per LOW_MCS_COOLDOWN_S per host.
LOW_MCS_FLOOR = 12
LOW_MCS_PEER_GAP = 8
LOW_MCS_MIN_ROUNDS = 2000
LOW_MCS_COOLDOWN_S = 1800.0
LOW_MCS_RECENT_SAMPLES = 20      # ~25 s of 1.28 s stats blocks
LOW_MCS_STREAK = 3               # keeper cycles in a row before a UE is flagged
LOW_MCS_MIN_GOODPUT = 1.0        # Mbps: a sample below this is idle, its MCS says nothing
LOW_MCS_BUSY_ROUNDS = 300        # ...unless the scheduler still ran this many DL rounds in it
_low_mcs_seen: dict = {}
_low_mcs_pending: dict = {}
_low_mcs_restarted_at: dict = {}


#: The runner's C0 (run_episode.BASELINE_ATTENUATION_DB), by keeper cell name.
CELL_BASELINE_ATTENUATION_DB = {'gnb1': 8.0, 'gnb2': 6.0}  # 2026-09-28 05:3x back from option B
_attenuation_off_seen: dict = {}


def _cell_attenuation_scope_owned(cell: str) -> bool:
    """Does the campaign-5 worker ledger name an owner of this cell's attenuation scope?"""
    target = {'gnb1': '12345678', 'gnb2': '87654321'}[cell]
    try:
        state = json.loads((HERE / f'campaign5-{cell}-worker.json').read_text())
    except (OSError, ValueError):
        return True     # unreadable: assume owned, never race the producer
    return any(k.startswith('action=104/') and k.endswith(f'target={target}')
               for k in (state.get('owners') or {}))


def restore_cell_attenuation_between_boards() -> None:
    """Between boards, put a cell's TX attenuation back at C0 when it has stayed off for two cycles.

    2026-09-27: twice tonight a stopped board left gnb2 at 21 dB (its rollback was lost), and with
    the energy requirement on gnb1 the E2 restore needs a UE on the cell -- a cell whose UEs dropped
    under a high attenuation cannot be brought back that way, and gnb1 re-attaches nobody far off
    its baseline.  The runner waits for C0 but cannot restore it; this does, over telnet (bed restore).
    """
    import re as _re
    for cell, want in CELL_BASELINE_ATTENUATION_DB.items():
        try:
            m = _re.search(r'current TX attenuation ([0-9.]+) dB', _cell_telnet(cell, 'ci rfatt'))
        except Exception:  # noqa: BLE001 - unreadable decides nothing
            continue
        if not m:
            continue
        seen = float(m.group(1))
        if abs(seen - want) < 0.05:
            _attenuation_off_seen.pop(cell, None)
            continue
        _attenuation_off_seen[cell] = _attenuation_off_seen.get(cell, 0) + 1
        if _attenuation_off_seen[cell] < 2:
            continue
        # Never under a live policy (run_episode's gate, board 675 on 09-26): a telnet restore
        # while the producer's policy still owns the cell scope makes every power trial of the
        # next board a 409.  Only an orphaned value -- no owner in the worker ledger -- is ours.
        if _cell_attenuation_scope_owned(cell):
            log('CELL_ATTENUATION_OFF_OWNED', cell=cell, seen=seen, want=want,
                note='a policy still owns the scope; the producer restores it on expiry')
            continue
        out = _cell_telnet(cell, f'ci rfatt {want:g}')
        log('CELL_ATTENUATION_RESTORED', cell=cell, was=seen, now=want, reply=out.strip()[:80])
        _attenuation_off_seen.pop(cell, None)


def mcs_verdicts(text: str) -> dict:
    """`{rnti: (p75 MCS, lagging)}` for loaded UEs when a loaded same-cell peer exists; UEs that
    cannot be judged (idle, or no loaded peer) are absent."""
    lag = dict(lagging_mcs_ues(text))
    return {r: (m, r in lag) for r, m in _loaded_mcs(text).items()} if len(_loaded_mcs(text)) >= 2 else {}


def _loaded_mcs(text: str) -> dict:
    """`{rnti: p75 DL MCS}` over the newest samples that carry traffic (goodput >= 1 Mbps).
    2026-09-27 10:38: ue3 1ca0 read p75 6 from idle keepalive samples (0.04 Mbps) while it ran
    18-19 under load -- an idle UE's MCS is not its link."""
    import re as _re
    per: dict = {}
    for rnti, first, mcs, goodput in _re.findall(
            r'UE ([0-9a-f]{4}): dlsch_rounds (\d+)/[^\n]*?MCS \(0\) (\d+)(?:[^\n]*?goodput ([\d.]+))?', text):
        per.setdefault(rnti, []).append((int(first), int(mcs), float(goodput) if goodput else None))
    loaded = {}
    for rnti, rows in per.items():
        rows = rows[-LOW_MCS_RECENT_SAMPLES - 1:]
        # 2026-09-27 18:4x: ue2 b014 sat at MCS 0, BLER 0.6, 0.4 Mbps under full load -- goodput
        # alone called it idle and the worst context was never judged.  A sample is busy when the
        # scheduler worked it (>= LOW_MCS_BUSY_ROUNDS new rounds; idle keepalive ~30, load 700-1400).
        busy = [m for (f0, _, _), (f, m, g) in zip(rows, rows[1:])
                if g is None or g >= LOW_MCS_MIN_GOODPUT or f - f0 >= LOW_MCS_BUSY_ROUNDS]
        if len(busy) < 3 or rows[-1][0] - rows[0][0] < LOW_MCS_MIN_ROUNDS:
            continue
        values = sorted(busy)
        loaded[rnti] = values[int(0.75 * (len(values) - 1) + 0.5)]
    return loaded


#: A context that sits far below the MCS its own host reached on the same cell, at baseline
#: attenuation.  2026-09-27 v5.1 blocks 2-3: after steering hand-backs, ue3 9b54/a46a/3bb4 ran
#: their whole life at MCS 10-12 and ue2 38b1 at 15 (RSRP unchanged, -94 / -104 dBm) while the
#: host's other contexts ran 20-26; both gnb1 UEs were low at once, so the peer gap saw nothing
#: and boards 768-769 started with gnb1 carrying 10 and 7.5 Mbps instead of 16-18.
LOW_MCS_SELF_GAP = 8
LOW_MCS_BEST_MAX_AGE_S = 6 * 3600.0
LOW_MCS_HISTORY = 5
LOW_MCS_BASELINE_CYCLES = 3        # attenuation at C0 this many cycles in a row (> the window)
MCS_BEST_FILE = OUT / 'mcs-best.json'
KEEPER_GAP = OUT / 'KEEPER_GAP'
_baseline_run: dict = {}


def self_lagging_mcs(cell: str, loaded: dict, host_of, best: dict, now: float) -> dict:
    """`{rnti: p75}` of loaded contexts at least LOW_MCS_SELF_GAP below the median p75 of the
    host's other recent healthy contexts on this cell (>= 2 of them, within
    LOW_MCS_BEST_MAX_AGE_S).  `best` is `{"host@cell": {rnti: [p75, at]}}`; a context not lagging
    is recorded there (newest LOW_MCS_HISTORY kept).  The median, not the maximum: ue3's healthy
    contexts ran 18-23 with one at 28, its stuck ones 10-12."""
    import statistics as _st
    lag = {}
    for rnti, m in loaded.items():
        host = host_of(rnti)
        if host is None:
            continue
        key = f'{host}@{cell}'
        seen = best.get(key)
        seen = seen if isinstance(seen, dict) else {}
        others = [v[0] for r, v in seen.items() if r != rnti and now - v[1] <= LOW_MCS_BEST_MAX_AGE_S]
        if len(others) >= 2 and m <= _st.median(others) - LOW_MCS_SELF_GAP:
            lag[rnti] = m
            continue
        seen[rnti] = [m, now]
        best[key] = dict(sorted(seen.items(), key=lambda kv: kv[1][1])[-LOW_MCS_HISTORY:])
    return lag


def _cell_at_baseline(cell: str) -> bool:
    import re as _re
    m = _re.search(r'current TX attenuation ([0-9.]+) dB', _cell_telnet(cell, 'ci rfatt'))
    ok = bool(m) and abs(float(m.group(1)) - CELL_BASELINE_ATTENUATION_DB[cell]) < 0.05
    _baseline_run[cell] = _baseline_run.get(cell, 0) + 1 if ok else 0
    return _baseline_run[cell] >= LOW_MCS_BASELINE_CYCLES


def low_mcs_verdicts_on(cell: str, text: str) -> dict:
    """`{rnti: (p75, lagging)}` for one cell, judged only at baseline attenuation (a power trial
    lowers every MCS on the cell): lagging behind a loaded peer or behind the host's own best.
    Every loaded context the self check can place is judged, so a healthy one breaks its streak."""
    loaded = _loaded_mcs(text)
    if not loaded or not _cell_at_baseline(cell):
        return {}
    verdicts = mcs_verdicts(text)
    lag = _self_lagging_on(cell, loaded)
    for rnti, m in loaded.items():
        if _host_of_rnti(rnti) is not None:
            verdicts[rnti] = (m, rnti in lag or verdicts.get(rnti, (m, False))[1])
    return verdicts


def _self_lagging_on(cell: str, loaded: dict) -> dict:
    """self_lagging_mcs with the best values kept across keeper restarts."""
    try:
        best = json.loads(MCS_BEST_FILE.read_text())
    except (OSError, ValueError):
        best = {}
    lag = self_lagging_mcs(cell, loaded, _host_of_rnti, best, time.time())
    try:
        tmp = MCS_BEST_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(best))
        tmp.replace(MCS_BEST_FILE)
    except OSError:
        pass
    return lag


def lagging_mcs_ues(text: str):
    """`(rnti, p75 MCS)` of loaded UEs far below a loaded same-cell peer."""
    loaded = _loaded_mcs(text)
    if len(loaded) < 2:
        return []
    best = max(loaded.values())
    return [(r, m) for r, m in loaded.items() if m < LOW_MCS_FLOOR and best - m >= LOW_MCS_PEER_GAP]


#: UE 호스트의 CPU 거버너.  `powersave` 면 주파수가 실시간 처리 중에도 0.4 GHz 까지
#: 떨어져 USRP 에 샘플을 제때 못 넘긴다.  2026-09-22 실측: ue3 만 `powersave` 였고
#: 주파수가 `4.94 4.94 2.49 0.40 4.30 GHz` 로 널뛰었다(ue1 은 3.76~3.95 로 안정).
#: 그 결과 `L`(late) 39,317 · `U`(underrun) 8,759 로 ue1 의 21배·8.6배가 났고,
#: 상향이 밀려 DTX -> 재전송 폭증 -> TBS 불일치 -> HARQ 결합 파탄 -> 하향 1 Mbps.
#: `scaling_governor` 는 root 소유라 이 계정은 못 고친다 -- 발견하면 알린다.
WANTED_GOVERNOR = 'performance'


def ensure_ue_governors() -> None:
    """UE 호스트의 CPU 거버너가 `performance` 인지 본다.  아니면 알린다."""
    for host in HOSTS:
        try:
            r = ssh(host, ['cat', '/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'],
                    timeout=20)
        except OSError:
            continue
        governor = (r.stdout or '').strip()
        if not governor or governor == WANTED_GOVERNOR:
            _governor_warned.discard(host)
            continue
        if host in _governor_warned:
            continue
        _governor_warned.add(host)
        log('ALERT_CPU_GOVERNOR', host=host, governor=governor, wanted=WANTED_GOVERNOR,
            action=(f"{host} 의 CPU 거버너가 {governor} 다.  실시간 처리 중 주파수가 "
                    f"0.4 GHz 까지 떨어져 USRP 샘플이 늦고, 그것이 DTX -> 재전송 -> "
                    f"HARQ 결합 파탄으로 이어진다.  운영자가 한 줄로 고친다: "
                    f"ssh {host} 'sudo cpupower frequency-set -g performance'"))


_governor_warned: set = set()


def broken_harq_ues(text: str):
    """HARQ 결합이 깨진 UE 를 `(rnti, 깊이비)` 로 돌려준다.

    `dlsch_rounds a/b/c/d` 는 1~4차 누적이다.  4차/1차 가 크다는 것은 재전송이
    **결합되지 않고** 끝까지 밀린다는 뜻이다.

    2026-09-23 감사: 비율은 **꼬리 창 안의 증분**(그 RNTI 의 첫 표본과 마지막 표본의 차)
    으로 잰다.  누적 비율은 한 번 넘은 RNTI 를 영원히 '망가짐' 으로 남겨, 09-23 하루
    gNB 강제 재기동 59회·진행 중 판 280행 중 92행이 HARQ 만으로 걸렸다(기록값 0.05~0.08).
    꼬리 300 KB 는 UE 셋에서 RNTI 당 약 1분이다.  실측 eaad: 누적 169/489199 = 0.00035,
    증분 10/215852 = 0.000046 (정상 0.00086 · 망가짐 0.0210 의 문턱 0.005 는 그대로).
    표본이 하나거나 카운터가 되감긴(gNB 재기동) RNTI 는 판단하지 않는다.
    """
    import re as _re
    seen: dict = {}
    for rnti, rounds in _re.findall(r'UE ([0-9a-f]{4}): dlsch_rounds ([0-9/]+)', text):
        parts = rounds.split('/')
        if len(parts) < 4:
            continue
        try:
            sample = (int(parts[0]), int(parts[3]))
        except ValueError:
            continue
        seen.setdefault(rnti, [sample, sample])[1] = sample
    out = []
    for rnti, ((first0, fourth0), (first1, fourth1)) in seen.items():
        d_first, d_fourth = first1 - first0, fourth1 - fourth0
        if d_first < HARQ_DEPTH_MIN_ROUNDS or d_fourth < 0:
            continue
        ratio = d_fourth / d_first
        if ratio >= HARQ_DEPTH_BAD:
            out.append((rnti, round(ratio, 5)))
    return sorted(out)


def stale_rnti_excess(text: str, placed: int) -> int:
    """최근 꼬리에서 통계를 내고 있는 RNTI 수 - 그 셀에 배치된 UE 수.

    **꼬리만 본다.**  해제된 RNTI 도 로그 전체에는 남으므로 전체를 세면 항상 초과로
    보인다(2026-09-22 실측: 전체 42개, 최근 창 3개).  살아 있다는 증거는 "지금도
    통계를 내는가" 뿐이다.
    """
    return len(active_rntis(text)) - int(placed)


def recent_stats_blocks(text: str, blocks: int = 8) -> str:
    """The last `blocks` MAC stats blocks (~1.28 s each); the whole text when it has fewer."""
    parts = text.split('Frame.Slot')
    return text if len(parts) <= blocks + 1 else 'Frame.Slot'.join(parts[-blocks:])


def desynced_rntis(text: str, min_seen: int = 3) -> set:
    """RNTIs whose every recent `UE RNTI xxxx CU-UE-ID n ...sync` line says out-of-sync."""
    import re as _re
    seen: dict = {}
    for rnti, state in _re.findall(r'UE RNTI ([0-9a-f]{4}) CU-UE-ID \d+ (in-sync|out-of-sync)', text):
        seen.setdefault(rnti, []).append(state)
    return {r for r, st in seen.items() if len(st) >= min_seen and all(x == 'out-of-sync' for x in st)}


def active_rntis(text: str) -> set:
    """지금도 통계를 내고 있는 RNTI 들.  해제된 것은 꼬리에서 사라진다."""
    import re as _re
    return set(_re.findall(r'UE ([0-9a-f]{4}): ulsch_rounds', text))


def _gnb2_tail(nbytes: int = STALE_RNTI_TAIL_BYTES) -> str:
    r = ssh('enb2', ['bash', '-c',
                     'L=$(ls -t /tmp/gnb2*.log 2>/dev/null | head -1); '
                     f'[ -n "$L" ] && tail -c {int(nbytes)} "$L" | sed "s/\x1b\[[0-9;]*m//g"'],
            timeout=40)
    return r.stdout or ''


#: gnb2 의 PRACH 줄 `energy … dB (I0 N, thres 250)` 의 I0 (0.1 dB 단위).  2026-09-24 기동별 중앙값:
#: 대개 231~241, 그런데 03:19 기동 275·05:20 기동 263 -- 그 기동에서는 UE 의 PRACH 가 묻혀
#: `RAR reception failed` 만 반복했고, 재기동하자 230 대로 돌아와 붙었다.
GNB2_I0_LIMIT = 250   # 18:3x: at I0 245-249 ghost PRACH (delay 0, 52-55 dB) took the CCEs and no UE could finish RA; good starts are 231-241
GNB2_I0_MIN_SAMPLES = 5
_gnb2_noise_checked: "str | None" = None


#: 2026-09-24 18:4x: ue3 locked its LO 13.8 kHz off (a half-subcarrier misread of the PSS/SSB FO
#: estimate).  Its PRACH then hit gnb2 as a flood of misread preambles (delay 0, random index) that
#: took the CCEs -- no UE could finish RA and no board ran for 100 min.  A restart re-measures.
UE_FO_LIMIT_HZ = 5000
UE_CARRIER_HZ = (3319680000, 3349920000)
_ue_fo_checked: dict = {}


def ue_tx_offset_hz(host: str):
    """(log, |last tx_freq - nearest carrier|) of the UE's current instance, or (None, None)."""
    try:
        r = ssh(host, ['bash', '-c', 'L=$(ls -t /home/*/ota-fixed38-%s-*.log | head -1); echo "$L"; '
                                     'grep -aoE "setting tx_freq [0-9]+ Hz" "$L" | tail -1' % host],
                timeout=20)
    except Exception:  # noqa: BLE001
        return None, None
    lines = (r.stdout or '').split('\n')
    log_name = lines[0].strip() if lines else ''
    m = re.search(r'setting tx_freq ([0-9]+) Hz', r.stdout or '')
    if not log_name or not m:
        return log_name or None, None
    hz = int(m.group(1))
    return log_name, min(abs(hz - c) for c in UE_CARRIER_HZ)


def ensure_ue_frequency_sane() -> None:
    for host in HOSTS:
        log_name, off = ue_tx_offset_hz(host)
        if log_name is None or off is None or off <= UE_FO_LIMIT_HZ:
            continue
        if _ue_fo_checked.get(host) == log_name:
            continue                      # once per UE instance
        _ue_fo_checked[host] = log_name
        log('UE_FREQUENCY_OFF', host=host, offsetHz=off, log=log_name,
            note='LO locked far from the carrier; its PRACH floods the cell with misread preambles')
        restart_ue(host, evidence='frequency-off')


def ensure_gnb2_noise_floor() -> None:
    """새로 뜬 gnb2 의 수신 잡음이 높게 떴으면 한 번 다시 띄운다 (로그 하나에 한 번만)."""
    global _gnb2_noise_checked
    if episode_in_sitting():
        return
    r = ssh('enb2', ['bash', '-c', 'L=$(ls -t /tmp/gnb2*.log 2>/dev/null | head -1); echo "$L"; '
                                   '[ -n "$L" ] && grep -a -o "I0 [0-9]*" "$L" | tail -40'], timeout=40)
    lines = (r.stdout or '').splitlines()
    name = lines[0].strip() if lines else ''
    values = [int(x.split()[1]) for x in lines[1:] if x.startswith('I0 ') and x.split()[1].isdigit()]
    if not name or name == _gnb2_noise_checked or len(values) < GNB2_I0_MIN_SAMPLES:
        return
    _gnb2_noise_checked = name
    median = sorted(values)[len(values) // 2]
    if median <= GNB2_I0_LIMIT:
        log('GNB2_NOISE_FLOOR_OK', log=name, i0Median=median)
        return
    log('GNB2_NOISE_FLOOR_HIGH', log=name, i0Median=median, limit=GNB2_I0_LIMIT,
        note='PRACH buried; restarting gnb2 once for this start')
    restart_gnb2(reason='noise-floor')


def _ue_held_rntis():
    """{host: 그 UE 가 지금 쥔 RNTI}, 하나라도 못 읽으면 None (그러면 아무것도 지우지 않는다)."""
    try:
        sys.path.insert(0, str(HERE))
        import ue_zombie
    except ImportError:
        return None
    held = {}
    for host in HOSTS:
        try:
            r = ssh(host, ['bash', '-c', 'L=$(ls -t /home/*/ota-fixed38-%s-*.log | head -1); '
                                         'grep -aE "RNTI [0-9a-f]{4} stats" "$L" | tail -3' % host],
                    timeout=30)
        except Exception:  # noqa: BLE001 - 못 읽으면 모른다
            return None
        rnti = ue_zombie.current_rnti(r.stdout or '')
        if not rnti:
            return None
        held[host] = rnti
    return held


def _retired_rntis() -> set:
    """UE 의 **이전** 인스턴스들(최근 로그 2~4번째)이 마지막으로 쥐었던 RNTI.

    그 프로세스는 이미 끝났으므로 이 RNTI 는 누구의 핸드오버도 아니다 -- 판 도중에도
    지워도 된다(2026-09-24 11:47 ue2 가 죽고 재시작 → 옛 RNTI 가 gnb1 에 남아 11:55
    판 도중 gnb1 강제 재기동)."""
    try:
        sys.path.insert(0, str(HERE))
        import ue_zombie
    except ImportError:
        return set()
    out = set()
    for host in HOSTS:
        try:
            r = ssh(host, ['bash', '-c', 'for L in $(ls -t /home/*/ota-fixed38-%s-*.log | sed -n 2,4p); do '
                                         'grep -aE "RNTI [0-9a-f]{4} stats" "$L" | tail -3; echo "@@"; done' % host],
                    timeout=30)
        except Exception:  # noqa: BLE001
            continue
        for chunk in (r.stdout or '').split('@@'):
            rnti = ue_zombie.current_rnti(chunk)
            if rnti:
                out.add(rnti)
    return out


def _cell_runs_checked_release(cell: str) -> bool:
    """그 셀의 gNB 가 NULL 검사가 든 libtelnetsrv_ci.so 를 물고 있나.  빌드가 파일을 새로 만들므로
    옛 라이브러리를 쥔 프로세스의 maps 에는 `(deleted)` 가 붙는다 -- 그러면 아직 옛 것이다."""
    probe = ('P=$(pgrep -x nr-softmodem | head -1); [ -n "$P" ] && '
             'grep telnetsrv_ci /proc/$P/maps | head -1')
    try:
        if cell == 'gnb1':
            out = subprocess.run(['bash', '-c', probe], capture_output=True, text=True,
                                 timeout=10).stdout
        else:
            out = ssh('enb2', ['bash', '-c', probe], timeout=20).stdout or ''
    except Exception:  # noqa: BLE001
        return False
    return 'libtelnetsrv_ci.so' in out and '(deleted)' not in out


def _cell_telnet(cell: str, command: str) -> str:
    if cell == 'gnb1':
        return _gnb_telnet(command)
    try:
        r = ssh('enb2', ['bash', '-c', '(echo %s; sleep 1.5) | timeout 5 nc 127.0.0.1 9091'
                         % shlex.quote(command)], timeout=20)
        return r.stdout or ''
    except Exception as exc:  # noqa: BLE001
        return f'TELNET_ERROR:{type(exc).__name__}'


def release_zombie_contexts(cell: str, live_rntis: set) -> list:
    """판 **밖**에서, 어느 UE 도 쥐지 않은 RNTI 컨텍스트만 `ci force_ul_failure` 로 지운다.

    2026-09-24 11:0x~11:36: UE 를 재시작할 때마다 gNB 에 옛 컨텍스트가 남고(gNB 는 스스로
    못 지운다), keeper 는 그걸 치우려 gNB 를 재기동했다 -> 세 UE 재부착 -> 또 옛 컨텍스트 ->
    또 재기동.  gnb2 가 40분에 다섯 번 재기동됐고 주소 풀이 말라 코어까지 초기화됐다.
    `ci force_ul_failure e805` 로 gnb1 이 `Remove UE context` 를 찍고 컨텍스트만 지우는 것을
    실측했다.  판 안에서는 쓰지 않는다: 핸드오버 중에는 UE 로그의 RNTI 가 늦어 새 컨텍스트를
    좀비로 오인할 수 있다.  RNTI 는 짧은 꼬리(`active_rntis`)에 지금 보이는 것만 대상이다
    (09-22: 이미 지워진 RNTI 에 해제를 걸어 gnb1 이 죽었다).
    """
    # 2026-09-24 12:11: `ci force_ul_failure 0006` 이 gnb2 를 죽였다(로그가 그 명령 줄에서 끝난다):
    # 텔넷 쪽이 `find_nr_UE()` 의 NULL 을 역참조했다.  고친 libtelnetsrv_ci.so
    # (oai_patches/telnet_ci_release_checks_the_ue_exists.patch) 를 **실제로 물고 있는** gNB 에만 쓴다.
    if os.environ.get('AIC_RELEASE_ZOMBIES') == '0' or not _cell_runs_checked_release(cell):
        return []
    held = _ue_held_rntis()
    if held is None:
        return []
    zombies = set(live_rntis) - set(held.values())
    if episode_in_sitting():
        # 판 안에서는 끝난 UE 인스턴스의 RNTI 만 (핸드오버 중인 새 컨텍스트를 지우지 않도록).
        zombies &= _retired_rntis()
    # 2026-09-25 08:0x: RNTI 는 셀마다 따로 준다 -- ue1 이 gnb1 에서 받은 6610 과 같은 값의 옛 컨텍스트가
    # gnb2 에 out-of-sync 로 남았는데 '쥔 RNTI' 라 지워지지 않았고, 그 좀비가 KPM 에 ue1 의 AMF id 를
    # 계속 보고해 조종·되돌리기가 죽은 컨텍스트로 갔다(판 591-593 연속 잠금).  이 셀에서 최근 내내
    # out-of-sync 인 컨텍스트는 판 안이든 쥔 RNTI 든 이 셀의 좀비다.
    desync = desynced_rntis(recent_stats_blocks(_gnb1_tail(200000) if cell == 'gnb1' else _gnb2_tail()))
    # 2026-09-27 03:26: a v5.1 trial (ue3 capped to 12 PRB and deprioritised at load 12) left ue3
    # barely scheduled, gnb1 marked its live context out-of-sync, and this released it mid-board.
    # The 591-593 rule is about a held RNTI's *stale twin on the other cell*: during a board a held
    # RNTI is released only when the other cell also shows it active (the UE lives there).
    if episode_in_sitting():
        other = 'gnb2' if cell == 'gnb1' else 'gnb1'
        try:
            other_live = active_rntis(recent_stats_blocks(
                _gnb1_tail(200000) if other == 'gnb1' else _gnb2_tail()))
        except Exception:  # noqa: BLE001 - unknown: protect the held context
            other_live = set()
        spared = {r for r in desync if r in set(held.values()) and r not in other_live}
        if spared:
            log('DESYNC_HELD_CONTEXT_SPARED', cell=cell, rntis=sorted(spared),
                note='held by a UE on this cell during a board: throttled, not a leftover')
        desync -= spared
    zombies = sorted(set(zombies) | desync)
    released = []
    for rnti in zombies:
        reply = _cell_telnet(cell, f'ci force_ue_release {rnti}' if rnti in desync
                             else f'ci force_ul_failure {rnti}')
        if 'TELNET_ERROR' in reply:
            break
        released.append(rnti)
        time.sleep(1)
        if 'softmodem' not in _cell_telnet(cell, 'ci rfatt'):   # 빈 응답도 죽은 것이다 (12:11 gnb2)
            log('ZOMBIE_RELEASE_STOPPED', cell=cell, rnti=rnti, reason='gNB did not answer after release')
            break
    if released:
        log('ZOMBIE_CONTEXTS_RELEASED', cell=cell, released=released, held=held)
    return released


#: `UE xxxx: RLF detected, but no callable RLF handler registered` 이 최근 꼬리에 이만큼이면 그 컨텍스트는 끝났다.
RLF_RELEASE_MIN_LINES = 20
_rlf_released: set = set()


def rlf_rntis(text: str) -> dict:
    """RNTI -> 최근 꼬리의 gNB RLC RLF 줄 수."""
    import re as _re
    out: dict = {}
    for rnti in _re.findall(r'UE ([0-9a-f]{4}): RLF detected', text):
        out[rnti] = out.get(rnti, 0) + 1
    return out


def release_rlf_contexts() -> list:
    """RLC 가 최대 재전송에 걸린 컨텍스트를 판 **안에서도** 해제한다 (2026-09-25).

    gNB 는 `RLF detected, but no callable RLF handler registered` 만 찍고 아무것도 하지 않아,
    ue2(d462) 가 판 566 내내(13분) RLC 가 멈춘 채 하향 0.01~0.35 Mbps 로 방치됐다.  그 컨텍스트는
    이미 끝났으므로 `ci force_ul_failure` 로 지우면 UE 가 곧바로 다시 붙는다.  지금도 통계를 내는
    RNTI 에만, RNTI 당 한 번.
    """
    released = []
    for cell, tail in (('gnb1', _gnb1_tail), ('gnb2', _gnb2_tail)):
        if os.environ.get('AIC_RELEASE_ZOMBIES') == '0' or not _cell_runs_checked_release(cell):
            continue
        text = tail(200000)
        live = active_rntis(text)
        for rnti, n in sorted(rlf_rntis(text).items()):
            if n < RLF_RELEASE_MIN_LINES or rnti not in live or rnti in _rlf_released:
                continue
            # 2026-09-25 06:4x: force_ul_failure only arms the UL-failure timer, and every decoded PUSCH
            # clears it -- an RLF context whose UE still sends UL (ue2 56be) went only after ~200 s.
            # force_ue_release expires the timer and requests the release at once.
            reply = _cell_telnet(cell, f'ci force_ue_release {rnti}')
            if 'TELNET_ERROR' in reply:
                break
            _rlf_released.add(rnti)
            released.append(rnti)
            log('RLF_CONTEXT_RELEASED', cell=cell, rnti=rnti, rlfLines=n, inSitting=episode_in_sitting())
    return released


def ensure_no_stale_contexts() -> None:
    """죽은 UE 컨텍스트를 들고 있는 gNB 를 재기동한다.

    판이 돌고 있어도 **미루기만 하고 포기하지 않는다**.  2026-09-22 23:03 실측:
    gnb2 는 재기동 5분 만에 좀비 RNTI 가 하나 생겼고, 그 상태에서 gNB 가 같은 HARQ
    프로세스에 다른 TBS 를 지시해 ue3 의 재전송 결합이 깨진다(UE 가 HARQ 버퍼를
    버린다 -- `nr_ue_procedures.c:952`).  그러면 그 판은 **이미 쓸모가 없다** --
    ue3 가 1 Mbps 로 떨어져 `I3g`·`I3d` 가 모든 T 에서 FAIL 이 된다.
    판을 지키려던 가드가 판을 쓸모없게 만드는 것이라, 연속으로 감지되면 고친다.
    `restart_gnb*` 안의 `_defer_gnb_restart` 가 같은 원리로 짧게만 더 미룬다.

    **한 셀만 보고 `CELL_OF` 와 비교하면 안 된다** (2026-09-23).  `CELL_OF` 는 고정
    지도인데 조종 축은 일부러 UE 를 다른 셀로 옮긴다.  그래서 조종이 성공할 때마다
    gnb1 은 `excess: 1` 로 보였고, 3연속이면 `GNB_RESTART_FORCED` 가 걸렸다.  재기동은
    E2 연결 epoch 을 올리고, 올라간 epoch 은 A1-P 인벤토리와 어긋나며, 어긋난 인벤토리는
    다음 판의 조종을 `AIC_E2_NOT_READY` 로 거절해 `RECOVERY_FAILURE` 를 만든다 --
    우리 가드가 우리 실험을 죽이는 닫힌 고리였다.

    **UE 수는 보존된다.**  셋뿐이므로 한 셀이 2개를 들고 있어도 다른 셀이 1개면
    아무도 죽지 않았고 하나가 옮겨간 것이다.  좀비의 증거는 한 셀의 초과가 아니라
    **두 셀 합이 UE 수를 넘는 것**이다.  읽지 못한 셀이 있으면 판정하지 않는다
    (fail-closed: 불필요한 재기동이야말로 판을 죽이는 쪽이다).
    관련: [[a-successful-handover-strands-the-placement]],
    [[a-verdict-frozen-in-a-constant-outlives-its-evidence]]
    """
    live: dict[str, set] = {}
    tails: dict[str, str] = {}
    for cell in CELL_NB:
        try:
            text = _gnb1_tail(STALE_RNTI_TAIL_BYTES) if cell == 'gnb1' else _gnb2_tail()
        except (OSError, subprocess.SubprocessError):   # 2026-09-23: ssh 시간초과도 '못 읽음'
            return
        if not text:
            return
        tails[cell] = text
        try:
            verdicts = low_mcs_verdicts_on(cell, text)
        except Exception as exc:  # noqa: BLE001 - an unreadable cell judges nothing
            verdicts = {}
            log('LOW_MCS_CHECK_ERROR', cell=cell, error=type(exc).__name__)
        for rnti, (_m, lag) in verdicts.items():
            if not lag:
                _low_mcs_seen.pop(rnti, None)   # judged fine: the streak breaks; unjudged keeps it
        for rnti, mcs in ((r, m) for r, (m, lag) in verdicts.items() if lag):
            count = _low_mcs_seen.get(rnti, (cell, 0))[1] + 1
            _low_mcs_seen[rnti] = (cell, count)
            host = _host_of_rnti(rnti)
            if count >= LOW_MCS_STREAK and host is not None and _low_mcs_pending.get(host) != rnti:
                _low_mcs_pending[host] = rnti
                log('UE_LOW_DL_MCS', cell=cell, host=host, rnti=rnti, mcsP75=mcs, streak=count)
        # 2026-09-25 05:3x: 300 KB of gnb1 log is minutes of 1.28 s stats blocks, so handover
        # leftovers removed long ago still counted as live -> false 'excess' -> gnb1 restarted mid-board
        # (board 581).  Judge liveness on the last few blocks; HARQ increments keep the long tail.
        live[cell] = active_rntis(recent_stats_blocks(text))
    # 2026-09-27 09:24: the next board took the lock between this cycle's read and the restart;
    # restart_ue aborted, yet the pending flag was dropped and the 30-min cooldown charged.  Only
    # a cycle with no episode lock at all restarts; otherwise the flag waits.
    # 2026-09-27 15:1x: the runner starts the next board seconds after the last one ends, so a
    # flagged UE (ue3 6a35, MCS 7 since 14:54) waited whole boards for a lock-free cycle.  While a
    # restart is pending, ask the runner for a gap; it waits (at most 90 s) before its next board.
    try:
        if _low_mcs_pending:
            KEEPER_GAP.write_text(' '.join(sorted(_low_mcs_pending)) + '\n')
        else:
            KEEPER_GAP.unlink(missing_ok=True)
    except OSError:
        pass
    if _low_mcs_pending and not episode_running():
        host, rnti = next(iter(_low_mcs_pending.items()))
        _low_mcs_pending.pop(host)
        # The same context still, and not restarted for this recently: restart the UE alone.
        if (_host_of_rnti(rnti) == host
                and time.time() - _low_mcs_restarted_at.get(host, 0.0) >= LOW_MCS_COOLDOWN_S):
            _low_mcs_restarted_at[host] = time.time()
            log('UE_LOW_DL_MCS_RESTART', host=host, rnti=rnti)
            # Hold the busy lock over the restart (Codex): run_episode refuses a live holder, so the
            # runner's next board cannot start while this UE is being restarted.
            try:
                BUSY.write_text(f'{os.getpid()} keeper-ue-restart {host}\n')
                restart_ue(host, evidence='low-dl-mcs')
            finally:
                try:
                    if BUSY.read_text().split()[0] == str(os.getpid()):
                        BUSY.unlink()
                except (OSError, IndexError):
                    pass
                KEEPER_GAP.unlink(missing_ok=True)
            return
    total_excess = sum(len(s) for s in live.values()) - len(HOSTS)
    for cell, nb in CELL_NB.items():
        text = tails[cell]
        placed = sum(1 for _ue, c in CELL_OF.items() if c == cell)
        excess = total_excess if len(live[cell]) > placed else 0
        broken = broken_harq_ues(text)
        if excess < STALE_RNTI_MARGIN and not broken:
            _stale_seen.pop(cell, None)
            _clear_context_deferral(cell)
            # 2026-09-25 08:0x: a desynced leftover can hide inside the placed count (ue1's old 6610 on gnb2).
            if desynced_rntis(recent_stats_blocks(text)):
                release_zombie_contexts(cell, live[cell])
            continue
        running = episode_in_sitting()   # 2026-09-24: 판 전 대기는 판이 아니다
        # 2026-09-23 감사: HARQ 만으로는 판 도중 gNB 를 재기동하지 않는다.  재기동은 세 UE 를
        # 리셋하고 epoch 을 올려 그 판과 다음 판의 조종까지 잃는다.  깨진 UE 를 판 도중
        # 재부착하는 것도 안 된다(09-20 다섯 판 손실: 판 도중 UE 는 Kernel 소유).  판 경계까지
        # 미루고, 그때 그 RNTI 의 UE 를 찾으면 UE 만 재부착한다.
        if excess < STALE_RNTI_MARGIN:
            # 초과가 사라진 주기다: 초과 연속 횟수와 그 미룸은 여기서 끊긴다(판 769, 09:53 의
            # streak 2 가 HARQ 만 보인 다섯 주기를 건너 09:59 조종 직후 초과 1개와 이어져 3 이 됐다).
            if _stale_reason.get(cell) == 'excess':
                _stale_seen.pop(cell, None)
                _stale_reason.pop(cell, None)
            if _gnb_restart_deferred_cause.get(cell) == 'excess':
                _clear_context_deferral(cell)
            if running:
                log('STALE_CONTEXTS_DEFERRED', cell=cell, reason='harq', brokenHarq=broken,
                    note='HARQ 만으로는 판 도중 재기동하지 않는다; 판 경계까지 미룬다')
                continue
            persists = False
            for rnti, _ratio in broken:
                host = _host_of_rnti(rnti)
                if host is not None:
                    _stale_seen.pop(cell, None)
                    # 2026-09-24 23:07: ue1 의 새 컨텍스트(570f)는 부하 20초 만에 다시 걸렸다 --
                    # 원인은 약한 하향에서의 DCI 놓침(UE 로그 `NDI indicates re-transmission but
                    # computed TBS` 수천 건)이라 재부착이 못 고치고 churn 만 만든다.
                    since = time.time() - _harq_reattached_at.get(host, 0.0)
                    if since < HARQ_REATTACH_COOLDOWN_S:
                        log('HARQ_BROKEN_PERSISTS_AFTER_REATTACH', cell=cell, rnti=rnti,
                            host=host, since_s=round(since), brokenHarq=broken)
                        persists = True
                        continue
                    _harq_reattached_at[host] = time.time()
                    log('HARQ_BROKEN_UE_REATTACH', cell=cell, rnti=rnti, host=host,
                        brokenHarq=broken)
                    restart_ue(host, evidence='harq-broken')
                    return
            if persists:
                continue
        reason = 'excess' if excess >= STALE_RNTI_MARGIN else 'harq'
        # 2026-09-24 22:13: a broken-HARQ context no UE holds (ue1's retired 1176) restarted the
        # whole gnb2 and dropped ue1·ue3; releasing it alone is enough, whatever the reason.
        if release_zombie_contexts(cell, live[cell]):
            _stale_seen.pop(cell, None)       # 재기동 대신 컨텍스트만 지웠다; 다음 주기에 다시 본다
            _stale_reason.pop(cell, None)
            return
        if _stale_reason.get(cell) != reason:
            _stale_seen.pop(cell, None)       # 연속은 같은 사유끼리만 센다
        _stale_reason[cell] = reason
        _stale_seen[cell] = _stale_seen.get(cell, 0) + 1
        # 2026-09-24 03:42: 인내는 락을 쥔 누구에게나 적용한다(판 전 대기 포함).  03:1x 에
        # `running` 을 판 안(`cli`)으로 좁히자 판 전 대기에서는 첫 목격에 곧바로 재기동했고,
        # 방금 다시 붙은 ue1·ue3 가 남긴 일시적 초과 1개에 gnb2 를 내려 두 UE 를 잃었다.
        # 05:34~05:52: 락이 없는 판 사이에도 인내가 없어, UE 재부착 직후의 일시적 초과 1개에
        # gNB 를 재기동 → UE 이탈 → 재부착 → 또 초과 → 또 재기동, 고리를 돌았다.  인내는 늘 적용한다.
        if _stale_seen[cell] < STALE_PATIENCE:
            log('STALE_CONTEXTS_DEFERRED', cell=cell, reason=reason, excess=excess,
                brokenHarq=broken, streak=_stale_seen[cell], patience=STALE_PATIENCE)
            continue
        # 2026-09-23 감사: 예산은 restart_gnb* 가 이미 뺀다 -- 여기서 또 빼면 이중 차감이었다.
        log('STALE_CONTEXTS_FOUND', cell=cell, reason=reason, placed=placed, excess=excess,
            brokenHarq=broken, streak=_stale_seen.pop(cell, 0), episodeRunning=running)
        (restart_gnb1 if cell == 'gnb1' else restart_gnb2)(reason=reason)
        return          # 한 주기에 한 셀만


def _host_of_rnti(rnti: str):
    """그 RNTI 를 지금 쥔 UE 호스트, 모르면 None (2026-09-23).  UE 로그의 마지막
    `UE 0 RNTI xxxx stats` 를 읽는다 -- ue_zombie.zombie() 와 같은 원천."""
    try:
        sys.path.insert(0, str(HERE))
        import ue_zombie
    except ImportError:
        return None
    for host in HOSTS:
        try:
            r = ssh(host, ['bash', '-c', 'L=$(ls -t /home/*/ota-fixed38-%s-*.log | head -1); '
                                         'grep -aE "RNTI [0-9a-f]{4} stats" "$L" | tail -3' % host],
                    timeout=30)
        except Exception:  # noqa: BLE001 - 못 읽으면 모른다
            continue
        if ue_zombie.current_rnti(r.stdout or '') == rnti:
            return host
    return None


#: DNN `oai` 는 `12.1.1.128/26` 이고 **ue1·ue2 가 공유**한다(약 61개).  주소는 재사용되지
#: 않고 단조 증가하므로 재부착이 잦으면 약 2시간이면 마른다 -- 마르면 UE 가
#: `Registration Accept` 까지 가고도 **PDU 세션만** 못 받아 tun 주소가 안 나온다
#: (2026-09-22 실측: ue1 이 `.179` 를 쥐어 11개만 남았고 ue2 가 못 붙었다).
POOL_TOP = 190                  # `oai` 풀의 마지막 쓸 수 있는 주소 12.1.1.190
POOL_LOW_WATER = 12             # 이만큼 밖에 안 남으면 할당기를 되돌린다


def pool_headroom(addresses) -> int:
    """`oai` 풀에 남은 주소 수.  가장 높이 쓴 주소가 기준이다(재사용되지 않으므로)."""
    used = []
    for a in addresses:
        try:
            last = int(str(a).split('/')[0].rsplit('.', 1)[1])
        except (ValueError, IndexError):
            continue
        if 129 <= last <= POOL_TOP:      # `oai` 풀 안의 것만 (ue3 는 다른 풀)
            used.append(last)
    return POOL_TOP - max(used) if used else POOL_TOP - 128


#: 지금까지 본 가장 높은 `oai` 풀 주소 (2026-09-23).  풀이 **말랐을 때** ue1·ue2 는 주소를
#: 못 받아 tun 자체가 없다 -- 그러면 `pool_headroom([])` 가 "여유 62" 를 돌려주어, 가드가
#: 정작 필요한 순간에 절대 발동하지 않았다 (실측: ue1 마지막 `.187`, 이후 15분간 두 UE
#: no-dl, 코어 재기동 0회; 사람이 손으로 되돌렸다).  주소는 코어를 되돌리기 전까지 재사용되지
#: 않으므로 본 최댓값은 단조이고, 되돌린 뒤에만 지운다.
POOL_HIGH_WATER = OUT / 'oai-pool-high-water.json'


def _remembered_pool_high_water() -> list:
    try:
        return [str(json.loads(POOL_HIGH_WATER.read_text())['address'])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _remember_pool_high_water(addresses) -> None:
    best = None
    for a in addresses:
        try:
            last = int(str(a).split('/')[0].rsplit('.', 1)[1])
        except (ValueError, IndexError):
            continue
        if 129 <= last <= POOL_TOP and (best is None or last > best[0]):
            best = (last, str(a).split('/')[0])
    if best is not None:
        try:
            POOL_HIGH_WATER.write_text(json.dumps({'address': best[1]}))
        except OSError:
            pass


def ensure_address_pool() -> None:
    """`oai` 풀이 마르기 전에 SMF·UPF 를 되돌린다.

    판이 돌고 있으면 하지 않는다 -- 세션이 전부 끊긴다.
    """
    if episode_running():
        return
    addresses = []
    for host in HOSTS:
        try:
            r = ssh(host, ['ip', '-4', '-o', 'addr', 'show', 'up', 'dev', 'oaitun_ue1'], timeout=20)
        except OSError:
            continue
        for part in (r.stdout or '').split():
            if part.startswith('12.1.1.'):
                addresses.append(part)
    known = addresses + _remembered_pool_high_water()
    _remember_pool_high_water(known)
    headroom = pool_headroom(known)
    if headroom > POOL_LOW_WATER:
        return
    if not budget('core'):
        log('ALERT_ADDRESS_POOL', headroom=headroom,
            action='oai 풀이 마르는데 코어 재기동 예산이 없다')
        return
    log('ADDRESS_POOL_LOW', headroom=headroom, addresses=addresses)
    r = run(['docker', 'restart', 'oai-smf', 'oai-upf'], timeout=180)
    log('CORE_ALLOCATOR_RESET', rc=r.returncode, err=(r.stderr or '')[:160])
    if r.returncode == 0:
        POOL_HIGH_WATER.unlink(missing_ok=True)      # 할당기가 처음부터 다시 준다


def restart_gnb1(reason: str = 'radio') -> None:
    """Restart gNB1 locally.

    The keeper used to only raise an alert here, on the premise that stopping
    gNB1 needs PC1-local sudo.  That premise is wrong for a gNB1 this account
    started: the process is ours, so SIGTERM works, and 38 PRB comes up without
    RT priority (threadCreate falls back to default priority).  Verified
    2026-09-14.  A gNB1 the operator started under sudo is NOT ours -- SIGTERM
    fails, and that case still becomes an alert.
    """
    if _defer_gnb_restart('gnb1', reason):
        return
    if not budget('gnb1'):
        log('ALERT_BUDGET_EXHAUSTED', target='gnb1',
            action='gNB1 keeps failing to come up; check the X310 at 192.168.40.4 '
                   '(power, SFP cable) from the PC1 console')
        return
    for pid in gnb1_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except PermissionError:
            log('ALERT_GNB1_NOT_OURS', pid=pid,
                action=f'gNB1 pid {pid} was started by another account; stop it from '
                       f'the PC1 console, the keeper will start a fresh one')
            return
        except ProcessLookupError:
            pass
    for _ in range(25):
        if not gnb1_pids():
            break
        time.sleep(2)
    if gnb1_pids():
        log('GNB1_STOP_FAILED', pids=gnb1_pids())
        return
    # The X310 needs ~15 s to release; enumerating it is the real readiness test.
    ready = False
    for _ in range(4):
        r = run(['timeout', '50', '/usr/local/bin/uhd_usrp_probe',
                 '--args', 'addr=192.168.40.4'], timeout=60)
        if r.returncode == 0:
            ready = True
            break
        time.sleep(5)
    if not ready:
        log('ALERT_GNB1_USRP', action='the X310 at 192.168.40.4 does not enumerate; '
                                      'power-cycle it and check its SFP cable')
        return
    stamp = time.strftime('%Y%m%dT%H%M%S')
    out = GNB1_LOG_DIR / f'gnb1-loop38-{stamp}.log'
    env = dict(os.environ,
               OAI_RC_STYLE2_BASELINE_BOOTSTRAP='1:-:1:100:0',
               LD_LIBRARY_PATH=os.pathsep.join(
                   (str(GNB1_BIN.parent), str(GNB1_TELNET_LIB_DIR))))
    with out.open('wb') as handle:
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [str(GNB1_BIN), '-O', str(GNB1_CONF), '--telnetsrv',
             '--log_config.global_log_options', 'level,nocolor,time'],
            cwd=str(GNB1_ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    # 2026-09-23 감사: 재핀 빚은 옛 gNB 를 내리고 새것을 띄운 **이 순간** 생긴다 -- epoch 은
    # ready 여부와 무관하게 움직인다.  ready 일 때만 적던 것을 gnb2 와 맞춘다.
    global _gnb_restarted_at
    _gnb_restarted_at = time.time()
    gates = ('RU 0 RF started', 'Received NGSetupResponse from AMF',
             'E2 SETUP RESPONSE rx', # 2026-09-22: 반송파를 3400.32 -> 3349.92 MHz 로 옮겼는데 이 게이트만 옛 값이었다.
               # conf(GNB1_CONF)에서 읽어 쓰면 다시 어긋나지 않는다.
               _gnb1_expected_tx_banner())
    for _ in range(60):
        time.sleep(2)
        try:
            text = out.read_text('utf8', 'ignore')
        except OSError:
            continue
        if all(g in text for g in gates):
            log('GNB1_RESTART', log=str(out), ready=True)
            return
        if not gnb1_pids():
            break
    log('GNB1_RESTART', log=str(out), ready=False,
        tail=_gnb1_tail(2000)[-300:].replace('\n', ' '))


def cell_usable(cell: str) -> bool:
    """False when the cell a UE belongs to cannot serve it, so restarting that UE
    would only burn its budget."""
    if cell == 'gnb2':
        return not gnb2_stalled()
    return _gnb1_tail(120000).count('got 0 from USRP') < 5


def restart_gnb2(reason: str = 'radio') -> None:
    if _defer_gnb_restart('gnb2', reason):
        return
    if not budget('gnb2'):
        log('ALERT_BUDGET_EXHAUSTED', target='gnb2',
            action='gNB2 keeps losing its USRP stream; power-cycle the X310 at '
                   '192.168.30.2 and check its SFP cable')
        return
    script = ('sudo -n pkill -TERM -x nr-softmodem; '
              'for i in $(seq 1 15); do ps -C nr-softmodem -o pid= >/dev/null || break; sleep 2; done; '
              'ps -C nr-softmodem -o pid= >/dev/null && sudo -n pkill -KILL -x nr-softmodem; '
              'sleep 15; cd /tmp; '
              'L=/tmp/gnb2-probe-$(date +%Y%m%dT%H%M%S).log; '
              'nohup /opt/ran-lab/gnb2/oai-build-campaign5/cmake_targets/ran_build/build/nr-softmodem '
              '-O /opt/ran-lab/gnb2/recovery-data38-p0m54-20260911/gnb2.gpsdo-th120.conf '
              '--telnetsrv --log_config.global_log_options level,nocolor,time '
              '> "$L" 2>&1 < /dev/null & sleep 30; '
              # 2026-09-20: 오류 패턴을 세면 그 목록에 없는 실패를 성공으로 읽는다.
              # X310 관리 채널이 죽었을 때 사인은 'Failure to create rfnoc_graph' 였고
              # key_error 도 No USRP Device Found 도 0 건이라 startupErrors=0 이 나왔다.
              # 재시도조차 하지 않고 넘어갔다.  진짜 질문은 "떴나" 이므로 그것을 묻는다.
              'pgrep -x nr-softmodem >/dev/null && echo 0 || echo 1')
    r = ssh('enb2', ['bash', '-c', script], timeout=200)
    failed = (r.stdout or '0').strip().splitlines()[-1:] or ['0']
    # startupErrors: '0' 이면 softmodem 이 떠 있다, 그 외면 안 떠 있다(재시도 대상).
    global _gnb_restarted_at
    _gnb_restarted_at = time.time()
    log('GNB2_RESTART', rc=r.returncode, startupErrors=failed[0])
    # A transient uhd::key_error on the X310 clears on an identical retry.
    if failed[0] not in ('0', ''):
        r = ssh('enb2', ['bash', '-c', script], timeout=200)
        log('GNB2_RESTART_RETRY', rc=r.returncode,
            startupErrors=((r.stdout or '0').strip().splitlines()[-1:] or ['?'])[0])




# -- RIC and gate -----------------------------------------------------------

def ric_running() -> bool:
    r = run(['docker', 'inspect', 'oran-aic-nearrt-ric', '--format', '{{.State.Running}}'],
            timeout=30)
    return (r.stdout or '').strip() == 'true'


def start_ric() -> None:
    if not budget('ric'):
        log('ALERT_BUDGET_EXHAUSTED', target='ric',
            action='the near-RT RIC keeps exiting; check whether an xApp is being '
                   'SIGKILLed, which segfaults it')
        return
    r = run(['docker', 'start', 'oran-aic-nearrt-ric'], timeout=60)
    log('RIC_START', rc=r.returncode, err=(r.stderr or '').strip()[:160])


def gate_ages() -> dict:
    """Freshest KPM indication age per nb_id, using the conductor's own reader."""
    sys.path.insert(0, str(HERE))
    import importlib.util
    spec = importlib.util.spec_from_file_location('cond', HERE / 'conductor.py')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass
    return module.kpm_fresh(), list(module.GATE_RUN)


def _witness() -> dict:
    """The RIC's own record of which E2 nodes are attached, and at which epoch."""
    try:
        r = run(['docker', 'exec', 'oran-aic-nearrt-ric',
                 'cat', '/run/ai-ran/flexric-connection-witness.json'], timeout=30)
        return json.loads(r.stdout or '{}')
    except Exception:  # noqa: BLE001 - unreadable witness is simply not ready
        return {}


def _live_epochs() -> dict:
    """nbId -> connectionEpoch for every active node the witness lists."""
    return {c['globalE2NodeId']['nbId']: c['connectionEpoch']
            for c in _witness().get('connections', [])
            if c.get('active') and isinstance(c.get('connectionEpoch'), int)
            and isinstance(c.get('globalE2NodeId'), dict)}


def _both_nodes_registered() -> bool:
    """True when the FlexRIC witness lists both configured E2 nodes as active."""
    return set(CELL_NB.values()) <= set(_live_epochs())


CHAIN_REBUILD_COOLDOWN_S = 900.0
_last_chain_rebuild = 0.0


def rebuild_chain() -> bool:
    """RIC -> both gNBs -> gate -> re-pin, as one operation.

    A gNB that re-registers into a running RIC takes the RIC down with it: the
    witness publish appends a second entry for the node, fails its own
    invariant, and the process aborts.  Both cells drop and only gnb1 comes
    back by itself, so the chain cannot be repaired one piece at a time -- on
    2026-09-20/21 that cost three hours of hand work twice.  Rebuilding the
    whole chain is the recovery, and it is the same two scripts an operator
    would run by hand.  Never during an episode, and never twice in a quarter
    hour, so a bed that refuses to come up does not thrash.
    """
    global _last_chain_rebuild
    if episode_in_sitting():   # 2026-09-24: 판 안에서만 건너뛴다 (_defer_gnb_restart 와 같은 판정)
        log('CHAIN_REBUILD_SKIPPED', reason='episode holds the hardware')
        return False
    # 2026-09-23 감사: 벽시계로 -- keeper 재기동 뒤에도 냉각이 이어지도록 상태 파일에 남긴다.
    since = time.time() - _last_chain_rebuild
    if since < CHAIN_REBUILD_COOLDOWN_S:
        log('CHAIN_REBUILD_SKIPPED', reason='cooldown', since_s=round(since))
        return False
    _last_chain_rebuild = time.time()
    log('CHAIN_REBUILD_STARTED', nodes=sorted(_live_epochs()))
    try:
        up = run(['bash', str(HERE / 'bring_up_oran.sh')], timeout=900)
    except Exception as exc:  # noqa: BLE001 - the ledger carries the reason
        log('CHAIN_REBUILD_FAILED', stage='bring_up', error=f'{type(exc).__name__}: {exc}'[:160])
        return False
    if 'READY' not in (up.stdout or ''):
        log('CHAIN_REBUILD_FAILED', stage='bring_up', tail=(up.stdout or '')[-200:], stderr=(getattr(up, 'stderr', '') or '')[-400:])
        return False
    try:
        rp = run(['bash', '/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/'
                  'scripts/hardware/repin_a1p.sh'], timeout=1200)
    except Exception as exc:  # noqa: BLE001 - the ledger carries the reason
        log('CHAIN_REBUILD_FAILED', stage='repin', error=f'{type(exc).__name__}: {exc}'[:160])
        return False
    if rp.returncode != 0:
        # 2026-09-24: 이유는 stderr 로 나온다 -- stdout 만 남겨 21번 실패의 이유가 하루 종일 안 보였다.
        log('CHAIN_REBUILD_FAILED', stage='repin', rc=rp.returncode,
            tail=(rp.stdout or '')[-200:], stderr=(getattr(rp, 'stderr', '') or '')[-400:])
        return False
    log('CHAIN_REBUILD_DONE', nodes=sorted(_live_epochs()))
    return True


#: X310 은 **이더넷 링크 MTU 로 전송 패킷 크기를 정한다.**  MTU 9000 이면 1996 샘플,
#: 1500 이면 364 샘플 -- 패킷 수가 5.5배가 되고 그 타이밍 지터가 상향 Msg3·HARQ-ACK
#: 디코드를 깨뜨린다.  증상은 "gnb1 하향이 안 나온다" 로 보여서 2026-09-21 밤에 전력·
#: 감쇠·안테나·배치·코어를 전부 뒤지게 만들었다.  전부 무관했다.
#:
#: 2026-09-21 02:46 재부팅이 이 설정을 지웠고, 그 뒤 판이 한 판도 안 돌아 아무도
#: 되돌리지 않았다.  부팅은 `usrp-ens1-link.service` 가 막고, 그 밖의 이유로 풀리는
#: 것은 여기서 막는다.  판정은 **로그가 아니라 링크 자체**로 한다.
#: 러너가 하향을 재는 도구가 사는 곳.  `atomic_formal_run_guarded.py::SOURCE` 와 같은 값.
#: 이 디렉터리가 UE 에 없으면 판은 **뜨자마자** `SOURCE_MISSING:flow_goodput.py:<ue>:preflight`
#: 로 죽고, 러너의 부착 판정도 이 도구로 하므로 화면에는 `ue1=no-dl` 로 보인다 --
#: UE 고장으로 오인하기 딱 좋다.  2026-09-22 새벽에 정확히 그렇게 다섯 시간을 썼다:
#: 세 UE 의 `/tmp` 가 비워져 디렉터리째 사라졌는데 ext-dn 에만 남아 있었다.
FLOW_SOURCE_DIR = '/tmp/aic-flow-9140cea9b49e-beaa921ad26a'
FLOW_SOURCE_FILES = ('flow_goodput.py', 'tagged_echo.py')


def ensure_flow_sources() -> None:
    """세 UE 에 측정 도구가 있도록 지킨다.  기준본은 `oai-ext-dn` 의 것이다."""
    missing = []
    for host in sorted(CELL_OF):
        r = ssh(host, ['sh', '-c',
                       'ls %s 2>/dev/null | tr "\n" " "' % FLOW_SOURCE_DIR], timeout=25)
        have = set((r.stdout or '').split())
        if not set(FLOW_SOURCE_FILES) <= have:
            missing.append(host)
    if not missing:
        return
    log('FLOW_SOURCES_MISSING', hosts=missing, dir=FLOW_SOURCE_DIR)
    stage = '/tmp/aic-flow-stage'
    run(['rm', '-rf', stage], timeout=20)
    run(['mkdir', '-p', stage], timeout=20)
    for name in FLOW_SOURCE_FILES:
        c = run(['docker', 'cp', 'oai-ext-dn:%s/%s' % (FLOW_SOURCE_DIR, name),
                 '%s/%s' % (stage, name)], timeout=60)
        if c.returncode != 0:
            log('FLOW_SOURCES_STAGE_FAILED', file=name,
                err=(c.stderr or '').strip()[:120])
            return
    for host in missing:
        ssh(host, ['mkdir', '-p', FLOW_SOURCE_DIR], timeout=25)
        rc = run(['scp', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', '-q',
                  *['%s/%s' % (stage, n) for n in FLOW_SOURCE_FILES],
                  '%s:%s/' % (host, FLOW_SOURCE_DIR)], timeout=90)
        log('FLOW_SOURCES_RESTORED', host=host, rc=rc.returncode)


X310_LINKS = (('ens1', 9000),)


def ensure_x310_link() -> None:
    """X310 이 물린 인터페이스의 점보 프레임을 지킨다."""
    for name, want in X310_LINKS:
        r = run(['ip', '-o', 'link', 'show', name], timeout=20)
        text = (r.stdout or '')
        if not text:
            continue
        found = None
        for token in text.split():
            if found == 'mtu':
                found = token
                break
            if token == 'mtu':
                found = 'mtu'
        try:
            current = int(found)
        except (TypeError, ValueError):
            continue
        if current == want:
            continue
        log('X310_MTU_WRONG', link=name, found=current, want=want)
        # 2026-09-22: MTU 를 **돌고 있는 gNB 밑에서** 바꾸면 CHDR 스트림이 끊기고
        # `[xmit] tx samples 0 != 7680` 이 쏟아진다.  그날 내가 자가복구를 시험한다고
        # 정확히 그렇게 해서 베드를 30분 망가뜨렸다.  고치는 값은 옳아도 **고치는
        # 시점**이 틀리면 더 나쁘다.  판이 돌고 있으면 기록만 남기고 손대지 않는다.
        if episode_running():
            log('X310_MTU_DEFERRED', link=name, reason='episode holds the radio')
            continue
        fix = run(['sudo', '-n', 'ip', 'link', 'set', name, 'mtu', str(want)], timeout=20)
        log('X310_MTU_RESTORED', link=name, rc=fix.returncode,
            err=(fix.stderr or '').strip()[:100],
            note='gNB restart required: an MTU change breaks a live CHDR stream')
        if fix.returncode == 0 and name == 'ens1':
            restart_gnb1(reason='mtu')


CORE_CONTAINERS = ('mysql', 'oai-nrf', 'oai-udr', 'oai-udm', 'oai-ausf',
                   'oai-amf', 'oai-smf', 'oai-upf', 'oai-ext-dn')


def ensure_core() -> None:
    """Start any core container that is down, DB first.

    2026-09-21: the `mysql` container exited at ~01:30 and nothing restarted it.
    The UDR could not read subscription data, so the AMF answered every
    Registration Request with `Registration Reject: Illegal_UE` -- UEs synced,
    did RA and completed RRC setup, then never got an address.  The bed looked
    like a radio fault for hours.  The whole core ran with `restart: no`, so a
    single exit was permanent.  Now the policy is `unless-stopped` and this
    checks it every tick as well, because a policy only acts on *its* daemon's
    restarts, not on something stopped by hand.
    """
    for name in CORE_CONTAINERS:
        r = run(['docker', 'inspect', '-f', '{{.State.Running}}', name], timeout=30)
        if (r.stdout or '').strip() == 'true':
            continue
        log('CORE_DOWN', container=name)
        s = run(['docker', 'start', name], timeout=90)
        log('CORE_STARTED', container=name, rc=s.returncode,
            err=(s.stderr or '').strip()[:120])
        if name == 'mysql':
            time.sleep(15)          # UDR/UDM only read it once it answers


def ensure_episodes() -> None:
    """Put the episode service back if it was left stopped.

    Maintenance stops it; forgetting to start it again costs whole nights.
    The hold file is the deliberate way to keep it down.
    """
    if (HERE / 'overnight' / 'NO_EPISODES').exists():
        return
    r = run(['systemctl', '--user', 'is-active', 'aic-v31-episodes'], timeout=30)
    if (r.stdout or '').strip() == 'active':
        return
    log('EPISODES_DOWN', state=(r.stdout or '').strip())
    s = run(['systemctl', '--user', 'start', 'aic-v31-episodes'], timeout=60)
    log('EPISODES_STARTED', rc=s.returncode)


#: 판이 preflight 를 지나려면 이 포트들이 열려 있어야 한다.
#: 2026-09-21: R1 조종 프로듀서(18443)가 재부팅 뒤 아무도 안 살려 죽어 있었고,
#: 부착 게이트를 통과한 판이 전부 1.2초 만에 `DEPENDENCY_PREFLIGHT_REFUSED:R1Error`
#: 로 즉사했다. 그날 아침 내가 만든 `ops/preflight.sh` 는 컨테이너·서비스·UE 는 보는데
#: **프로듀서 포트는 안 봤다** — 막으려고 만든 점검에서 정작 그게 빠져 있었다.
#: (이름, 호스트, 포트, 작업디렉터리, 기동명령). 하나라도 닫히면 판이 preflight 에서 죽는다.
PRODUCERS = (
    ('R1조종', '192.168.50.1', 18443,
     '/opt/ran-lab/controller/oran-deploy/session-20260819',
     ['python3', 'tools/run_nonrt_https.py']),
    ('캠페인5액션', '192.168.50.1', 9445,
     '/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN',
     ['bash', 'scripts/hardware/run_campaign5_producer.sh']),
)

#: 캠페인5 프로듀서는 **맵이 없으면 gnb1 한 셀만** 등록한다
#: (`scripts/hardware/run_campaign5_producer.sh`: `--cell-id $HW_CAMPAIGN5_CELL_ID
#: --nb-id $HW_GNB1_NB_ID`).  지금 배치는 ue2·ue3 가 gnb2 에 있으므로, 맵을 안 주면
#: **세 UE 중 둘의 보조 축(cap·pfWeight·priority)이 조용히 죽는다** -- 2026-09-21 에
#: 내가 맨 스크립트로 띄워 정확히 그 상태였다.  상대 원장 경로는 맵 파일 기준으로 풀린다.
PRODUCER_ENV = {
    9445: {'HW_CAMPAIGN5_CELL_BINDINGS': str(HERE / 'cell-bindings.json')},
}


#: 프로듀서를 띄운 뒤 바인드까지 줄 시간.  주 루프는 20초마다 도는데 기동은 그보다
#: 오래 걸릴 수 있다(R1 은 계약 번들을 훑는다 -- 2026-09-21 실측 약 12초, 부하가 걸리면
#: 더).  기억해 두지 않으면 **같은 프로듀서를 두 번 띄워 포트를 두고 경쟁시킨다.**
PRODUCER_START_GRACE_S = 90
_producer_started_at: "dict[int, float]" = {}


def _port_is_open(host: str, port: int) -> bool:
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.settimeout(3.0)
        return sock.connect_ex((host, port)) == 0


#: R1 액세스 토큰은 **300초짜리 JWT** 다.  회전 데몬이 죽으면 파일이 그 자리에 남고,
#: 캡/가중치 프로듀서(9445)는 그것을 공유비밀로 받아들여 통과시키지만 **조종
#: 프로듀서(18443)는 JWT 를 제대로 검증해 401 로 거절한다.**  그래서 다른 축은 멀쩡한데
#: 조종만 실패하고, 증상이 "UE 가 핸드오버 명령에 죽는다" 와 구별이 안 된다.
R1_TOKEN = Path('/opt/ran-lab/controller/oran-deploy/session-20260819/secrets/r1/r1-access-token')
#: 토큰 수명 300초 - 갱신 여유 90초 = 210초마다 다시 쓰인다.  그 두 배면 확실히 고장이다.
R1_TOKEN_MAX_AGE_S = 420.0


def _r1_refresher_running() -> bool:
    """회전 데몬이 살아 있나.  `pkill -f` 류로 세면 내 셸까지 잡힌다."""
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            argv = (entry / 'cmdline').read_bytes()
        except OSError:
            continue
        if b'r1_token_refresher.py' in argv:
            return True
    return False


def ensure_r1_token_refresher() -> None:
    """R1 토큰을 신선하게 유지한다.

    2026-09-17 에 이 고장을 찾아 `overnight/token_refresher.sh` 를 만들어 두고도
    **그것을 살려 두는 장치를 안 만들었다.**  systemd 유닛도 아니고 여기서 보지도
    않았으므로, 09-21 02:46 호스트 재부팅에 조용히 사라졌다.  그 뒤 09-22 판들에서
    **조종 시행 15건이 전부** 같은 401 로 죽었다 --
    `R1Error: POST …/policies returned 401: R1 token is outside its valid time window`.
    한 건도 무선에 닿지 못했으므로 그 기간의 "조종은 효과가 없다" 는 판정은 근거가 없다.

    프로세스 유무와 **파일 나이**를 둘 다 본다: 데몬이 붙어만 있고 갱신을 못 하는 경우가
    프로세스 부재보다 알아채기 어렵다.  스크립트 자신이 flock 을 잡으므로 중복 기동은
    무해하다.
    """
    try:
        age = time.time() - R1_TOKEN.stat().st_mtime
    except OSError:
        age = float('inf')
    running = _r1_refresher_running()
    if running and age <= R1_TOKEN_MAX_AGE_S:
        return
    log('R1_TOKEN_REFRESHER_DOWN', running=running,
        tokenAgeS=(None if age == float('inf') else round(age, 1)))
    stamp = time.strftime('%Y%m%dT%H%M%S')
    out = HERE / 'overnight' / ('token-refresher-%s.log' % stamp)
    try:
        with open(out, 'ab') as handle:
            subprocess.Popen(['bash', str(HERE / 'overnight' / 'token_refresher.sh')],
                             cwd=str(HERE), stdout=handle, stderr=handle,
                             start_new_session=True)
        log('R1_TOKEN_REFRESHER_STARTED')
    except OSError as error:
        log('R1_TOKEN_REFRESHER_START_FAILED', error=str(error))


def ensure_producers() -> None:
    """프로듀서 포트를 살려 둔다.

    이것들이 없으면 판은 **뜨기는 뜨고 1초 만에 죽는다** — 부착 게이트는 통과하므로
    "UE 가 안 붙는다" 류의 증상이 전혀 안 보이고, 판 수만 빠르게 늘면서 전부
    `exit.json` 에 `DEPENDENCY_PREFLIGHT_REFUSED:R1Error` 를 남긴다.

    2026-09-21: 오너의 전원 재투입 뒤 18443·9445 를 아무도 안 살렸다. 그날 아침 내가
    만든 `ops/preflight.sh` 는 컨테이너·서비스·UE 는 보는데 **포트는 안 봤다** —
    막으려고 만든 점검에서 정작 그게 빠져 있었다.
    """
    for name, host, port, cwd, command in PRODUCERS:
        if _port_is_open(host, port):
            _producer_started_at.pop(port, None)
            continue
        waited = time.time() - _producer_started_at.get(port, 0.0)
        if waited < PRODUCER_START_GRACE_S:
            continue                 # 방금 띄웠다 -- 바인드를 기다리는 중이다
        log('PRODUCER_DOWN', producer=name, host=host, port=port,
            waitedS=round(waited, 1) if port in _producer_started_at else None)
        stamp = time.strftime('%Y%m%dT%H%M%S')
        out = HERE / 'overnight' / ('producer-%s-%s.log' % (port, stamp))
        try:
            with open(out, 'ab') as handle:
                subprocess.Popen(command, cwd=cwd, stdout=handle, stderr=handle,
                                 env=dict(os.environ, **PRODUCER_ENV.get(port, {})),
                                 start_new_session=True)
        except OSError as error:
            log('PRODUCER_START_FAILED', producer=name, error=str(error))
            continue
        _producer_started_at[port] = time.time()
        log('PRODUCER_STARTED', producer=name, log=out.name)


#: 판이 이 시간 넘게 안 나오면 생산이 멈춘 것이다. 판 하나는 보통 10~16분.
#: 2026-09-23 오너 지시로 45 → 20분.  느린 판(16분)에 준비 대기가 겹치면 문턱에 닿을 수 있으나,
#: 조치 뒤 냉각(BOARD_ACTION_COOLDOWN_S)이 연달아 치는 것을 막는다.
BOARD_STALL_S = 20 * 60
#: 재기동을 이 횟수 시도하고도 안 나오면 사람을 불러야 한다.
BOARD_ESCALATIONS = 3
#: 재기동 뒤 판 하나가 나오려면 부착 게이트 + 판 진행으로 최소 이만큼 걸린다.
#: 이 냉각이 없으면 keeper 주기마다 재기동을 걸어 세 번을 90초에 태운다.
BOARD_ACTION_COOLDOWN_S = 20 * 60
_board_strikes = 0
_board_acted_at = 0.0
#: 이 시각 이후로만 판 생산을 판정한다(keeper 기동·보류 해제 시 갱신).
_board_watch_from = time.time()


def _newest_board_age_s() -> float:
    """가장 최근에 **증거를 남긴** 판의 나이(초). 하나도 없으면 무한대.

    디렉터리 mtime 을 보면 안 된다: 판 디렉터리는 **preflight 보다 먼저** 만들어지므로,
    의존성이 죽어 1.2초 만에 거절되는 판도 계속 새 디렉터리를 남긴다 -- 2026-09-21 저녁에
    그런 판이 1분마다 생기는 동안 "생산은 정상" 으로 보였다.  판이 실제로 돌았다는 증거는
    `evidence/` 안의 파일이다(거절된 판은 그 디렉터리가 비어 있다).
    """
    newest = 0.0
    try:
        for name in _os.listdir(HERE.parent):
            if not name.startswith('formal38guarded-'):
                continue
            evidence = HERE.parent / name / 'evidence'
            try:
                stamps = [entry.stat().st_mtime for entry in _os.scandir(evidence)]
            except OSError:
                continue
            if stamps:
                newest = max(newest, max(stamps))
    except OSError:
        return 0.0
    return float('inf') if newest == 0.0 else time.time() - newest


def ensure_boards_are_being_produced() -> None:
    """판이 나오고 있는지 본다 -- 서비스가 살아 있는 것과 다른 질문이다.

    2026-09-21: 에피소드 서비스는 17시간 내내 `active` 였고 시도를 54번 했는데 판은
    **0개**였다. `ensure_episodes()` 는 서비스가 살아 있으니 아무 일도 하지 않았다.
    살아 있는 것과 산출물을 내는 것은 다른 질문이고, 아무도 뒤쪽을 묻지 않았다.

    멈춰 있으면 시도를 한 번 갈아 끼우고(서비스 재기동), 그래도 안 되면 사람을 부른다.
    """
    global _board_strikes, _board_watch_from
    if (HERE / 'overnight' / 'NO_EPISODES').exists():
        # 보류 중에는 판이 안 나오는 게 정상이고, 해제 직후에도 **첫 판이 나올 시간**을
        # 줘야 한다.  2026-09-21: 이 시계가 없으면 두 시간 쉬었다 재개하는 순간 오래된
        # 판 나이 때문에 곧바로 에피소드 서비스를 재기동한다.
        _board_strikes = 0
        _board_watch_from = time.time()
        return
    if time.time() - _board_watch_from < BOARD_STALL_S:
        return
    if episode_running():
        # 판이 하드웨어를 쥐고 있으면 생산은 진행 중이다.  여기서 서비스를 재기동하면
        # 바로 그 판을 죽인다.
        return
    age = _newest_board_age_s()
    if age < BOARD_STALL_S:
        if _board_strikes:
            log('BOARDS_RECOVERED', ageMin=round(age / 60, 1), strikes=_board_strikes)
        _board_strikes = 0
        return
    global _board_acted_at
    if time.time() - _board_acted_at < BOARD_ACTION_COOLDOWN_S:
        return                      # 앞선 조치에 아직 시간을 주는 중이다.
    _board_strikes += 1
    _board_acted_at = time.time()
    log('BOARDS_STALLED', ageMin=round(age / 60, 1), strike=_board_strikes)
    if _board_strikes <= BOARD_ESCALATIONS:
        r = run(['systemctl', '--user', 'restart', 'aic-v31-episodes'], timeout=90)
        log('BOARDS_STALL_RESTART', rc=r.returncode, strike=_board_strikes)
        return
    # 재기동으로 안 되는 것은 내가 못 고치는 것이다. 흔적을 남겨 사람이 보게 한다.
    # "판이 안 나온다" 만 적으면 읽는 사람이 처음부터 다시 조사해야 한다.  막힌 자리를
    # 같이 적는다 -- 게이트가 기다리는 대상과 프로듀서 포트가 거의 항상 답이다.
    lines = ['판이 %.0f분째 안 나온다. 에피소드 서비스 재기동 %d회로 안 풀렸다.'
             % (age / 60, BOARD_ESCALATIONS),
             '마지막 확인 %s' % time.strftime('%Y-%m-%d %H:%M:%S'), '']
    try:
        logs = sorted((HERE / 'overnight').glob('v31-P1-L8-attempt*.log'),
                      key=lambda p: p.stat().st_mtime)
        if logs:
            tail = logs[-1].read_text('utf8', 'replace').splitlines()
            waiting = [t for t in tail if 'waiting:' in t]
            lines.append('게이트가 기다리는 것: ' + (waiting[-1].strip() if waiting
                                                    else (tail[-1].strip() if tail else '(로그 비어 있음)')))
    except OSError:
        pass
    for name, host, port, _cwd, _cmd in PRODUCERS:
        lines.append('%s %s:%d %s' % (name, host, port,
                                      '열림' if _port_is_open(host, port) else '**닫힘**'))
    lines += ['', '다음에 볼 것: 위 줄에서 no-dl 인 UE 가 어느 셀에 붙는지 보고',
              '그 셀의 **어제 로그와** 비교하라 (옆 셀과 비교하면 차이만 보이고 변화는 안 보인다).',
              '  ls -t ~/gnb1-loop38-*.log | head -3',
              "  grep -aoE 'I0 [0-9]+' <로그> | grep -oE '[0-9]+$' | sort -n   # 0.1 dB 단위"]
    flag = HERE / 'overnight' / 'BOARDS-STALLED'
    try:
        flag.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    except OSError:
        pass
    log('BOARDS_STALL_NEEDS_A_HAND', ageMin=round(age / 60, 1))


GNB1_N3_ADDR = '192.168.70.140/26'          # keeper 주석 참조: gnb2 와 conntrack 이 겹치지 않게 전용
GNB1_N3_DEV = 'demo-oai'


def ensure_gnb1_n3() -> None:
    """gNB1 의 N3 주소가 브리지에 없으면 붙인다.

    2026-09-21: 재부팅으로 `192.168.70.140` 이 사라졌다.  gNB1 은 기동은 하지만
    `bind: Cannot assign requested address` 로 GTP-U 인스턴스를 못 만들고, 첫 PDU
    세션에서 `Unable to create GTP-U tunnel for N3` assert 로 죽는다.  무선은 멀쩡한데
    UE 가 주소를 못 받는 모양이라 라디오 고장으로 오인하기 쉽다.  PC1 은 sudo 가 없으므로
    NET_ADMIN 을 가진 컨테이너로 붙인다.
    """
    r = run(['ip', '-br', 'addr', 'show', GNB1_N3_DEV], timeout=30)
    if GNB1_N3_ADDR.split('/')[0] in (r.stdout or ''):
        return
    log('GNB1_N3_MISSING', dev=GNB1_N3_DEV, addr=GNB1_N3_ADDR)
    a = run(['docker', 'run', '--rm', '--network', 'host', '--cap-add', 'NET_ADMIN',
             'busybox', 'ip', 'addr', 'add', GNB1_N3_ADDR, 'dev', GNB1_N3_DEV], timeout=90)
    log('GNB1_N3_ADDED', rc=a.returncode, err=(a.stderr or '').strip()[:120])


GNB2_N3_SRC = '192.168.50.2/32'
UPF_N3_ADDR = '192.168.70.134/32'


#: PC1 에는 sudo 가 없어 호스트 netfilter 는 NET_ADMIN 컨테이너로만 만진다.  예전에는
#: 여기에 `ubuntu:22.04` 를 쓰고 매번 `apt-get install iptables` 를 했다 -- 규칙 하나를
#: **읽기만** 하는 검사인데 주기마다 네트워크 설치가 붙어, CADENCE 10초짜리 루프가 한
#: 바퀴에 몇 분씩 걸렸다.  2026-09-23 00:12 keeper 가 gnb2 를 재기동해 ue1·ue3 가 떨어졌고
#: ue3 는 돌아오지 못했는데, keeper 는 **23분간 이 컨테이너 안에 있느라** 그것을 보지도
#: 못했다(그 사이 판은 SOURCE_INTERFACE_DOWN:ue3 로 거절).  UPF 이미지는 이미 디스크에
#: 있고 iptables 를 갖고 있다 -- 같은 검사가 0.14초다.
_NETADMIN_SH = ('docker', 'run', '--rm', '--network', 'host', '--privileged',
                '--entrypoint', 'sh', 'oaisoftwarealliance/oai-upf:develop', '-c')


def ensure_gnb2_upf_path() -> None:
    """gNB2 는 다른 호스트라 UPF 로 가는 raw 예외가 필요하다.

    2026-09-21: PC1 의 **raw 테이블** PREROUTING 에
    `-d 192.168.70.134/32 ! -i demo-oai -j DROP` 가 있어, enb2 에서 오는 GTP-U 가
    UPF 에 **한 패킷도** 닿지 않았다.  raw 는 filter·nat 보다 먼저라 iptables 필터와
    라우팅과 MASQUERADE 를 아무리 봐도 안 보인다(그걸 찾느라 몇 시간을 썼다).
    증상은 "UE 가 붙고 주소도 받는데 사용자 평면이 안 통한다" 이다 -- gnb1 은 UPF 와
    같은 호스트라 멀쩡해서 셀 간 비대칭으로 보인다.  PC1 은 sudo 가 없으므로
    NET_ADMIN 컨테이너로 넣는다.
    """
    chk = run([*_NETADMIN_SH,
               f'iptables -t raw -C PREROUTING -s {GNB2_N3_SRC} -d {UPF_N3_ADDR} -j ACCEPT '
               '2>/dev/null && echo present || echo missing'], timeout=60)
    if 'present' in (chk.stdout or ''):
        return
    log('GNB2_UPF_PATH_MISSING')
    a = run([*_NETADMIN_SH,
             f'iptables -t raw -I PREROUTING 1 -s {GNB2_N3_SRC} -d {UPF_N3_ADDR} -j ACCEPT'],
            timeout=60)
    log('GNB2_UPF_PATH_ADDED', rc=a.returncode)


def refresh_cost_ledger() -> None:
    """Regenerate the per-episode ledgers from the evidence.

    Two of them: what each arm **spent** (llm_cost_ledger) and what it **got**
    (results_ledger -- concession depth, moves, whether T0 held).  The second
    is the comparison the paper is about; the first is its price.

    Derived files only -- both read episode records and write TSVs beside them.
    A failure here must never stop the keeper watching the radio, so it is
    logged and swallowed rather than raised.
    """
    for script, event in (('llm_cost_ledger.py', 'COST_LEDGER_FAILED'),
                          ('results_ledger.py', 'RESULTS_LEDGER_FAILED')):
        try:
            r = run([sys.executable, str(HERE / script)], timeout=120)
            if r.returncode != 0:
                log(event, rc=r.returncode, err=(r.stderr or '').strip()[:160])
        except Exception as exc:  # noqa: BLE001 - a ledger is not worth a crash
            log(event, error=f'{type(exc).__name__}: {exc}'[:160])


def sync_binding_epochs() -> None:
    """Re-pin the deployment binding to the epochs the nodes are actually at.

    Every gNB restart raises that node's connectionEpoch.  The launch preflight
    (`kpm_dependencies`) discards any KPM indication whose epoch differs from
    the binding's `expectedEpochs`, so a single restart makes that cell's
    telemetry invisible and every episode is refused with
    FRESH_TWO_CELL_KPM_REQUIRED -- which is exactly what burned blocks 81-91 on
    2026-09-14 after this keeper restarted gNB2.  The witness is the authority
    on the current epoch, so the binding follows it rather than the operator.

    This is deliberately narrower than scripts/hardware/repin_a1p.sh: that
    rebuilds the capability manifest and the E2 inventory, which are only stale
    when the gNB's RAN function *definitions* changed.  A plain restart changes
    nothing but the epoch.
    """
    # Never under a running sitting: rewriting the binding beneath a live trial would
    # change what that trial was launched from.  The readiness gate is a different
    # phase -- no trial exists yet -- and refusing there deadlocked the bed on
    # 2026-09-16: gNB2 exited on an OAI assertion, its restart took epoch 2816 from
    # 809 to 812, and attempts 121, 122 and 123 were each refused with
    # FRESH_TWO_CELL_KPM_REQUIRED, the one condition this repin clears, while their
    # own lock was what kept the repin from running.
    if episode_in_sitting():
        return
    live = _live_epochs()
    if not set(CELL_NB.values()) <= set(live):
        return                       # not both attached; nothing trustworthy to pin to
    try:
        doc = json.loads(BINDING.read_text())
        epochs = doc['kpm']['expectedEpochs']
    except Exception as exc:  # noqa: BLE001 - the ledger carries the reason
        log('BINDING_UNREADABLE', error=f'{type(exc).__name__}: {exc}'[:160])
        return
    changed = {}
    for node in list(epochs):
        for nb in live:
            if f'nb={nb:010d}' in node and epochs[node] != live[nb]:
                changed[node] = (epochs[node], live[nb])
                epochs[node] = live[nb]
    if not changed:
        return
    # Replace atomically: the conductor reads this file at episode start and a
    # torn read would refuse the episode for a different, misleading reason.
    tmp = BINDING.with_suffix(BINDING.suffix + f'.repin-{os.getpid()}')
    tmp.write_text(json.dumps(doc, indent=2) + '\n')
    os.replace(tmp, BINDING)
    log('BINDING_EPOCH_REPINNED',
        changed={node.split(';')[2]: {'was': was, 'now': now}
                 for node, (was, now) in changed.items()})


def _gate_pinned_epochs() -> dict:
    """게이트 컨테이너가 **지금 핀하고 있는** nbId -> epoch.

    env 는 순서 있는 두 목록(`KPM_GATE_TOPOLOGY`, `KPM_GATE_CONNECTION_EPOCHS`)으로만
    말한다.  순서로 짝짓는 것은 전에 두 번 틀린 방식이므로, topology 항목 자신이 들고 있는
    gNB_ID(16진)를 파싱해 nb 를 얻고 **개수가 맞을 때만** 짝짓는다.
    """
    try:
        r = run(['docker', 'inspect', 'oran-aic-kpm-gate',
                 '--format', '{{range .Config.Env}}{{println .}}{{end}}'], timeout=30)
    except Exception:  # noqa: BLE001
        return {}
    env = dict(line.split('=', 1) for line in (r.stdout or '').splitlines() if '=' in line)
    topology = [item for item in (env.get('KPM_GATE_TOPOLOGY') or '').split(',') if item.strip()]
    epochs = [item.strip() for item in (env.get('KPM_GATE_CONNECTION_EPOCHS') or '').split(',')
              if item.strip()]
    nbs = []
    for item in topology:
        found = re.search(r'-0x([0-9a-fA-F]+)-', item)
        if not found:
            return {}
        nbs.append(int(found.group(1), 16))
    if len(nbs) != len(epochs):
        return {}
    try:
        return {nb: int(epoch) for nb, epoch in zip(nbs, epochs)}
    except ValueError:
        return {}


def _gate_epoch_drift() -> dict:
    """nbId -> (핀된 epoch, live epoch) -- 둘이 다른 노드만."""
    pinned, live = _gate_pinned_epochs(), _live_epochs()
    return {nb: [pinned[nb], live[nb]] for nb in pinned
            if nb in live and pinned[nb] != live[nb]}


#: A1-P 프로듀서가 인벤토리 불일치로 기동을 거부했을 때 남기는 줄.
A1P_STALE_MARK = 'invalid live xApp binding'
_a1p_repin_at = 0.0
A1P_REPIN_COOLDOWN_S = 600.0

#: 우리가 gNB 를 재기동한 시각.  재기동은 E2 연결 epoch 을 올리고, 올라간 epoch 은
#: A1-P 인벤토리와 어긋나며, 어긋난 인벤토리는 조종을 `AIC_E2_NOT_READY` 로 세운다.
#: 재핀은 지금까지 판 사이에만 돌아서(`if not busy:`) 판 도중 재기동이 나면 그 판은
#: 물론 **다음 판까지** 조종을 잃었다.  강제 재기동은 세 UE 를 이미 리셋하므로 그
#: 판은 그 순간 희생된 것이고, 지켜야 할 것이 남아 있지 않다 -- 미루면 손해만
#: 다음 판으로 넘어간다 (2026-09-23).
_gnb_restarted_at: float = 0.0

#: gNB 재기동 **직후** 재핀하면 롤백된다 -- 22초 뒤에는 `KPM gate did not report both
#: nodes` 로 실패했고 5분 19초 뒤에는 통과했다.  KPM 게이트가 두 노드를 다시 볼
#: 시간을 준다 ([[producer-inventory-epoch-fails-silently]]).
A1P_REPIN_AFTER_GNB_S = 330.0


A1P_INVENTORY = Path('/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live'
                     '/e2-capability-inventory.json')


def _inventory_epochs() -> dict:
    """A1-P 인벤토리가 **지금 들고 있는** nbId -> epoch.

    같은 노드를 witness 와 **다른 철자**로 적는다.  witness 는
    `globalE2NodeId.nbId` 로 정수를 주는데, 인벤토리는
    `globalE2NodeId.nodeId.hex` 로 `"0x00000e00"` 을 준다 (= 3584).  그래서 여기서
    16진을 풀어 witness 쪽과 같은 정수로 맞춘다 -- 두 목록을 이름으로 짝지으려면
    먼저 이름을 같은 말로 옮겨야 한다.
    """
    try:
        document = json.loads(A1P_INVENTORY.read_text())
    except Exception:  # noqa: BLE001 - 못 읽으면 판정하지 않는다
        return {}
    found: dict = {}
    for connection in (document.get('connections') or []):
        if not isinstance(connection, dict) or not connection.get('active'):
            continue
        epoch = connection.get('connectionEpoch')
        node = ((connection.get('globalE2NodeId') or {}).get('nodeId') or {})
        text = node.get('hex')
        if not isinstance(epoch, int) or not isinstance(text, str):
            continue
        try:
            found[int(text, 16)] = epoch
        except ValueError:
            continue
    return found


def _inventory_drift() -> dict:
    """nbId -> [인벤토리 epoch, live epoch] -- 둘이 다른 노드만."""
    pinned, live = _inventory_epochs(), _live_epochs()
    return {nb: [pinned[nb], live[nb]] for nb in pinned
            if nb in live and pinned[nb] != live[nb]}


def repay_repin_owed_after_gnb_restart() -> None:
    """우리가 gNB 를 재기동했으면 판 중이어도 인벤토리를 다시 핀다.

    `ensure_a1p_inventory` 는 판 사이에만 돌지만, epoch 을 움직인 것이 **우리**일 때는
    기다릴 이유가 없다: 강제 재기동은 세 UE 를 이미 리셋해 그 판을 희생시켰고, 미루면
    다음 판까지 조종을 잃는다.  2026-09-23 04:56 의 gnb1 재기동이 epoch 774 -> 776 을
    만들었고, 20:04 의 조종 정책은 생성 62 ms 뒤 `AIC_E2_NOT_READY` 로 거절됐다.

    재핀 함수 자체가 쿨다운·예산·drift 판정을 들고 있어 반복 호출이 안전하다.
    드리프트가 닫히면 빚이 사라진다.
    """
    global _gnb_restarted_at
    if not _gnb_restarted_at:
        return
    if time.time() - _gnb_restarted_at < A1P_REPIN_AFTER_GNB_S:
        return
    if not _inventory_drift():
        _gnb_restarted_at = 0.0
        return
    log('A1P_REPIN_OWED_AFTER_GNB_RESTART',
        sinceS=round(time.time() - _gnb_restarted_at),
        drift={str(k): v for k, v in _inventory_drift().items()})
    ensure_a1p_inventory()
    if not _inventory_drift():
        _gnb_restarted_at = 0.0


def start_exited_a1p_producer() -> None:
    """Start an A1-P producer that exited with a current inventory, in every phase (2026-09-25).

    It exits when its xApp worker stops (08:31:30, mid-board 595); ensure_a1p_inventory() only runs
    between boards, so the producer stayed down 7 min and the board's UE rebind hit
    ConnectionRefused -> HARDWARE_UNAVAILABLE.  A plain start needs no repin and cannot disturb a board.
    """
    try:
        status = (run(['docker', 'inspect', '-f', '{{.State.Status}}', 'oran-aic-a1p-producer'],
                      timeout=30).stdout or '').strip()
        if status != 'exited' or _inventory_drift() or not budget('a1p'):
            return
        r = run(['docker', 'start', 'oran-aic-a1p-producer'], timeout=60)
        log('A1P_PRODUCER_STARTED', was=status, rc=r.returncode, inSitting=episode_in_sitting(),
            reason='exited with a current inventory (xApp worker stopped)')
    except Exception as exc:  # noqa: BLE001 - the next cycle tries again
        log('A1P_PRODUCER_START_FAILED', error=f'{type(exc).__name__}: {exc}'[:160])


def ensure_a1p_inventory() -> None:
    """A1-P 프로듀서가 낡은 인벤토리로 기동을 거부하면 재핀한다.

    **게이트 drift 와 다른 고장이다.**  게이트는 :func:`fix_gate` 가 매 주기 live epoch 으로
    다시 만들어 스스로 낫지만, A1-P 인벤토리·capability·binding 은 `repin_a1p.sh` 만 고친다.
    낡으면 프로듀서가 `refusing startup: invalid live xApp binding` 으로 죽고, 그러면
    R1(18443)이 A1-P(9444)로 못 내려보내 **조종 정책이 `AIC_E2_NOT_READY` 로 서고 gNB 에는
    아무 흔적도 안 남는다** -- 증상이 "조종은 원래 안 된다" 와 구별되지 않는다
    (2026-09-23, 조종 시행 15건이 그렇게 죽은 뒤 이 재핀으로 처음 성공했다).

    재핀은 프로듀서를 재기동하고 KPM 스트림을 회전시키므로 **판이 없을 때만** 한다.
    """
    global _a1p_repin_at
    # **프로세스 상태로 판정하면 안 된다.**  2026-09-23 02:36 에 gnb2 epoch 이 762->766 으로
    # 올랐는데 프로듀서는 02:23 에 뜬 채 계속 `running` 이었다 -- 거부는 기동할 때만 하기
    # 때문이다.  게이트는 keeper 가 맞춰 줘서 KPM 은 멀쩡했고, **조종 쓰기만 조용히 실패해**
    # v4.4 판 셋이 전부 그 시행에서 `PARTIAL_APPLY 9/9 acknowledged (readback confirmed only
    # 2)` 로 죽었다.  판정의 근거는 인벤토리가 든 epoch 그 자체다.
    drift = _inventory_drift()
    try:
        r = run(['docker', 'inspect', '-f', '{{.State.Status}}', 'oran-aic-a1p-producer'],
                timeout=30)
        status = (r.stdout or '').strip()
    except Exception:  # noqa: BLE001
        return
    if not drift:
        if status == 'running':
            return
        try:
            logs = run(['docker', 'logs', '--tail', '20', 'oran-aic-a1p-producer'], timeout=30)
            text = (logs.stdout or '') + (logs.stderr or '')
        except Exception:  # noqa: BLE001
            text = ''
        if A1P_STALE_MARK not in text:
            # 2026-09-25 00:52: the producer exits when its xApp worker stops at a board's end
            # ('xApp worker stopped'), with the inventory still current.  Nobody started it again,
            # port 9444 stayed closed and the next board sat in wait_for_bed() for 10 min.  The
            # container is intact, so start it as it is.
            if status == 'exited' and budget('a1p'):
                r = run(['docker', 'start', 'oran-aic-a1p-producer'], timeout=60)
                log('A1P_PRODUCER_STARTED', was=status, rc=r.returncode,
                    reason='exited with a current inventory (xApp worker stopped)')
                return
            log('A1P_PRODUCER_DOWN', status=status, reason='not the stale-inventory signature')
            return
    if time.time() - _a1p_repin_at < A1P_REPIN_COOLDOWN_S:
        return
    if not budget('a1p'):
        log('ALERT_BUDGET_EXHAUSTED', target='a1p',
            action='steering will keep failing with AIC_E2_NOT_READY until the '
                   'A1-P inventory is re-pinned by hand (scripts/hardware/repin_a1p.sh)')
        return
    _a1p_repin_at = time.time()
    log('A1P_INVENTORY_STALE', producer=status,
        inventoryDrift={str(k): v for k, v in drift.items()},
        gateDrift={str(k): v for k, v in _gate_epoch_drift().items()},
        live={str(k): v for k, v in _live_epochs().items()})
    stamp = time.strftime('%Y%m%dT%H%M%S')
    out = HERE / 'overnight' / ('repin-a1p-%s.log' % stamp)
    try:
        repo = HERE.parent.parent.parent
        with open(out, 'ab') as handle:
            # **레포 루트에서 돌려야 한다.**  repin_a1p.sh 는 `deployment/...` 를 상대경로로
            # 찾으므로 ops/ 에서 부르면 rc=1 로 죽는다 -- 같은 실패가 이미
            # `ops/gnb1_loglevel_between.sh:15` 에 적혀 있었고(22:14 rc=1 from ops/),
            # 2026-09-23 03:45 에 이 자리에서 그대로 재현됐다.
            rc = subprocess.call(['bash', str(repo / 'scripts/hardware/repin_a1p.sh')],
                                 cwd=str(repo), stdout=handle, stderr=handle, timeout=900)
        log('A1P_REPINNED', rc=rc, log=out.name)
    except Exception as error:  # noqa: BLE001
        log('A1P_REPIN_FAILED', error=f'{type(error).__name__}: {error}'[:160])


def fix_gate(gate_run) -> None:
    # Restarting the gate cannot help while a cell is not registered with the
    # RIC: the gate only binds when it sees exactly the two configured E2 nodes.
    # On 2026-09-13 this loop ran six times in ten minutes against a gNB1 whose
    # radio was down, which is the same churn that segfaulted the RIC earlier.
    if not _both_nodes_registered():
        log('GATE_RESTART_SKIPPED',
            reason='the RIC does not list both E2 nodes yet; a gate restart cannot bind')
        return
    if not budget('gate'):
        log('ALERT_BUDGET_EXHAUSTED', target='gate',
            action='the KPM gate will not stay fresh on both nodes; check that both '
                   'gNBs are registered with the RIC before restarting it again')
        return
    # Stop politely first: 'docker rm -f' SIGKILLs the xApp and FlexRIC segfaults.
    # 2026-09-19: 'stop -t 10' 뒤 'rm -f' 가 RIC 을 죽였다 -- 01:25:38 이 자리의 게이트
    # 재시작과 같은 초에 RIC 이 재시작(RestartCount 1)했고, 두 gNB 가 E2 를 다시 맺어
    # epoch 이 1034/1028 → 1036/1037 로 올라 바인딩·게이트·a1p 인벤토리가 갈라졌다.
    # repin_a1p.sh 처럼 충분히 기다리고, 강제 제거는 하지 않는다.
    run(['docker', 'stop', '-t', '15', 'oran-aic-kpm-gate'], timeout=60)
    run(['docker', 'rm', 'oran-aic-kpm-gate'], timeout=60)
    r = run(gate_run, timeout=120)
    log('GATE_RESTART', rc=r.returncode, err=(r.stderr or '').strip()[:160])


# -- main -------------------------------------------------------------------

KPM_JSONL = Path('/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/a1-live-kpm.jsonl')
HEADER_FRESH_S = 10.0
_imsi_of: dict[str, str] = {}   # memory only; never logged or written


def _amf_of_host() -> dict[str, int]:
    """host -> current amfUeNgapId, joined through the AMF UE table (ue_host_map.py's join)."""
    import re
    out = run(['docker', 'logs', '--tail', '4000', 'oai-amf'], timeout=30)
    amf_of_imsi = {}
    for line in (out.stdout + out.stderr).splitlines():
        cells = [c.strip() for c in line.split('|')]
        ids = [c for c in cells if re.fullmatch(r'\d{15}', c)]
        hexes = [c for c in cells if re.fullmatch(r'0x[0-9A-Fa-f]+', c)]
        if ids and hexes:
            amf_of_imsi[ids[0]] = int(hexes[-1], 16)
    mapping = {}
    for host in HOSTS:
        if host not in _imsi_of:
            found = re.search(r'(\d{15})', ssh(host, ['sh', '-c',
                "grep -hoE 'imsi *= *\"[0-9]+\"' ~/ai-ran-stage/runtime/phase-b/nr-ue.conf | head -1"],
                timeout=20).stdout or '')
            if not found:
                continue
            _imsi_of[host] = found.group(1)
        if _imsi_of[host] in amf_of_imsi:
            mapping[host] = amf_of_imsi[_imsi_of[host]]
    return mapping


def refresh_control_headers() -> None:
    """Keep ~/rlive/<host>-hdr.env on each UE's current identity, for GUI and runner alike.

    The action producer addresses a cap/PF control through these files; stale ones
    make the control a silent no-op (2026-09-14).  The formal runner refreshes them
    only for its own attempt, so a GUI episode had nothing.  Newest fresh indication
    wins, which follows a handover's new node and ran_ue_id.
    """
    spec = importlib.util.spec_from_file_location('afrg', EXP / 'atomic_formal_run_guarded.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    wanted = {amf: host for host, amf in _amf_of_host().items()}
    with KPM_JSONL.open('rb') as handle:
        handle.seek(max(0, KPM_JSONL.stat().st_size - 400_000))
        lines = handle.read().decode('utf8', 'ignore').splitlines()[1:]
    now_us = time.time() * 1_000_000
    rows: dict[str, dict] = {}
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        at, nb, epoch = rec.get('recv_unix_us'), rec.get('nb_id'), rec.get('connection_epoch')
        if (rec.get('event') != 'kpm_indication' or nb not in CELL_NB.values()
                or type(at) is not int or type(epoch) is not int
                or not 0 <= (now_us - at) / 1e6 <= HEADER_FRESH_S):
            continue
        for ue in rec.get('ues') or ():
            host = wanted.get(ue.get('amf_ue_ngap_id'))
            guami = ue.get('guami')
            if (host is None or type(ue.get('ran_ue_id')) is not int or not isinstance(guami, dict)
                    or any(type(guami.get(k)) is not int for k, _ in runner._CONTROL_HEADER_GUAMI)):
                continue
            if host in rows and rows[host]['receivedUnixUs'] >= at:
                continue
            rows[host] = {'RC_HEADER_RRC_UE_ID': ue['ran_ue_id'],
                          'RC_HEADER_AMF_UE_NGAP_ID': ue['amf_ue_ngap_id'],
                          **{name: guami[k] for k, name in runner._CONTROL_HEADER_GUAMI},
                          'nbId': nb, 'connectionEpoch': epoch, 'receivedUnixUs': at}
    before = {host: _header_amf(host) for host in rows}
    changed = runner.write_control_headers(rows)
    if changed:
        log('CONTROL_HEADERS_REFRESHED', changed=changed)
        _record_rebinds({host: (before[host], row['RC_HEADER_AMF_UE_NGAP_ID'])
                         for host, row in rows.items() if host in changed})


#: Where the steering A1-P producer (container `/state/ue-rebinds.json`) learns that a
#: role re-registered.  Its readback keys observations by the policy's composition-time
#: AMF UE NGAP ID, so a UE that died mid-handover and came back under a new id was never
#: seen again: the episode sat in RECOVERY_PENDING for 13 min and every PUT/DELETE on
#: that policy was refused (v46r8 board 462, 2026-09-23).  The body keeps its id; only
#: the readback follows the role (the 2026-09-16 invariant).
UE_REBIND_MAP = Path('/opt/ran-lab/controller/oran-deploy/session-20260819/state/a1p/ue-rebinds.json')
UE_REBIND_KEEP = 200


def _header_amf(host: str):
    try:
        for line in (Path.home() / 'rlive' / f'{host}-hdr.env').read_text().splitlines():
            if line.startswith('RC_HEADER_AMF_UE_NGAP_ID='):
                return int(line.split('=', 1)[1].strip().strip("'\""))
    except (OSError, ValueError):
        pass
    return None


def _record_rebinds(pairs: dict) -> None:
    """Append ``from -> to`` for every role whose AMF UE NGAP ID changed; atomic rewrite."""
    from datetime import datetime, timezone
    fresh = [{'role': host, 'from': int(old), 'to': int(new),
              'at': datetime.now(timezone.utc).isoformat()}
             for host, (old, new) in sorted(pairs.items())
             if old is not None and new is not None and int(old) != int(new)]
    if not fresh:
        return
    try:
        document = json.loads(UE_REBIND_MAP.read_text())
        entries = list(document.get('rebinds') or [])
    except (OSError, ValueError, AttributeError):
        entries = []
    entries = (entries + fresh)[-UE_REBIND_KEEP:]
    try:
        tmp = UE_REBIND_MAP.with_name(f'.{UE_REBIND_MAP.name}.{os.getpid()}')
        tmp.write_text(json.dumps({'rebinds': entries}, sort_keys=True) + '\n')
        os.replace(tmp, UE_REBIND_MAP)
        log('UE_REBIND_RECORDED', rebinds=fresh)
    except OSError as exc:
        log('UE_REBIND_RECORD_FAILED', error=str(exc)[:160])


def publish(ready: bool, reason: str, detail: dict) -> None:
    """Write the single file the conductor reads.  The conductor never probes the
    deployment itself any more, so this is the whole contract between them."""
    payload = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'ready': ready,
               'reason': reason, **detail}
    tmp = READY.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + '\n')
    tmp.replace(READY)


def episode_running() -> bool:
    """True while somebody holds the hardware.  The keeper then only observes.

    The lock is deliberately forgiving about what is written in it: anyone who
    is about to touch the radio may take it, including a person working by hand,
    and the file may carry a note after the pid.  On 2026-09-13 a manual holder
    wrote "<pid> core-reset"; the old parser called int() on the whole line, got
    a ValueError, concluded no one held the lock, and restarted two UEs while
    the core was being reset.  A lock protocol that breaks when one side is
    slightly untidy is not a lock, so parse the first token and ignore the rest.
    """
    try:
        first = BUSY.read_text().split()[0]
    except (OSError, IndexError):
        return False
    try:
        if int(first) == os.getpid():   # the keeper's own restart holds it (low-MCS, 2026-09-27)
            return False
        os.kill(int(first), 0)
    except (OSError, ValueError):
        return False
    return True


#: keeper 판단 상태 (2026-09-23 감사).  재핀 빚·재기동 유예·좀비 연속 횟수·냉각 시각이
#: 메모리에만 있어, keeper 재기동 한 번이 빚을 지우고 유예·냉각을 0 으로 돌렸다.
KEEPER_STATE = OUT / 'keeper-state.json'
_STATE_SCALARS = ('_gnb_restarted_at', '_a1p_repin_at', '_board_acted_at', '_last_chain_rebuild')
_STATE_DICTS = ('_stale_seen', '_gnb_restart_deferred', '_stale_reason', '_gnb_restart_deferred_cause')


def _save_state() -> None:
    g = globals()
    document = {name: g[name] for name in _STATE_SCALARS + _STATE_DICTS}
    try:
        tmp = KEEPER_STATE.with_name(f'.{KEEPER_STATE.name}.{os.getpid()}')
        tmp.write_text(json.dumps(document, sort_keys=True) + '\n')
        os.replace(tmp, KEEPER_STATE)
    except OSError as exc:
        log('KEEPER_STATE_SAVE_FAILED', error=str(exc)[:160])


def _load_state() -> None:
    """깨졌거나 없으면 아무것도 복원하지 않는다 -- 기본값이 곧 '기억 없음' 이다."""
    try:
        document = json.loads(KEEPER_STATE.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(document, dict):
        return
    g = globals()
    for name in _STATE_SCALARS:
        if isinstance(document.get(name), (int, float)):
            g[name] = float(document[name])
    for name in _STATE_DICTS:
        if isinstance(document.get(name), dict):
            g[name].clear()
            g[name].update({str(k): int(v) for k, v in document[name].items()
                            if isinstance(v, (int, float))})


def cycle() -> None:
    """keeper 한 주기.  예외는 main() 이 기록하고 다음 주기로 넘어간다 (2026-09-23)."""
    if not ric_running():
        log('RIC_DOWN')
        start_ric()
        time.sleep(20)

    try:
        ages, gate_run = gate_ages()
        needed = set(CELL_NB.values())
        stale = [nb for nb in needed
                 if nb not in ages or ages[nb] > GATE_MAX_AGE]
        # 2026-09-19: 그 셀에 배치된 UE 가 **전부 직전 주기에 비정상**이면 KPM 공백은
        # 게이트 고장이 아니라 UE 부재다.  01:24 ue1(gnb2 의 유일한 UE)이 좀비로
        # 재시작되자 2816 기록이 끊겼고, 01:25:38 여기서 게이트를 재시작했다 --
        # 그 뒤 E2 번호가 1036/1037 로 올라 바인딩·게이트·a1p 인벤토리가 제각각이
        # 됐다.  UE 가 없는 셀 때문에 게이트를 흔들지 않는다.
        no_ue = [nb for nb in stale
                 if all(_streak.get(host, 0) > 0 for host, cell in CELL_OF.items()
                        if CELL_NB.get(cell) == nb)]
        # 2026-09-23: 이 면제가 **교착**을 만들었다.  02:17 에 gnb2 가 재기동돼
        # epoch 이 758->762 로 올랐는데 게이트는 750 에 핀돼 있었다.  그 셀이 KPM 에서
        # 통째로 사라지니 러너가 그 UE 들의 amfUeNgapId 를 못 찾아 부착 게이트에서
        # 겉돌았고, 그래서 UE 가 "비정상" 으로 보였고, 그래서 여기서 게이트 수리를
        # 건너뛰었다.  **면제의 근거가 면제 때문에 생긴 상태다.**  판이 43분간 0개였다.
        # 핀된 epoch 이 live 와 다르면 그것은 UE 부재가 아니라 epoch 문제이므로
        # 면제하지 않는다.
        if no_ue:
            drifted = _gate_epoch_drift()
            mismatched = [nb for nb in no_ue if nb in drifted]
            if mismatched:
                log('GATE_STALE_EPOCH_DRIFT', drift={str(k): v for k, v in drifted.items()},
                    refusedExemption=mismatched)
                no_ue = [nb for nb in no_ue if nb not in mismatched]
        if no_ue:
            log('GATE_STALE_NO_UE', ages={str(k): round(v, 1) for k, v in ages.items()},
                skipped=no_ue)
            stale = [nb for nb in stale if nb not in no_ue]
        # 2026-09-27 03:00: an 11 s gap on 2816 restarted the gate, the RIC then listed no E2 node
        # and the whole chain was rebuilt (both gNBs); 11 of the last 40 gate restarts were followed
        # by a CHAIN_DOWN within 3 min.  A gap must last two cycles in a row before the gate is touched.
        global _gate_stale_streak
        _gate_stale_streak = _gate_stale_streak + 1 if stale else 0
        if stale and _gate_stale_streak < GATE_STALE_STREAK:
            log('GATE_STALE_WAIT', ages={str(k): round(v, 1) for k, v in ages.items()},
                stale=stale, streak=_gate_stale_streak)
        elif stale:
            log('GATE_STALE', ages={str(k): round(v, 1) for k, v in ages.items()},
                stale=stale)
            _gate_stale_streak = 0
            fix_gate(gate_run)
    except Exception as exc:  # noqa: BLE001 - the ledger carries the reason
        log('GATE_CHECK_FAILED', error=f'{type(exc).__name__}: {exc}'[:160])

    # 코어·DB 가 꺼지면 무선은 멀쩡한데 UE 가 Illegal_UE 로 거절된다 (2026-09-21).
    try:
        ensure_core()
        ensure_x310_link()
        ensure_flow_sources()
        ensure_gnb1_n3()
        ensure_gnb2_upf_path()
        ensure_episodes()
        ensure_producers()
        ensure_r1_token_refresher()
        ensure_boards_are_being_produced()
    except Exception as exc:  # noqa: BLE001 - the ledger carries the reason
        log('CORE_CHECK_FAILED', error=f'{type(exc).__name__}: {exc}'[:160])

    # A gNB that re-registered into a running RIC takes the RIC with it;
    # what is left cannot be repaired a piece at a time (2026-09-21).
    if not _both_nodes_registered():
        log('CHAIN_DOWN', nodes=sorted(_live_epochs()))
        rebuild_chain()

    if gnb2_stalled():
        log('GNB2_RF_STALLED')
        restart_gnb2()
    if gnb1_stalled():
        log('GNB1_RF_STALLED')
        restart_gnb1()

    try:
        refresh_control_headers()
    except Exception as exc:  # noqa: BLE001 - a missed refresh keeps the last good header
        log('CONTROL_HEADERS_FAILED', error=f'{type(exc).__name__}: {exc}'[:160])

    busy = episode_running()
    reap_finished_board_workload()
    reap_stalled_board()
    keep_ues_warm()
    release_undecodable_ue_contexts()
    release_rlf_contexts()
    ensure_no_stale_contexts()
    ensure_gnb2_noise_floor()
    ensure_ue_frequency_sane()
    ensure_address_pool()
    ensure_ue_governors()
    # A gNB restart moves the connection epoch and the preflight then sees
    # only one cell, so this runs in every phase but the sitting; the guard
    # lives in sync_binding_epochs itself.
    sync_binding_epochs()
    repay_repin_owed_after_gnb_restart()
    start_exited_a1p_producer()
    if not busy:
        # Between episodes: clear any workload a manual probe left, or
        # the next episode is refused before it starts.
        # 2026-09-23 감사: busy 는 주기 첫머리 값이다 -- 재핀 직전에 다시 본다.
        if not episode_running():
            ensure_a1p_inventory()
            restore_cell_attenuation_between_boards()
        sweep_stale_workload()
        # The owner asked for the per-arm LLM latency and token counts
        # to be recorded for every episode as a matter of course.  The
        # numbers are already in each episode record; this regenerates
        # the readable ledger from them, so it is always current
        # without the runner having to write anything new.
        refresh_cost_ledger()
    snapshot: dict[str, dict] = {}
    for host in HOSTS:
        alive = ue_alive(host)
        address = ue_address(host) if alive else ''
        healthy = alive and downlink(host, address)
        snapshot[host] = {'alive': alive, 'tun': address or None,
                          'downlink': healthy, 'cell': CELL_OF[host]}
        if not busy:
            _frozen_since.pop(host, None)     # 2026-09-23 감사: 판 밖이면 동결 목격을 잊는다
        if busy:
            try:
                recover_during_episode(host, healthy)
            except Exception as exc:  # noqa: BLE001 - never fatal to the watch
                log('BUSY_RECOVERY_FAILED', host=host, error=f'{type(exc).__name__}: {exc}'[:160])
            continue
        if healthy:
            if _streak[host]:
                log('UE_RECOVERED', host=host, tun=address)
            _streak[host] = 0
            continue
        _streak[host] += 1
        log('UE_UNHEALTHY', host=host, alive=alive, tun=address or None,
            streak=_streak[host])
        if _streak[host] >= FAIL_STREAK and not cell_usable(CELL_OF[host]):
            if budget('cell-down:' + CELL_OF[host]):
                log('ALERT_CELL_DOWN', target=host, cell=CELL_OF[host],
                    action=f'{CELL_OF[host]} is not serving, so restarting {host} '
                           f'cannot help; recover the cell first')
            _streak[host] = 0
            continue
        if _streak[host] >= FAIL_STREAK:
            # A restart that hands back the SAME tunnel address and the
            # same dead downlink is not recovering anything - ue1 did
            # exactly that six times in a row on 2026-09-13 while the
            # core happily re-issued 12.1.1.133 each time.  Say so once
            # and stop burning the budget on it.
            if _last_address.get(host) == address and address:
                _futile[host] = _futile.get(host, 0) + 1
            else:
                _futile[host] = 0
            _last_address[host] = address
            if _futile.get(host, 0) >= 2:
                if _futile[host] == 2:
                    log('ALERT_RESTART_IS_FUTILE', target=host, tun=address,
                        action=f'{host} keeps coming back on {address} with no '
                               f'downlink, so restarting it changes nothing; the '
                               f'fault is between the UPF and its gNB, not in the UE')
                _streak[host] = 0
                continue
            restart_ue(host)
            _streak[host] = 0

    # Two hosts holding the SAME tunnel address is not a health problem
    # of either one: the core reissues a released UE's address to the
    # next attach, and then per-flow goodput cannot be attributed at
    # all.  The runner already refuses the episode for it
    # (DUPLICATE_UP_TUN_ADDRESS), so every trial burns ~9 s until the
    # duplicate is broken.  `_last_address` does not see this -- it
    # compares a host against its own previous address, not against the
    # other hosts.
    #
    # This has to run **after** the per-host loop, over the complete
    # snapshot, and must not reuse the name `host`: an earlier version
    # sat inside that loop and rebound it, which silently moved every
    # per-host judgement onto the wrong UE and left two of them
    # unwatched for fifteen minutes.
    if not busy:
        claimed: dict[str, list[str]] = {}
        for watched, row in snapshot.items():
            if row['tun']:
                claimed.setdefault(row['tun'], []).append(watched)
        for tun, sharing in claimed.items():
            if len(sharing) < 2:
                continue
            # Restart the one that is not carrying traffic, not the one
            # that is.
            loser = next((h for h in sharing
                          if not snapshot[h]['downlink']), sharing[-1])
            log('DUPLICATE_TUN_ADDRESS', tun=tun, hosts=sharing,
                restarting=loser)
            restart_ue(loser)
            _streak[loser] = 0

    try:
        ages, _ = gate_ages()
    except Exception:  # noqa: BLE001
        ages = {}
    bad = [h for h, s in snapshot.items() if not s['downlink']]
    gate_ok = (set(CELL_NB.values()) <= set(ages)
               and all(ages[nb] <= GATE_MAX_AGE for nb in CELL_NB.values()))
    nodes_ok = _both_nodes_registered()
    # `bad` (no verified downlink) is reported but does not gate the
    # campaign, for the same deadlock reason.
    ready = gate_ok and nodes_ok
    reason = ('' if ready else
              '; '.join(filter(None, [
                  f'no downlink on {", ".join(bad)}' if bad else '',
                  'kpm gate stale' if not gate_ok else '',
                  'the RIC does not list both E2 nodes' if not nodes_ok else ''])))
    # The case, not the watch list, sets the bar -- but the bar is what
    # the runner actually needs: every case UE attached with a tun up at
    # the same moment (its own gate is JOINT_UP_TUN_NOT_OBSERVED).
    # Requiring a *verified downlink* here deadlocks the campaign: an
    # idle bearer answers nothing, and the only thing that would give it
    # traffic is the episode this check is blocking.
    missing = [h for h in CASE_UES
               if not ((snapshot.get(h) or {}).get('alive')
                       and (snapshot.get(h) or {}).get('tun'))]
    if missing:
        ready = False
        reason = 'CASE_SHAPE: not attached: ' + ', '.join(missing)
    unwatched = [h for h in CASE_UES if h not in HOSTS]
    if unwatched:
        ready = False
        reason = ('CASE_SHAPE: ' + ', '.join(unwatched)
                  + ' required by the case but not supervised')

    publish(ready, reason, {'ues': snapshot,
                            'gateAges': {str(k): round(v, 1) for k, v in ages.items()},
                            'bothE2Nodes': nodes_ok,
                            'episodeRunning': busy})


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            os.kill(pid, 0)
            print(f'keeper already running as {pid}', file=sys.stderr)
            return 9
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    LOCK.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    log('KEEPER_START', pid=os.getpid(), cadence=CADENCE,
        budgetPerHour=BUDGET_PER_HOUR, cells=CELL_OF)
    _load_state()
    try:
        while not _stop:
            # 2026-09-23 감사: ric_running() 의 subprocess.TimeoutExpired 한 번이 keeper 전체를
            # 죽였다(ensure_* 는 OSError 만 잡는다).  한 주기의 예외는 기록하고 다음 주기로.
            try:
                cycle()
            except Exception as exc:  # noqa: BLE001 - the ledger carries the reason
                log('CYCLE_FAILED', error=f'{type(exc).__name__}: {exc}'[:200])
                try:
                    publish(False, 'keeper cycle failed',
                            {'ues': {}, 'gateAges': {}, 'bothE2Nodes': False,
                             'episodeRunning': episode_running()})
                except OSError:
                    pass
            _save_state()
            for _ in range(int(CADENCE)):
                if _stop:
                    break
                time.sleep(1)
    finally:
        log('KEEPER_END')
        try:
            LOCK.unlink()
        except OSError:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
