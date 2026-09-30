"""세 방식을 **진짜 대결 판만** 골라 비교한다.

부전승(기준선 C0 가 이미 어떤 T 를 충족한 판)을 빼지 않으면 방식 차이가 아니라
그날 베드 상태를 비교하게 된다 -- 2026-09-23 새벽 v4.4 판 31개 중 **17개가 부전승**이었다
([[the-baseline-now-wins-before-the-agent-moves]]).

"성공은 아무 T 나 하나" 이므로([[success-is-any-T-not-T0]]) 부전승 판별은 한 줄이다:
``any(trials[0]['success'].values())``.  ``trials[0]`` 은 언제나 C0 기준선이다.

**시간 교란을 조심하라.**  방식마다 표본이 돈 시각이 다르면 베드 드리프트가 방식 차이로
둔갑한다.  그래서 `--since` 로 창을 자르고, 방식별 표본 수를 항상 같이 찍는다.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
BOARDS = os.path.join(HERE, '..', 'formal38guarded-*')
METHODS = ('three-agent', 'internal-monolith', 'basic-monolith')


def board_summary(path):
    """판 하나의 요약, 읽을 수 없으면 None."""
    episodes = glob.glob(os.path.join(path, 'evidence', '*-episode.json'))
    if not episodes:
        return None
    try:
        episode = json.load(open(episodes[0]))
    except (OSError, ValueError):
        return None
    trials = episode.get('trials') or []
    if not trials:
        return None
    baseline = trials[0].get('success') or {}
    gained = set()
    for trial in trials[1:]:
        for column, ok in (trial.get('success') or {}).items():
            if ok and not baseline.get(column):
                gained.add(column)
    # **조종 선호도.**  방식 차이를 주장하려면 그 차이를 만드는 행동이 있어야 한다.
    # 2026-09-23: 판 4개에서 "internal-monolith 가 조종을 일찍 집는다" 고 읽었는데
    # 전수(17/8/7판)로는 35%/34%/29% 로 구별되지 않았다 -- 행동 지표를 같이 찍어
    # 같은 착시를 반복하지 않는다 ([[read-both-prompts-before-claiming-an-arm-difference]]).
    homes = {k: v for k, v in (trials[0].get('configuration') or {}).items()
             if k.startswith('servingCell@')}
    def _steers(trial):
        config = trial.get('configuration') or {}
        return any(config.get(axis) != home for axis, home in homes.items())
    attempts = trials[1:]
    steering = sum(1 for t in attempts if _steers(t))
    termination = episode.get('termination') or {}
    intents = os.path.join(path, 'intents.json')
    size = None
    if os.path.exists(intents):
        try:
            body = json.load(open(intents))
            size = len(body if isinstance(body, list) else body.get('intents', []))
        except (OSError, ValueError):
            pass
    return dict(method=episode.get('method', ''), trials=len(trials),
                walkover=any(baseline.values()), gained=len(gained), intents=size,
                attempts=len(attempts), steering=steering,
                first_is_steering=bool(attempts) and _steers(attempts[0]),
                termination=termination.get('kernelTermination')
                or termination.get('reason') or '')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', default='', help='판 이름의 타임스탬프 하한 (예: 20260922T203000)')
    parser.add_argument('--intents', type=int, default=7, help='이 크기의 코퍼스만 (기본 7 = v4.4)')
    args = parser.parse_args()

    rows = []
    for path in sorted(glob.glob(BOARDS)):
        stamp = re.search(r'formal38guarded-(\d{8}T\d{6})', path)
        if not stamp or stamp.group(1) < args.since:
            continue
        row = board_summary(path)
        if row is None or (args.intents and row['intents'] != args.intents):
            continue
        row['name'] = stamp.group(1)
        rows.append(row)

    contests = [r for r in rows if not r['walkover']]
    print(f'판 {len(rows)}개 (인텐트 {args.intents}개'
          + (f', {args.since} 이후' if args.since else '') + ')'
          + f'  부전승 {len(rows) - len(contests)}  진짜 대결 {len(contests)}')
    if not contests:
        print('  비교할 판이 없다')
        return
    print('\n진짜 대결 판만 — 방식별')
    for method in METHODS:
        mine = [r for r in contests if r['method'] == method]
        if not mine:
            print(f'  {method:18s} 판 0')
            continue
        won = sum(1 for r in mine if r['gained'])
        terms = collections.Counter(r['termination'][:18] for r in mine)
        attempts = sum(r['attempts'] for r in mine)
        steering = sum(r['steering'] for r in mine)
        first = sum(1 for r in mine if r['first_is_steering'])
        print(f'  {method:18s} 판 {len(mine):2d}  T 를 얻은 판 {won:2d} ({won / len(mine):.0%})'
              f'  평균시행 {sum(r["trials"] for r in mine) / len(mine):.1f}')
        print(f'{"":20s} 조종 시행 {steering}/{attempts}'
              f'({steering / attempts:.0%})  첫 시행이 조종 {first}/{len(mine)}' if attempts else '')
        print(f'{"":20s} 종료: {dict(terms)}')
    smallest = min(len([r for r in contests if r['method'] == m]) for m in METHODS)
    if smallest < 10:
        print(f'\n경고: 가장 적은 방식의 진짜 대결 판이 {smallest}개다.  '
              '표본이 기울면 베드 드리프트가 방식 차이로 둔갑한다 -- 아직 비교하지 마라.')


if __name__ == '__main__':
    main()
