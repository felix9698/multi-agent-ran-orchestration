#!/usr/bin/env python3
"""Per-episode *result* ledger: how far the target was relaxed, and how many moves.

The cost ledger answers "what did it spend". This answers "what did it get",
which is the comparison the paper is about: a method is better if it holds the
original target, or concedes less, or needs fewer live moves to get there.
``T`` and ``C`` sizes have nothing to do with it -- they are internal
representations, not outcomes.

Columns, and why each is here:

  t0Success      did the ORIGINAL joint target hold, with no relaxation at all
  bestTarget     the best target attained (T0 is the original)
  dMax / dMean   how deep the concession was, over requirements: the paper's
                 own axes.  0.0 means nothing was given up
  trials         every dispatched trial
  counted        the ones charged against the budget (contract v4 section 2)
  rolledBack     trials whose configuration was reversed
  settled        trials the Kernel settled without rolling back
  retained       whether the attained configuration was still held at the end
  termination    the axis the episode ended on

Regenerated from the evidence each run, like the cost ledger, and for the same
reason: appending needs state and state drifts from what it summarises.
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Dict, List

EXP = pathlib.Path('/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/'
                   'experiment_results/ota-20260911')

HEADER = ['at', 'method', 'condition', 'block', 'termination', 'kernelTermination',
          't0Success', 'bestTarget', 'dMax', 'dMean', 'trials', 'counted',
          'rolledBack', 'settled', 'retained', 'perOwnerConcession', 'episode']


def row_of(doc: Dict[str, Any], episode: str) -> Dict[str, Any]:
    trials = doc.get('trials') or []
    best = doc.get('bestAttained') or {}
    concession = best.get('concession') or {}
    retained = doc.get('retained') or {}
    termination = doc.get('termination') or {}
    timing = doc.get('timing') or {}
    per_owner = concession.get('perOwner') or {}
    return {
        'at': str(timing.get('t0') or timing.get('prepStart') or '')[:19],
        'method': doc.get('method') or '',
        'condition': (doc.get('condition') or {}).get('name') or '',
        'block': doc.get('block'),
        'termination': termination.get('reason') or '',
        'kernelTermination': termination.get('kernelTermination') or '',
        't0Success': doc.get('t0Success'),
        # ``bestAttained`` is absent when nothing was ever attained -- which is
        # a result, not a gap, so it is written as such rather than left blank.
        'bestTarget': best.get('targetId') or 'none',
        'dMax': concession.get('max', ''),
        'dMean': concession.get('mean', ''),
        'trials': len(trials),
        'counted': sum(1 for t in trials if t.get('counted')),
        'rolledBack': sum(1 for t in trials if t.get('rolledBack')),
        # 2026-09-18: `t['success']` 는 **dict** 다 (`{"T0": false, "T1": false, ...}`).
        # `if t.get('success')` 는 비어 있지 않은 dict 라서 **전부 false 여도 참**이고,
        # 그래서 이 열은 '정착한 시행' 이 아니라 '롤백 안 된 시행' 을 세고 있었다.
        # `retained` 에서 이미 같은 병을 앓았다([[retention-rate-mixes-two-populations]]
        # 의 형제 자리).  값을 본다 -- 어느 목표 하나라도 True 면 그 시행은 정착이다.
        'settled': sum(1 for t in trials
                       if any((t.get('success') or {}).values())
                       and not t.get('rolledBack')),
        'retained': retained.get('qualified'),
        'perOwnerConcession': ' '.join(
            f'{owner}={value}' for owner, value in sorted(per_owner.items())),
        'episode': episode,
    }


def write_tsv(path: pathlib.Path, header: List[str], rows: List[Dict[str, Any]]) -> None:
    def cell(value: Any) -> str:
        text = str(value)
        return text if text.strip() else '-'
    path.write_text('\t'.join(header) + '\n'
                    + ''.join('\t'.join(cell(r[k]) for k in header) + '\n'
                              for r in rows))


def main(argv: List[str]) -> int:
    windows = [EXP / a for a in argv[1:]] or sorted(EXP.glob('window-*'))
    for window in windows:
        if not window.is_dir():
            continue
        rows = []
        for path in sorted(window.rglob('*episode*.json')):
            try:
                doc = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            rows.append(row_of(doc, path.parent.parent.name))
        if not rows:
            continue
        rows.sort(key=lambda r: (str(r['at']), r['episode']))
        write_tsv(window / 'RESULTS-BY-EPISODE.tsv', HEADER, rows)

        # One line per method: the comparison itself.
        by_method: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_method.setdefault(str(row['method']), []).append(row)
        summary = []
        for method, items in sorted(by_method.items()):
            held = [r for r in items if r['t0Success'] is True]
            # A T0 success with no counted trial is a **walkover**: the initial
            # measurement already met every original requirement, so the method
            # was never asked anything.  Counting those as wins makes the arm
            # that acts least look best, which is backwards -- on 2026-09-12 a
            # 1 Mbps load produced exactly that.  They are separated, not
            # dropped: the episode did happen.
            walkover = [r for r in held if int(r['counted']) == 0]
            earned = [r for r in held if int(r['counted']) > 0]
            acted = [r for r in items if int(r['counted']) > 0]
            depths = [float(r['dMax']) for r in acted
                      if str(r['dMax']).replace('.', '', 1).isdigit()]
            summary.append({
                'method': method,
                'episodes': len(items),
                'walkovers': len(walkover),
                'contested': len(acted),
                't0HeldEarned': len(earned),
                'dMaxMeanContested': round(sum(depths) / len(depths), 4) if depths else '-',
                'dMaxWorst': max(depths) if depths else '-',
                'trialsMean': round(sum(int(r['trials']) for r in items) / len(items), 2),
                'countedMean': round(sum(int(r['counted']) for r in items) / len(items), 2),
                'rolledBackTotal': sum(int(r['rolledBack']) for r in items),
                'retainedTrue': sum(1 for r in items if r['retained'] is True),
            })
        write_tsv(window / 'RESULTS-BY-METHOD.tsv',
                  ['method', 'episodes', 'walkovers', 'contested', 't0HeldEarned',
                   'dMaxMeanContested', 'dMaxWorst', 'trialsMean', 'countedMean',
                   'rolledBackTotal', 'retainedTrue'],
                  summary)
        print('%s: %d episodes -> RESULTS-BY-EPISODE.tsv, RESULTS-BY-METHOD.tsv'
              % (window.name, len(rows)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
