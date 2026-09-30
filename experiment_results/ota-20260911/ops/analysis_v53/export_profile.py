"""Figure-6 implementation-profile export (owner 2026-09-30).  Original record values only: nothing is
estimated or reconstructed.  Run from experiment_results/ota-20260911.

    AIC_ANALYSIS_CAMPAIGNS=... python3 ops/analysis_v53/export_profile.py <out_dir>

calls_profile.csv   one row per model call: prompt bytes per input section (the stored prompt text),
                    system prompt hash and bytes, sent options, response model, parse/validation refusals
trial_gateway.csv   one row per gateway step of a trial: kernel tokens and results (events), adapter
                    operations VALIDATE/APPLY/READ/HALT/UNDO/FINALIZE with their times (gatewayEvidence),
                    trial state changes; joined to the trial by the kernel dispatchId the trial records
trial_windows.csv   one row per trial: observation window times, samples, coverage per KPI, validity
ue_events.csv       identity rebinds and hardware disconnects as recorded (times, case, reason)
board_profile.csv   one row per board: termination detail and time, T/C filtering, measurement rules
"""
import csv, glob, hashlib, json, os, sys

sys.path.insert(0, os.path.dirname(__file__))
from common import CAMPAIGNS, boards  # noqa: E402

KEY = ['campaign', 'block', 'slot', 'method', 'attempt']


def jl(path):
    out = []
    for line in open(path, errors='ignore'):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def system_sizes(board):
    """sha256 -> bytes of every system prompt text the board's prompt directory kept."""
    sizes = {}
    for f in glob.glob(f'{board}/evidence/*-prompts/*-system.txt'):
        b = open(f, 'rb').read()
        sizes[hashlib.sha256(b).hexdigest()] = len(b)
        sizes[hashlib.sha256(b.strip()).hexdigest()] = len(b.strip())
    return sizes


def sections(prompt):
    """Bytes of each input.* section of the stored user prompt, and of the schema tail."""
    try:
        head, _sep, tail = prompt.partition('\n\nOUTPUT SCHEMA')
        body = json.loads(head[head.index('{'):])
    except ValueError:
        return {}, len(prompt.encode())
    return ({k: len(json.dumps(v, separators=(',', ':')).encode()) for k, v in body.items()},
            len(prompt.encode()))


def main(out):
    os.makedirs(out, exist_ok=True)
    W = {n: csv.writer(open(f'{out}/{n}.csv', 'w', newline='')) for n in
         ('calls_profile', 'trial_gateway', 'trial_windows', 'ue_events', 'board_profile')}
    W['calls_profile'].writerow(KEY + ['callIndex', 'role', 'phase', 'model', 'attempts', 'systemPromptSha256',
                                       'systemPromptBytes', 'userPromptBytes', 'userPromptSectionBytes',
                                       'responseModel', 'sentOptions', 'repairRetries', 'transportRetries',
                                       'refusedBecause', 'transportFailures', 'truncatedAtLimit', 'responseSuccess',
                                       'droppedRows', 'dropped'])
    W['trial_gateway'].writerow(KEY + ['trialIndex', 'dispatchId', 'source', 'step', 'adapter', 'outcome', 'at',
                                       'detail'])
    W['trial_windows'].writerow(KEY + ['trialIndex', 'windowStart', 'windowEnd', 'trailingCollectionEnd', 'samples',
                                       'excisedSamples', 'extensionSamples', 'valid', 'reason', 'unknownKpis',
                                       'coverage', 'deadlineCohortIssued', 'observationValid', 'observationReason',
                                       'appliedAt', 'holdEndedAt', 'settledAt', 'evaluationCompletedAt'])
    W['ue_events'].writerow(KEY + ['kind', 'at', 'index', 'case', 'reason', 'outcome', 'trialsBefore', 'record'])
    W['board_profile'].writerow(KEY + ['terminationReason', 'kernelTermination', 'terminationDetail',
                                       'searchTerminatedAt', 'targetRowsFromModel', 'targetsInContract',
                                       'targetNotes', 'targetRefusedGenerations', 'controlRowsDropped',
                                       'controlDropped', 'measurementRules', 'timingMode'])
    n = 0
    for camp, blk, slot, meth, att, d, ep in boards():
        n += 1
        key = [camp, blk, slot, meth, att]
        sysz = system_sizes(d)
        for i, c in enumerate(ep.get('calls') or []):
            gens = c.get('generations') or []
            last = gens[-1] if gens else {}
            req = last.get('request') or {}
            sec, total = sections(req.get('prompt') or '') if req.get('prompt') else ({}, '')
            sha = req.get('systemPromptSha256')
            W['calls_profile'].writerow(key + [
                i, c.get('role'), c.get('phase'), c.get('model'), len(gens), sha or '', sysz.get(sha, '') if sha else '',
                total, json.dumps(sec), last.get('responseModel'), json.dumps(last.get('sentOptions'), default=str),
                c.get('repairRetries'), c.get('transportRetries'),
                json.dumps([g.get('refusedBecause') for g in gens if g.get('refusedBecause')]),
                json.dumps([g.get('transportFailure') for g in gens if g.get('transportFailure')]),
                any(g.get('truncatedAtLimit') for g in gens), json.dumps([g.get('responseSuccess') for g in gens]),
                len(c.get('dropped') or []), json.dumps(c.get('dropped') or [], default=str)[:2000]])
        trials = ep.get('trials') or []
        by_dispatch = {((t.get('kernel') or {}).get('execution') or {}).get('dispatchId'): t.get('trialIndex')
                       for t in trials}
        by_dispatch.pop(None, None)
        evs = []
        for f in glob.glob(f'{d}/evidence/*events*.jsonl'):
            evs += jl(f)
        for x in evs:
            p = x.get('payload') or {}
            tid = p.get('trialId') or (x.get('objectId') if ':trial:' in str(x.get('objectId')) else None)
            if tid not in by_dispatch:
                continue
            kind = x.get('eventKind')
            if kind == 'TokenIssued':
                step, outc = p.get('tokenKind'), f"fence {p.get('fencingToken')}"
            elif kind == 'GatewayResultRecorded':
                step, outc = p.get('tokenKind'), p.get('outcome')
            elif kind == 'TrialStateChanged':
                step, outc = f"{p.get('from')}->{p.get('to')}", p.get('reason')
            else:
                continue
            W['trial_gateway'].writerow(key + [by_dispatch[tid], tid, f'event:{kind}', step, '', outc,
                                               x.get('timestamp'), ''])
        cases = list((ep.get('execution') or {}).get('retiredCases') or [])
        for rc in cases + [{'gatewayEvidence': (ep.get('execution') or {}).get('gatewayEvidence') or {}}]:
            for k, v in (rc.get('gatewayEvidence') or {}).items():
                tid = k[3:].split('#', 1)[0] if k.startswith('tx:') else None
                if tid not in by_dispatch:
                    continue
                adapter = k.rsplit(':', 1)[-1]
                W['trial_gateway'].writerow(key + [by_dispatch[tid], tid, 'gatewayEvidence', v.get('operation'),
                                                   adapter, v.get('outcome'), v.get('at'),
                                                   (v.get('detail') or '')[:200]])
        for t in trials:
            w, ov = t.get('window') or {}, t.get('observationValidity') or {}
            ex = ((t.get('kernel') or {}).get('execution') or {})
            co = ((w.get('cohorts') or {}).get('deadlineSuccessRatio@ue2') or {})
            W['trial_windows'].writerow(key + [t.get('trialIndex'), w.get('start'), w.get('end'),
                                               w.get('trailingCollectionEnd'), w.get('samples'),
                                               w.get('excisedSamples'), w.get('extensionSamples'), w.get('valid'),
                                               w.get('reason'), json.dumps(w.get('unknownKpis')),
                                               json.dumps(w.get('coverage')), co.get('issued'), ov.get('valid'),
                                               ov.get('reason'), ex.get('appliedAt'), ex.get('holdEndedAt'),
                                               ex.get('settledAt'), ex.get('evaluationCompletedAt')])
        for r in ep.get('identityRebinds') or []:
            W['ue_events'].writerow(key + ['identityRebind', r.get('at'), r.get('index'), r.get('case'), r.get('reason'),
                                           r.get('outcome'), r.get('trialsBefore'),
                                           json.dumps({k: r.get(k) for k in ('ues', 'freed')}, default=str)[:1000]])
        for r in ep.get('hardwareDisconnects') or []:
            W['ue_events'].writerow(key + ['hardwareDisconnect', r.get('at') or r.get('start'), '', r.get('case', ''),
                                           r.get('reason', ''), r.get('outcome', ''), '',
                                           json.dumps(r, default=str)[:1000]])
        term, T, C = ep.get('termination') or {}, ep.get('T') or {}, ep.get('C') or {}
        tcall = next((c for c in ep.get('calls') or [] if c.get('role') == 'target'), {})
        ccall = next((c for c in ep.get('calls') or [] if c.get('role') == 'control'), {})
        W['board_profile'].writerow(key + [
            term.get('reason'), term.get('kernelTermination'), term.get('detail'), term.get('searchTerminatedAt'),
            len(((T.get('provenance') or {}).get('modelSelection') or {}).get('order') or []),
            len(T.get('alternatives') or []), json.dumps((T.get('provenance') or {}).get('notes'), default=str)[:1500],
            sum(1 for g in tcall.get('generations') or [] if g.get('refusedBecause')),
            len(ccall.get('dropped') or []), json.dumps(ccall.get('dropped') or [], default=str)[:1500],
            json.dumps(ep.get('measurementRules')), (ep.get('timing') or {}).get('timingMode')])
    print(f'{n} boards from {",".join(CAMPAIGNS)} -> {out}')


if __name__ == '__main__':
    main(sys.argv[1])
