"""Which UEs actually need a reattach, judged by evidence rather than a clock.

Both polarities were measured on 2026-09-16 before this threshold was chosen, which is
the rule a destructive check has to follow (keeper-health-check-was-thrashing-the-bed:
an unmeasured check drove 493 UE restarts in one day).

    healthy  19:05  ue1 1 SR failure / 2 RA over 1,894 s
                    ue2 2 / 3 over 2,150 s
                    ue3 1 / 2 over 2,581 s
    sick     18:00  ue3 26 SR failures / 36 RA / 22 distinct RNTIs
                    ue2 seven distinct RNTIs inside one 78-minute instance

The gap is an order of magnitude, so the threshold sits in the empty band: a UE is stale
when its current softmodem instance has 8 or more 'SR not served' warnings or has held 4
or more distinct RNTIs.  Prints the hosts that qualify, one per line, and nothing at all
when the bed is well -- a timer alone must never spend a healthy context.
"""
import shlex
import subprocess
import sys

SR_LIMIT = 8
RNTI_LIMIT = 4
REMOTE = (
    'L=$(ls -t /home/*/ota-fixed38-%s-*.log 2>/dev/null | head -1); '
    '[ -n "$L" ] || exit 1; '
    'echo "$(grep -ac "SR not served" "$L") '
    '$(grep -ao "UE 0 RNTI [0-9a-f]\\{4\\} stats" "$L" | awk "{print \\$4}" | sort -u | wc -l) '
    '$(grep -ac "Processing reconfigurationWithSync" "$L")"'
)


def stale(host):
    """(is_stale, sr_failures, distinct_rntis) for one host; unreadable is never stale."""
    try:
        # One quoted argument: ssh joins argv and hands it to the remote LOGIN shell,
        # so an unquoted $( ) would be expanded there before bash -c ever sees it
        # (caught 19:08 -- the check silently returned "no evidence" for every host).
        remote = 'bash -c ' + shlex.quote(REMOTE % host)
        out = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
                              host, remote],
                             capture_output=True, text=True, timeout=25).stdout.split()
        sr, rntis = int(out[0]), int(out[1])
        # 2026-09-24 13:11: every steering handover (and its hand-back) gives the UE a new RNTI, so
        # a healthy UE on a board with steering trials reached 4-7 and all three were queued for a
        # reattach.  Only RNTIs a handover did not explain count as churn.
        rntis = max(1, rntis - int(out[2])) if len(out) > 2 else rntis
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        return False, None, None          # no evidence, no restart
    return (sr >= SR_LIMIT or rntis >= RNTI_LIMIT), sr, rntis


def main():
    for host in ('ue1', 'ue2', 'ue3'):
        is_stale, sr, rntis = stale(host)
        if is_stale:
            print(host)
        if '--explain' in sys.argv:
            print(f'   {host}: SR실패={sr} RNTI종류={rntis} '
                  f'{"갱신대상" if is_stale else "정상"}', file=sys.stderr)


if __name__ == '__main__':
    if '--self-check' in sys.argv:
        # the measured polarities, as a fixed check on the threshold
        assert not (1 >= SR_LIMIT or 2 >= RNTI_LIMIT), 'healthy ue1 must not qualify'
        assert not (2 >= SR_LIMIT or 3 >= RNTI_LIMIT), 'healthy ue2 must not qualify'
        assert 26 >= SR_LIMIT and 22 >= RNTI_LIMIT, 'sick ue3 must qualify'
        assert 7 >= RNTI_LIMIT, 'a seven-RNTI instance must qualify'
        print('self-check OK')
    else:
        main()
