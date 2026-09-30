"""Figure-4 detail export (owner 2026-09-30): call timeline, T and C definitions, preparation timeline,
roll-backs, for every board of the chosen campaigns.  Run from experiment_results/ota-20260911.

    AIC_ANALYSIS_CAMPAIGNS=blocks-v52,blocks-v53,... python3 ops/analysis_v53/export_board_detail.py <out_dir>
"""
import csv, glob, json, os, sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from common import CAMPAIGNS, boards  # noqa: E402

UES = ("ue1", "ue2", "ue3")
REQS = ("I4e.r1", "I1g.r1", "I2d.r1", "I3g.r1", "I2g.r1")


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')) if s else None


def iso(d):
    return d.isoformat().replace('+00:00', 'Z') if d else ''


def axes(cfg):
    gnb1 = next((k for k in cfg if k.startswith('txAttenuationDb@')), 'txAttenuationDb@12345678')
    return ([cfg.get(f'servingCell@{u}') for u in UES] + [cfg.get(f'dlPrbCap@{u}') for u in UES]
            + [cfg.get(f'pfWeight@{u}') for u in UES] + [cfg.get(gnb1)])


AXES = ([f'servingCell@{u}' for u in UES] + [f'dlPrbCap@{u}' for u in UES] + [f'pfWeight@{u}' for u in UES]
        + ['txAttenuationDb@gnb1'])


def diff(cfg, base):
    return json.dumps({k: v for k, v in (cfg or {}).items() if str((base or {}).get(k)) != str(v)}, sort_keys=True)


def events(board):
    out = []
    for f in glob.glob(f'{board}/evidence/*events*.jsonl'):
        for line in open(f, errors='ignore'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def main(out):
    os.makedirs(out, exist_ok=True)
    w = {n: csv.writer(open(f'{out}/{n}.csv', 'w', newline='')) for n in
         ('calls_timeline', 'targets', 'controls', 'prep_timeline', 'rollbacks')}
    key_cols = ['campaign', 'block', 'slot', 'method', 'attempt']
    w['calls_timeline'].writerow(key_cols + ['callIndex', 'role', 'phase', 'model', 'startedAt', 'endedAt', 'latencyMs',
                                             'forTrialIndex', 'staleAtArrival', 'accepted', 'repairRetries',
                                             'inputTokens', 'outputTokens', 'reasoningTokens', 'visibleOutputTokens',
                                             'cacheReadInputTokens', 'cacheCreationInputTokens', 'generations',
                                             'declaredReasoningEffort', 'sentOptions', 'notSent', 'usageScope'])
    w['targets'].writerow(key_cols + ['targetId', 'p_paper', 'cost_legacy_sum_w_q2'] + [f'level.{r}' for r in REQS]
                          + ['deadlineLevel.I2d.r1']
                          + [f'threshold.{r}' for r in REQS] + ['deadlineMs.I2d.r1'])
    w['controls'].writerow(key_cols + ['controlId'] + AXES + ['changesFromC0', 'rationale', 'relatedKpis',
                                                               'triedInTrials'])
    w['prep_timeline'].writerow(key_cols + ['timingMode', 'inputRelease_t0', 'prepEnd', 'prepMs', 'epochFrozenAt',
                                            'caseOpenedAt', 'c0SampleAt', 'trial0WindowStart', 'trial0WindowEnd',
                                            'firstSelectionCallStart', 'firstTrialAppliedAt', 'searchEndedAt',
                                            'hardwareWaitTotalMs', 'excisedTraceMs', 'recoveryMs', 'deadlineBMs'])
    w['rollbacks'].writerow(key_cols + ['trialIndex', 'controlId', 'rolledBack', 'retain', 'retentionReason',
                                        'previousBestPRank', 'currentBestPRank', 'retentionConfirmedAt',
                                        'baselineBefore_changesFromC0', 'appliedAfter_changesFromC0',
                                        'resetToBaselineAt', 'resetDurationMs', 'resetRestored',
                                        'recoveryRequested', 'recoveryRequestedAt', 'recoveryResolvedAt',
                                        'recoveryResolution'])
    n = 0
    for camp, blk, slot, meth, att, d, ep in boards():
        n += 1
        key = [camp, blk, slot, meth, att]
        trials = ep.get('trials') or []
        c0 = next((t.get('configuration') for t in trials if t.get('trialIndex') == 0), None) or \
            ((ep.get('C') or {}).get('candidates') or [{}])[0].get('configuration') or {}
        applied = sorted([(ts(t.get('appliedAt')), t['trialIndex']) for t in trials
                          if t.get('trialIndex', 0) >= 1 and t.get('appliedAt')])
        first_sel = None
        for i, c in enumerate(ep.get('calls') or []):
            start = ts(c.get('startedAt'))
            end = start + timedelta(milliseconds=float(c.get('latencyMs') or 0)) if start else None
            if c.get('phase') in ('formation', 'intake', 'clarification'):
                for_trial = 'prep'
            else:
                first_sel = first_sel or start
                for_trial = next((str(ix) for a, ix in applied if end and a >= end), 'none (search ended)')
            gens = c.get('generations') or []
            tot = lambda k: (sum(int(g.get(k) or 0) for g in gens) if any(g.get(k) is not None for g in gens) else '')
            reasoning = tot('reasoningTokens')
            sent = (gens[-1].get('sentOptions') or {}) if gens else {}
            w['calls_timeline'].writerow(key + [i, c.get('role'), c.get('phase'), c.get('model'), iso(start), iso(end),
                                               c.get('latencyMs'), for_trial, c.get('staleAtArrival'), c.get('accepted'),
                                               c.get('repairRetries'), c.get('inputTokens'), c.get('outputTokens'),
                                               reasoning,
                                               (int(c.get('outputTokens') or 0) - reasoning) if reasoning != '' else '',
                                               tot('cacheReadInputTokens'), tot('cacheCreationInputTokens'), len(gens),
                                               (c.get('options') or {}).get('reasoningEffort'),
                                               json.dumps({k: v for k, v in sent.items() if k != 'notSent'}),
                                               ','.join(sorted((sent.get('notSent') or {}).keys())),
                                               json.dumps(gens[-1].get('usageScope')) if gens else ''])
        T = ep.get('T') or {}
        rows = ([dict(T.get('t0') or {}, targetId=(T.get('t0') or {}).get('targetId', 'T0'))]
                + list(T.get('alternatives') or []))
        pe = ep.get('paperEvaluation') or {}
        roc = dict(zip(pe.get('ownerPriority') or [], pe.get('rocWeights') or []))   # p = sum ROC_w * level
        for tgt in rows:
            lv, req = tgt.get('levels') or {}, tgt.get('requirements') or {}
            p_paper = sum(w_ * int(lv.get(r) or 0) for r, w_ in roc.items()) if roc else ''
            w['targets'].writerow(key + [tgt.get('targetId'), p_paper, tgt.get('cost')] + [lv.get(r) for r in REQS]
                                  + [(tgt.get('deadlineLevels') or {}).get('I2d.r1')] + [req.get(r) for r in REQS]
                                  + [(tgt.get('deadlines') or {}).get('I2d.r1')])
        tried = {}
        for t in trials:
            tried.setdefault(t.get('controlId'), []).append(t['trialIndex'])
        for cand in (ep.get('C') or {}).get('candidates') or []:
            cfg = cand.get('configuration') or {}
            w['controls'].writerow(key + [cand.get('controlId')] + axes(cfg) + [diff(cfg, c0), cand.get('rationale'),
                                   json.dumps(cand.get('relatedKpis')), json.dumps(tried.get(cand.get('controlId'), []))])
        tm, hw, ev = ep.get('timing') or {}, ep.get('hardwareWaitMs') or {}, events(d)
        first = lambda kind: min((x.get('timestamp') for x in ev if x.get('eventKind') == kind), default='')
        sample = next((x.get('at') for x in ep.get('nonTrialEvents') or [] if x.get('kind') == 'sample'), '')
        t0 = next((t for t in trials if t.get('trialIndex') == 0), {})
        w['prep_timeline'].writerow(key + [tm.get('timingMode'), tm.get('t0') or tm.get('prepStart'), tm.get('prepEnd'),
                                           tm.get('prepMs'), first('EpochFrozen'), first('CaseOpened'), sample,
                                           (t0.get('window') or {}).get('start'), (t0.get('window') or {}).get('end'),
                                           iso(first_sel), iso(applied[0][0]) if applied else '',
                                           (ep.get('termination') or {}).get('searchTerminatedAt'),
                                           hw.get('totalMs'), hw.get('excisedTraceMs'), hw.get('recoveryMs'),
                                           (ep.get('budget') or {}).get('deadlineBMs')])
        resets = {x.get('trialIndex'): x for x in ep.get('nonTrialEvents') or [] if x.get('kind') == 'reset-to-baseline'}
        rets = {}
        for r in ep.get('retentionDecisions') or []:
            rets.setdefault(r.get('trialIndex'), r)
        confirms = {}
        for x in ev:
            p = x.get('payload') or {}
            if x.get('eventKind') == 'GatewayResultRecorded' and p.get('tokenKind') in (
                    'RECOVERY_CONFIRM', 'REVERSE_ROLLBACK', 'CONFIGURATION_REREAD'):
                confirms.setdefault(str(p.get('trialId', '')).rsplit(':', 1)[-1], []).append(
                    f"{p.get('tokenKind')}:{p.get('outcome')}@{x.get('timestamp')}")
        order = [r for r in (ep.get('retentionDecisions') or [])]
        for t in trials:
            i = t.get('trialIndex', 0)
            if i < 1:
                continue
            ret = rets.get(i) or (order[i - 1] if i - 1 < len(order) and 'trialIndex' not in order[i - 1] else {})
            rs, rec = resets.get(i) or {}, ((t.get('kernel') or {}).get('recovery') or {})
            w['rollbacks'].writerow(key + [i, t.get('controlId'), t.get('rolledBack'), ret.get('retain'), ret.get('reason'),
                                           ret.get('previousBestPRank'), ret.get('currentBestPRank'),
                                           ret.get('retentionConfirmedAt'), diff(ret.get('baselineBefore'), c0) if ret else '',
                                           diff(ret.get('appliedConfiguration'), c0) if ret else '', rs.get('at'),
                                           rs.get('durationMs'), rs.get('restored'), rec.get('requested'),
                                           rec.get('requestedAt'), rec.get('resolvedAt'), rec.get('resolution')])
    print(f'{n} boards from {",".join(CAMPAIGNS)} -> {out}')


if __name__ == '__main__':
    main(sys.argv[1])
