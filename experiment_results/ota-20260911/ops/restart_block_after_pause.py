#!/usr/bin/env python3
"""Owner 2026-09-26: "pause 걸면 판당으로 짜르고 새 블록 시작하게해".

Called by run_blocks_campaign.sh when it leaves PAUSE.  A PAUSE cuts at the board that is
running; on resume the current block starts over: its reference/corpus files are set aside
(renamed, never deleted), progress goes back to slot 0 so the reference is measured again on
the bed as it is now, and every board of that block that already started is excluded from the
report with a ``block-restart`` ledger note (append-only).  Nothing to do when the block has
neither a frozen reference nor a started board.

usage: restart_block_after_pause.py <campaign> <progress.json> <overnight dir>
"""
import json
import os
import sys
import time
from pathlib import Path


#: The notes the report treats as exclusions (export_v5_trajectories.EXCLUDING); any other
#: note is informational and must not hide a board from a restart's exclusion.
EXCLUDING = ('block-restart', 'excised-our-side', 'walkover-expunged')


def excluded_attempts(rows, campaign):
    out = set()
    for row in rows:
        if row.get('campaign') == campaign and row.get('note') in EXCLUDING:
            out.update(int(a) for a in (row.get('attempts') or [row.get('attempt')]) if a is not None)
    return out


def restart(campaign, state_path, overnight, now=None):
    now = now or time.strftime('%Y%m%dT%H%M%S')
    state = json.loads(Path(state_path).read_text())
    block = int(state['block'])
    ledger = Path(overnight) / 'v31-ledger.jsonl'
    rows = []
    for line in ledger.read_text(errors='ignore').splitlines() if ledger.exists() else []:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    done = excluded_attempts(rows, campaign)
    started = sorted({int(r['attempt']) for r in rows
                      if r.get('campaign') == campaign and r.get('episodeStarted')
                      and int(r.get('block', -1)) == block and r.get('attempt') is not None} - done)
    frozen = [Path(overnight) / f'{campaign}.block{block}.{suffix}' for suffix in ('env', 'reference.json')]
    # 2026-09-27 21:xx: a reference that failed froze nothing (no .env) and carries which weak UE
    # the next attempt must reattach and which it already did (reference_dl.unstable_on_last_attempt,
    # _refreshed_on_last_attempt).  Setting it aside on every runner start -- keeper's stall restart
    # is one -- erased that each time, and block 4 re-measured the same weak ue3 without end.
    if not frozen[0].exists():
        frozen = frozen[:1]
    frozen = [p for p in frozen if p.exists()]
    if not started and not frozen:
        return None
    # Progress first, then the note, then the files: a crash anywhere leaves a state a retry
    # completes (slot 0 already, the unexcluded boards and the frozen files still found).
    state['slot'] = 0
    if block in state.get('incompleteBlocks', []):
        state['incompleteBlocks'].remove(block)      # the restarted block is a fresh block
    state.setdefault('runnerStarts', []).append(
        {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'kind': 'block-restart-after-pause',
         'block': block, 'excluded': started})
    tmp = str(state_path) + '.tmp'
    Path(tmp).write_text(json.dumps(state))
    os.replace(tmp, state_path)
    if started:
        with open(ledger, 'a') as fh:
            fh.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'note': 'block-restart',
                                 'campaign': campaign, 'block': block, 'attempts': started,
                                 'cause': 'PAUSE mid-block; the block restarts with a new reference '
                                          '(owner 2026-09-26)'}) + '\n')
    for p in frozen:
        p.rename(p.with_name(f'{p.name}.superseded-pause-{now}'))
    return {'block': block, 'excluded': started, 'setAside': [p.name for p in frozen]}


def _selfcheck():
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / 'c.block3.env').write_text('x')
    (d / 'v31-ledger.jsonl').write_text('\n'.join(json.dumps(r) for r in [
        {'campaign': 'c', 'block': 3, 'attempt': 10, 'episodeStarted': True},
        {'campaign': 'c', 'block': 3, 'attempt': 11, 'episodeStarted': False},
        {'campaign': 'c', 'block': 3, 'attempt': 12, 'episodeStarted': True},
        {'campaign': 'c', 'note': 'block-restart', 'block': 3, 'attempts': [10]},
        {'campaign': 'c', 'note': 'correction', 'attempt': 12},
        {'campaign': 'c', 'block': 2, 'attempt': 9, 'episodeStarted': True}]) + '\n')
    (d / 'p.json').write_text(json.dumps({'block': 3, 'slot': 2, 'incompleteBlocks': [3]}))
    got = restart('c', d / 'p.json', d, now='T')
    assert got == {'block': 3, 'excluded': [12], 'setAside': ['c.block3.env']}, got
    assert json.loads((d / 'p.json').read_text())['slot'] == 0
    assert json.loads((d / 'p.json').read_text())['incompleteBlocks'] == []
    assert (d / 'c.block3.env.superseded-pause-T').exists()
    assert restart('c', d / 'p.json', d) is None          # second resume: nothing left to cut
    (d / 'c.block3.reference.json').write_text('{"unstable": ["weak:ue3"]}')   # failed: no .env
    assert restart('c', d / 'p.json', d) is None and (d / 'c.block3.reference.json').exists()
    print('selfcheck ok')


if __name__ == '__main__':
    if sys.argv[1:] == ['--selfcheck']:
        _selfcheck()
    else:
        print(json.dumps(restart(*sys.argv[1:4])))
