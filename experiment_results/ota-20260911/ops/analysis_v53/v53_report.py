#!/usr/bin/env python3
"""All v5.3 metrics in one pass (2026-09-29): deadline sweep, method comparison at a chosen deadline,
control effects, trade-offs (energy-service hypervolume, Jain fairness, echo p90), paired block wins.
Usage (from experiment_results/ota-20260911): python3 ops/analysis_v53/v53_report.py [deadline_ms]"""
import collections, statistics as st, sys
from math import comb
sys.path.insert(0, 'ops/analysis_v53')
from common import METHODS, boards, cohort, echo_rtts, ratio_within, targets

DL = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
SWEEP = (35, 60, 75, 100, 150, 200, 300, 500, 1000, 2000)


def p90(rtts, co, lost_ms=5000.0):
    if rtts is None or co.get('firstSeq') is None:
        return None
    v = sorted(rtts.get(q) if rtts.get(q) is not None else lost_ms
               for q in range(co['firstSeq'], co['lastSeq'] + 1))
    return v[min(len(v) - 1, int(0.9 * len(v)))] if v else None


def jain(a, b):
    return (a + b) ** 2 / (2 * (a * a + b * b)) if a * a + b * b > 0 else None


def hypervolume(points):
    """2-D area dominated by points (x = saving dB, y = gnb1 goodput / baseline) w.r.t. (0, 0)."""
    area, best_y = 0.0, 0.0
    for x, y in sorted({(max(0, x), max(0, y)) for x, y in points}, key=lambda p: (-p[0], -p[1])):
        if y > best_y:
            area += x * (y - best_y)
            best_y = y
    return area


def sign_tail(k, n, p=1 / 3):
    return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


rows = []
for camp, blk, slot, meth, att, bd, ep in boards():
    th, R = targets(ep), echo_rtts(bd)
    base, trials = None, []
    for t in ep.get('trials', []):
        k = t.get('kpis') or {}
        rec = dict(idx=t.get('trialIndex'), counted=t.get('counted', True), verdicts=t.get('verdicts') or {},
                   co=cohort(t), cfg=t.get('configuration') or {}, g1=k.get('cellGoodputMbps@12345678'),
                   att=k.get('cellTxAttenuationDb@12345678'), u2=k.get('dlGoodputMbps@ue2'),
                   u3=k.get('dlGoodputMbps@ue3'))
        rec['lat'] = p90(R, rec['co'])
        if rec['idx'] == 0:
            base = rec
        elif rec['counted']:
            trials.append(rec)
    rows.append(dict(camp=camp, blk=f'{camp}:{blk}', meth=meth, att=att, R=R, th=th, base=base, trials=trials))


def attained(row, dl):
    first = best = None
    for t in row['trials']:
        r = ratio_within(row['R'], t['co'], dl)
        for T, vv in t['verdicts'].items():
            thr = (row['th'].get(T) or {}).get('I2d.r1')
            ok = all(x == 'PASS' for k, x in vv.items() if k != 'I2d.r1') and (
                'I2d.r1' not in vv or (r is not None and thr is not None and r >= thr))
            if ok:
                first = t['idx'] if first is None else first
                best = int(T[1:]) if best is None else min(best, int(T[1:]))
    return first, best


print(f'# boards: {len(rows)}  ' + ' '.join(f"{m}={sum(r['meth'] == m for r in rows)}" for m in METHODS))
print('\n## deadline sweep: boards attaining any target (all methods)')
for dl in SWEEP:
    print(f'{dl:5d} ms: ' + '  '.join(f"{m}={sum(attained(r, dl)[0] is not None for r in rows if r['meth'] == m)}"
                                       for m in METHODS)
          + f"  total={sum(attained(r, dl)[0] is not None for r in rows)}/{len(rows)}")

print(f'\n## method comparison at {DL:.0f} ms')
for m in METHODS:
    b = [r for r in rows if r['meth'] == m]
    res = [attained(r, DL) for r in b]
    ok = [x for x in res if x[0] is not None]
    ratios = [ratio_within(r['R'], t['co'], DL) for r in b for t in r['trials']]
    ratios = [x for x in ratios if x is not None]
    i2g = [vv.get('I2g.r1') == 'PASS' for r in b for t in r['trials'] for vv in t['verdicts'].values()]
    print(f"{m:18s} boards={len(b)} attained={len(ok)} ({100 * len(ok) / len(b):.0f}%) "
          f"firstTrial(median)={st.median([x[0] for x in ok]) if ok else None} "
          f"meanRatio={st.mean(ratios):.2f} I2gPASS={100 * st.mean(i2g):.0f}%")

print(f'\n## control effects on the {DL:.0f} ms ratio (trial vs same-board initial measurement)')
eff = []
for r in rows:
    if r['base'] is None:
        continue
    b0 = ratio_within(r['R'], r['base']['co'], DL)
    for t in r['trials']:
        x = ratio_within(r['R'], t['co'], DL)
        if x is None or b0 is None:
            continue
        c = t['cfg']
        eff.append(dict(d=x - b0, r=x, pf2=float(c.get('pfWeight@ue2', 1)), pf3=float(c.get('pfWeight@ue3', 1)),
                        cap3=float(c.get('dlPrbCap@ue3', 0)), c2=c.get('servingCell@ue2'),
                        c3=c.get('servingCell@ue3'), att=float(c.get('txAttenuationDb@12345678', 8))))
G1, G2 = '12345678', '87654321'
for name, sel in (('no ue2/ue3 control', lambda e: e['pf2'] == 1 and e['pf3'] == 1 and e['cap3'] == 0 and e['c2'] == G1 and e['c3'] == G1),
                  ('ue3 steered to gnb2', lambda e: e['c3'] == G2 and e['c2'] == G1),
                  ('ue2 PF raised', lambda e: e['pf2'] > 1 and e['c2'] == G1),
                  ('ue3 PF lowered', lambda e: e['pf3'] < 1),
                  ('ue3 PRB cap', lambda e: e['cap3'] > 0 and e['c2'] == G1),
                  ('ue2 steered to gnb2', lambda e: e['c2'] == G2),
                  ('gnb1 attenuation raised', lambda e: e['att'] > 8 and e['c2'] == G1)):
    xs = [e for e in eff if sel(e)]
    if xs:
        print(f"{name:26s} n={len(xs):3d} meanRatio={st.mean(e['r'] for e in xs):.2f} "
              f"meanDelta={st.mean(e['d'] for e in xs):+.2f} improved>0.1={sum(e['d'] > 0.1 for e in xs)}")

print('\n## trade-offs (per board, trials vs initial measurement)')
tr = []
for r in rows:
    b = r['base']
    if not b or not b.get('g1') or b.get('att') is None:
        continue
    pts = [(t['att'] - b['att'], t['g1'] / b['g1']) for t in r['trials'] if t['g1'] is not None and t['att'] is not None]
    if not pts:
        continue
    fair = [jain(t['u2'], t['u3']) for t in r['trials'] if t['u2'] is not None and t['u3'] is not None]
    fair = [f for f in fair if f is not None]
    bj = jain(b['u2'] or 0, b['u3'] or 0)
    dl = [t['lat'] - b['lat'] for t in r['trials'] if t['lat'] is not None and b.get('lat') is not None]
    tr.append(dict(blk=r['blk'], meth=r['meth'], hv=hypervolume(pts + [(0, 1.0)]),
                   save90=max([x for x, y in pts if y >= 0.9] + [0]),
                   fair_gain=(max(fair) - bj) if fair and bj is not None else None,
                   best_fair=max(fair) if fair else None, dp90=min(dl) if dl else None))
for m in METHODS:
    b = [x for x in tr if x['meth'] == m]
    f = lambda k: st.mean([x[k] for x in b if x[k] is not None])
    print(f"{m:18s} n={len(b)} HV={f('hv'):.2f} maxSaving@90%={f('save90'):.2f}dB bestJain={f('best_fair'):.2f} "
          f"JainGain={f('fair_gain'):+.2f} bestP90change={f('dp90'):+.0f}ms")
by = collections.defaultdict(dict)
for x in tr:
    by[x['blk']][x['meth']] = x
full = [v for v in by.values() if all(m in v for m in METHODS)]
print(f'\n## within-block paired wins ({len(full)} blocks with all three methods; sign-test tail vs 1/3)')
for key, better in (('hv', max), ('save90', max), ('fair_gain', max), ('dp90', min)):
    w = collections.Counter()
    n = 0
    for v in full:
        vals = {m: v[m][key] for m in METHODS if v[m][key] is not None}
        if len(vals) == 3:
            w[better(vals, key=vals.get)] += 1
            n += 1
    top, k = w.most_common(1)[0]
    print(f"{key:10s} {dict(w)}  leader={top} {k}/{n} p={sign_tail(k, n):.3f}")
