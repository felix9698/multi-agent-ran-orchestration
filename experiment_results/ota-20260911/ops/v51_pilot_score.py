#!/usr/bin/env python3
"""v5.1 pilot re-scoring (owner 2026-09-26): every pilot trial under the ROC-weighted evaluator,
with the energy requirement's original re-set to A0 + 9 / 12 / 15 dB -- the width is an
evaluation condition, so the same observations answer all three.  Internal only: the 25-35 %
difficulty band is the pilot's selection rule and never reaches a prompt or a report.

usage: v51_pilot_score.py [--widths 9,12,15]   -> overnight/v51-pilot-score.md (+ stdout)
"""
import argparse
import dataclasses
import json
import os
import re
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT.parents[1]))
from assurance.coordination import concession as c  # noqa: E402
from assurance.coordination.tc import Intent  # noqa: E402


def boards():
    """Pilot boards from v51-pilot.log (the pilot calls run_case.sh directly, so the campaign
    runner's ledger never sees them)."""
    for line in (HERE / 'overnight' / 'v51-pilot.log').read_text(errors='ignore').splitlines():
        m = re.search(r' L([0-9.]+) part (\d+) attempt (\d+) dir=(formal38guarded-\S+)', line)
        if not m:
            continue
        ep = next((ROOT / m.group(4)).glob('evidence/AGENT-*-episode.json'), None)
        if ep:
            yield float(m.group(1)), {'attempt': int(m.group(3)), 'dir': m.group(4)}, json.loads(ep.read_text())


def score(ep, width):
    intents = sorted((Intent.from_record(i) for i in ep['intents']), key=lambda i: (i.priority, i.intent_id))
    order = tuple(i.requirement.req_id for i in intents)
    reqs = []
    for req in c.requirements_from_intents(intents, by_requirement=True):
        if isinstance(req, c.Level) and req.kpi_key.startswith('cellTxAttenuationDb') and req.limit is not None:
            req = dataclasses.replace(req, original=req.limit + width)
        reqs.append(req)
    n = sum(1 for r in reqs if c._adjustable(r))
    scale = c.PRECISION * sum(c.roc_weights(n))
    rows = []
    for t in ep['trials']:
        validity = t.get('observationValidity') or {}
        if isinstance(validity, dict) and validity.get('valid') is False:
            rows.append((t['trialIndex'], None, 'unassessable'))
            continue
        v = c.evaluate(reqs, order, dict(t.get('kpis') or {}), True)
        rows.append((t['trialIndex'], (1 - v.p / scale) if v.attained else None,
                     'attained' if v.attained else v.reason))
    return rows


def label(conf, base):
    diff = {k: v for k, v in (conf or {}).items() if base.get(k) != v}
    return ', '.join(f'{k.split("@")[0]}@{k.split("@")[1][-4:]}={v}' for k, v in sorted(diff.items())) or 'baseline'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--widths', default='9,12,15')
    widths = [float(w) for w in ap.parse_args().widths.split(',')]
    out = ['# v5.1 pilot — ROC-weighted A per trial (internal)', '',
           'A = 1 − p/960 over E, UE1, UE2-deadline, UE3 (UE2 goodput gates only); None = a limit or the '
           'protected level failed, or the window was unassessable.', '']
    best = {}
    for load, row, ep in sorted(boards(), key=lambda x: (x[0], x[1]['attempt'])):
        base = ep['trials'][0].get('configuration') or {}
        out += [f'## load {load:g} Mbps/UE — attempt {row["attempt"]} ({row["dir"]})', '',
                '| trial | control | ' + ' | '.join(f'A @ +{w:g} dB' for w in widths) + ' | note |',
                '|---|---|' + '---|' * len(widths) + '---|']
        per_w = {w: score(ep, w) for w in widths}
        for k, t in enumerate(ep['trials']):
            cells = []
            for w in widths:
                a = per_w[w][k][1]
                cells.append('—' if a is None else f'{a:.3f}')
                if a is not None:
                    best[(load, w)] = max(best.get((load, w), -1), a)
            out.append(f'| {t["trialIndex"]} | {label(t.get("configuration"), base)} | ' + ' | '.join(cells)
                       + f' | {per_w[widths[1 if len(widths) > 1 else 0]][k][2]} |')
        out.append('')
    out += ['## best A per load and energy width', '',
            '| load | ' + ' | '.join(f'+{w:g} dB' for w in widths) + ' |', '|---|' + '---|' * len(widths)]
    for load in sorted({k[0] for k in best}):
        out.append(f'| {load:g} | ' + ' | '.join(f'{best.get((load, w), float("nan")):.3f}' for w in widths) + ' |')
    text = '\n'.join(out) + '\n'
    (HERE / 'overnight' / 'v51-pilot-score.md').write_text(text)
    print(text)


if __name__ == '__main__':
    main()
