"""한 캠페인의 결정 §5.3 지표를 방식별로 뽑는다 (2026-09-23, v4.6).

    python3 ops/campaign_report.py --campaign blocks18-v46r4

판이 스스로 적은 ``condition.campaign`` 으로 고르고, 같은 캠페인 안에서도 라이브 경로
판본(``sourceDigest``)이 둘 이상이면 **따로** 찍는다 -- 캠페인 중 동결을 어겼다면 여기서
보인다.  숫자는 전부 ``experiments/agent_quality.py`` 가 판 기록에서 읽은 것이다.

**비교 주장은 하지 않는다.**  방식별 판 수를 늘 같이 찍고, 가장 적은 방식이 6판(두
블록 순열 한 바퀴) 미만이면 그렇게 적는다.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from experiments.agent_quality import QUALITY_THRESHOLDS, quality_summary  # noqa: E402

METHODS = ('three-agent', 'internal-monolith', 'basic-monolith')
MIN_PER_METHOD = 6
MAX_BYTES = 100 * 2 ** 20          # 2026-09-21 에 2 GiB 로 잘린 판 기록이 하나 있다


def load(campaign, ledger=None, from_block=0):
    """시작 원장이 모집단이다 (v4.7 Codex #18): 캠페인에서 시작한 판마다 원장 행 하나.
    판 기록이 없거나 읽을 수 없는 판도 빠지지 않고 ``missing`` 으로 남는다."""
    ledger = ledger or os.path.join(HERE, 'overnight', 'v31-ledger.jsonl')
    started, rows, missing = [], [], []
    for line in open(ledger, errors='ignore'):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (row.get('campaign') == campaign and row.get('episodeStarted')
                and int(row.get('block') or 0) >= from_block):
            started.append(row)
    # 2026-09-26: the ledger's exclusion notes were never applied here -- a block restarted
    # for a deploy, or a board excised as our own disruption, was still counted.
    excluded = {}
    for line in open(ledger, errors='ignore'):
        try:
            note = json.loads(line)
        except ValueError:
            continue
        if note.get('campaign') == campaign and note.get('note') in EXCLUDING_NOTES:
            for attempt in (note.get('attempts') or [note.get('attempt')]):
                if attempt is not None:
                    excluded[int(attempt)] = note['note']
    kept = []
    for row in started:
        if row.get('attempt') is not None and int(row['attempt']) in excluded:
            EXCLUSIONS.append((row, excluded[int(row['attempt'])]))
        else:
            kept.append(row)
    started = kept
    for row in started:
        paths = glob.glob(os.path.join(HERE, '..', row.get('dir') or '-', 'evidence', '*-episode.json'))
        try:
            if not paths or os.path.getsize(paths[0]) > MAX_BYTES:
                raise OSError
            episode = json.load(open(paths[0]))
        except (OSError, ValueError):
            missing.append(row)
            continue
        episode.setdefault('method', row.get('method'))
        episode['_ledger'] = row
        if walkover(episode):
            # Owner 2026-09-26: T0 met at trial 0 means the block reference was taken on a
            # different bed than the board ran on -- expunged, not counted as a success.
            EXCLUSIONS.append((row, 'walkover-expunged'))
            started = [r for r in started if r is not row]
            continue
        episode['_drift'] = drift(episode, row, campaign)
        rows.append(episode)
    return started, rows, missing


#: Ledger notes that take an attempt out of the population.
EXCLUDING_NOTES = ('block-restart', 'excised-our-side', 'walkover-expunged')
#: (ledger row, why) for every attempt taken out; printed by main().
EXCLUSIONS = []
#: Bed drift worth an owner's review (my choice, 2026-09-26): trial 0 (the block's own
#: configuration A) more than 30% away from the block reference on ue1 or the gnb1 sum.
DRIFT_LIMIT = 0.30


def walkover(episode):
    trials = sorted(episode.get('trials') or (), key=lambda t: int(t.get('trialIndex', 0)))
    return bool(trials) and int(trials[0].get('trialIndex', -1)) == 0 and \
        bool(dict(trials[0].get('success') or {}).get('T0'))


def drift(episode, row, campaign):
    """{'ue1': ratio, 'gnb1Sum': ratio, 'flag': bool} of trial 0 against the block reference
    measured in the same configuration A; None when either side is missing."""
    try:
        ref = json.load(open(os.path.join(HERE, 'overnight',
                                          f"{campaign}.block{row['block']}.reference.json")))['reference']
        trials = sorted(episode.get('trials') or (), key=lambda t: int(t.get('trialIndex', 0)))
        k = dict(trials[0].get('kpis') or {})
        ue1 = k.get('dlGoodputMbps@ue1')
        cell = sum(float(v) for key, v in k.items() if key.startswith('cellGoodputMbps@12345678'))
        out = {'ue1': round(ue1 / ref['ue1'], 3) if ue1 and ref.get('ue1') else None,
               'gnb1Sum': round(cell / ref['gnb1Sum'], 3) if cell and ref.get('gnb1Sum') else None}
        out['flag'] = any(v is not None and abs(v - 1) > DRIFT_LIMIT for v in out.values())
        return out
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def handovers(campaign):
    """판별 핸드오버 합계 (run_blocks_campaign.sh 가 판마다 적는다, 계획 4절)."""
    path = os.path.join(HERE, 'overnight', f'{campaign}-handovers.jsonl')
    table = collections.defaultdict(collections.Counter)
    if os.path.exists(path):
        for line in open(path, errors='ignore'):
            row = json.loads(line)
            for cell in ('gnb1', 'gnb2'):
                table[row.get('method')].update(row.get(cell) or {})
    return table


def paper_metric(rows):
    """v4.7 논문 지표: 판마다 ``paperEvaluation.best`` (공통 평가기, 0.05 정밀도)."""
    bests = [(e.get('paperEvaluation') or {}).get('best') for e in rows]
    attained = [b for b in bests if b]
    owners = next(((e.get('paperEvaluation') or {}).get('ownerPriority') for e in rows
                   if e.get('paperEvaluation')), [])
    mean_bins = [round(sum(b['ownerBins'][k] for b in attained) / len(attained), 2)
                 for k in range(len(owners))] if attained else []
    return {'attained': len(attained), 't0': sum(1 for b in attained if b.get('t0')),
            'noEvaluation': sum(1 for e in rows if not e.get('paperEvaluation')),
            'owners': owners, 'meanBins': mean_bins}


def lockdown_classes(episodes):
    """잠금 시행을 커널 상세로 가른다 -- 도착/잠금 불일치 조사(§6)와 같은 분류."""
    table = collections.Counter()
    for episode in episodes:
        for trial in episode.get('trials') or []:
            kernel = trial.get('kernel') or {}
            if (kernel.get('terminalState') != 'INCIDENT_LOCKDOWN'
                    and (kernel.get('recovery') or {}).get('resolution') != 'INCIDENT_LOCKDOWN'):
                continue
            detail = kernel.get('detail') or ''
            if 'could not be read' in detail or 'did not produce an observation' in detail:
                table['readback-unavailable'] += 1
            elif 'still shows the baseline' in detail or 'confirmed only' in detail:
                table['readback-shows-baseline'] += 1
            elif 'confirmed none' in detail:
                table['readback-off-plan'] += 1
            elif 'refused' in detail:
                table['r1-refused'] += 1
            else:
                table['other'] += 1
    return dict(table)


def retention_reasons(episodes):
    return dict(collections.Counter(
        (row.get('reason'), bool(row.get('retain')))
        for episode in episodes for row in episode.get('retentionDecisions') or []))


def report(campaign, episodes, started=(), missing=()):
    lines = [f'캠페인 {campaign}: 시작한 판 {len(started)}개 (기록 있음 {len(episodes)}, 기록 없음 {len(missing)} -- 없는 판은 미달성으로 센다)']
    ho = handovers(campaign)
    digests = sorted({str(e.get('sourceDigest') or '')[:12] for e in episodes})
    if len(digests) > 1:
        lines.append(f'  경고: 라이브 경로 판본이 {len(digests)}개다 {digests} -- 캠페인 중 동결이 깨졌다')
    counts = {m: sum(1 for e in episodes if e.get('method') == m) for m in METHODS}
    lines.append('  방식별 판: ' + ', '.join(f'{m} {n}' for m, n in counts.items()))
    if min(counts.values() or [0]) < MIN_PER_METHOD:
        lines.append(f'  아직 비교하지 마라: 가장 적은 방식이 {min(counts.values() or [0])}판 '
                     f'(< {MIN_PER_METHOD}, 순서 순열 한 바퀴)')
    for method in METHODS:
        rows = [e for e in episodes if e.get('method') == method]
        if not rows:
            continue
        s = quality_summary(rows)
        n_started = sum(1 for r in started if r.get('method') == method)
        lines.append(f'\n[{method}] 시작 {n_started} · 기록 N={s["N"]}')
        pm = paper_metric(rows)
        lines.append(f'  논문 지표(공통 평가기): 달성 {pm["attained"]}/{n_started}, 그중 T0 {pm["t0"]}'
                     f', 평가 없음 {pm["noEvaluation"]}'
                     + (f', 달성 판의 소유자별 평균 양보 칸 {dict(zip(pm["owners"], pm["meanBins"]))}'
                        if pm['meanBins'] else ''))
        h = ho.get(method)
        if h:
            lines.append(f'  핸드오버: 시도 {h["triggered"]} · 완료 {h["completed"]} · 무결성 실패 '
                         f'{h["integrityFail"]} · Ongoing 거절 {h["ongoingRefusals"]}')
        recovery = sum(1 for r in started if r.get('method') == method and
                       ((r.get('episodeTermination') or {}).get('kernelTermination') == 'RECOVERY_FAILURE'))
        lines.append(f'  복구 실패(RECOVERY_FAILURE) 판: {recovery}')
        lines.append('  초기상태: ' + ', '.join(f'{k} {v}' for k, v in s['initialState'].items()))
        operator = (s['initialOwnerOriginalMet'].get('operator-gnb1')
                    or s['initialOwnerOriginalMet'].get('operator-gnb2'))
        if operator:
            lines.append(f'  사업자 원수준 초기 충족: {operator["met"]} / 불충족 {operator["notMet"]}'
                         f' / 모름 {operator["unknown"]}')
        for q in QUALITY_THRESHOLDS:
            block = s['tQ'][str(q)]['all']
            unmet = s['tQ'][str(q)]['unmetAtReference']
            mean = block['restrictedMeanMs']
            lines.append(f'  T_{q}: 도달 {block["attained"]}/{block["total"]}'
                         + (f', mean(min(T,B)) {mean / 1000:.0f} s' if mean is not None else '')
                         + f'  | 기준관측 미충족 부분군 {unmet["attained"]}/{unmet["total"]}')
        eff = s['efficiency']
        lines.append(f'  파견: 정식 {eff["formalDispatches"]:.1f} · 후속 탐색 {eff["searchDispatches"]:.1f}'
                     f' · 무효 합계 {eff["invalidDispatches"]}')
        unresolved = s['finalPreference']['unresolvedFraction']
        lines.append(f'  B 까지 미해결 비율: {unresolved:.2f}' if unresolved is not None
                     else '  B 까지 미해결 비율: -')
        lines.append(f'  유지 판정: {retention_reasons(rows)}')
        lines.append(f'  잠금 부류: {lockdown_classes(rows)}')
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', required=True)
    # 2026-09-26: the comparison boundary moves with the design (v5 UE2 deadline A-p75 from block 15).
    parser.add_argument('--from-block', type=int, default=0,
                        help='only boards of this block and later (the current comparison boundary)')
    args = parser.parse_args()
    started, episodes, missing = load(args.campaign, from_block=args.from_block)
    print(report(args.campaign, episodes, started, missing))
    for row in missing:
        print(f'  기록 없음: attempt {row.get("attempt")} {row.get("method")} {row.get("dir")}')
    for row, why in EXCLUSIONS:
        print(f'  제외: attempt {row.get("attempt")} {row.get("method")} ({why})')
    for e in episodes:
        d = e.get('_drift') or {}
        if d.get('flag'):
            print(f'  검토: attempt {e["_ledger"].get("attempt")} {e.get("method")} 기준 대비 시행 0 '
                  f'ue1 {d.get("ue1")} · gnb1 합 {d.get("gnb1Sum")} (±{int(DRIFT_LIMIT*100)}% 밖)')


if __name__ == '__main__':
    main()
