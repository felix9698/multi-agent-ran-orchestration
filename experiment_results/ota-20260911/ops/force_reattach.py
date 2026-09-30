"""Force a clean re-attach of every UE after a UPF/SMF restart.

The keeper's futile-guard refuses to restart a UE that keeps returning on the
same tun address, which is right for a wedged board and wrong here: after the
core drops every PFCP session the UE holds a stale address whose session no
longer exists, and only a restart gets a new one.
"""
import os, shlex, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent


BUSY_LOCK = HERE / 'overnight' / 'episode-busy.lock'


def _refuse_while_the_runner_holds_the_bed() -> None:
    """러너가 판을 세우는 중이면 거절한다 (2026-09-21).

    2026-09-21 에 나는 아무 서비스도 멈추지 않았는데 판이 하루 종일 한 번도 못 떴다.
    진단하느라 이 스크립트를 열 번 돌렸는데, 러너는 **세 UE 가 동시에 20 초** 버텨야
    판을 시작한다.  재부착 한 번마다 그 시계가 0 으로 돌아갔다.  가드를 별도 스크립트로
    두면 부르는 걸 잊으므로, 하드웨어를 만지는 이 도구 안에 넣는다.

    의도적으로 뺏어야 하면 AIC_TAKE_BED=1 을 주고, 끝나면 판 서비스를 되살려라.
    """
    if os.environ.get('AIC_TAKE_BED') == '1':
        return
    lock = BUSY_LOCK
    try:
        pid = int(lock.read_text().split()[0])
    except Exception:
        return
    try:
        os.kill(pid, 0)
    except OSError:
        return
    # 락 주인이 내 조상이면 러너가 나를 부른 것이다 -- 막으면 러너 자신이 막힌다.
    me = os.getpid()
    for _ in range(40):
        if me == pid:
            return
        try:
            me = int(open(f'/proc/{me}/stat').read().split(') ', 1)[1].split()[1])
        except Exception:
            break
        if me <= 1:
            break
    sys.exit('거부: 러너가 베드를 쓰는 중이다 (pid %d). '
             '재부착하면 세 UE 동시 부착 시계가 0 으로 돌아간다. '
             '읽기 전용으로 재거나 AIC_TAKE_BED=1 로 명시하라.' % pid)


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


# AUTHORIZED_CODE·CARRIER 는 keeper.py 에 한 벌만 둔다 (2026-09-23 감사: keeper.restart_ue 가
# 같은 토글·되읽기를 쓰도록 옮겼다).

EXP = Path('/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/'
           'experiment_results/ota-20260911')
WINDOW = EXP / 'window-20260914T050000'
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
# One placement table: keeper.CELL_OF (honours AIC_KEEPER_CELLS).  A second hard-coded copy here
# silently disagreed when the keeper's was edited (2026-09-15 21:35).
sys.path.insert(0, str(HERE))
from keeper import AUTHORIZED_CODE, CARRIER, CARRIER_READBACK, CELL_OF  # noqa: E402,F401
# 2026-09-22 15:0x: gnb1 을 3400.32 -> 3349.92 MHz 로 옮긴다.  두 셀의 설정·잡음바닥
# (I0 중앙값 둘 다 22.2 dB)·TDD 가 동일한데 gnb1 만 상향 PUSCH 가 전멸한다
# (ulsch BLER 0.99998 대 gnb2 0.00000).  PUCCH 는 살아 있어 NACK 과 SNR 14.4 dB 가
# 돌아오므로 전력·잡음 문제가 아니다 -- 대역 가장자리의 좁은 PUCCH 는 되고 28 PRB
# 짜리 PUSCH 만 죽는 것은 대역 안 협대역 간섭의 모양이고, I0 평균에는 묻힌다.
# CARRIER 는 keeper.py 에서 온다 (위 import).



def ssh(host, args, timeout=200, stdin=None):
    """Identical quoting to keeper.ssh: shlex.join, not repr."""
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
                           host, shlex.join(args)],
                          capture_output=True, text=True, timeout=timeout, input=stdin)


# Accept "host" or "host=cell": the initial placement is not fixed and each UE
# names both cells as servingCell candidates, so a board that cannot hold one
# cell is tried on the other rather than restarted at the same one forever.
def main(argv) -> int:
    """0 이면 모든 UE 가 멈추고·기동하고·요청한 반송파로 되읽혔다.  아니면 1.

    2026-09-23 감사: 이 스크립트는 무엇이 실패해도 늘 exit 0 이었다 -- 부른 쪽이
    rc 로 실패를 알 길이 없었다.  그리고 09-21 18:2x 에 "교착 시험" 으로 주석 처리된
    러너 보호 가드가 그대로 방치돼 있었다.  교착은 러너가 자기 자식으로 부를 때의
    문제인데 가드는 조상을 거슬러 올라가 그 경우를 이미 통과시킨다 -- 되살린다.
    """
    _refuse_while_the_runner_holds_the_bed()
    password = _ue_password()
    source = (WINDOW / 'execute_once.py').read_text()
    i = source.index('STOP_CODE = """') + len('STOP_CODE = """')
    stop_code = source[i:source.index('"""', i)]
    start_code = (EXP / 'start_fixed38_ue_when_stopped.py').read_text()
    failed = []
    for spec in argv or ['ue1', 'ue2', 'ue3']:
        host, _, want = spec.partition('=')
        cell = want or CELL_OF[host]
        r = ssh(host, ['sudo', '-S', '-p', '', 'python3', '-c', stop_code],
                timeout=120, stdin=password + '\n')
        print(f'  {host} STOP  rc={r.returncode} {(r.stderr or "").strip()[:100]}', flush=True)
        if r.returncode != 0:
            # 멈추지 못한 UE 위에 새 softmodem 을 올리지 않는다 (keeper.restart_ue 와 같은 판정).
            failed.append(f'{host}:stop')
            continue
        time.sleep(5)
        if os.environ.get('AIC_REATTACH_USB_RESET') == '1':
            # Only once the softmodem is down (run_episode.restart_ue): resetting a
            # device a live process still holds kills it on a read timeout.
            from run_episode import usb_reset
            usb_reset(host)
        if os.environ.get('AIC_REATTACH_USB_AUTHORIZED') == '1':
            # Only after the softmodem is down, for the same reason as the reset above.
            r = ssh(host, ['sudo', '-S', '-p', '', 'python3', '-c', AUTHORIZED_CODE],
                    timeout=120, stdin=password + '\n')
            print(f'  {host} usb authorized: '
                  f'{(r.stdout or r.stderr or "").strip()[:140]}', flush=True)
            time.sleep(8)
            seen = ssh(host, ['bash', '-lc',
                              # The softmodem's UHD, not the system 4.1 on PATH: 4.1 does not know the
                              # B206mini and says "No UHD Devices Found" for a healthy radio -- that
                              # false negative sent the owner to replug ue1 three times on 09-23.
                              'LD_LIBRARY_PATH=/opt/uhd-4.9.0.0/lib UHD_IMAGES_DIR=/opt/uhd-4.9.0.0/share/uhd/images '
                              'timeout 25 /opt/uhd-4.9.0.0/bin/uhd_find_devices 2>&1 '
                              '| grep -E "serial|product|No UHD|No devices" | head -3'],
                       timeout=60)
            print(f'  {host} uhd_find_devices: '
                  f'{(seen.stdout or seen.stderr or "(출력 없음)").strip()[:140]}', flush=True)
        r = ssh(host, ['sudo', '-S', '-p', '', 'env', 'AIC_UE_NO_SCAN=1',
                       'python3', '-c', start_code, host, cell],
                timeout=200, stdin=password + '\n')
        # Read back the carrier the process actually got.  Printing the requested
        # cell alone once reported a swap that never happened: the argv still
        # carried CELL_OF[host] while the log said cell=gnb1.
        time.sleep(6)
        got = (ssh(host, ['bash', '-c', CARRIER_READBACK], timeout=30).stdout or '').strip()
        want_hz = CARRIER[cell]
        ok = 'OK' if got == str(want_hz) else f'MISMATCH (wanted {want_hz})'
        print(f'  {host} START rc={r.returncode} cell={cell} carrier={got or "?"} {ok} '
              f'{(r.stderr or "").strip()[:120]}', flush=True)
        if r.returncode != 0:
            failed.append(f'{host}:start')
        elif ok != 'OK':
            failed.append(f'{host}:carrier')
        # Sequential: two UEs coming up together race for the same address.
        time.sleep(12)
    if failed:
        print(f'  FAILED: {", ".join(failed)}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
