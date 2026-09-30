#!/usr/bin/env python3
"""Per-episode LLM cost ledger, regenerated from the evidence.

The numbers were always there -- every ``AGENT-*-episode.json`` carries
``calls[]`` with ``role``, ``model``, ``latencyMs``, ``inputTokens`` and
``outputTokens``.  What was missing is a form you can read without opening
each file, which is what the owner asked for: record it for every episode, as
a matter of course.

This regenerates the whole ledger from the evidence each run rather than
appending.  That is deliberate: appending needs state, and state drifts from
the evidence it claims to summarise.  Regeneration is idempotent, so it is
safe to call after every episode, twice, or never.

It writes **derived** files only.  It never touches a trial, a profile or an
episode record, so it is safe to run while the campaign is live -- unlike
changing the runner, which would redefine what an arm is mid-window.

Two ledgers per window:

  LLM-COST-BY-EPISODE.tsv  one row per episode
  LLM-COST-BY-ROLE.tsv     one row per (method, role): n, median, mean, total

Windows are kept apart on purpose.  Input sizes are not comparable across
them: commit 0c2b7771d took Target and Control off the live path and stopped
the predictor table bloating every call, which moved the Control role's median
input from 67 354 to 6 176 tokens.  Averaging across that change would be
fiction.
"""
from __future__ import annotations

import json
import pathlib
import statistics
import sys
from collections import defaultdict

EXP = pathlib.Path('/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/'
                   'experiment_results/ota-20260911')

#: A call the model did not answer.  Counted separately: folding a fallback
#: into the latency median would report the arm as faster than it is.
DETERMINISTIC = 'deterministic'


def method_of(doc: dict) -> str:
    """The arm the record names.

    ``method`` is a top-level field.  An earlier version of this script
    inferred the arm from ``models`` instead and put every basic monolith in
    the internal monolith's row -- both arms fill ``models.monolith``, because
    both ask one model, and the field says *which model*, not *which method*.
    Read the field that answers the question being asked.
    """
    return str(doc.get('method') or '').strip() or 'unknown'


def requested_models(doc: dict) -> str:
    """The model names the arm asked for, as the record kept them.

    This is the **request label**, not necessarily the served identity: the
    resolver matches a label against the backend enum, and ``MODEL_IDS`` maps
    that entry to whatever id is actually sent.  When the operator sets the
    label to the served id they coincide (two episodes on 2026-09-14 read
    ``gpt-5.6-luna`` here); when the label is ``claude-sonnet`` they do not,
    and the served identity has to come from the proxy's own log.
    """
    return ','.join(sorted({str(v) for v in (doc.get('models') or {}).values() if v})) or '-'


def episode_rows(window: pathlib.Path):
    for path in sorted(window.rglob('*episode*.json')):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        calls = doc.get('calls') or []
        served = [c for c in calls if c.get('model') != DETERMINISTIC]
        fell_back = [c for c in calls if c.get('model') == DETERMINISTIC
                     and c.get('role') != 'intake']
        cost = doc.get('resourceCost') or {}
        timing = doc.get('timing') or {}
        yield {
            'episode': path.parent.parent.name,
            'at': str(timing.get('t0') or timing.get('prepStart') or '')[:19],
            'method': method_of(doc),
            'models': requested_models(doc),
            'block': doc.get('block'),
            'condition': str((doc.get('condition') or {}).get('name') or ''),
            'termination': str((doc.get('termination') or {}).get('reason') or ''),
            'kernelTermination': str((doc.get('termination') or {})
                                     .get('kernelTermination') or ''),
            'trials': len(doc.get('trials') or []),
            'servedCalls': len(served),
            'fallbackCalls': len(fell_back),
            'inputTokens': sum(c.get('inputTokens') or 0 for c in served),
            'outputTokens': sum(c.get('outputTokens') or 0 for c in served),
            'latencyS': round(sum(c.get('latencyMs') or 0 for c in served) / 1000, 1),
            'slowestCallS': round(max([c.get('latencyMs') or 0
                                       for c in served] or [0]) / 1000, 1),
        }, [dict(c, _method=method_of(doc)) for c in served]


def write_tsv(path: pathlib.Path, header, rows) -> None:
    # An empty cell is correct TSV but reads as a shifted column under
    # `column -t`, which is how these files get looked at.  Write the absence
    # explicitly instead.
    def cell(value):
        text = str(value)
        return text if text.strip() else '-'
    path.write_text('\t'.join(header) + '\n'
                    + ''.join('\t'.join(cell(r[k]) for k in header) + '\n'
                              for r in rows))


def main(argv) -> int:
    windows = [EXP / a for a in argv[1:]] or sorted(EXP.glob('window-*'))
    for window in windows:
        if not window.is_dir():
            continue
        episodes, calls = [], []
        for row, served in episode_rows(window):
            episodes.append(row)
            calls.extend(served)
        if not episodes:
            continue
        episodes.sort(key=lambda r: (r['at'], r['episode']))
        header = ['at', 'method', 'models', 'block', 'condition', 'termination',
                  'kernelTermination', 'trials', 'servedCalls', 'fallbackCalls',
                  'inputTokens', 'outputTokens', 'latencyS', 'slowestCallS',
                  'episode']
        write_tsv(window / 'LLM-COST-BY-EPISODE.tsv', header, episodes)

        # Grouped by phase as well as role.  Role alone cannot separate the
        # internal monolith's formation call from its selection calls -- both
        # are ``role='monolith'`` -- and those two differ by an order of
        # magnitude in output tokens and latency.  Averaging them together is
        # how a per-episode mean stops meaning anything: an episode that ended
        # at T0 with no decision costs nothing and drags the arm down.  Cost
        # has to be read as a fixed part (formation, once per episode) plus a
        # marginal part (one call per decision).
        by_role = defaultdict(list)
        for c in calls:
            by_role[(c['_method'], c.get('phase'), c.get('role'))].append(c)
        role_rows = []
        for (method, phase, role), cs in sorted(by_role.items(), key=lambda kv: str(kv[0])):
            def col(key):
                return [c.get(key) or 0 for c in cs]
            role_rows.append({
                'method': method, 'phase': phase, 'role': role, 'calls': len(cs),
                'latencyMedianS': round(statistics.median(col('latencyMs')) / 1000, 1),
                'latencyMeanS': round(statistics.fmean(col('latencyMs')) / 1000, 1),
                'latencyMaxS': round(max(col('latencyMs')) / 1000, 1),
                'inputMedian': int(statistics.median(col('inputTokens'))),
                'outputMedian': int(statistics.median(col('outputTokens'))),
                'inputTotal': sum(col('inputTokens')),
                'outputTotal': sum(col('outputTokens')),
            })
        write_tsv(window / 'LLM-COST-BY-ROLE.tsv',
                  ['method', 'phase', 'role', 'calls', 'latencyMedianS',
                   'latencyMeanS', 'latencyMaxS', 'inputMedian', 'outputMedian',
                   'inputTotal', 'outputTotal'], role_rows)
        print('%s: %d episodes, %d served calls -> LLM-COST-BY-EPISODE.tsv, '
              'LLM-COST-BY-ROLE.tsv' % (window.name, len(episodes), len(calls)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
