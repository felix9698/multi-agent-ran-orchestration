"""Full per-board / per-trial history export (owner 2026-09-30).  Run from experiment_results/ota-20260911.

    AIC_ANALYSIS_CAMPAIGNS=blocks-v53,blocks-v53x,blocks-v53y,blocks-v53z \
        python3 ops/analysis_v53/export_history.py <out_dir>

Writes boards.csv (one row per board), trials.csv (one row per trial, trial 0 = initial
measurement), calls.csv (one row per model call) and trials_raw.jsonl (every trial record
exactly as the episode stored it, plus its board keys).
"""
import csv, glob, json, os, statistics, sys

sys.path.insert(0, os.path.dirname(__file__))
from common import CAMPAIGNS, boards, cohort, echo_rtts, ratio_within  # noqa: E402

UES = ("ue1", "ue2", "ue3")
REQS = ("I4e.r1", "I1g.r1", "I2d.r1", "I3g.r1", "I2g.r1")
DEADLINES = (35, 60, 75, 100, 150, 200, 500, 1000)


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else None


def reference(camp, block):
    f = f'ops/overnight/{camp}.block{block}.reference.json'
    try:
        return json.load(open(f))
    except (OSError, ValueError):
        return {}


def requirement_rows(ep):
    reqs = (((ep.get('T') or {}).get('authorization') or {}).get('requirements')
            or ((ep.get('T') or {}).get('authorization') or {}))
    return reqs if isinstance(reqs, dict) else {}


def main(out):
    os.makedirs(out, exist_ok=True)
    bw = csv.writer(open(f'{out}/boards.csv', 'w', newline=''))
    tw = csv.writer(open(f'{out}/trials.csv', 'w', newline=''))
    cw = csv.writer(open(f'{out}/calls.csv', 'w', newline=''))
    raw = open(f'{out}/trials_raw.jsonl', 'w')
    bw.writerow(['campaign', 'block', 'slot', 'method', 'attempt', 'board_dir', 'codeRevision', 'offeredLoadMbps',
                 'ue2DeadlineMs', 'termination', 'kernelTermination', 'terminationDetail', 'bestAttained',
                 'retainedControl', 'retainedTarget', 'retainedQualified', 'trialsRecorded', 'llmCalls',
                 'inputTokens', 'outputTokens', 'prepMs', 'totalMs', 'hardwareWaitMs', 'hardwareDisconnects',
                 'identityRebinds', 'nonTrialEvents', 'targetsInT']
                + [f'{r}.{k}' for r in REQS for k in ('kpi', 'scope', 'original', 'limit')]
                + ['ref.ue1', 'ref.ue2', 'ref.ue3', 'ref.gnb1Sum', 'ref.ue2DeadlineMeasuredMs',
                   'ref.ue2SuccessAtDInA', 'ref.ue2SuccessAtDInB', 'ref.unstable', 'ref.refreshedThisBlock'])
    tw.writerow(['campaign', 'block', 'slot', 'method', 'attempt', 'trialIndex', 'counted', 'controlId',
                 'catalogCandidateId', 'appliedAt', 'elapsedMs', 'decisionLatencyMs', 'decisionModel',
                 'decisionRole', 'decisionTargetId', 'decisionRepairRetries', 'decisionRationale']
                + [f'servingCell@{u}' for u in UES] + [f'dlPrbCap@{u}' for u in UES]
                + [f'pfWeight@{u}' for u in UES] + ['txAttenuationDb@gnb1']
                + [f'kpi.dlGoodputMbps@{u}' for u in UES]
                + ['kpi.deadlineSuccessRatio@ue2', 'kpi.cellTxAttenuationDb@gnb1', 'kpi.cellGoodput@gnb1',
                   'kpi.cellGoodput@gnb2', 'kpi.servingCells', 'observationValid', 'observationReason',
                   'attainmentStatus', 'attainedTargets', 'successColumns', 'p', 'ownerBins', 'coordinates',
                   'kernelTerminalState', 'kernelStopReason', 'kernelDetail', 'rolledBack', 'partialApply',
                   'echo.issued', 'echo.p50Ms', 'echo.p90Ms', 'echo.noReply']
                + [f'echo.ratio@{d}ms' for d in DEADLINES])
    cw.writerow(['campaign', 'block', 'slot', 'method', 'attempt', 'role', 'phase', 'model', 'servedModel',
                 'latencyMs', 'inputTokens', 'outputTokens', 'repairRetries', 'transportRetries', 'accepted',
                 'fallbackReason', 'staleAtArrival'])
    n = 0
    for camp, blk, slot, meth, att, d, ep in boards():
        n += 1
        key = [camp, blk, slot, meth, att]
        term, ret, cost = ep.get('termination') or {}, ep.get('retained') or {}, ep.get('resourceCost') or {}
        reqs = requirement_rows(ep)
        ref = reference(camp, blk)
        rv = ref.get('reference') or {}
        paper = {int(t.get('trialIndex', i)): t for i, t in
                 enumerate((ep.get('paperEvaluation') or {}).get('trials') or [])}
        best = ep.get('bestAttained')
        bw.writerow(key + [d, ep.get('codeRevision'), ep.get('offeredLoadMbps'),
                           (reqs.get('I2d.r1') or {}).get('deadlineMs'), term.get('reason'),
                           term.get('kernelTermination'), (term.get('detail') or '')[:300],
                           json.dumps(best) if best else '', ret.get('controlId'), ret.get('targetId'),
                           ret.get('qualified'), len(ep.get('trials') or []), cost.get('llmCalls'),
                           cost.get('inputTokens'), cost.get('outputTokens'), cost.get('prepMs'),
                           cost.get('totalMs'), ep.get('hardwareWaitMs'), len(ep.get('hardwareDisconnects') or []),
                           len(ep.get('identityRebinds') or []), len(ep.get('nonTrialEvents') or []),
                           len(((ep.get('T') or {}).get('alternatives') or [])) + 1]
                   + [(reqs.get(r) or {}).get(k) for r in REQS for k in ('kpi', 'scope', 'original', 'limit')]
                   + [rv.get('ue1'), rv.get('ue2'), rv.get('ue3'), rv.get('gnb1Sum'),
                      ref.get('ue2DeadlineMeasuredMs'), ref.get('ue2SuccessAtDInA'), ref.get('ue2SuccessAtDInB'),
                      json.dumps(ref.get('unstable')) if ref else '', json.dumps(ref.get('refreshedThisBlock')) if ref else ''])
        rtts = echo_rtts(d)
        for t in ep.get('trials') or []:
            i = int(t.get('trialIndex', 0))
            cfg, kp = t.get('configuration') or {}, t.get('kpis') or {}
            dec, ks = t.get('decision') or {}, t.get('executionStatus') or {}
            pe = paper.get(i) or {}
            verdict_cols = [tid for tid, v in (t.get('verdicts') or {}).items()
                            if isinstance(v, dict) and v and all(x in ('PASS', True) for x in v.values())]
            ratio = kp.get('deadlineSuccessRatio@ue2')
            cells = {k: v for k, v in kp.items() if k.startswith('servingCell@')}
            gnb1 = next((k.split('@')[1] for k in kp if k.startswith('cellTxAttenuationDb@')), '12345678')
            co = cohort(t)
            vals = ([rtts.get(q) for q in range(co['firstSeq'], co['lastSeq'] + 1)]
                    if rtts is not None and co.get('firstSeq') is not None else [])
            got = [v for v in vals if v is not None]
            tw.writerow(key + [i, t.get('counted'), t.get('controlId'), t.get('catalogCandidateId'),
                               t.get('appliedAt'), t.get('elapsedMs'), t.get('decisionLatencyMs'),
                               dec.get('model'), dec.get('role'), dec.get('targetId'), dec.get('repairRetries'),
                               (dec.get('rationale') or '')[:300]]
                       + [cfg.get(f'servingCell@{u}') for u in UES] + [cfg.get(f'dlPrbCap@{u}') for u in UES]
                       + [cfg.get(f'pfWeight@{u}') for u in UES] + [cfg.get(f'txAttenuationDb@{gnb1}')]
                       + [kp.get(f'dlGoodputMbps@{u}') for u in UES]
                       + [json.dumps(ratio) if isinstance(ratio, dict) else ratio,
                          kp.get(f'cellTxAttenuationDb@{gnb1}'), kp.get('cellGoodputMbps@12345678'),
                          kp.get('cellGoodputMbps@87654321'), json.dumps(cells),
                          (t.get('observationValidity') or {}).get('valid'),
                          (t.get('observationValidity') or {}).get('reason'),
                          (t.get('attainment') or {}).get('status'),
                          json.dumps((t.get('attainment') or {}).get('targets')), json.dumps(verdict_cols),
                          pe.get('p'), json.dumps(pe.get('ownerBins')), json.dumps(pe.get('coordinates')),
                          ks.get('terminalState'), ks.get('stopReason'),
                          ((t.get('kernel') or {}).get('detail') or '')[:300], t.get('rolledBack'),
                          ks.get('partialApply'), co.get('issued'), pct(got, 0.5), pct(got, 0.9),
                          len(vals) - len(got) if vals else None]
                       + [ratio_within(rtts, co, dl) if rtts is not None else None for dl in DEADLINES])
            raw.write(json.dumps({'campaign': camp, 'block': blk, 'slot': slot, 'method': meth,
                                  'attempt': att, 'boardDir': d, 'trial': t,
                                  'paperEvaluation': pe}, default=str) + '\n')
        for c in ep.get('calls') or []:
            cw.writerow(key + [c.get('role'), c.get('phase'), c.get('model'), c.get('servedModel'),
                               c.get('latencyMs'), c.get('inputTokens'), c.get('outputTokens'),
                               c.get('repairRetries'), c.get('transportRetries'), c.get('accepted'),
                               c.get('fallbackReason'), c.get('staleAtArrival')])
    print(f'{n} boards from {",".join(CAMPAIGNS)} -> {out}')


if __name__ == '__main__':
    main(sys.argv[1])
