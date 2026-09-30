#!/usr/bin/env python3
"""One command that answers "is the bed actually producing boards?" (2026-09-20).

Written after an afternoon spent reporting "a board is running" from the loop's
*start* line while every attempt died in the gate: 16:33-18:44 produced zero
board directories.  A start line is not a board.  A board is a directory.

Checks, in the order a failure propagates:
  boards   - directories created since the given time (the only real output)
  kpm      - the stream's age and whether the gate bound both E2 nodes
  ue       - each UE's tun address (the gate's own admission test)
  procs    - gNBs, keeper, loop, and whether a gate attempt is in flight

    python3 ops/state_check.py [HH:MM]     # default: boards since 2 hours ago
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
KPM = Path("/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/a1-live-kpm.jsonl")
HOSTS = ("ue1", "ue2", "ue3")


def sh(args, timeout=25):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:  # noqa: BLE001 - a probe must never take the check down
        return ""


def boards_since(cut: float):
    rows = []
    for path in sorted(EXP.glob("formal38guarded-2026*/evidence/AGENT-*-episode.json")):
        board = path.parent.parent
        if board.stat().st_mtime < cut:
            continue
        try:
            record = json.loads(path.read_text())
        except ValueError:
            continue
        rows.append((board.name[:31], record.get("method"),
                     (record.get("termination") or {}).get("reason"),
                     len(record.get("trials") or [])))
    return rows


def main() -> int:
    when = sys.argv[1] if len(sys.argv) > 1 else None
    if when:
        hh, mm = (int(x) for x in when.split(":"))
        cut = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
    else:
        cut = time.time() - 7200
    print(f"== {time.strftime('%H:%M:%S')}  (기준 {time.strftime('%H:%M', time.localtime(cut))} 이후)")

    rows = boards_since(cut)
    print(f"판 {len(rows)}개")
    for name, method, reason, trials in rows:
        print(f"   {name} {method} {reason} 시행{trials}")

    age = time.time() - KPM.stat().st_mtime if KPM.exists() else -1
    gate = sh(["docker", "logs", "--tail", "40", "oran-aic-kpm-gate"])
    bind = [line for line in gate.splitlines() if '"event":"bind"' in line]
    ok = bool(bind) and '"ok":true' in bind[-1]
    print(f"KPM  {age:.0f}초 전 기록 · 바인딩 {'ok' if ok else '실패/없음'}")
    if bind and not ok:
        print("   " + bind[-1][:150])

    state = {}
    for host in HOSTS:
        state[host] = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host,
                          "ip -4 -o addr show up dev oaitun_ue1 | awk '{print $4}'"]) or "none"
    print("UE   " + "  ".join(f"{h}={v}" for h, v in state.items()))

    sys.path.insert(0, str(HERE))
    import keeper as _k  # noqa: E402 - after the path is set
    gnb1 = len(_k.gnb1_pids())
    gnb2 = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "enb2",
               "ps -C nr-softmodem -o pid= | wc -l"]) or "?"
    keeper = sh(["systemctl", "--user", "is-active", "aic-keeper"])
    loop = sum(1 for p in Path("/proc").iterdir() if p.name.isdigit() and
               "run_loop_v4.sh" in _cmdline(p))
    gating = sum(1 for p in Path("/proc").iterdir() if p.name.isdigit() and
                 "run_episode.py" in _cmdline(p))
    print(f"proc gnb1={gnb1} gnb2={gnb2} keeper={keeper} loop={loop} 게이트진행={gating}")

    # 2026-09-20 오너 지적: 판만 보다가 배경으로 걸어 둔 로컬 모델 비교를 두 시간 반
    # 방치했다.  같은 화면에서 본다 -- 산출물이 0 이면 그 자리에서 처리한다.
    probes = sum(1 for p in Path("/proc").iterdir() if p.name.isdigit()
                 and "control_probe.py" in _cmdline(p))
    newest = sorted(HERE.glob("overnight/local-*"), key=lambda p: p.stat().st_mtime)[-1:]
    for directory in newest:
        answers = len(list(directory.rglob("[0-9][0-9].json")))
        age = (time.time() - directory.stat().st_mtime) / 60
        print(f"probe 실행중={probes} · {directory.name}: 응답 {answers}개, "
              f"마지막 갱신 {age:.0f}분 전")
    if not newest:
        print(f"probe 실행중={probes} · 로컬 산출물 없음")
    return 0


def _cmdline(proc: Path) -> str:
    try:
        return proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf8", "replace")
    except OSError:
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
