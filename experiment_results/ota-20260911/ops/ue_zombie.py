"""Cause-based UE restart gate: a UE may be restarted only when its cell released its current RNTI.

2026-09-15 20:4x: with blind UE restarts switched off (overnight/NO_UE_AUTO_RESTART), ue1 on
gnb1 (RNTI c92d) and ue2 on gnb2 (RNTI 2e4b) were released by their gNB after PUSCH UL failure
but missed the RRC Release: the softmodem kept printing its RNTI, the tun kept its address and
no data flowed, forever.  That state never heals by itself, so a restart is the fix -- but only
with this evidence, never because a probe failed once.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

GNB2_LOG_CMD = "tail -c 6000000 $(ls -t /tmp/gnb2*.log | head -1)"
_SUPI_OF: dict = {}   # memory only: a subscriber id is never logged, printed or written


def session_released(smf_log: str, supi_digits: str) -> bool:
    """True when this subscriber's last PDU session event in ``smf_log`` is a release.

    2026-09-16 00:0x: the AMF's 116-minute implicit de-registration of a stale context
    (event IMPLICIT_DEREGISTRATION, cause REL_DUE_TO_REACTIVATION) released ue1's
    *current* SM context; RRC stayed up and re-established over and over, the tun kept
    its address, and no downlink ever came back -- the readiness gate expired on it.
    No RRC release exists for that, so only the core's own log shows it.
    """
    import re
    state, retrieved = None, None
    for line in smf_log.splitlines():
        if 'PDU Session Create SM Context Request message from AMF, SUPI imsi-' in line:
            if ('imsi-' + supi_digits) in line:
                state = 'created'
            continue
        found = re.search(r'Retrieve SMF context with SUPI imsi-(\d+)', line)
        if found:
            retrieved = found.group(1)
            continue
        if 'n4_session_deletion_response (Release SM Context Request)' in line and retrieved == supi_digits:
            state = 'released'
    return state == 'released'


def current_rnti(ue_log_tail: str) -> Optional[str]:
    """The RNTI the UE softmodem last reported (``UE 0 RNTI xxxx stats``)."""
    found = re.findall(r'UE 0 RNTI ([0-9a-f]{4}) stats', ue_log_tail)
    return found[-1] if found else None


def idle_after_release(ue_log_tail: str) -> bool:
    """True when the UE's last word is an RRC Release with no connected activity after it.

    2026-09-15 21:08: ue1's first attach on gnb1 got Msg4 then lost PUSCH; the UE did hear the
    release and went RRC_IDLE with no tun, no stats and no retry -- a state it never leaves.
    """
    at = ue_log_tail.rfind('Received RRC Release')
    if at < 0:
        return False
    after = ue_log_tail[at:]
    return not re.search(r'RNTI [0-9a-f]{4} stats|RA procedure succeeded', after)


def ra_looping(ue_log_tail: str) -> bool:
    """True when the UE keeps completing random access and never settles on one context.

    2026-09-16 13:28-13:43: ue2 held no tun address at all while its log printed
    `4-Step RA procedure succeeded` about once a second under a new RNTI each time
    (b2b5 then 3363 within three seconds), and 159 initial syncs with 158 USRP retunes
    in seven minutes.  None of the other classes fired -- the softmodem was alive, no
    release was the last word, the cell had not released the RNTI it currently held,
    and it had synced plenty -- so the readiness gate printed `UE restart held by
    NO_UE_AUTO_RESTART: ue2` on every round and the attempt expired on it.

    A healthy board attaches once and its tail is that one RNTI's stats: ue1 and ue3
    were at one and nine initial syncs over twenty-five minutes in the same window,
    each holding a single RNTI.  Both polarities matter here because the verdict costs
    a restart (keeper-health-check-was-thrashing-the-bed), so this asks for BOTH
    repeated success AND a context that keeps changing.
    """
    succeeded = len(re.findall(r'RA procedure succeeded', ue_log_tail))
    contexts = set(re.findall(r'UE 0 RNTI ([0-9a-f]{4}) stats', ue_log_tail))
    return succeeded >= 3 and len(contexts) >= 2


#: The data network the UE pings.  Same address the gate's own probe uses.
EXT_DN = '192.168.70.135'


def bearer_frozen(host: str, ssh: Callable, settle_s: float = 6.0) -> bool:
    """True when the tun is up, the UE is sending, and nothing comes back.

    The night of 2026-09-17 produced this shape on three UEs across BOTH cells:
    the gNB reports ``in-sync PH 10`` with a normal RSRP, the UE holds a tun
    address, the bearer's byte counters have carried gigabytes and are now
    frozen, and a UE-originated ping loses 100% at every payload size.  None of
    the existing classes name it -- the cell never released the context, the UE
    never looped RA, the softmodem is alive -- so the gate printed
    ``held by NO_UE_AUTO_RESTART`` and burned its readiness limit while a
    re-attach would have fixed it (it did, each time it was run by hand).

    The check is deliberately narrow.  It asks the UE for its own receive
    counter twice around a settle window and for one UE-originated round trip;
    a UE that receives anything at all, or answers once, is not this.  A probe
    that cannot be read returns False -- no evidence, no restart.
    """
    read = ('cat /sys/class/net/oaitun_ue1/statistics/rx_bytes 2>/dev/null || echo x')
    try:
        first = (ssh(host, ['bash', '-c', read], timeout=30).stdout or '').strip()
        if not first.isdigit():
            return False
        # One UE-originated round trip during the settle window: it is both the
        # traffic that would move the counter and the liveness question itself.
        probe = ssh(host, ['bash', '-c',
                           'timeout %d ping -I oaitun_ue1 -s 1200 -c 6 -i 0.5 -w %d %s'
                           % (int(settle_s) + 8, int(settle_s) + 4, EXT_DN)], timeout=60)
        second = (ssh(host, ['bash', '-c', read], timeout=30).stdout or '').strip()
        if not second.isdigit():
            return False
        return int(second) == int(first) and probe.returncode != 0
    except Exception:  # noqa: BLE001 - a failed probe is not evidence
        return False


def released(gnb_log_tail: str, rnti: str) -> bool:
    """True when the gNB log shows it released this RNTI."""
    return bool(re.search(r'RNTI %s\) Send RRC Release|UE %s: request release' % (rnti, rnti),
                          gnb_log_tail))


def never_synced(syncs: int, failures: int, elapsed_s: float) -> bool:
    """True for a softmodem that has been up a while and never found its cell.

    2026-09-16 10:05-10:20: ue2 ran for fifteen minutes printing `synch Failed` and
    `Scanning GSCN` with no `Initial sync successful`, so it had no RNTI to be released
    and no tun to look at -- no evidence, no restart, and the readiness gate expired on
    it twice.  gnb2 was serving throughout, so the UE's own front end is wedged and the
    remedy is the restart with the USB reset that force_reattach does.
    """
    return syncs == 0 and failures > 50 and elapsed_s > 120


def tun_absent(uptime_s: float, tun_lines: int, min_uptime_s: float = 120.0) -> bool:
    """True for a softmodem that has been up a while with no tun address at all.

    2026-09-18 16:55-17:00: ue1 had synced earlier in its life, lost the cell and never
    got back.  `never_synced` needs `syncs == 0` so it said no; `bearer_frozen` reads
    `rx_bytes` through the tun that is not there so it also said no; there was no RNTI
    to be released.  **Every evidence class returned False and the runner printed
    "held by NO_UE_AUTO_RESTART" for five minutes until the gate expired** -- two
    boards in a row were refused with `the three UEs did not all carry downlink`.

    A softmodem past its attach time with no address is not ambiguous: it is not
    serving and a restart with the USB reset is what has fixed it every time.
    """
    return uptime_s > min_uptime_s and tun_lines == 0


def tun_absent_evidence(host: str, ssh: Callable) -> bool:
    """Read the two numbers :func:`tun_absent` judges; False when they cannot be read."""
    command = ('E=$(ps -o etimes= -C nr-uesoftmodem | head -1); '
               'A=$(ip -4 -o addr show dev oaitun_ue1 2>/dev/null | wc -l); '
               'echo "${E:-0} $A"')
    try:
        parts = (ssh(host, ['bash', '-c', command], timeout=30).stdout or '').split()
        return tun_absent(float(parts[0]), int(parts[1]))
    except Exception:  # noqa: BLE001 - 읽지 못한 증거는 증거가 아니다
        return False


def scanning_evidence(host: str, ssh: Callable) -> bool:
    """Read the three numbers :func:`never_synced` judges; False when they cannot be read."""
    command = ('L=$(ls -t /home/*/ota-fixed38-%s-*.log | head -1); '
               'S=$(grep -ac "Initial sync successful" "$L"); '
               'F=$(tail -n 3000 "$L" | grep -ac "synch Failed"); '
               'E=$(ps -o etimes= -C nr-uesoftmodem | head -1); echo "$S $F $E"' % host)
    try:
        parts = (ssh(host, ['bash', '-c', command], timeout=40).stdout or '').split()
        return never_synced(int(parts[0]), int(parts[1]), float(parts[2]))
    except (ValueError, IndexError, AttributeError):
        return False


def core_session_released(host: str, ssh: Callable, run: Callable = subprocess.run) -> bool:
    """The core released the PDU session this UE's running softmodem registered (see
    :func:`session_released`); False whenever the evidence cannot be read."""
    import re
    try:
        name = (ssh(host, ['bash', '-c', 'basename "$(ls -t /home/*/ota-fixed38-%s-*.log | head -1)"' % host],
                    timeout=30).stdout or '').strip()
        started = re.search(r'-(\d{8})T(\d{6})', name)
        if not started:
            return False
        if host not in _SUPI_OF:
            conf = ssh(host, ['sh', '-c', "grep -hoE 'imsi *= *\"[0-9]+\"' ~/ai-ran-stage/runtime/phase-b/nr-ue.conf | head -1"],
                       timeout=20).stdout or ''
            digits = re.search(r'(\d{15})', conf)
            if not digits:
                return False
            _SUPI_OF[host] = digits.group(1)
        day, clock = started.groups()
        since = f"{day[:4]}-{day[4:6]}-{day[6:]}T{clock[:2]}:{clock[2:4]}:{clock[4:]}+09:00"
        # --tail keeps the read under a second: --since alone scans the whole container log
        # (~12 s each time, measured 2026-09-16); 300k lines is ~26 h of this SMF's logging.
        out = run(['docker', 'logs', '--since', since, '--tail', '300000', 'oai-smf'],
                  capture_output=True, text=True, errors='ignore', timeout=60)
        return session_released((out.stdout or '') + (out.stderr or ''), _SUPI_OF[host])
    except Exception:  # noqa: BLE001 - no evidence means no restart
        return False


def amf_state_of(amf_log: str, supi_digits: str) -> Optional[str]:
    """This subscriber's 5GMM state in the AMF's newest UE table, or None.

    The AMF prints the whole table periodically, so the *last* block is the
    current one and any earlier block is history.  The subscriber id is matched
    in memory and never returned, logged or written.
    """
    rows = re.findall(r"\|\s*\d+\s*\|\s*(5GMM-[A-Z-]+)\s*\|\s*(\d{15})",
                      amf_log)
    state = None
    for found_state, digits in rows:
        if digits == supi_digits:
            state = found_state          # keep the last, which is the newest table
    return state


#: Longest measured registration after a softmodem start (ue2 85 s) with margin.
REGISTER_GRACE_S = 150


def deregistered_at_amf(host: str, ssh: Callable, run: Callable = subprocess.run) -> bool:
    """The radio is up but the core has this UE deregistered, so no session and no tun.

    2026-09-16 21:0x: ue2's softmodem was alive and RRC-connected -- RNTI ba24,
    DL and UL harq both advancing, no RRC Release at all -- and the tun interface
    did not exist, because the AMF held it ``5GMM-DEREGISTERED``.  No RNTI was
    released, so :func:`released` sees nothing; the session was never created, so
    :func:`session_released` sees nothing either; and every other class needs a
    tun that once worked.  The keeper printed ``UE_RESTART_HELD`` on every round
    while the readiness gate expired.  The AMF's own table is the only place this
    state is written down, and a re-attach is what clears it.
    """
    try:
        # 2026-09-19 21:06~21:12: the AMF table kept ue2's *old* context as
        # 5GMM-DEREGISTERED, and while a freshly started softmodem was still
        # registering (58-85 s on ue2) that stale row was the only one -- so the
        # keeper killed ue2 every ~90 s just before it registered, four times in
        # a row, and a prepared board was refused "ue2 has no current
        # amfUeNgapId".  A deregistration only means something once the UE has
        # had time to register.
        age = ssh(host, ['sh', '-c', 'ps -o etimes= -C nr-uesoftmodem | sort -n | tail -1'],
                  timeout=20).stdout or ''
        if not age.strip().isdigit() or int(age.strip()) < REGISTER_GRACE_S:
            return False
        if host not in _SUPI_OF:
            conf = ssh(host, ['sh', '-c', "grep -hoE 'imsi *= *\"[0-9]+\"' ~/ai-ran-stage/runtime/phase-b/nr-ue.conf | head -1"],
                       timeout=20).stdout or ''
            digits = re.search(r'(\d{15})', conf)
            if not digits:
                return False
            _SUPI_OF[host] = digits.group(1)
        out = run(['docker', 'logs', '--since', '10m', '--tail', '4000', 'oai-amf'],
                  capture_output=True, text=True, errors='ignore', timeout=60)
        return amf_state_of((out.stdout or '') + (out.stderr or ''),
                            _SUPI_OF[host]) == '5GMM-DEREGISTERED'
    except Exception:  # noqa: BLE001 - no evidence means no restart
        return False


def cell_is_down(cell: str, ssh: Callable, run: Callable = subprocess.run) -> bool:
    """그 UE 가 붙어야 할 셀의 softmodem 이 없는가.

    2026-09-20: gnb2 의 X310 관리 채널이 죽어 셀이 사라졌는데, 게이트가 ue1 을
    ``never-synced`` 좀비로 판정하고 USB 를 리셋했다.  **셀이 없으면 UE 는 당연히
    스캔만 한다** -- scanning_evidence 는 그 상태를 구별하지 못한다.  멀쩡한 UE 의
    엔드포인트를 리셋해 두면 셀이 돌아왔을 때 오히려 더 늦게 붙는다.

    읽지 못하면 False 를 돌려 평소대로 진단한다 -- 증거가 없다고 조치를 막지는 않는다.

    **keeper.cell_usable() 과의 관계**: keeper 에는 이미 같은 취지의 가드가 있다
    (`_streak >= FAIL_STREAK and not cell_usable(...)` -> ALERT_CELL_DOWN).  그런데
    게이트는 keeper 를 거치지 않고 run_episode.py:140 에서 zombie() 를 직접 부르므로
    그 가드가 걸리지 않았다 -- 06:23:59 오진이 난 경로가 바로 이것이다.  그래서 가드를
    zombie() 안에 두어 **두 호출자 모두**에 걸리게 했다.

    판정 방식은 일부러 다르다.  cell_usable 은 gnb2_stalled()/gnb1 로그의
    'got 0 from USRP' 로 "못 쓴다" 까지 보고, 이쪽은 프로세스 부재만 본다 -- 더 좁으므로
    과잉 차단을 하지 않는다.  RF 만 죽은 경우는 keeper 쪽 가드가 잡는다.
    """
    count = 'ps -eo args | grep -c "^[^ ]*nr-softmodem "'
    for attempt in (1, 2):          # 베드가 깜빡일 때 한 번은 봐준다
        try:
            if cell == 'gnb2':
                out = (ssh('enb2', ['bash', '-c', count], timeout=30).stdout or '').strip()
            else:
                out = (run(['bash', '-c', count], capture_output=True, text=True,
                           errors='ignore', timeout=30).stdout or '').strip()
            if out:                 # 읽혔다 -- 프로세스 수가 답한다
                return out == '0'
        except Exception:  # noqa: BLE001 - 한 번은 깜빡임일 수 있다
            pass
        if attempt == 1:
            time.sleep(2)
    # 2026-09-20 13:45: enb2 노드가 통째로 내려가 ssh 가 'No route to host' 를 냈다.
    # 그때 이 함수는 False(셀 살아있음)를 돌려주고 있었다 -- 노드에 닿지도 못하는데
    # 셀이 서비스 중이라고 답한 것이다.  두 번 다 읽지 못하면 그 셀은 쓸 수 없다.
    return True


def zombie(host: str, cell: str, ssh: Callable, gnb1_log: Callable[[], Optional[Path]]) -> Optional[str]:
    """Return the released RNTI when ``host`` still holds a context its cell already released."""
    # 2026-09-15 22:22: ue3's softmodem exited after 2,275 sync failures; with no process there is no
    # release to find and the restart was held forever.  A missing softmodem is its own evidence.
    alive = ssh(host, ['bash', '-c', 'ps -eo args | grep -c "^[^ ]*nr-uesoftmodem "'], timeout=30)
    if (alive.stdout or '').strip() == '0':
        return 'softmodem-absent'
    # 셀이 없으면 이 아래 증거들은 전부 UE 를 잘못 고발한다: 스캔도, tun 부재도,
    # 세션 해제도 셀이 사라진 결과다.  고칠 대상은 UE 가 아니라 셀이다.
    if cell_is_down(cell, ssh):
        return None
    ue = ssh(host, ['bash', '-c', 'L=$(ls -t /home/*/ota-fixed38-%s-*.log | head -1); grep -aE "RNTI [0-9a-f]{4} stats|Received RRC Release|RA procedure succeeded" "$L" | tail -50' % host],
             timeout=30)
    if idle_after_release(ue.stdout or ''):
        return 'idle-after-release'
    if ra_looping(ue.stdout or ''):
        return 'ra-looping'
    if scanning_evidence(host, ssh):
        return 'never-synced'
    if tun_absent_evidence(host, ssh):
        return 'tun-absent'
    if core_session_released(host, ssh):
        return 'session-released'
    if deregistered_at_amf(host, ssh):
        return 'deregistered-at-amf'
    if bearer_frozen(host, ssh):
        return 'bearer-frozen'
    rnti = current_rnti(ue.stdout or '')
    if not rnti:
        return None
    # Both cells, whatever ``cell`` says: a handover or a placement other than the keeper's
    # intention leaves the UE on the other cell, and only that cell's log holds the release
    # (2026-09-15 21:34: ue1 on gnb2, checked against gnb1, held forever).
    texts = [ssh('enb2', ['bash', '-c', GNB2_LOG_CMD], timeout=60).stdout or '']
    path = gnb1_log()
    if path is not None:
        texts.append(subprocess.run(['tail', '-c', '6000000', str(path)], capture_output=True,
                                    text=True, errors='ignore').stdout)
    return rnti if any(released(t, rnti) for t in texts) else None


if __name__ == '__main__':
    ue_tail = 'x\n543411.3 [NR_MAC] I UE 0 RNTI 2e4b stats sfn: 128.8\n'
    assert current_rnti(ue_tail) == '2e4b'
    assert current_rnti('nothing') is None
    assert released('4053526.357216 [NR_RRC] A [DL] (cellID 5397fb1, UE ID 1 RNTI 2e4b) Send RRC Release', '2e4b')
    assert released('4068674.003461 [MAC]    W UE c92d: request release after UL failure timer expiry', 'c92d')
    assert not released('UE 2e4b: Received Ack of Msg4', '2e4b')
    assert not released('(cellID x, UE ID 1 RNTI 1111) Send RRC Release', '2e4b')
    assert idle_after_release('x RA procedure succeeded\n[NR_RRC] I [UE 0] Received RRC Release (gNB 0)\nPF match')
    assert not idle_after_release('Received RRC Release\nRA procedure succeeded\nUE 0 RNTI 1234 stats')
    assert not idle_after_release(ue_tail)
    smf = ('[x] Handle a PDU Session Create SM Context Request message from AMF, SUPI imsi-111111111111111, SNSSAI\n'
           '[x] Retrieve SMF context with SUPI imsi-222222222222222\n'
           '[x] Handle itti_n4_session_deletion_response (Release SM Context Request): pdu-session-id 10\n')
    assert not session_released(smf, '111111111111111')      # another subscriber was released
    assert session_released(smf.replace('imsi-222222222222222', 'imsi-111111111111111'), '111111111111111')
    assert not session_released(smf.replace('imsi-222222222222222', 'imsi-111111111111111')
                                + smf.splitlines()[0] + '\n', '111111111111111')   # re-created after
    # tun_absent: 2026-09-18 에 판 둘을 잃은 빈틈.  붙었다가 떨어진 UE 는
    # never_synced(syncs==0 요구)도 bearer_frozen(tun 필요)도 잡지 못했다.
    assert tun_absent(1098.0, 0)              # 18분 떠 있는데 주소가 없다 -> 고장
    assert not tun_absent(1098.0, 1)          # 주소가 있으면 이 증거가 아니다
    assert not tun_absent(30.0, 0)            # 막 떴으면 아직 붙는 중이다
    assert not tun_absent(0.0, 0)             # softmodem 이 없으면 softmodem-absent 가 잡는다
    # 증거를 못 읽으면 재시작하지 않는다 -- tun_absent_evidence 도 같은 성질.
    class _NoRead:
        def __call__(self, *a, **k): raise OSError('unreachable')
    assert not tun_absent_evidence('ue-x', _NoRead())

    # bearer_frozen: 증거를 못 읽으면 재시작하지 않는다(가장 중요한 성질).
    class _Bad:
        def __call__(self, *a, **k): raise OSError('unreachable')
    assert not bearer_frozen('ue-x', _Bad())
    class _Reply:
        def __init__(self, out, rc=0): self.stdout, self.returncode = out, rc
    seq = iter(['100\n', _Reply('', 0), '100\n'])
    def _alive(host, args, timeout=0):
        v = next(seq)
        return v if isinstance(v, _Reply) else _Reply(v)
    # 카운터가 멈췄어도 왕복이 성공하면 이 분류가 아니다.
    assert not bearer_frozen('ue-x', _alive)
    seq = iter(['100\n', _Reply('', 1), '100\n'])
    assert bearer_frozen('ue-x', _alive)          # 멈춤 + 왕복 실패 = 이 분류
    seq = iter(['100\n', _Reply('', 1), '140\n'])
    assert not bearer_frozen('ue-x', _alive)      # 받고 있으면 아니다

    looping = ('[RAPROC] 4-Step RA procedure succeeded. CBRA\n'
               'UE 0 RNTI b2b5 stats sfn: 0.8\n'
               '[RAPROC] 4-Step RA procedure succeeded. CBRA\n'
               '[RAPROC] 4-Step RA procedure succeeded. CBRA\n'
               'UE 0 RNTI 3363 stats sfn: 256.8\n')
    assert ra_looping(looping)
    assert not ra_looping(looping.replace('3363', 'b2b5'))      # one context, however many attaches
    assert not ra_looping(ue_tail)                              # a settled board
    table = ('   |  Index |     5GMM State     |                IMSI/SUPI               |\n'
             '   |    1   |   5GMM-REGISTERED  |             208950000000001            |\n'
             '   |    2   |  5GMM-DEREGISTERED |             208950000000002            |\n')
    assert amf_state_of(table, '208950000000002') == '5GMM-DEREGISTERED'
    assert amf_state_of(table, '208950000000001') == '5GMM-REGISTERED'
    assert amf_state_of(table, '208950000000009') is None      # not in the table at all
    # The newest block wins: the same subscriber re-registering must not read as deregistered.
    assert amf_state_of(table + table.replace('5GMM-DEREGISTERED', '5GMM-REGISTERED '),
                        '208950000000002') == '5GMM-REGISTERED'
    assert never_synced(0, 900, 600) and not never_synced(1, 900, 600)
    assert not never_synced(0, 900, 30) and not never_synced(0, 5, 600)
    # A softmodem still inside its registration window is never judged by the AMF table.
    class _Out:
        def __init__(self, text): self.stdout = text
    assert deregistered_at_amf('ueX', lambda *a, **k: _Out('40\n'),
                               run=lambda *a, **k: (_ for _ in ()).throw(AssertionError('read the AMF'))) is False
    print('ue_zombie self-check ok')

    # cell_is_down: 2026-09-20 gnb2 X310 사망 중 ue1 을 좀비로 오진한 건.
    class _R:
        def __init__(self, out): self.stdout = out
    assert cell_is_down('gnb2', lambda *a, **k: _R('0\n'))
    assert not cell_is_down('gnb2', lambda *a, **k: _R('2\n'))
    assert cell_is_down('gnb1', lambda *a, **k: _R('?'), run=lambda *a, **k: _R('0\n'))
    assert not cell_is_down('gnb1', lambda *a, **k: _R('?'), run=lambda *a, **k: _R('1\n'))
    def _boom(*a, **k): raise OSError('unreachable')
    assert cell_is_down('gnb2', _boom)              # 끝내 못 읽으면 그 셀은 쓸 수 없다
                                                     # (한 번만 깜빡인 경우는 아래 _blink 가 지킨다)

    # zombie(): UE 는 돌고 있는데 셀이 없는 경우 -- 06:23:59 에 ue1 이 당한 오진.
    # softmodem 은 있고(1), 스캔 로그가 never-synced 를 참으로 만들 수 있는 상태에서도
    # 셀이 죽었으면 None 이어야 한다.
    def _ssh_ue_alive_cell_dead(host, argv, timeout=30):
        cmd = argv[-1]
        if host == 'enb2':
            return _R('0\n')                      # gnb2 softmodem 없음 = 셀 사망
        if 'nr-uesoftmodem ' in cmd and 'grep -c' in cmd:
            return _R('1\n')                      # UE softmodem 은 돌고 있다
        return _R('')                              # 나머지 증거는 비어 있다
    assert zombie('ue1', 'gnb2', _ssh_ue_alive_cell_dead, lambda: None) is None
    def _ssh_ue_absent_cell_dead(host, argv, timeout=30):
        cmd = argv[-1]
        if host == 'enb2':
            return _R('0\n')
        if 'nr-uesoftmodem ' in cmd and 'grep -c' in cmd:
            return _R('0\n')                      # UE 프로세스도 없다
        return _R('')
    # 셀이 죽었어도 UE 프로세스가 없으면 띄워야 한다 -- 셀이 돌아올 때 붙으려면.
    assert zombie('ue1', 'gnb2', _ssh_ue_absent_cell_dead, lambda: None) == 'softmodem-absent'

    # cell_is_down: 노드에 닿지 못하면 셀도 못 쓴다 (2026-09-20 enb2 노드 다운)
    def _always_boom(*a, **k): raise OSError('No route to host')
    assert cell_is_down('gnb2', _always_boom)         # 두 번 다 실패 -> 못 쓴다
    _tries = {'n': 0}
    def _blink(*a, **k):
        _tries['n'] += 1
        if _tries['n'] == 1: raise OSError('blink')
        return _R('2\n')
    assert not cell_is_down('gnb2', _blink)           # 깜빡였다가 읽히면 그 답을 쓴다
    assert cell_is_down('gnb2', lambda *a, **k: _R(''))   # 빈 출력 두 번도 못 읽은 것
