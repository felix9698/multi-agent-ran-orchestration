#!/usr/bin/env python3
"""Keep the v3.1 collection session (Claude Code in an Orca terminal) working.

Run by a systemd user timer.  It never touches equipment, UEs, the episode
service or the ledger; it only (1) brings the collection session back into an
Orca terminal if the terminal or the Claude process is gone, and (2) sends one
Korean nudge when the session has sat idle with no WORKLOG/ledger update for too
long.  Every action is appended to ops/overnight/session-watchdog.log.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OVERNIGHT = HERE / 'overnight'
REPO = HERE.parents[2]
ORCA = '/opt/ran-lab/controller/.config/orca/linux-orca-cli-shim/orca'
SESSION_ID = 'c287675f-a811-4c4b-938c-04f536676081'
HANDLE_FILE = OVERNIGHT / 'handoff-terminal.txt'
STATE_FILE = OVERNIGHT / 'session-watchdog.state.json'
LOG = OVERNIGHT / 'session-watchdog.log'
WORKLOG = OVERNIGHT / 'WORKLOG.md'
LEDGER = OVERNIGHT / 'v31-ledger.jsonl'
IDLE_S = 45 * 60          # no WORKLOG/ledger update for this long while idle -> nudge
NUDGE_GAP_S = 30 * 60     # at most one nudge per this interval
LAUNCH = f'claude --dangerously-skip-permissions --resume {SESSION_ID}'
NUDGE = ('워치독 알림: 45분 넘게 WORKLOG·원장 갱신이 없고 화면이 대기 상태다. '
         'experiment_results/ota-20260911/ops/overnight/HANDOFF-20260915.md 와 WORKLOG.md 를 다시 읽고, '
         'run-forever.log Monitor 와 ScheduleWakeup 루프를 다시 걸어 "앞으로 할 일"을 이어서 진행하라. '
         '서비스는 멈추지 않고, 장비·UE·USB는 건드리지 않는다. 화면에 📌현재 상태·📋앞으로 할 일·▶지금 할 일 메모 형식으로 적어 가며 일한다. 모든 기록과 답변은 한국어.')
RESUME = ('워치독 알림: 이 세션이 끊겨 Orca 터미널에서 다시 재개됐다. HANDOFF-20260915.md 와 WORKLOG.md 를 다시 읽고, '
          'Monitor 와 ScheduleWakeup 루프를 다시 걸어 이어서 진행하라. 모든 기록과 답변은 한국어.')


def log(event: str, **fields) -> None:
    row = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': event, **fields}
    with LOG.open('a') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def orca(*args: str, timeout: int = 60) -> dict:
    r = subprocess.run([ORCA, *args, '--json'], capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {'ok': False, 'error': (r.stderr or r.stdout)[-300:]}


def screen(handle: str) -> str:
    r = subprocess.run([ORCA, 'terminal', 'read', '--terminal', handle, '--screen'],
                       capture_output=True, text=True, timeout=60)
    return r.stdout if r.returncode == 0 else ''


def send(handle: str, text: str) -> None:
    subprocess.run([ORCA, 'terminal', 'send', '--terminal', handle, '--text', text, '--enter'],
                   capture_output=True, text=True, timeout=60)


def claude_running() -> bool:
    r = subprocess.run(['pgrep', '-f', f'resume {SESSION_ID}'], capture_output=True, text=True)
    return bool(r.stdout.strip())


def busy(text: str) -> bool:
    tail = '\n'.join(text.splitlines()[-10:])
    return '…' in tail and '(' in tail


def state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    STATE_FILE.write_text(json.dumps(data))


def main() -> None:
    st = state()
    handle = HANDLE_FILE.read_text().strip() if HANDLE_FILE.exists() else ''
    shown = orca('terminal', 'show', '--terminal', handle) if handle else {'ok': False}
    terminal = (shown.get('result') or {}).get('terminal') or {}
    if not shown.get('ok') or not terminal.get('connected'):
        created = orca('terminal', 'create', '--worktree', f'path:{REPO}',
                       '--title', 'v3.1 수집 인수인계', '--command', LAUNCH)
        new = ((created.get('result') or {}).get('terminal') or {}).get('handle')
        log('TERMINAL_RECREATED', old=handle, new=new, ok=bool(created.get('ok')))
        if new:
            HANDLE_FILE.write_text(new + '\n')
            time.sleep(40)
            send(new, RESUME)
            st['lastNudge'] = time.time()
            save_state(st)
        return
    if not claude_running():
        send(handle, LAUNCH)
        log('CLAUDE_RELAUNCHED', handle=handle)
        time.sleep(40)
        send(handle, RESUME)
        st['lastNudge'] = time.time()
        save_state(st)
        return
    now = time.time()
    newest = max(p.stat().st_mtime for p in (WORKLOG, LEDGER) if p.exists())
    text = screen(handle)
    if now - newest > IDLE_S and not busy(text) and now - st.get('lastNudge', 0) > NUDGE_GAP_S:
        send(handle, NUDGE)
        st['lastNudge'] = now
        save_state(st)
        log('NUDGED', idleMin=round((now - newest) / 60, 1), handle=handle)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - the log carries the reason; the timer runs again
        log('WATCHDOG_ERROR', error=f'{type(exc).__name__}: {exc}'[:300])
