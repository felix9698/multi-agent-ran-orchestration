#!/usr/bin/env python3
"""run_loop_v4.sh 프로세스만 정확히 센다.

`grep run_loop_v4.sh` 로 세면 **그 문자열을 담은 내 셸까지** 잡힌다(18개로 나왔다).
argv[0] 이 bash 이고 argv[1] 의 basename 이 그 이름일 때만 센다.
"""
import pathlib, sys
n, pids = 0, []
for entry in pathlib.Path('/proc').iterdir():
    if not entry.name.isdecimal():
        continue
    try:
        argv = [a for a in (entry / 'cmdline').read_bytes().split(b'\0') if a]
    except OSError:
        continue
    if len(argv) >= 2 and argv[0].split(b'/')[-1] in (b'bash', b'sh') \
       and argv[1].split(b'/')[-1] == b'run_loop_v4.sh':
        n += 1; pids.append(entry.name)


def _ppid(pid):
    try:
        return open('/proc/%s/stat' % pid).read().rsplit(')', 1)[1].split()[1]
    except Exception:
        return '0'


# 래퍼 bash 와 그 자식이 같은 argv 를 가져 **둘 다 세진다**.  2026-09-17 에 루프가 1 개인데
# 2 로 보고해 "중복 가동" 으로 오진할 뻔했다 (pid 2664250 의 자식이 2666330 이었다).
# 부모가 이 목록 안에 있으면 그것은 래퍼의 몸통이므로 한 번만 센다.
own = set(pids)
pids = [q for q in pids if _ppid(q) not in own]
print(len(pids), " ".join(pids))
sys.exit(0 if n == 0 else 1)
