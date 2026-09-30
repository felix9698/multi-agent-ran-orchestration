#!/usr/bin/env python3
"""How many IMPLICIT_DEREGISTRATION events the AMF logged in the last N minutes (AMF clock).

2026-09-27 02:4x: a stale mobile-reachable timer deregistered ue1's *new* session every 2-4 min
(19:45:03, 19:49:36 AMF clock) and three references were refused; the maintenance daemon only knew
the 116-min echo.  'Now' is the AMF's own last log line -- docker's --since let pre-restart lines
through, and the AMF clock is not the host's.  Prints the count (0 when unreadable).
"""
import datetime
import re
import subprocess
import sys

STAMP = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')


def count(lines, minutes):
    stamps = [(STAMP.match(l), l) for l in lines]
    stamps = [(datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'), l) for m, l in stamps if m]
    if not stamps:
        return 0
    now = stamps[-1][0]
    return sum(1 for t, l in stamps if 'event: [IMPLICIT_DEREGISTRATION]' in l
               and datetime.timedelta(0) <= now - t <= datetime.timedelta(minutes=minutes))


if __name__ == '__main__':
    if sys.argv[1:] == ['--selfcheck']:
        lines = ['[2026-09-26 19:40:00.1] x', '[2026-09-26 19:45:03.6] a (event: [IMPLICIT_DEREGISTRATION])',
                 '[2026-09-26 19:49:36.5] b (event: [IMPLICIT_DEREGISTRATION])', '[2026-09-26 19:52:00.0] y']
        assert count(lines, 15) == 2 and count(lines, 5) == 1, (count(lines, 15), count(lines, 5))
        # after a restart the AMF clock can read earlier than old lines: those are not recent
        assert count(lines + ['[2026-09-26 10:53:08.0] restart'], 15) == 0
        print('selfcheck ok')
        raise SystemExit
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    try:
        out = subprocess.run(['docker', 'logs', '--tail', '20000', 'oai-amf'], capture_output=True,
                             text=True, timeout=30, errors='replace')
        print(count((out.stdout + out.stderr).splitlines(), minutes))
    except Exception:  # noqa: BLE001 - unreadable decides nothing
        print(0)
