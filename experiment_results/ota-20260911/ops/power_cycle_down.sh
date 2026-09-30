#!/bin/bash
# 2026-09-25 16:4x owner power cycle: after the running board, bring the bed down (runbook section 0).
O="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say(){ echo "$(date +%T) $*"; }
touch "$O/overnight/PAUSE" "$O/overnight/NO_EPISODES"
while pgrep -f "python3 /home.*ops/run_episode.py" >/dev/null; do sleep 5; done; say "board ended"
systemctl --user stop aic-v31-episodes aic-keeper; say "services: $(systemctl --user is-active aic-v31-episodes aic-keeper | tr '\n' ' ')"
cd "$O" && python3 - <<'PY'
import keeper
from keeper import ssh, _ue_password, WINDOW
pw = _ue_password(); src = (WINDOW / 'execute_once.py').read_text()
i = src.index('STOP_CODE = """') + len('STOP_CODE = """'); stop = src[i:src.index('"""', i)]
for h in ('ue1', 'ue2', 'ue3'):
    r = ssh(h, ['sudo', '-S', '-p', '', 'python3', '-c', stop], timeout=90, stdin=pw + '\n')
    print(h, 'UE_STOP rc', r.returncode)
PY
for p in $(pgrep -x nr-softmodem); do kill -TERM "$p"; done
ssh enb2 'sudo -n pkill -TERM -x nr-softmodem'
for i in $(seq 1 30); do pgrep -x nr-softmodem >/dev/null || ssh enb2 pgrep -x nr-softmodem >/dev/null || break; sleep 2; done
say "gnb1 $(pgrep -cx nr-softmodem) gnb2 $(ssh enb2 pgrep -cx nr-softmodem) ue $(for h in ue1 ue2 ue3; do ssh $h pgrep -cx nr-uesoftmodem; done | tr '\n' ' ')"
sleep 15; say "READY FOR POWER CYCLE"
