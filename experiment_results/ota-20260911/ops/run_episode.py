#!/usr/bin/env python3
"""Run one guarded attempt while holding the keeper's busy lock.

Without the lock the keeper restarts a UE mid-attempt and the run is refused
with REMOTE_REFUSED:<host>:identity -- that is exactly what happened at
19:29:23, eight seconds after the attempt pinned ue2 at 12.1.1.130.  The lock
is released in a finally block so a crash or a timeout cannot leave the keeper
permanently observing.
"""
import json, os, shlex, signal, subprocess, sys, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUSY = HERE / 'overnight' / 'episode-busy.lock'
EXP = Path('/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/'
           'experiment_results/ota-20260911')



# 2026-09-17: 이 로그에는 **시각이 한 줄도 없었다**.  그래서 언제 무엇이 일어났는지를
# 파일 mtime 으로 *추정*할 수밖에 없었고, 판당 대기 지표를 하루에 다섯 번 틀렸다.
# 시각은 추정하지 말고 찍는다.  줄머리를 고정 파싱하는 소비자는 없음을 확인했다
# (catch-ue-no-dl.sh 의 중복 제거만 접두를 벗기도록 같이 고쳤다).
_real_print = print


def print(*a, **kw):                     # noqa: A001 - 의도적으로 가린다
    if a and isinstance(a[0], str):
        a = (time.strftime('%H:%M:%S ') + a[0],) + a[1:]
    return _real_print(*a, **kw)


def held_by_someone_else() -> bool:
    try:
        pid = int(BUSY.read_text().split()[0])
    except (OSError, IndexError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return pid != os.getpid()
    except (ProcessLookupError, PermissionError):
        return False


HOSTS = ('ue1', 'ue2', 'ue3')
EXT_DN = '192.168.70.135'


def ue_carries_downlink(host: str) -> str:
    """The tun address when this UE both holds one and answers over it, else ''.

    UE-originated, tunnel-bound: unsolicited downlink from ext-dn does not
    traverse this bed even for a healthy UE, so probing that way declares
    everything dead (see the keeper's own probe, fixed the same evening).
    """
    ip = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', host,
         "ip -4 -o addr show up dev oaitun_ue1 | awk '{print $4}' | cut -d/ -f1"],
        capture_output=True, text=True, timeout=20).stdout.strip()
    if not ip:
        return ''
    ok = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', host,
         f'timeout 16 ping -I oaitun_ue1 -s 1200 -c 8 -i 0.5 -w 12 {EXT_DN} >/dev/null 2>&1'],
        timeout=40).returncode == 0
    return ip if ok else ''


def usb_reset(host: str) -> None:
    """Reset the USRP's USB endpoint before re-attaching.

    On 2026-09-14 ue2 sat at 0.5 Mbps on BOTH cells with a normal 19.1 dB SNR
    but 15,721 pucch0_DTX against ue1's 184 on the same cell: its ACKs never
    reached the gNB, so every downlink block was retransmitted to death.  Plain
    restarts did not fix it and a cell swap proved the fault followed the board.
    One USBDEVFS_RESET restored it to 11.6 Mbps.

    The keeper's usb_wedged() returned False throughout, because it looks for
    the LIBUSB_TRANSFER_ERROR signature of a hard wedge and this degradation has
    none.  The reset is cheap, needs no root (/dev/bus/usb nodes are 0666) and
    is harmless on a healthy board, so do it unconditionally on the way back.
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
        '        fcntl.ioctl(fd, 0x5514, 0)\n'
        '        print("RESET_OK " + node)\n'
        '    finally:\n'
        '        os.close(fd)\n'
    )
    out = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', host,
         'python3 -c ' + shlex.quote(code)],
        capture_output=True, text=True, timeout=60).stdout.strip()
    print(f'  {host} usb: {out or "no output"}', flush=True)


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
    """The released RNTI when the gNB released this UE's current context, else None.

    A failed check is not the same fact as an absent one, and collapsing them
    is expensive: the gate then prints "held by NO_UE_AUTO_RESTART" and burns
    its whole readiness limit on a UE nobody even managed to look at.  Attempts
    152, 153, 155 and 156 of 2026-09-16 each lost their gate to that line.  The
    refusal still stands -- no evidence, no restart -- but the reason is said
    out loud so the next reader can tell a healthy UE from a broken checker.
    """
    try:
        import keeper as _k
        import ue_zombie
        return ue_zombie.zombie(host, _k.CELL_OF[host], _k.ssh, _k.gnb1_log)
    except Exception as exc:  # noqa: BLE001 - a failed check is still no restart
        print(f'zombie evidence check failed for {host}: '
              f'{type(exc).__name__}: {exc}'[:300], flush=True)
        return None


def restart_ue(host: str) -> int:
    """Re-attach one UE.  Only the lock holder may call this.

    While this process holds the busy lock the keeper stands down, so a UE that
    loses its bearer here stays lost: the gate would wait out its whole limit
    for a repair nobody is going to make.  Whoever holds the lock owns recovery.
    """
    if ue_auto_restart_held():
        rnti = zombie_release_evidence(host)
        if rnti is None:
            print(f'UE restart held by NO_UE_AUTO_RESTART: {host}', flush=True)
            return 1
        print(f'{host}: gNB released RNTI {rnti} but the UE kept it; restarting (zombie)', flush=True)
    # The reset runs inside force_reattach between STOP and START.  Called here,
    # before STOP, it pulled the device from under a running softmodem: 80 of
    # 199 UE logs on 2026-09-15 end in ERROR_CODE_TIMEOUT -> NO_DEVICE -> SIGINT,
    # deaths this runner caused and crash counts then blamed on the board.
    # 2026-09-19: 같은 UE 를 이 게이트 안에서 두 번째로 되살려야 하면 사다리를 한 단 올려
    # sysfs `authorized` 토글까지 쓴다.  ue1 은 USBDEVFS_RESET 을 02:04~02:18 내내(그리고
    # 01:2x 에 15 번) 받고도 동기하지 못했는데, 01:42 에 authorized 토글은 한 번에 붙었다
    # ([[usb-authorized-toggle-unwedges-a-usrp]]).  첫 번째는 종전처럼 가벼운 리셋만 한다.
    _restarts[host] = _restarts.get(host, 0) + 1
    env = {**os.environ, 'AIC_REATTACH_USB_RESET': '1'}
    if _restarts[host] >= 2:
        env['AIC_REATTACH_USB_AUTHORIZED'] = '1'
        print(f'{host}: restart #{_restarts[host]} in this gate -- escalating to the '
              'usb authorized toggle', flush=True)
    return subprocess.call([sys.executable, str(HERE / 'force_reattach.py'), host], env=env)


_restarts: dict = {}


KPM_JSONL = ('/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/'
             'a1-live-kpm.jsonl')


def kpm_attribution(window_s: float = 30.0) -> dict:
    """{nb_id: {amfUeNgapId, ...}} seen in the last *window_s* of indications.

    The gateway's pre-policy READ does not ask the policy anything -- there is no
    policy yet -- it asks the KPM UE attribution stream where the UE is
    (`CorroboratedServingCellReadback`).  A UE that pings happily but is absent
    from that stream therefore fails PREPARE with "the configuration could not be
    read", and one missing UE fails the whole surface: every attempt on
    2026-09-14 evening died this way while its UEs answered pings.  So the gate
    checks what the READ checks.
    """
    import time as _t
    cutoff_us = (_t.time() - window_s) * 1e6
    seen: dict = {}
    try:
        with open(KPM_JSONL, errors='replace') as handle:
            lines = handle.readlines()[-4000:]
    except OSError:
        return seen
    for line in lines:
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (row.get('recv_unix_us') or 0) < cutoff_us:
            continue
        for ue in (row.get('ues') or ()):
            identifier = ue.get('amf_ue_ngap_id')
            if identifier is not None:
                seen.setdefault(row.get('nb_id'), set()).add(identifier)
    return seen


def _placed_nodes() -> set:
    """The nb ids the CURRENT placement actually puts a UE on.

    ``len(seen) < 2`` was the old test and it was right while the deployment
    spread UEs over both cells.  It is wrong the moment a placement uses one:
    2026-09-17, with gNB2's uplink receive chain broken and all three UEs moved
    to gnb1, the gate refused every attempt for "only 1 node(s) attribute a UE"
    -- demanding attribution from a cell no UE is on and none can reach.  The
    READ source has to answer for the cells this sitting uses, which is what
    the placement names, and nothing about that relaxes the check for a cell
    that IS in use.
    """
    try:
        import keeper as _k
        return {_k.CELL_NB[cell] for cell in set(_k.CELL_OF.values())}
    except Exception:  # noqa: BLE001 - an unreadable placement is not a licence
        return set()


def kpm_ready(expected_ues: int = 3) -> str:
    """'' when the stream can answer a READ for every UE, else why it cannot."""
    seen = kpm_attribution()
    if not seen:
        return 'no KPM UE attribution at all'
    empty = [nb for nb, ues in seen.items() if not ues]
    total = sum(len(ues) for ues in seen.values())
    wanted = _placed_nodes() or {nb for nb in seen} | {None}
    missing = sorted(nb for nb in wanted if not seen.get(nb))
    if missing or empty:
        return (f'{sorted(wanted)} placed, but no UE is attributed on '
                f'{missing or empty}: {dict((k, sorted(v)) for k, v in seen.items())}')
    if total < expected_ues:
        return f'{total} of {expected_ues} UEs attributed: {dict((k, sorted(v)) for k, v in seen.items())}'
    return ''


def kpm_identities(window_s: float = 12.0) -> dict:
    """{nb: frozenset(ids)} over a short window -- the *current* attribution.

    ``kpm_attribution``'s 30 s window deliberately forgives a gap, which makes
    it the right gate for "can a READ be answered" and the wrong one for "has
    the bed stopped moving": a UE that re-attached ten seconds ago appears in
    it under BOTH its old and its new id.
    """
    return {nb: frozenset(ids) for nb, ids in kpm_attribution(window_s).items()}


def identities_stable(settle_s: float = 20.0, limit_s: float = 240.0) -> str:
    """'' once the attributed identities hold still, else why they have not.

    The attempt of 23:30 was refused after both model calls with

        the pfWeight@2098 axis cannot verify RAN.UE.PfWeight:
        ATTRIBUTION_UNAVAILABLE; no fresh same-UE/cell/epoch configuration
        counter was published

    and the counter was never the problem: measured afterwards, every
    attributed UE carries ``RAN.UE.PfWeight`` on a 1.00 s cadence against a
    4 s freshness bound, on both nodes.  What moved was the UE.  ``2098`` was
    ue2's ``amfUeNgapId`` when the runner pinned identities; by the time the
    axis was verified ue2 had re-attached and the stream carried a different
    id, so the pinned one had no fresh counter -- correct fail-closed
    behaviour, reported against the wrong-looking subject.

    A re-attach costs the gate twenty seconds and costs an attempt two model
    calls and its whole formation budget, so the gate waits for the identities
    to repeat before letting an attempt pin them.
    """
    deadline = time.monotonic() + limit_s
    previous = None
    while time.monotonic() < deadline:
        current = kpm_identities()
        # Stable is not enough: a UE that dropped leaves a stable set of the
        # other two (2026-09-15 attempt 6 passed with 2 ids and was refused at
        # preflight with REMOTE_REFUSED:ue1:identity).
        complete = sum(len(ids) for ids in current.values()) == len(HOSTS)
        if previous is not None and current == previous and complete:
            return ''
        if previous is not None and current != previous:
            print(f'identities still moving: {_render(previous)} -> {_render(current)}',
                  flush=True)
        previous = current
        time.sleep(settle_s)
    return f'the attributed identities never held still for {settle_s:.0f}s'


def _hosts_off_their_cell() -> list:
    """Hosts that are not on their placed cell, by KPM attribution.

    The placement table is the authority on where a UE belongs; KPM attribution
    is the authority on where it *is*.  Two ways they disagree:

    * the placed cell attributes no UE at all (the UE was carried off and the
      cell is empty), and
    * the role's own AMF UE NGAP ID (``~/rlive/<host>-hdr.env``, kept on the
      current identity by the keeper) is attributed *only* on another cell.

    The second case was missing.  2026-09-23 board 153808 (basic-monolith)
    started with ue3 on gnb1 next to ue2 -- an earlier board's steering had
    left it there -- while gnb2 still carried ue1, so no cell was empty and
    nothing re-pinned it.  The board's baseline then disagreed with the
    placement, the trial moved ue3 back to gnb2, the hand-back to gnb1 was
    refused by the gNB ("Ongoing handover for UE 4, cannot trigger new") and
    the board ended in RECOVERY_FAILURE.  An unknown identity (no header, or an
    id no cell attributes) decides nothing -- the other gates handle it.
    """
    try:
        import keeper as _k
    except Exception:  # noqa: BLE001 - an unreadable placement repairs nothing
        return []
    seen = kpm_attribution()
    empty = {nb for nb, ues in seen.items() if not ues}
    empty |= {_k.CELL_NB[cell] for cell in set(_k.CELL_OF.values())
              if _k.CELL_NB[cell] not in seen}
    now = kpm_identities()
    hosts = []
    for host, cell in _k.CELL_OF.items():
        placed = _k.CELL_NB[cell]
        amf = _k._header_amf(host)
        at = {nb for nb, ids in now.items() if amf is not None and amf in ids}
        if placed in empty or (at and placed not in at):
            hosts.append(host)
    return hosts


def repin_ue(host: str) -> None:
    """Restart *host* pinned to its configured cell, via the project's own tool.

    ``force_reattach.py`` already stops the softmodem, restarts it with the cell
    in the argv and then *verifies the argv it actually got* -- a check worth
    keeping, because passing the cell alone once reported a swap that never
    happened.
    """
    import keeper as _k
    subprocess.call([sys.executable, str(HERE / 'force_reattach.py'),
                     f'{host}={_k.CELL_OF[host]}'], timeout=300)


def _render(mapping: dict) -> str:
    return '{' + ', '.join(f'{nb}: {sorted(ids)}' for nb, ids in sorted(mapping.items())) + '}'


#: 재시작한 UE 가 동기·부착에 쓸 수 있는 시간.
#: 2026-09-24 03:39~03:41 통제 시험(러너·keeper 정지, UE 무간섭): ue1 2분 30초, ue3 약 4분 만에
#: 스스로 붙었다.  그날 밤 첫 PSS 까지 30 s~6 분이 걸렸는데 150 s 유예 + 놓친 3 라운드(약 4분)
#: 마다 다시 내려, 부착 직전의 UE 를 계속 끊었다(03:20~03:38 세 UE 모두 PSS 0).  관측 최대치에 맞춘다.
ACQUIRE_GRACE_S = 360.0

def bed_not_ready() -> list:
    """What the power-on runbook's pass criteria (steps 4-6) still miss, or [] when ready.

    2026-09-24, owner: "성공하는 기준 시작 순서는 확실히 알고 있을거 아니야 ... 훅으로 걸어놓든
    해서 그 순서는 지켜야 할거 아니냐".  The order lived only in
    docs/runbooks/usrp-power-on-startup.md, so a board could start in the middle of it (a gNB
    restarted, its re-pin still owed).  This is that runbook as a gate: whoever restarted
    what, a board starts only on a bed that has finished the sequence.  UEs (step 7) are
    wait_for_ues(); the C0 baseline (step 8) is wait_for_baseline().
    """
    import keeper as _k
    missing = []
    # 2026-09-25 (Codex review of the power calibration): a gnb2 attenuation the calibration
    # could not prove restored blocks every board, whichever path starts it.
    unsafe = Path(__file__).resolve().parent / 'overnight' / 'POWER_UNSAFE'
    if unsafe.exists():
        missing.append('POWER_UNSAFE: ' + unsafe.read_text(errors='ignore').strip()
                       + ' -- verify gnb2 attenuation, then remove the file')
    # 2026-09-26 board 670: a trial locked down mid-apply left gnb2 at 9 dB, and the next board
    # would have frozen 9 dB as its baseline.  Once a calibration has recorded the bed's
    # attenuation, a board starts only on that value (fail closed; restore, then it passes).
    recorded = Path(__file__).resolve().parent / 'overnight' / 'power-calibration' / 'baseline.json'
    if recorded.exists():
        try:
            import json as _json
            import reference_dl as _ref
            want = round(float(_json.loads(recorded.read_text())['db']), 1)
            # gnb2 by name: reference_dl.POWER_NB follows the v5.1 energy cell (gnb1 under option 3),
            # and this check read gnb1's 8 dB against gnb2's 6 (2026-09-27 01:40, a board held 14 min).
            got = _ref.cell_attenuation(Path('/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/a1-live-kpm.jsonl'),
                                        nb=2816)
            if got is not None and got[0] != want:
                # Wait, never write (2026-09-26 board 675): a board that succeeded with a power
                # control leaves its A1 policy owning the gnb2 scope until the policy expires
                # and the producer restores the baseline on the official path.  Restoring the
                # hardware by telnet here made this check pass while the policy still owned the
                # scope, and every power trial of the next board was refused 409.
                missing.append(f'gnb2 attenuation {got[0]} dB is not the bed baseline {want} dB '
                               '(a retained or unrestored power policy; waiting for it to expire)')
        except Exception:  # noqa: BLE001 - an unreadable check is not a verdict
            pass
    if not _k.gnb1_pids():
        missing.append('gnb1 process')
    try:
        n = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'enb2',
                            'pgrep -c -x nr-softmodem'], capture_output=True, text=True,
                           timeout=20).stdout.strip()
        if not n or int(n) < 1:
            missing.append('gnb2 process')
    except Exception:  # noqa: BLE001 - an unreachable enb2 is not ready
        missing.append('gnb2 process (enb2 unreachable)')
    live = _k._live_epochs()
    for nb in _k.CELL_NB.values():
        if nb not in live:
            missing.append(f'E2 node {nb} not active in the RIC witness')
    for name, drift in (('binding', _k._gate_epoch_drift()), ('A1-P inventory', _k._inventory_drift())):
        if drift:
            missing.append(f'{name} epoch behind the witness {drift} (re-pin owed)')
    for port in (18443, 9444, 9445):
        if not _k._port_is_open('192.168.50.1', port):
            missing.append(f'port {port} closed')
    try:
        age = time.time() - _k.R1_TOKEN.stat().st_mtime
        if age > _k.R1_TOKEN_MAX_AGE_S:
            missing.append(f'R1 token {age:.0f} s old')
    except OSError:
        missing.append('R1 token missing')
    return missing


def clear_leftover_a1p_policies() -> list:
    """Withdraw every A1-P policy left by an earlier board; the deleted ids.

    2026-09-29 boards 885/895: a steering policy outlived its board (an UNDO DELETE refused
    409, or a UE that never came back so no DELETE was sent) and held the live UE's scope --
    the next board's steer to that UE was refused 409 AIC_POLICY_CONFLICT (EXECUTION_FAILURE).
    Called with the busy lock held: boards run one at a time, so nothing at the producer is
    this board's yet.  Best effort -- a producer that cannot be read is left to the board."""
    tools = Path.home() / 'oran-deploy/session-20260819/tools'
    try:
        sys.path.insert(0, str(tools))
        import sqlite3
        import clear_ue_scope as scope
        con = sqlite3.connect(f'file:{scope.DB}?mode=ro', uri=True)
        ids = [r[0] for r in con.execute('select policy_id from policies')]
        con.close()
        gone = []
        for i, pid in enumerate(ids):
            status, _ = scope.delete(pid, 2000 + i)
            if status in (200, 202, 204, 404):
                gone.append(pid)
        if ids:
            print(f'withdrew {len(gone)}/{len(ids)} leftover A1-P policies: '
                  + ' '.join(p[:8] for p in ids))
        return gone
    except Exception as exc:  # noqa: BLE001 - best effort, the board still runs
        print(f'leftover A1-P policies not cleared: {type(exc).__name__}: {exc}'[:200])
        return []
    finally:
        if str(tools) in sys.path:
            sys.path.remove(str(tools))


def wait_for_bed(limit_s: float = 900.0) -> bool:
    """Hold until bed_not_ready() is empty; one line a minute says what is still missing."""
    deadline = time.monotonic() + limit_s
    last = None
    while time.monotonic() < deadline:
        missing = bed_not_ready()
        if not missing:
            print('bed ready: gNBs up, epochs re-pinned, producers and token fresh')
            return True
        if last is None or time.monotonic() - last >= 60:
            last = time.monotonic()
            print('bed not ready: ' + '; '.join(missing))
        time.sleep(10)
    return False


def wait_for_ues(limit_s: float = 900.0, stuck_rounds: int = 3) -> bool:
    """Hold until all three UEs carry downlink at once, repairing what does not.

    An attempt that starts while one UE is mid-reattach is refused at preflight
    with FRESH_UE_KPM_REQUIRED or REMOTE_REFUSED:<host>:identity -- both happened
    on 2026-09-14 -- and the refusal costs the same wall-clock as waiting would
    have.  This only decides when to start, never what the attempt measures.
    """
    deadline = time.monotonic() + limit_s
    misses: dict[str, int] = {h: 0 for h in HOSTS}
    restarted_at: dict[str, float] = {}
    empty_cell_rounds = 0
    repinned_off: set = set()
    while time.monotonic() < deadline:
        state = {h: ue_carries_downlink(h) for h in HOSTS}
        if all(state.values()):
            missing = kpm_ready(len(HOSTS))
            if missing:
                print('downlink is fine but the READ source is not: ' + missing,
                      flush=True)
                # ...and waiting alone will never fix it.  A *successful* steering
                # handover moves the UE to the other cell and leaves it there: the
                # softmodem argv still says the configured cell, every UE carries
                # downlink, so neither the miss counter below (it counts *no*
                # downlink) nor the keeper's zombie evidence fires.  The placed
                # cell simply stays empty and this loop prints the same line for
                # ever.  Observed 2026-09-17 11:20: judged trial 5 of episode
                # 021337 moved ue1 gnb2 -> gnb1, the reversal at trial 6 answered
                # PARTIAL_APPLY, and the next run sat here for eight minutes until
                # a hand re-pinned it.  Re-pin what the placement says owns that cell.
                empty_cell_rounds += 1
                if empty_cell_rounds >= stuck_rounds:
                    empty_cell_rounds = 0
                    import keeper as _k
                    for host in _hosts_off_their_cell():
                        print(f'{host} carries downlink but its placed cell is '
                              f'empty; re-pinning it to {_k.CELL_OF[host]}',
                              flush=True)
                        repin_ue(host)
                time.sleep(15)
                continue
            empty_cell_rounds = 0
            # Every cell attributes a UE, but a UE may still sit on the wrong one
            # (steered there by an earlier board): re-pin it now, before this
            # board takes the wrong cell as its baseline.
            import keeper as _k
            # Once per host per wait: a re-pin that did not take would otherwise
            # restart the same UE every round until the limit.
            off = [h for h in _hosts_off_their_cell() if h not in repinned_off]
            if off:
                for host in off:
                    repinned_off.add(host)
                    print(f'{host} is attributed on another cell than its placed '
                          f'{_k.CELL_OF[host]}; re-pinning it before the board',
                          flush=True)
                    repin_ue(host)
                time.sleep(15)
                continue
            moving = identities_stable()
            if moving:
                print('downlink and attribution are fine but ' + moving, flush=True)
                time.sleep(15)
                continue
            print('all three UEs carry downlink, are attributed in KPM and '
                  'their identities are stable: ' +
                  ', '.join(f'{h}={v}' for h, v in state.items()), flush=True)
            print('  attributed: ' + _render(kpm_identities()), flush=True)
            return True
        print('waiting: ' + ', '.join(f'{h}={v or "no-dl"}' for h, v in state.items()),
              flush=True)
        for host, ip in state.items():
            # 2026-09-19: 재시작한 UE 에게 획득 시간을 준다.  동기 획득 실측 -- ue3 8 s 대,
            # ue2 최근 58~85 s, ue1 최대 82 s -- 인데 게이트는 약 110 s 마다(리셋 12 s 포함)
            # 다시 내려 느린 UE 가 거의 붙을 무렵 끊었다(02:20~02:36 ue1 재시작 8 번).
            # 유예 동안은 놓친 횟수를 세지 않는다.
            if ip or time.monotonic() - restarted_at.get(host, -1e9) >= ACQUIRE_GRACE_S:
                misses[host] = 0 if ip else misses[host] + 1
            if misses[host] >= stuck_rounds:
                print(f'{host} has missed {misses[host]} rounds; re-attaching it '
                      f'(the keeper is standing down for our lock)', flush=True)
                restart_ue(host)
                restarted_at[host] = time.monotonic()
                misses[host] = 0
        time.sleep(15)
    return False


#: C0 -- what every board must start from.  Cell attenuation by nb_id, UE axes for all UEs.
#: 2026-09-22 07:10: gnb1(3584)의 기준선을 0.0 에서 8.0 으로 옮긴다.  실측 훑기에서
#: 이 셀은 8 dB 에서만 산다 -- 10 이면 UE 가 SSB 동기를 못 잡고, 6 이하면 주소는 받아도
#: 사용자평면이 죽고, 0 이면 과출력으로 `Nack in Msg4` 가 나 RA 가 실패한다.
#: 기준선을 안 옮기면 러너가 동작점을 '이전 판이 남긴 제어'로 보고 영원히 기다린다
#: (`waiting for the previous board's controls to expire: txAttenuationDb@nb3584=8`).
#: 근거: [[gnb1-lives-in-a-two-decibel-window]]
#: 2026-09-24 06:1x: gnb2(2816) 10 -> 8.  ue1<->gnb2 하향 여유 부족(Msg4 NACK 37, 재등록 반복, 하향 0)을
#: 풀려고 하향을 2 dB 올린다.  02:0x 에 같은 8 이 Msg3 를 전멸시켰던 건 UE 가 경로손실을 작게 보고
#: 약하게 쏜 탓이었고, 이제 gnb2 PRACH 목표 전력을 -76 -> -68 로 올려 두었다(795ed3e6a).
BASELINE_ATTENUATION_DB = {3584: 8.0, 2816: 6.0}  # 2026-09-28 05:3x back from option B (gnb1 energy baseline 8)
BASELINE_UE = {'RAN.UE.DlPrbCap': 0.0, 'RAN.UE.PfWeight': 1.0}


def leftover_controls(window_s: float = 15.0) -> list:
    """Axes whose newest KPM value in the last *window_s* is not the baseline.

    2026-09-19 18:28: a board settled txAttenuationDb@12345678 = 6.0 and kept it
    (retention), the policy's notAfter ran to 09:29:07Z, and the next board --
    started 12 s after -- read its live baseline at composition while 6.0 was
    still on the radio.  The policy then expired, the radio went back to 0.0,
    and both power trials were refused at PREPARE with REJECTED_CONFIG_MISMATCH
    (the plan's baseline 6.0 against a live 0.0); two in a row end the board as
    EXECUTION_FAILURE.  Waiting here until the radio is back at C0 also gives
    every method the same starting point -- otherwise one method's retained
    control becomes the next method's baseline.
    """
    cutoff_us = (time.time() - window_s) * 1e6
    newest: dict = {}
    try:
        with open(KPM_JSONL, errors='replace') as handle:
            lines = handle.readlines()[-4000:]
    except OSError:
        return ['KPM stream unreadable']
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (row.get('recv_unix_us') or 0) < cutoff_us:
            continue
        nb = row.get('nb_id')
        for m in row.get('measurements') or ():
            if m.get('name') == 'RAN.Cell.TxAttenuationDb' and 'value' in m:
                newest[f'txAttenuationDb@nb{nb}'] = (float(m['value']),
                                                     BASELINE_ATTENUATION_DB.get(nb))
        for ue in row.get('ues') or ():
            for m in ue.get('measurements') or ():
                if m.get('name') in BASELINE_UE and 'value' in m:
                    newest[f"{m['name']}@{ue.get('amf_ue_ngap_id')}"] = (
                        float(m['value']), BASELINE_UE[m['name']])
    return [f'{axis}={seen:g} (C0 {want:g})' for axis, (seen, want) in sorted(newest.items())
            if want is not None and abs(seen - want) > 1e-6]


def wait_for_baseline(limit_s: float = 330.0) -> bool:
    """Hold the board until the radio is back at C0 (expiry retries back off to 300 s)."""
    deadline = time.monotonic() + limit_s
    while True:
        left = leftover_controls()
        if not left:
            return True
        if time.monotonic() > deadline:
            print('the radio is still not at C0 after %d s: %s' % (limit_s, ', '.join(left)),
                  flush=True)
            return False
        print('waiting for the previous board\'s controls to expire: ' + ', '.join(left),
              flush=True)
        time.sleep(10)


def restore_placement() -> None:
    """Put a steered UE back on its placed cell the moment the board ends.

    The gateway already hands a steered UE back and refuses to withdraw the
    policy until the baseline is observed (``R1Adapter._restore_by_handover``).
    When that observation never comes the trial locks down -- correctly, the
    evidence is missing -- and the board stops.  Nothing then moved the UE:
    2026-09-20 ran two boards with all three UEs on gnb1 and gnb2 empty,
    because the placement was only ever checked *before* the next board and the
    UE had been carried off by the trial before it.  Repair here, between
    boards, where no verdict can be polluted by a restart.
    """
    try:
        for host in _hosts_off_their_cell():
            print(f'{host} is not on its placed cell after the board; re-pinning it',
                  flush=True)
            repin_ue(host)
    except Exception as exc:  # noqa: BLE001 - repair must never mask the board's own exit
        print(f'placement restore skipped: {exc}', flush=True)


#: 2026-09-23 감사: systemd stop/restart 의 SIGTERM 에 이 러너는 기본 동작으로 즉사해
#: finally(락 해제)가 돌지 않았고, subprocess.call 은 예외에 자식을 SIGKILL 해 판 러너의
#: exit.json·원장 행·원격 부하원 정리까지 끊었다(판 20260923T120725 에 exit.json 없음).
#: SIGTERM 은 KeyboardInterrupt 와 같은 정리 경로를 탄다; 판이 돌고 있으면 먼저 자식에게
#: 넘기고 자식이 정리를 마칠 때까지 기다린다.
_child = None
_terminated = False


def _install_sigterm() -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def on_term(_signum, _frame):
        global _terminated
        signal.signal(signal.SIGTERM, signal.SIG_IGN)    # 정리 도중의 두 번째 SIGTERM 무시
        _terminated = True
        child = _child
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signal.SIGTERM)
            except OSError:
                pass
            return          # 아래 wait() 가 자식의 정리를 기다린다
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)


def main() -> int:
    global _child
    _install_sigterm()
    if held_by_someone_else():
        print(f'refused: {BUSY} is held by a live pid; not racing it')
        return 4
    # The bed gate runs BEFORE the busy lock: while this waits, the keeper must be free
    # to rebuild the chain and re-pin.  With the lock held it read "an episode holds
    # the hardware" and skipped the rebuild (02:13, 2026-09-24) -- gate and keeper each
    # waited for the other.
    if not wait_for_bed():
        print('refused: the bed has not finished the power-on/restart sequence '
              '(docs/runbooks/usrp-power-on-startup.md steps 4-6)')
        return 5
    BUSY.write_text(f'{os.getpid()} manual-attempt\n')
    try:
        # Let the keeper notice the lock before the attempt pins UE identities.
        time.sleep(12)
        if not wait_for_ues():
            print('refused: the three UEs did not all carry downlink in time; '
                  'not spending an attempt on a bed that is still settling')
            return 5
        clear_leftover_a1p_policies()
        if not wait_for_baseline():
            print('refused: a previous board\'s control is still on the radio; '
                  'a board started now would take it as its baseline')
            return 5
        # The keeper may restart a gNB while the UEs are awaited (it no longer waits for
        # a pre-board lock), and a restart owes a re-pin: look at the bed once more.
        missing = bed_not_ready()
        if missing:
            print('refused: the bed changed while the UEs were awaited: ' + '; '.join(missing))
            return 5
        # The gate owns UE recovery while it runs; from here this process is blocked in
        # the sitting and cannot repair anything, so the lock says so and the keeper
        # takes evidence-based recovery over (keeper.recover_during_episode).
        BUSY.write_text(f'{os.getpid()} cli\n')
        _child = subprocess.Popen([sys.executable, str(HERE / 'run_with_proxy.py'),
                                   str(EXP / 'atomic_formal_run_guarded.py'),
                                   *sys.argv[1:]])
        code = _child.wait()
        _child = None
        if _terminated:
            raise KeyboardInterrupt
        restore_placement()
        return code
    except KeyboardInterrupt:
        # 멈추라는 신호다 -- 배치 복원(UE 재부착)처럼 하드웨어를 만지는 일은 하지 않는다.
        print('interrupted: stopping without touching the radio', flush=True)
        return 130
    finally:
        try:
            if BUSY.read_text().split()[0] == str(os.getpid()):
                BUSY.unlink()
                print('busy lock released')
        except (OSError, IndexError):
            pass


if __name__ == '__main__':
    raise SystemExit(main())
