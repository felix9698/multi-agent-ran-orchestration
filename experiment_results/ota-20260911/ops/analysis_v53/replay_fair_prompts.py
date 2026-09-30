"""Prompt-only fairness replay (owner 2026-09-30 "공정하게 바꾸고 다시 프롬프트만 실험해봐").  No bed.

Recorded v5.3 requests (v53y blocks 2-5, v53z) are re-sent to the same model label with:
  control / old2  : the recorded Control text, energyStepValues 1-2 steps (as v5.3 ran)
  control / old4  : the recorded Control text with the v5.4s four-step values
  control / fair4 : the fair Control text (no 4-2-4 slot quota; the same energy sentence BM gets)
  bm      / old2  : the recorded BM text and values
  bm      / fair4 : the fair BM text, four-step values
and reports the attenuations proposed.

    python3 ops/run_with_proxy.py ops/analysis_v53/replay_fair_prompts.py <out.jsonl> [per] [reps]
"""
import concurrent.futures as cf, glob, json, os, random, re, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
os.environ.setdefault('AIC_V52', '1'); os.environ.setdefault('AIC_V53', '1')
from assurance.coordination import v52  # noqa: E402
from assurance.coordination.agents import CallRecord, RoleAgents, RoleModels  # noqa: E402

EXP = REPO / 'experiment_results' / 'ota-20260911'
OPTIONS = {'maxTokens': 4000, 'thinkingBudgetTokens': 0, 'reasoningEffort': 'medium', 'jsonMode': True}
OPTIONS_BM = dict(OPTIONS, maxTokens=2000)

FAIR_ENERGY = ("energyStepValues give the attenuations that earn the first energy improvement steps; any "
               "supported attenuation may be used. Consider compensating combinations, not only single changes. "
               "Compensation must change relative resource allocation or contention. Equal PF scaling of all "
               "competitors does not establish protection.")
FAIR_CONTROL = (
    "Construct exactly ten distinct compatible candidates, excluding C0, using intents, authorization, "
    "functions and evidence. Do not use generated targets.\n\n"
    "Cover different tradeoffs rather than minor variants: energy improvements of different depths, repairs of "
    "floor failures or the most vulnerable services without an energy increase, and joint configurations with "
    "credible weighted benefit. " + FAIR_ENERGY + "\n\n"
    "A candidate is a complete configuration relative to C0. Omitted settings reset to C0. Respect actual "
    "resolution and the changed-entry limit. Avoid scheduler-only changes on isolated UEs. Name the "
    "beneficiary and potentially harmed service. Keep unmeasured effects hypothetical and report relatedKpis "
    "and supplied evidenceRefs or [].")
FAIR_BM = (
    "Propose one untried compatible configuration from the same supported action space. " + FAIR_ENERGY +
    " Avoid scheduler-only changes on isolated UEs.\n\n"
    "Return instructions relative to C0 and rationale. Explicitly include every nonbaseline setting you intend "
    "to retain. Respect actual resolution and the changed-entry limit. Identify the intended beneficiary and "
    "potentially harmed service without inventing numeric outcomes.")
SYSTEM = {'control/fair4': v52.V53_COMMON + "\n\n" + FAIR_CONTROL,
          'bm/fair4': v52.V53_COMMON + "\n\n" + v52.V53_POLICY + "\n\n" + FAIR_BM}
FOUR = [{"txAttenuationDb": 9.0, "energyLevel": 19, "energyImprovementSteps": 1},
        {"txAttenuationDb": 9.5, "energyLevel": 18, "energyImprovementSteps": 2},
        {"txAttenuationDb": 10.0, "energyLevel": 17, "energyImprovementSteps": 3},
        {"txAttenuationDb": 10.5, "energyLevel": 16, "energyImprovementSteps": 4}]


def pick(kind, n, seed):
    out = []
    pat = 'three-agent' if kind == 'control' else 'basic-monolith'
    name = 'control' if kind == 'control' else 'monolith'
    for camp, blocks in (('blocks-v53y', range(2, 6)), ('blocks-v53z', range(0, 4))):
        for b in blocks:
            for lf in glob.glob(str(EXP / f'ops/overnight/{camp}-b{b}s*-{pat}-attempt*.log')):
                dd = re.findall(r'formal38guarded-[0-9T]+-[0-9a-f]{32}', open(lf, errors='ignore').read())
                if not dd:
                    continue
                reqs = sorted(glob.glob(str(EXP / dd[-1]) + f'/evidence/*-prompts/*-{name}-request.txt'))
                syss = sorted(glob.glob(str(EXP / dd[-1]) + f'/evidence/*-prompts/*-{name}-system.txt'))
                if reqs and syss:
                    out.append((dd[-1], reqs[0 if kind == 'control' else min(2, len(reqs) - 1)], syss[0]))
    random.Random(seed).shuffle(out)
    return out[:n]


def four_step(request):
    head, sep, tail = request.partition('\n\nOUTPUT SCHEMA')
    body = json.loads(head[head.index('{'):])
    steps = body.get('input.energyStepValues')
    if isinstance(steps, dict) and 'values' in steps:
        steps['values'] = FOUR
    return 'INPUTS:\n' + json.dumps(body, separators=(',', ':')) + sep + tail


def attenuations(text, kind):
    try:
        j = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except ValueError:
        return None, None
    rows = j.get('candidates') if kind == 'control' else [{'functions': j.get('instructions') or []}]
    atts, joint = [], 0
    for cand in rows or []:
        fns = cand.get('functions') or []
        a = [float((f.get('policy') or {}).get('txAttenuationDb')) for f in fns
             if f.get('functionId') == 'cell-tx-attenuation' and (f.get('policy') or {}).get('txAttenuationDb') is not None]
        atts.append(a[0] if a else 8.0)
        joint += int(bool(a) and len(fns) > 1)
    return atts, joint


def main(out, per=6, reps=2):
    jobs = []
    for kind, variants in (('control', ('old2', 'old4', 'fair4')), ('bm', ('old2', 'fair4'))):
        for board, req, sysf in pick(kind, per, 7):
            for var in variants:
                for r in range(reps):
                    jobs.append((kind, var, board, req, sysf, r))
    agents = RoleAgents(models=RoleModels(control='claude-sonnet', monolith='claude-sonnet'))

    def one(job):
        kind, var, board, req, sysf, r = job
        text = open(req).read()
        text = text if var == 'old2' else four_step(text)
        system = open(sysf).read() if var != 'fair4' else SYSTEM[f'{kind}/fair4']
        rec = CallRecord(role='control' if kind == 'control' else 'monolith', model='claude-sonnet', phase='x')
        t0 = time.monotonic(); err = None; raw = ''
        try:
            parsed = agents._send('claude-sonnet', text, system, rec, OPTIONS if kind == 'control' else OPTIONS_BM)
            raw = json.dumps(parsed)
        except Exception as exc:
            err = str(exc)[:200]
        atts, joint = attenuations(raw, kind) if raw else (None, None)
        return {'kind': kind, 'variant': var, 'board': board, 'rep': r, 'latencyS': round(time.monotonic() - t0, 1),
                'outputTokens': rec.output_tokens, 'attenuations': atts, 'energyWithCompensation': joint,
                'error': err}

    with open(out, 'w') as fh, cf.ThreadPoolExecutor(4) as pool:
        for row in pool.map(one, jobs):
            fh.write(json.dumps(row) + '\n'); fh.flush()
            print(row['kind'], row['variant'], row['latencyS'], row['attenuations'], row['error'] or '', flush=True)


if __name__ == '__main__':
    main(sys.argv[1], *(int(a) for a in sys.argv[2:4]))
