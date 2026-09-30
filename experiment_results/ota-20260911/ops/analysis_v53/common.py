"""Shared loaders for the v5.3 analyses (2026-09-29).  Run from experiment_results/ota-20260911."""
import glob, json, os, re

# v5.4: AIC_ANALYSIS_CAMPAIGNS / AIC_ANALYSIS_METHODS (comma lists) pick other campaigns and methods.
CAMPAIGNS = tuple(os.environ.get('AIC_ANALYSIS_CAMPAIGNS', 'blocks-v53,blocks-v53x,blocks-v53y').split(','))
METHODS = tuple(os.environ.get('AIC_ANALYSIS_METHODS', 'three-agent,internal-monolith,basic-monolith').split(','))


def boards():
    """(campaign, block, slot, method, attempt, board_dir, episode) for every finished board."""
    for camp in CAMPAIGNS:
        for lf in sorted(glob.glob(f'ops/overnight/{camp}-b*-attempt*.log')):
            m = re.search(r'-b(\d+)s(\d+)-(.+)-attempt(\d+)', lf)
            blk, slot, meth, att = m.groups()
            dd = re.findall(r'formal38guarded-[0-9T]+-[0-9a-f]{32}', open(lf, errors='ignore').read())
            if not dd:
                continue
            so = dd[-1] + '/live-sitting.stdout'
            eps = glob.glob(dd[-1] + '/evidence/AGENT-*-episode.json')
            if not eps or not os.path.exists(so) or not re.search(r'^termination', open(so, errors='ignore').read(), re.M):
                continue
            yield camp, blk, slot, meth, att, dd[-1], json.load(open(eps[0]))


def echo_rtts(board):
    """{seq: RTT ms or None (no reply)} from the board's ue2 tagged-echo client log."""
    f = f'{board}/sources/ue2/ue2-echo-client.jsonl'
    if not os.path.exists(f):
        return None
    iss, rep = {}, {}
    for line in open(f, errors='ignore'):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('event') == 'issued':
            iss[e['seq']] = e.get('issuedAtMs', e['atMs'])
        elif e.get('event') == 'reply' and 'seq' in e:
            rep.setdefault(e['seq'], e['atMs'])
    return {s: (rep[s] - t if s in rep else None) for s, t in iss.items()}


def cohort(trial):
    """The Kernel-frozen echo cohort (firstSeq..lastSeq, issued) of a trial's window."""
    return ((((trial.get('window') or {}).get('cohorts') or {})
             .get('deadlineSuccessRatio@ue2') or {}).get('cohort') or {})


def ratio_within(rtts, co, deadline_ms):
    if rtts is None or co.get('firstSeq') is None or not co.get('issued'):
        return None
    ok = sum(1 for q in range(co['firstSeq'], co['lastSeq'] + 1)
             if rtts.get(q) is not None and rtts[q] <= deadline_ms)
    return ok / co['issued']


def targets(ep):
    out = {}

    def walk(o):
        if isinstance(o, dict):
            if 'targetId' in o and isinstance(o.get('requirements'), dict):
                out[o['targetId']] = o['requirements']
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(ep.get('T'))
    return out
