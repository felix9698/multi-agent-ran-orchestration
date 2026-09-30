"""Export every v5 board (block >= --from-block) of a campaign: how T and C were formed, which
target/control each trial chose and why, and what each trial measured -- as one Markdown report
and graph-ready CSVs.

    python3 ops/export_v5_trajectories.py --campaign blocks18-v47 --from-block 16 [--out DIR]

Boards come from the start ledger (overnight/v31-ledger.jsonl) minus the exclusion notes the
campaign report also drops (block-restart, excised-our-side, walkover-expunged).
"""
import argparse
import csv
import datetime
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXCLUDING = ('block-restart', 'excised-our-side', 'walkover-expunged')
KST = datetime.timezone(datetime.timedelta(hours=9))
WEIGHTS = {'I4e.r1': 25, 'I1g.r1': 13, 'I2d.r1': 7, 'I3g.r1': 3}   # v5.1+ weighted p, the evaluator's


def wp(levels):
    return sum(w * int((levels or {}).get(k, 0) or 0) for k, w in WEIGHTS.items())


AXES = ('servingCell@ue1', 'servingCell@ue2', 'servingCell@ue3', 'dlPrbCap@ue1', 'dlPrbCap@ue2',
        'dlPrbCap@ue3', 'pfWeight@ue1', 'pfWeight@ue2', 'pfWeight@ue3', 'txAttenuationDb@12345678', 'txAttenuationDb@87654321')


def kst(iso):
    if not iso:
        return ''
    try:
        return datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00')).astimezone(KST).strftime('%m-%d %H:%M:%S')
    except ValueError:
        return str(iso)


def boards(campaign, from_block):
    excluded, started = {}, []
    for line in open(os.path.join(HERE, 'overnight', 'v31-ledger.jsonl'), errors='ignore'):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('campaign') != campaign:
            continue
        if row.get('note') in EXCLUDING:
            for a in (row.get('attempts') or [row.get('attempt')]):
                if a is not None:
                    excluded[int(a)] = row['note']
        elif row.get('episodeStarted') and int(row.get('block') or 0) >= from_block:
            started.append(row)
    out = []
    for row in started:
        if int(row['attempt']) in excluded:
            continue
        paths = glob.glob(os.path.join(ROOT, row.get('dir') or '-', 'evidence', '*-episode.json'))
        if paths:
            out.append((row, paths[0]))
    return sorted(out, key=lambda x: int(x[0]['attempt']))


def reference(campaign, block):
    try:
        return json.load(open(os.path.join(HERE, 'overnight', f'{campaign}.block{block}.reference.json')))
    except (OSError, ValueError):
        return {}


def ratio(kpis):
    v = (kpis.get('deadlineSuccessRatio@ue2') or {}).get('byDeadlineMs') if isinstance(kpis.get('deadlineSuccessRatio@ue2'), dict) else None
    return next(iter(v.values()), '') if v else ''


def fmt(v, nd=2):
    return f'{v:.{nd}f}' if isinstance(v, (int, float)) and not isinstance(v, bool) else ('' if v is None else str(v))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--campaign', default='blocks18-v47')
    ap.add_argument('--from-block', type=int, default=16)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    stamp = datetime.datetime.now(KST).strftime('%Y%m%dT%H%M')
    out = args.out or os.path.join(ROOT, 'reports', f'v5-trajectories-{stamp}')
    os.makedirs(out, exist_ok=True)

    rows = {k: [] for k in ('boards', 'trials', 'targets', 'controls', 'calls')}
    md = [f'# v5 궤적 전체 기록 — {args.campaign}, 블록 {args.from_block} 이후', '',
          f'생성: {datetime.datetime.now(KST):%Y-%m-%d %H:%M} KST · 원장 기준 판(제외 노트 {", ".join(EXCLUDING)} 적용) · '
          '공통 평가기 p 는 작을수록 좋음(v5: 사전식 E → UE1 → UE2 마감 → UE3 / v5.1: ROC 가중합 '
          '25kE+13k1+7k2+3k3, A = 1 − p/960 — 판마다 paperEvaluation.rule 이 정한다; 칸 = ceil(20·양보율)). '
          '그래프용 CSV: boards.csv · trials.csv · targets.csv · controls.csv · calls.csv (같은 폴더).', '']
    for row, path in boards(args.campaign, args.from_block):
        ep = json.load(open(path))
        method, block, attempt = ep.get('method'), int(row['block']), int(row['attempt'])
        ref = reference(args.campaign, block)
        pe = ep.get('paperEvaluation') or {}
        pe_rows = {r.get('trialIndex'): r for r in pe.get('trials') or []}
        best = pe.get('best') or {}
        term = ep.get('termination') or {}
        retained = ep.get('retained') or {}
        calls = ep.get('calls') or []
        prompts_dir = os.path.relpath(os.path.dirname(path), ROOT)

        md += [f'## 판 {attempt} · 블록 {block} 슬롯 {row.get("slot")} · {method}', '',
               f'- 디렉터리: `{row.get("dir")}` · 프롬프트 원문: `{prompts_dir}/*-prompts/`',
               f'- 모델: {json.dumps(ep.get("models"), ensure_ascii=False)}',
               f'- 블록 참조: L={ref.get("loadMbps")} Mbps · 참조 {ref.get("reference")} · d={fmt(ref.get("ue2DeadlineMs"),1)} ms ({ref.get("ue2DeadlineRule")}) · A@d={ref.get("ue2SuccessAtDInA")} · E(기준 감쇠)={ref.get("energyBaselineDb")} dB',
               f'- 종료: {term.get("reason") if isinstance(term, dict) else term} — {(term.get("detail") if isinstance(term, dict) else "")}',
               f'- Kernel 종료: {(term.get("kernelTermination") if isinstance(term, dict) else "") or ""} · 유지: {retained.get("controlId")} for {retained.get("targetId")} qualified={retained.get("qualified")} — {retained.get("detail")}',
               f'- 공통 평가기 최선: 달성={best.get("attained")} · p={best.get("p")} · 칸={best.get("ownerBins")} · 시행 {best.get("trialIndex")} · T0={best.get("t0")}', '']

        # intents
        md += ['### 인텐트 (운용자 입력)', '', '| id | 소유자 | KPI | 원래 요구 | 하한(bound) | 단계 |', '|---|---|---|---|---|---|']
        for it in ep.get('intents') or []:
            rq = it.get('requirement') or {}
            md.append(f'| {it.get("intentId")} | {it.get("owner")} | {rq.get("kpi")}{"@"+str(it.get("ueId")) if it.get("ueId") else ""} | {rq.get("value", rq.get("threshold", ""))} | {rq.get("bound")} | {rq.get("steps")} |')
        md.append('')

        # formation calls
        md += ['### 형성·결정 호출 (LLM)', '', '| # | 시작 | 역할 | 단계 | 모델 | 지연 ms | 입력 토큰 | 출력 토큰 | 수용 | 재질문 | 대체 사유 | 버려진 항목 |', '|---|---|---|---|---|---|---|---|---|---|---|---|']
        for i, c in enumerate(calls, 1):
            dropped = '; '.join(c.get('dropped') or [])[:220]
            md.append(f'| {i} | {kst(c.get("startedAt"))} | {c.get("role")} | {c.get("phase")} | {c.get("model")} | {fmt(c.get("latencyMs"),0)} | {c.get("inputTokens")} | {c.get("outputTokens")} | {c.get("accepted")} | {c.get("repairRetries")} | {c.get("fallbackReason") or ""} | {dropped} |')
            rows['calls'].append({'attempt': attempt, 'block': block, 'method': method, 'n': i, 'startedAt': c.get('startedAt'),
                                  'role': c.get('role'), 'phase': c.get('phase'), 'model': c.get('model'),
                                  'latencyMs': c.get('latencyMs'), 'inputTokens': c.get('inputTokens'),
                                  'outputTokens': c.get('outputTokens'), 'accepted': c.get('accepted'),
                                  'repairRetries': c.get('repairRetries'), 'fallbackReason': c.get('fallbackReason')})
        md.append('')

        # T
        T = ep.get('T') or {}
        t0 = T.get('t0') or {}
        reqs = sorted(set().union(*[set((a.get('requirements') or {}).keys()) for a in [t0] + list(T.get('alternatives') or [])]))
        md += ['### T — 목표 집합 (T0 = 원래 요구, 나머지는 형성 단계가 고른 완화 목표)', '',
               '| 목표 | p | ' + ' | '.join(reqs) + ' | 단계(level) |', '|---' * (len(reqs) + 3) + '|']
        for a in [dict(t0, targetId=t0.get('targetId', 'T0'))] + list(T.get('alternatives') or []):
            r = a.get('requirements') or {}
            md.append(f'| {a.get("targetId")} | {wp(a.get("levels"))} | ' + ' | '.join(fmt(r.get(k), 3) for k in reqs) + f' | {json.dumps(a.get("levels") or {})} |')
            rows['targets'].append(dict({'attempt': attempt, 'block': block, 'method': method, 'targetId': a.get('targetId'),
                                         'cost': a.get('cost'), 'p': wp(a.get('levels'))}, **{k: r.get(k) for k in reqs},
                                        **{'level:' + k: v for k, v in (a.get('levels') or {}).items()}))
        md.append('')

        # C
        C = ep.get('C') or {}
        cands = C.get('candidates') or []
        base = (cands[0].get('configuration') if cands else {}) or {}
        md += ['### C — 제어 후보 (C0 대비 바뀐 축만)', '', '| 제어 | 바뀐 축 | 형성 근거 |', '|---|---|---|']
        for cd in cands:
            cfg = cd.get('configuration') or {}
            diff = ', '.join(f'{k}={v}' for k, v in cfg.items() if base.get(k) != v) or '(기준)'
            md.append(f'| {cd.get("controlId")} | {diff} | {(cd.get("rationale") or "; ".join(cd.get("applicability") or [])).replace("|", "/")[:300]} |')
            rows['controls'].append(dict({'attempt': attempt, 'block': block, 'method': method, 'controlId': cd.get('controlId'),
                                          'rationale': cd.get('rationale')}, **{k: cfg.get(k) for k in AXES}))
        md.append('')

        # trajectory
        md += ['### 궤적 — 시행마다 무엇을 골랐고 왜, 무엇이 측정됐나', '',
               '| 시행 | 적용 시각 | 목표 | 제어 | 결정자 | 결정 ms | 결과 | ue1 Mbps | ue2 Mbps | ue3 Mbps | UE2 마감 | gnb1 감쇠 | gnb2 감쇠 | 평가 달성 | p | 칸[E,UE1,I2d,UE3,I2g] | 누적 최선 p | 근거 |',
               '|' + '---|' * 18]
        running = None
        for t in ep.get('trials') or []:
            k = t.get('kpis') or {}
            d = t.get('decision') or {}
            pr = pe_rows.get(t.get('trialIndex')) or {}
            if pr.get('attained') and pr.get('p') is not None:
                running = pr['p'] if running is None else min(running, pr['p'])
            ex = t.get('executionStatus') or {}
            md.append(f'| {t.get("trialIndex")} | {kst(t.get("appliedAt"))} | {d.get("targetId") or t.get("proposedTargetId") or ""} | {t.get("controlId")} | '
                      f'{d.get("role") or ""}/{d.get("model") or ""} | {fmt(d.get("decisionLatencyMs"),0)} | {ex.get("outcome") or ""} | '
                      f'{fmt(k.get("dlGoodputMbps@ue1"))} | {fmt(k.get("dlGoodputMbps@ue2"))} | {fmt(k.get("dlGoodputMbps@ue3"))} | {fmt(ratio(k),3)} | '
                      f'{k.get("cellTxAttenuationDb@12345678","")} | {k.get("cellTxAttenuationDb@87654321","")} | {pr.get("attained","")} | {pr.get("p","")} | {pr.get("ownerBins","")} | {running if running is not None else ""} | '
                      f'{(d.get("rationale") or "").replace("|", "/")[:260]} |')
            cfg = t.get('configuration') or {}
            bins = pr.get('ownerBins') or [None] * 5
            rows['trials'].append(dict({
                'attempt': attempt, 'block': block, 'method': method, 'trial': t.get('trialIndex'),
                'appliedAt': t.get('appliedAt'), 'elapsedMs': t.get('elapsedMs'),
                'targetId': d.get('targetId') or t.get('proposedTargetId'), 'controlId': t.get('controlId'),
                'role': d.get('role'), 'model': d.get('model'), 'decisionLatencyMs': d.get('decisionLatencyMs'),
                'outcome': ex.get('outcome'), 'rolledBack': t.get('rolledBack'),
                'observationValid': (t.get('observationValidity') or {}).get('valid'),
                'ue1Mbps': k.get('dlGoodputMbps@ue1'), 'ue2Mbps': k.get('dlGoodputMbps@ue2'), 'ue3Mbps': k.get('dlGoodputMbps@ue3'),
                'ue2DeadlineRatio': ratio(k), 'gnb1AttenDb': k.get('cellTxAttenuationDb@12345678'), 'gnb2AttenDb': k.get('cellTxAttenuationDb@87654321'),
                'gnb1CellMbps': k.get('cellGoodputMbps@12345678'), 'gnb2CellMbps': k.get('cellGoodputMbps@87654321'),
                'attained': pr.get('attained'), 'p': pr.get('p'), 'binE': bins[0], 'binUE1': bins[1], 'binI2d': bins[2],
                'binUE3': bins[3], 'binI2g': bins[4], 'runningBestP': running, 'rationale': d.get('rationale')},
                **{'cfg:' + a: cfg.get(a) for a in AXES}))
        md.append('')
        firsts = [r.get('trialIndex') for r in pe.get('trials') or [] if r.get('attained')]
        rows['boards'].append({
            'attempt': attempt, 'block': block, 'slot': row.get('slot'), 'method': method, 'dir': row.get('dir'),
            'loadMbps': ref.get('loadMbps'), 'refUe1': (ref.get('reference') or {}).get('ue1'), 'refUe2': (ref.get('reference') or {}).get('ue2'),
            'refUe3': (ref.get('reference') or {}).get('ue3'), 'refGnb1Sum': (ref.get('reference') or {}).get('gnb1Sum'),
            'deadlineMs': ref.get('ue2DeadlineMs'), 'aAtD': ref.get('ue2SuccessAtDInA'),
            'trials': len(ep.get('trials') or []) - 1, 'attained': best.get('attained'), 'bestP': best.get('p'),
            'bestTrial': best.get('trialIndex'), 't0': best.get('t0'), 'firstAttainTrial': firsts[0] if firsts else None,
            'termination': term.get('reason') if isinstance(term, dict) else term,
            'kernelTermination': term.get('kernelTermination') if isinstance(term, dict) else None,
            'retainedControl': retained.get('controlId'), 'retainedQualified': retained.get('qualified'),
            'llmCalls': len(calls), 'llmLatencyMs': sum(float(c.get('latencyMs') or 0) for c in calls),
            'inputTokens': sum(int(c.get('inputTokens') or 0) for c in calls),
            'outputTokens': sum(int(c.get('outputTokens') or 0) for c in calls),
            'T_size': 1 + len(T.get('alternatives') or []), 'C_size': len(cands)})

    # method summary and board index at the top
    import statistics as st
    summary = ['## 방식별 요약', '', '| 방식 | 판 | 평가 달성 | T0 | E 지킨 판(칸0) | p 중앙값 | p 최소 | 첫 달성 시행 중앙값 | 판당 LLM 지연 합(s) 중앙값 | 판당 입력 토큰 중앙값 |',
               '|---|---|---|---|---|---|---|---|---|---|']
    for m in sorted({b['method'] for b in rows['boards']}):
        bs = [b for b in rows['boards'] if b['method'] == m]
        att = [b for b in bs if b['attained']]
        ps = [b['bestP'] for b in att if b['bestP'] is not None]
        eheld = sum(1 for b in att if any(t['attempt'] == b['attempt'] and t['trial'] == b['bestTrial'] and t['binE'] == 0
                                         for t in rows['trials']))
        fa = [b['firstAttainTrial'] for b in att if b['firstAttainTrial'] is not None]
        summary.append(f'| {m} | {len(bs)} | {len(att)} | {sum(1 for b in bs if b["t0"])} | {eheld} | '
                       f'{st.median(ps) if ps else ""} | {min(ps) if ps else ""} | {st.median(fa) if fa else ""} | '
                       f'{round(st.median(b["llmLatencyMs"] for b in bs) / 1000, 1)} | {st.median(b["inputTokens"] for b in bs)} |')
    summary += ['', '판 목록: ' + ' · '.join(f'[{b["attempt"]} {b["method"]}](#판-{b["attempt"]}--블록-{b["block"]}-슬롯-{b["slot"]}--{b["method"]})'
                                           for b in rows['boards']), '']
    md[3:3] = summary

    for name, data in rows.items():
        if not data:
            continue
        keys = []
        for d in data:
            keys += [k for k in d if k not in keys]
        with open(os.path.join(out, f'{name}.csv'), 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(data)
    with open(os.path.join(out, 'README.md'), 'w') as f:
        f.write('\n'.join(md) + '\n')
    print(out, {k: len(v) for k, v in rows.items()})


if __name__ == '__main__':
    main()
