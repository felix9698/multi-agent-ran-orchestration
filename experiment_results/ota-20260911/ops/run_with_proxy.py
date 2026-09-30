#!/usr/bin/env python3
"""Run an attempt with inherited API/local-model configuration and forward SIGTERM.

The historical filename is retained for campaign-script compatibility. No
credential file is read, and no provider, endpoint or model is substituted.
"""
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

def main() -> int:
    if len(sys.argv) < 2:
        print('usage: run_with_proxy.py <script.py> [args...]')
        return 2
    script = Path(sys.argv[1]).resolve()
    env = dict(os.environ)
    # 2026-09-23 감사: subprocess.call 은 어떤 예외에도 자식을 SIGKILL 한다.  systemd stop 의
    # SIGTERM 에 이 프로세스가 기본 동작으로 죽거나 call 이 자식을 죽이면, 판 러너
    # (atomic_formal_run_guarded) 의 finally -- exit.json·원격 부하원 정리 -- 가 돌지 못한다
    # (판 20260923T120725 에 exit.json 이 없다).  SIGTERM 은 자식에게 넘기고, 자식이 정리를
    # 마치고 나올 때까지 기다린다.
    child = subprocess.Popen([sys.executable, str(script), *sys.argv[2:]],
                             env=env, cwd=str(script.parent))
    _forward_sigterm_to(child)
    return child.wait()


def _forward_sigterm_to(child) -> None:
    """SIGTERM 을 받으면 한 번만 자식에게 넘긴다.  신호 처리기는 메인 스레드에서만 건다."""
    if threading.current_thread() is not threading.main_thread():
        return

    def forward(_signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)    # 두 번째 SIGTERM 이 대기를 깨지 않게
        try:
            child.send_signal(signal.SIGTERM)
        except OSError:
            pass
    signal.signal(signal.SIGTERM, forward)


if __name__ == '__main__':
    raise SystemExit(main())
