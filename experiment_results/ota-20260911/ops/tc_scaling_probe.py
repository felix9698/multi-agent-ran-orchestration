#!/usr/bin/env python3
"""T·C 개수에 따른 3A 호출의 토큰·지연 측정 (오프라인 재생, 베드 무접촉).

실판 한 판의 저장된 프롬프트(<board>/evidence/AGENT-*-prompts)를 뼈대로
  1) target 형성: "up to eight" 을 N 으로 바꿔 호출
  2) control 형성: construction_policy.retain 을 C 로 바꿔 호출
  3) trajectory 선택: 1)·2) 의 최대 출력에서 앞 T-1 개 대안·앞 C 개 후보를 잘라 넣어 호출
각 조건을 --reps 번, 순서를 섞고 첫 줄에 nonce 를 붙여(캐시 방지) 같은 모델에 던진다.
실행: python3 ops/run_with_proxy.py ops/tc_scaling_probe.py --prompts <dir>
"""
import argparse, csv, datetime, json, os, random, re, statistics, sys, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from decision.llm_backend import ClaudeBackend, LLMBackendType  # noqa: E402

KST = datetime.timezone(datetime.timedelta(hours=9))


def split(path):
    head, _, tail = Path(path).read_text().partition('\n\nOUTPUT SCHEMA:')
    return json.loads(head.split('\n', 1)[1]), '\n\nOUTPUT SCHEMA:' + tail


def render(inputs, tail):
    return 'INPUTS:\n' + json.dumps(inputs, separators=(',', ':')) + tail


def resolved(alt, auth, t0, tid):
    """형성 출력(levels 만)을 궤적 입력의 해석된 대안 형식으로. cost=Σk² 는 실판 값과 같은 꼴(근사)."""
    lv = {r: int(alt.get('levels', {}).get(r, 0)) for r in t0['levels']}
    req = {r: (auth[r]['levels'][min(k, len(auth[r]['levels']) - 1)] if auth[r].get('levels') else t0['requirements'][r])
           for r, k in lv.items()}
    con = {r: (k / auth[r]['steps'] if auth[r].get('steps') else 0.0) for r, k in lv.items()}
    con['I2d.r1#deadline'] = 0.0
    return {'targetId': tid, 'requirements': req, 'concession': con, 'levels': lv,
            'cost': float(sum(k * k for k in lv.values())), 'deadlines': t0['deadlines'],
            'deadlineLevels': t0['deadlineLevels']}


def observed(kpis, a):
    v = kpis.get(f"{a['kpi']}@{a['scope'].split('@', 1)[1]}")
    return next(iter(v['byDeadlineMs'].values()), None) if isinstance(v, dict) else v


def verdict(x, thr, op):
    if x is None:
        return 'UNKNOWN'
    return 'PASS' if (x >= thr if op == '>=' else x <= thr) else 'FAIL'


def rebind_targets(inp, alts):
    """대안을 바꾸면 그 대안을 가리키는 판정·격차·출처도 새 대안 기준으로 다시 쓴다(옛 T 참조 금지)."""
    tc = inp['input.target_contract']
    auth, t0 = tc['authorization'], tc['t0']
    tc['alternatives'] = alts
    tc['provenance']['targetMembership']['modelAdditions'] = [
        {'targetId': x['targetId'], 'levels': dict(x['levels'], **{'I2d.r1#deadline': 0})} for x in alts]
    tc['provenance']['modelSelection']['order'] = [
        {'levels': x['levels'], 'deadlineLevels': x['deadlineLevels'], 'targetId': x['targetId'],
         'selectionRole': None, 'reason': '', 'evidenceRefs': []} for x in alts]
    tc['provenance']['notes'] = [n for n in tc['provenance']['notes'] if not n.startswith('T holds')] + [
        f'T holds 1 mandatory and {len(alts)} model-selected targets']
    for ob in inp['input.observations']:
        ob['verdicts'] = {t['targetId']: {r: verdict(observed(ob['kpis'], auth[r]), t['requirements'][r], auth[r]['op'])
                                         for r in t['requirements']} for t in [t0] + alts}
    nxt = min(alts, key=lambda t: t['cost']) if alts else None
    for r, g in inp['input.kpi_gaps']['perRequirement'].items():
        if nxt is None:
            g.pop('againstNext', None)
            continue
        thr = nxt['requirements'][r]
        x = g.get('observed')
        g['againstNext'] = {'targetId': nxt['targetId'], 'threshold': thr,
                            'shortfall': (round(max(0.0, thr - x), 6) if x is not None else None), 'verdict': verdict(x, thr, g['op'])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prompts', required=True)
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--t', default='2,4,8,16')
    ap.add_argument('--c', default='3,6,11,20,30')
    ap.add_argument('--max-tokens', type=int, default=16000)
    ap.add_argument('--out', default=None)
    ap.add_argument('--method', default='three-agent', choices=('three-agent', 'internal-monolith'),
                    help='internal-monolith: one formation call carries both limits (T x C grid), '
                         'then the monolith selection prompt; --prompts must be an IM board')
    ap.add_argument('--im-t', default='2,8,16')
    ap.add_argument('--im-c', default='3,11,30')
    a = ap.parse_args()
    P = Path(a.prompts)
    Ts, Cs = [int(x) for x in a.t.split(',')], [int(x) for x in a.c.split(',')]
    out = Path(a.out or ROOT / 'experiment_results/ota-20260911/reports' /
               f'tc-scaling-{datetime.datetime.now(KST):%Y%m%dT%H%M}')
    out.mkdir(parents=True, exist_ok=True)
    be = ClaudeBackend(backend_type=LLMBackendType.CLAUDE_SONNET)
    if not be.is_available():
        sys.exit('refused: backend unavailable (run through run_with_proxy.py)')
    im = a.method == 'internal-monolith'
    if im:
        sysp = {'form': (P / '001-monolith-system.txt').read_text(),
                'trajectory': (P / '002-monolith-system.txt').read_text()}
        m_in, m_tail = split(P / '001-monolith-request.txt')
        j_in, j_tail = split(P / '002-monolith-request.txt')
    else:
        sysp = {r: (P / f'{n}-{r}-system.txt').read_text() for n, r in (('001', 'target'), ('002', 'control'), ('003', 'trajectory'))}
        t_in, t_tail = split(P / '001-target-request.txt')
        c_in, c_tail = split(P / '002-control-request.txt')
        j_in, j_tail = split(P / '003-trajectory-request.txt')
    rows, answers = [], {}
    f = open(out / 'calls.csv', 'w', newline='')
    w = csv.DictWriter(f, ['phase', 'T', 'C', 'rep', 'latencyS', 'inputTokens', 'outputTokens', 'reasoningTokens',
                           'visibleOutputTokens', 'returned', 'parsed', 'truncated', 'servedModel', 'error'])
    w.writeheader()

    def call(phase, T, C, rep, system, prompt, count):
        r = be.generate(f'REQUEST-NONCE: {uuid.uuid4().hex}\n' + prompt, system, {'maxTokens': a.max_tokens})
        n = count(r.parsed_json) if r.parsed_json else None
        row = {'phase': phase, 'T': T, 'C': C, 'rep': rep, 'latencyS': round(r.latency_ms / 1000, 2),
               'inputTokens': r.input_tokens, 'outputTokens': r.output_tokens, 'reasoningTokens': r.reasoning_tokens,
               'visibleOutputTokens': (r.output_tokens - r.reasoning_tokens) if None not in (r.output_tokens, r.reasoning_tokens) else None,
               'returned': n, 'parsed': r.parsed_json is not None,
               'truncated': bool(r.output_tokens and r.output_tokens >= a.max_tokens), 'servedModel': r.response_model,
               'error': (r.error or '')[:200]}
        w.writerow(row); f.flush(); rows.append(row)
        print(phase, T, C, rep, row['latencyS'], row['inputTokens'], row['outputTokens'], n, flush=True)
        return r.parsed_json

    # 1) 형성
    if im:
        # One call forms T and C together: vary both limits on a small grid.
        grid = [(T, C, k) for T in (int(x) for x in a.im_t.split(',')) for C in (int(x) for x in a.im_c.split(','))
                for k in range(a.reps)]
        random.shuffle(grid)
        for T, C, k in grid:
            inp = json.loads(json.dumps(m_in)); inp['input.construction_policy']['retain'] = C
            s = sysp['form'].replace('up to eight additional', f'up to {T - 1} additional')
            ans = call('im-formation', T, C, k, s, render(inp, m_tail),
                       lambda p: f"{len(p.get('alternatives') or [])}+{len(p.get('candidates') or [])}")
            for key, field in ((('target', T), 'alternatives'), (('control', C), 'candidates')):
                if ans and len(ans.get(field) or []) > len((answers.get(key) or {}).get(field) or []):
                    answers[key] = {field: ans.get(field)}
    jobs = [] if im else [('target', T, None, k) for T in Ts for k in range(a.reps)] + \
           [('control', None, C, k) for C in Cs for k in range(a.reps)]
    random.shuffle(jobs)
    tsys = sysp.get('target', '')
    for phase, T, C, k in jobs:
        if phase == 'target':
            s = tsys.replace('up to eight additional', f'up to {T - 1} additional')
            ans = call(phase, T, None, k, s, render(t_in, t_tail), lambda p: len(p.get('alternatives') or []))
            key = ('target', T)
        else:
            inp = json.loads(json.dumps(c_in)); inp['input.construction_policy']['retain'] = C
            ans = call(phase, None, C, k, sysp['control'], render(inp, c_tail), lambda p: len(p.get('candidates') or []))
            key = ('control', C)
        if ans and len(ans.get('alternatives') or ans.get('candidates') or []) > len(
                (answers.get(key) or {}).get('alternatives') or (answers.get(key) or {}).get('candidates') or []):
            answers[key] = ans
    json.dump({f'{k[0]}-{k[1]}': v for k, v in answers.items()}, open(out / 'formation-answers.json', 'w'), indent=1)

    # 2) 선택: 가장 큰 형성 출력에서 잘라 쓴다(부분집합이 중첩되도록)
    tc = j_in['input.target_contract']
    auth, t0 = tc['authorization'], tc['t0']
    alts = max((v['alternatives'] for k, v in answers.items() if k[0] == 'target'), key=len, default=[])
    cands = max((v['candidates'] for k, v in answers.items() if k[0] == 'control'), key=len, default=[])
    pool_alts = [resolved(x, auth, t0, f'T{i + 1}') for i, x in enumerate(alts)]
    pool_c = []
    for i, x in enumerate(cands):
        cid = 'C0' if not x.get('functions') else x.get('controlId') or f'C{i}'
        pool_c.append({'controlId': cid, 'functions': x.get('functions') or [], 'relatedKpis': x.get('relatedKpis') or [],
                       'rationale': x.get('rationale', ''),
                       'status': {'candidateId': f'candidate/{i:06d}', 'availability': 'AVAILABLE', 'eligible': True, 'tried': False}})
    (out / 'pools.json').write_text(json.dumps({'alternatives': pool_alts, 'controls': pool_c}, indent=1))
    sel = [(T, C, k) for T in Ts for C in Cs for k in range(a.reps)
           if T - 1 <= len(pool_alts) and C <= len(pool_c)]
    skipped = [(T, C) for T in Ts for C in Cs if (T, C, 0) not in sel]
    random.shuffle(sel)
    for T, C, k in sel:
        inp = json.loads(json.dumps(j_in))
        rebind_targets(inp, pool_alts[:T - 1])
        inp['input.control_candidates'] = pool_c[:C]
        call('im-selection' if im else 'trajectory', T, C, k, sysp['trajectory'], render(inp, j_tail),
             lambda p: 1 if p.get('controlId') else 0)
    f.close()

    # 요약
    md = [f'# T·C 개수별 3A 호출 토큰·지연 — {datetime.datetime.now(KST):%Y-%m-%d %H:%M} KST', '',
          f'뼈대: `{P}` · 반복 {a.reps} · maxTokens {a.max_tokens} (실판은 4000) · 모델 {be.model} → '
          f'{sorted({r["servedModel"] for r in rows if r["servedModel"]})}', '',
          '| 호출 | T | C | 성공/시도 | 입력 토큰 | 출력 토큰(추론 포함) | 추론 | 지연 중앙 s | 지연 범위 s | 반환 개수 |',
          '|---|---|---|---|---|---|---|---|---|---|']
    groups = {}
    for r in rows:
        groups.setdefault((r['phase'], r['T'] or 0, r['C'] or 0), []).append(r)
    med = lambda v: statistics.median(v) if v else ''
    for (ph, T, C), v in sorted(groups.items()):
        ok = [r for r in v if not r['error'] and r['inputTokens'] is not None]
        lat = [r['latencyS'] for r in ok]
        md.append(f"| {ph} | {T or '-'} | {C or '-'} | {len(ok)}/{len(v)} | {med([r['inputTokens'] for r in ok])} | "
                  f"{med([r['outputTokens'] for r in ok])} | {med([r['reasoningTokens'] for r in ok if r['reasoningTokens'] is not None])} | "
                  f"{med(lat)} | {(f'{min(lat)}–{max(lat)}') if lat else ''} | "
                  f"{sorted(r['returned'] for r in v if r['returned'] is not None)} |")
    md += ['', f'풀 크기: 대안 {len(pool_alts)} · 후보 {len(pool_c)} · 풀이 모자라 건너뛴 선택 조건 (T,C): {skipped or "없음"}',
           '', '원자료: calls.csv · 형성 출력: formation-answers.json · 선택에 쓴 풀: pools.json',
           '주의: 선택 입력의 대안은 형성 출력(levels)을 해석형으로 바꾼 것(cost=Σk² 근사); 판이 도는 중 같은 프록시를 써서 지연에 판 호출과의 경합이 섞일 수 있다.']
    (out / 'README.md').write_text('\n'.join(md) + '\n')
    print(out / 'README.md')


if __name__ == '__main__':
    main()
