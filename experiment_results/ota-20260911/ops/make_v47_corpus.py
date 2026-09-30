#!/usr/bin/env python3
"""v4.7 block corpus: the v4.5 intent file with requirement levels = block reference DL x
the owner-approved ratios (.orca/drops/V47_FINAL_PLAN.md section 2.2, 2026-09-25).

    python3 ops/make_v47_corpus.py <reference-dl.json>
    -> pilot38-v4.7-L10-<stamp>/ {intents.json, manifest.json, answers.json, *_SHA.txt}
    -> prints `export AIC_BLOCK_PILOT=... AIC_BLOCK_INTENTS_SHA=... AIC_BLOCK_MANIFEST_SHA=...`

Refuses a reference that is missing or not positive (plan 2.1).  Every adjustable coordinate
gets steps 20 (5% of its interval, owner 2026-09-25: no ladder).  The grid is never expanded:
T holds T0, the all-at-limit target and the Target agent's own choices (tc.sparse_targets).  The paper metric is computed separately at 0.05
by assurance.coordination.concession and does not depend on this grid.
"""
import copy
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

EXP = Path(__file__).resolve().parents[1]
BASE = EXP / 'pilot38-v4.5-L10'
GNB1_CELL = '12345678'
STEPS = 20  # 2026-09-25 23:xx owner: no ladder -- the Target agent picks any 5% point in [limit, original];
            # the grid is never expanded (tc.sparse_targets).  Was 2 (Omega 729) because steps 5 was judged in full.
RATIOS = {'ue1': (0.75, 0.60), 'ue2': (0.80, None), 'ue3': (0.60, 0.40), 'gnb1': (1.00, 0.85)}


def levels(ref):
    """reqId-prefix -> (value, bound) in Mbps, rounded to 0.1, bound strictly below value."""
    def pair(value, bound):
        # Two decimals keep the approved ratios (Codex review #13); no artificial gap.
        v = round(value, 2)
        b = None if bound is None else round(bound, 2)
        if not v > 0 or (b is not None and not 0 < b < v):
            raise SystemExit(f'CORPUS_REFUSED: generated level {v}/{b} is not a positive span')
        return v, b
    ue2 = round(RATIOS['ue2'][0] * ref['ue2'], 2)
    op_value = RATIOS['gnb1'][0] * ref['gnb1Sum']
    # min(85% of the gnb1 reference, ue2's protected level): moving ue3 off gnb1 leaves
    # ue2 alone there, so a higher floor would close that path (plan 2.2, review 2026-09-25).
    op_bound = min(RATIOS['gnb1'][1] * ref['gnb1Sum'], ue2)
    return {'I0c': pair(op_value, op_bound),
            'I1g': pair(RATIOS['ue1'][0] * ref['ue1'], RATIOS['ue1'][1] * ref['ue1']),
            'I2g': (ue2, None),
            'I3g': pair(RATIOS['ue3'][0] * ref['ue3'], RATIOS['ue3'][1] * ref['ue3'])}


#: 안 1 (2026-09-25 20:3x, .orca/drops/V47_REQUIREMENT_TENSION_QUESTION.md): ratios on each UE's
#: share under contention (reference configuration A), not on its solo value, so the baseline
#: leaves ue2 short and pushing it costs ue3 and the operator in every block.  OFF unless
#: AIC_V47_TENSION=1 -- the owner decides; the approved ratios above stay the default.
TENSION = {'ue2': 1.05, 'ue3': (1.00, 0.60), 'gnb1': (1.00, 0.85), 'ue2_cap_of_load': 0.97}


def contention_levels(reference, load=10.0):
    """Levels from configuration A medians (per-UE share while ue2 and ue3 both carry the load)."""
    import statistics
    a = (reference.get('raw') or {}).get('A') or {}
    share = {h: statistics.median(a[h]) for h in ('ue2', 'ue3') if a.get(h)}
    if set(share) != {'ue2', 'ue3'}:
        raise SystemExit('CORPUS_REFUSED: tension levels need configuration A windows for ue2 and ue3')
    ref = reference['reference']
    base = levels(ref)
    ue2 = round(min(TENSION['ue2'] * share['ue2'], TENSION['ue2_cap_of_load'] * load), 2)
    op_value = TENSION['gnb1'][0] * (share['ue2'] + share['ue3'])
    op_bound = min(TENSION['gnb1'][1] * (share['ue2'] + share['ue3']), ue2)
    v3, b3 = TENSION['ue3'][0] * share['ue3'], TENSION['ue3'][1] * share['ue3']
    out = dict(base)
    out['I2g'] = (ue2, None)
    out['I0c'] = (round(op_value, 2), round(op_bound, 2))
    out['I3g'] = (round(v3, 2), round(b3, 2))
    for key in ('I0c', 'I3g'):
        v, b = out[key]
        if not 0 < b < v:
            raise SystemExit(f'CORPUS_REFUSED: tension level {key} {v}/{b} is not a positive span')
    return out


GNB2_CELL = '87654321'
GNB1_CELL = '12345678'


def contention_cell():
    """The cell ue2 and ue3 share: gnb1 (placement B), or gnb2 under AIC_CONTENTION_CELL=gnb2 (owner
    2026-09-28 option B: ue3 lost its gnb1 downlink after the power cycle, so the cells swap roles and
    the UE roles -- ue1 alone, ue2 deadline, ue3 competitor -- stay as they were)."""
    return GNB2_CELL if os.environ.get('AIC_CONTENTION_CELL', '').strip() == 'gnb2' else GNB1_CELL


def _shared_name():
    return 'gnb2' if contention_cell() == GNB2_CELL else 'gnb1'


def _placement_text():
    shared = contention_cell()
    lone = GNB1_CELL if shared == GNB2_CELL else GNB2_CELL
    name = {GNB1_CELL: 'gnb1', GNB2_CELL: 'gnb2'}
    return f'ue2 + ue3 on {name[shared]} (cell {shared}), ue1 on {name[lone]} (cell {lone})'


def energy_cell():
    """The cell the energy requirement and the attenuation axis live on.  v5 used gnb2 (ue1 alone);
    AIC_V51_ENERGY_CELL=gnb1 (owner 2026-09-27, option 3) moves both onto gnb1, where ue2 and ue3
    share the cell, so attenuation costs the services it competes with."""
    return GNB1_CELL if os.environ.get('AIC_V51_ENERGY_CELL', '').strip() == 'gnb1' else GNB2_CELL


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def energy_intent(reference, intents):
    """v4.7 cell power (owner 2026-09-25): the gnb2 operator's energy intent, when the block
    reference measured one (``reference['energy'] = {cell, originalDb, boundDb}``).

    ``cellTxAttenuationDb >= originalDb`` (save that much), bound ``boundDb`` (at worst stay as
    now).  Absent -> None and the corpus is exactly what it was."""
    e = reference.get('energy')
    if not e:
        return None
    value, bound = float(e['originalDb']), float(e['boundDb'])
    if not value > bound >= 0:
        raise SystemExit(f'CORPUS_REFUSED: energy intent {value}/{bound} dB is not a positive span')
    return {'intentId': 'I4e', 'owner': 'operator-gnb2',
            'priority': max(int(i.get('priority') or 0) for i in intents) + 1, 'weight': 1.0,
            'requirement': {'reqId': 'I4e.r1', 'kpi': 'cellTxAttenuationDb', 'op': '>=',
                            'value': value, 'bound': bound, 'steps': STEPS, 'unit': 'dB',
                            'scope': f"cell@{e.get('cell') or GNB2_CELL}"}}


def att_cells(reference):
    """``AIC_BLOCK_ATT_CELLS`` for a block with an energy intent: the measured ``controlRungs``
    (reference_dl's sweep), else every 3 dB above the bound up to the original (the original
    always a rung).  '' without one."""
    e = reference.get('energy')
    if not e:
        return ''
    ladder = e.get('controlRungs')
    if not ladder:
        bound, value = float(e['boundDb']), float(e['originalDb'])
        ladder = [round(bound + 3.0 * k, 1) for k in range(1, int((value - bound) // 3) + 1)]
        if not ladder or ladder[-1] != value:
            ladder.append(value)
    return f"{e.get('cell') or GNB2_CELL}:" + ','.join(f'{float(x):g}' for x in ladder)


#: v5 design (owner 2026-09-26): one owner ("operator") declares five requirements; the four
#: adjustable coordinates are compared lexicographically in priority order (energy, UE1 goodput,
#: UE2 deadline success, UE3 goodput) and UE2 goodput is a fixed condition on every target.
#: Enabled by overnight/V5_DESIGN; absent -> the v4.7 corpus below, unchanged.
V5_FLAG = Path(__file__).resolve().parent / 'overnight' / 'V5_DESIGN'
V5_ENERGY_ORIGINAL_OFFSET_DB = 12.0      # original = A0 + 12 dB, limit = A0 (candidate, 2026-09-26)
#: The gnb2 attenuation control ladder as offsets from the block's baseline A0 -- the
#: coordinator's interim choice, kept in one place so it can be changed.
V5_ATT_LADDER_OFFSETS_DB = (0.0, 3.0, 6.0, 9.0, 12.0, 15.0)
V5_RATIOS = {'ue1': (0.95, 0.60), 'ue2_goodput': 0.40, 'ue2_deadline': (0.90, 0.60),
             'ue3': (0.95, 0.40)}


def v5_enabled():
    return V5_FLAG.exists()


def build_v5(reference):
    """The v5 corpus from the block reference: R1/R2/R3 = reference ue1/ue2/ue3, the UE2 deadline
    d = reference['ue2DeadlineMs'] (fixed, not relaxable) and the gnb2 baseline attenuation
    A0 = reference['energyBaselineDb'].  Refuses when any of them is missing."""
    import math
    ref = reference.get('reference') or {}
    r = {k: ref.get(k) for k in ('ue1', 'ue2', 'ue3')}
    bad = [k for k, v in r.items() if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0]
    if bad:
        raise SystemExit(f'CORPUS_REFUSED: reference missing or not positive: {bad}')
    d, a0 = reference.get('ue2DeadlineMs'), reference.get('energyBaselineDb')
    if isinstance(d, bool) or not isinstance(d, (int, float)) or not d > 0:
        raise SystemExit('CORPUS_REFUSED: no valid UE2 deadline d (reference ue2DeadlineMs)')
    if isinstance(a0, bool) or not isinstance(a0, (int, float)) or a0 < 0:
        raise SystemExit('CORPUS_REFUSED: no gnb2 baseline attenuation (reference energyBaselineDb)')
    base = json.loads((BASE / 'intents.json').read_text(encoding='utf-8'))
    ue_of = {i['intentId'][:2]: i.get('ueId') for i in base['intents'] if i.get('ueId')}

    def intent(iid, priority, ue, req):
        row = {'intentId': iid, 'owner': 'operator', 'priority': priority, 'weight': 1.0,
               'requirement': dict(req, reqId=f'{iid}.r1')}
        if ue:
            row['ueId'] = ue
        return row

    def span(value, bound):
        v, b = round(value, 2), round(bound, 2)
        if not 0 <= b < v:
            raise SystemExit(f'CORPUS_REFUSED: generated level {v}/{b} is not a positive span')
        return {'value': v, 'bound': b, 'steps': STEPS}

    ratio = V5_RATIOS
    intents = [
        intent('I4e', 1, None, {'kpi': 'cellTxAttenuationDb', 'op': '>=', 'unit': 'dB',
                                'scope': f'cell@{energy_cell()}',
                                **span(a0 + _env_float('AIC_V51_ENERGY_OFFSET_DB', V5_ENERGY_ORIGINAL_OFFSET_DB), a0)}),
        intent('I1g', 2, ue_of.get('I1'), {'kpi': 'dlGoodputMbps', 'op': '>=', 'unit': 'Mbps',
                                          **span(ratio['ue1'][0] * r['ue1'], ratio['ue1'][1] * r['ue1'])}),
        intent('I2d', 3, ue_of.get('I2'), {'kpi': 'deadlineSuccessRatio', 'op': '>=', 'unit': 'ratio',
                                          'deadlineMs': float(d), 'deadlineBound': float(d),
                                          'deadlineSteps': 0, **span(*ratio['ue2_deadline'])}),
        intent('I3g', 4, ue_of.get('I3'), {'kpi': 'dlGoodputMbps', 'op': '>=', 'unit': 'Mbps',
                                          **span(ratio['ue3'][0] * r['ue3'], ratio['ue3'][1] * r['ue3'])}),
        intent('I2g', 5, ue_of.get('I2'), {'kpi': 'dlGoodputMbps', 'op': '>=', 'unit': 'Mbps',
                                          'value': round(ratio['ue2_goodput'] * r['ue2'], 2),
                                          'steps': 0}),
    ]
    domain = {'v5': {
        'rule': ('one owner (operator); ROC weights from the priority order E -> UE1 goodput -> '
                 'UE2 deadline success -> UE3 goodput, p = 25 kE + 13 k1 + 7 k2 + 3 k3, A = 1 - p/960, '
                 'k = ceil(20 q); UE2 goodput >= 0.40 R2 on every target (v5.1)'
                 if os.environ.get('AIC_V51') == '1' else
                 'one owner (operator); coordinates compared lexicographically in priority order '
                 'E -> UE1 goodput -> UE2 deadline success -> UE3 goodput, p = 21^3 kE + 21^2 k1 '
                 '+ 21 k2 + k3, k = ceil(20 q); UE2 goodput >= 0.40 R2 on every target'),
        'ratios': V5_RATIOS, 'energyOriginalOffsetDb': V5_ENERGY_ORIGINAL_OFFSET_DB,
        'energyBaselineDb': a0, 'ue2DeadlineMs': d,
        'ue2DeadlineRule': reference.get('ue2DeadlineRule') or 'B-p90', 'reference': ref,
        'referenceMeasuredAt': reference.get('at'), 'sendRateMbps': reference.get('loadMbps'),
        'loadRule': reference.get('loadRule'), 'steps': STEPS,
        'attLadderOffsetsDb': list(V5_ATT_LADDER_OFFSETS_DB),
        'mandatoryTargets': 'T0 only; the all-at-limit target stays in Omega'},
        'placement': _placement_text(),
        'observedNotRequired': f'{_shared_name()} cell goodput is observed and recorded, not a requirement'}
    return {'domain': domain, 'intents': intents, 'jointConditions': []}


#: v5.1 (AIC_V51=1 or AIC_POLICY_RANGE=1, owner 2026-09-26 "범위만 주자"): the models see only min..max, so
#: the frozen grid is the RU's 0.5 dB step across the same span instead of the 3 dB ladder.
V51_ATT_GRID_STEP_DB = 0.5


def v5_att_cells(reference):
    a0 = float(reference['energyBaselineDb'])
    offsets = V5_ATT_LADDER_OFFSETS_DB
    if '1' in (os.environ.get('AIC_POLICY_RANGE'), os.environ.get('AIC_V51')):
        # AIC_V51_ATT_MAX_OFFSET_DB: the measured support of the energy cell (gnb1 cannot re-attach
        # a UE far off its baseline), else the v5 span.
        top = _env_float('AIC_V51_ATT_MAX_OFFSET_DB', max(offsets))
        offsets = [k * V51_ATT_GRID_STEP_DB for k in range(int(round(top / V51_ATT_GRID_STEP_DB)) + 1)]
    return f'{energy_cell()}:' + ','.join(f'{a0 + x:g}' for x in offsets)


def build(reference):
    ref = reference['reference']
    import math
    bad = [k for k, v in ref.items() if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0]
    if bad:
        raise SystemExit(f'CORPUS_REFUSED: reference missing or not positive: {bad}')
    corpus = json.loads((BASE / 'intents.json').read_text(encoding='utf-8'))
    out = copy.deepcopy(corpus)
    lv = contention_levels(reference) if os.environ.get('AIC_V47_TENSION') == '1' else levels(ref)
    for intent in out['intents']:
        req = intent['requirement']
        iid = intent['intentId']
        if iid in lv:
            value, bound = lv[iid]
            req['value'] = value
            if bound is None:                      # protected
                req['steps'] = 0
                req.pop('bound', None)
            else:
                req['bound'], req['steps'] = bound, STEPS
        if iid == 'I0c':
            intent['owner'] = f'operator-{_shared_name()}'
            req['scope'] = f'cell@{contention_cell()}'
        if iid in ('I2d', 'I3d') and int(req.get('steps') or 0) > 0:
            req['steps'] = STEPS
        if iid == 'I3d' and int(req.get('deadlineSteps') or 0) > 0:
            req['deadlineSteps'] = STEPS
    energy = energy_intent(reference, out['intents'])
    if energy is not None:
        out['intents'].append(energy)
    out['domain']['v47'] = {
        'rule': 'requirement = block reference DL x fixed ratio; operator limit = '
                f'min(85% of the {_shared_name()} reference, ue2 protected level)',
        'ratios': RATIOS, 'reference': ref, 'referenceMeasuredAt': reference.get('at'),
        'keepaliveMbps': reference.get('keepaliveMbps'), 'steps': STEPS,
        'sendRateMbps': reference.get('loadMbps'), 'loadRule': reference.get('loadRule'),
        'energy': reference.get('energy'),
        'plan': '.orca/drops/V47_FINAL_PLAN.md'}
    # The v4.5 descriptions name the old placement (ue1+ue3 on gnb2, operator on gnb2);
    # replace them rather than ship contradictions (Codex review #20).
    for stale in ('placement', 'owners', 'conflictNote', 'versionNote', 'baselineAttainment',
                  'whyEachLadderIsOnThatAxis', 'expansionCaveat'):
        out['domain'].pop(stale, None)
    out['domain']['placement'] = f'v4.7 placement: {_placement_text()}; steering may move any UE.'
    out['domain']['owners'] = {f'operator-{_shared_name()}': f'cell operator of {_shared_name()}: total goodput of the '
                               'UEs served by that cell', 'ue1-video': 'UE1 service',
                               'ue2-map': 'UE2 service (goodput protected)',
                               'ue3-incumbent': 'UE3 service'}
    out['domain']['targetGrid'] = ('no ladder: each adjustable coordinate is [limit, original] at '
                                   '5% resolution (steps 20); the Target agent chooses its own '
                                   'intermediate targets, T0 and the all-at-limit target are '
                                   'mandatory, and the paper metric is the best supported target '
                                   'at 0.05 precision.')
    return out


def main(argv):
    if len(argv) != 2:
        raise SystemExit(__doc__)
    reference = json.loads(Path(argv[1]).read_text(encoding='utf-8'))
    v5 = v5_enabled()
    corpus = build_v5(reference) if v5 else build(reference)
    stamp = time.strftime('%Y%m%dT%H%M%S')
    target = EXP / (f'pilot38-v5-{stamp}' if v5 else f'pilot38-v4.7-L10-{stamp}')
    target.mkdir()
    (target / 'intents.json').write_text(json.dumps(corpus, ensure_ascii=False, indent=1) + '\n',
                                         encoding='utf-8')
    shutil.copy2(BASE / 'manifest.json', target / 'manifest.json')
    answers = json.loads((BASE / 'answers.json').read_text(encoding='utf-8'))
    answers.setdefault('_provenance', {})['for'] = f'{target.name}/intents.json'
    (target / 'answers.json').write_text(json.dumps(answers, ensure_ascii=False, indent=1) + '\n',
                                         encoding='utf-8')
    sha = {name: hashlib.sha256((target / name).read_bytes()).hexdigest()
           for name in ('intents.json', 'manifest.json')}
    (target / 'INTENTS_SHA.txt').write_text(sha['intents.json'] + '\n')
    (target / 'MANIFEST_SHA.txt').write_text(sha['manifest.json'] + '\n')
    # The block's send rate travels with its corpus (solo-max rule, 2026-09-25): the runner sources
    # this line, and run_case.sh prefers AIC_BLOCK_LOAD over its load argument.
    load = reference.get('loadMbps') or 10.0
    # AIC_BLOCK_PILOT stays first: the runner checks the line with '^export AIC_BLOCK_PILOT='.
    print(f"export AIC_BLOCK_PILOT={target} AIC_BLOCK_INTENTS_SHA={sha['intents.json']} "
          f"AIC_BLOCK_MANIFEST_SHA={sha['manifest.json']} AIC_BLOCK_LOAD={load:g}"
          # Always assigned, empty without an energy intent: the runner sources each block's
          # env into the same shell, so omitting it would carry the previous block's ladder
          # (Codex review 2026-09-25 #3).
          + f" AIC_BLOCK_ATT_CELLS={(v5_att_cells(reference) if v5 else att_cells(reference)) or ''}"
          # The sitting turns v5 ranking on only from this declaration (Codex review 2026-09-26);
          # always assigned so a v4.7 block never inherits it.
          + f" AIC_DESIGN={'v5' if v5 else ''}"
          + (" AIC_V51=1" if v5 and os.environ.get('AIC_V51') == '1' else '')
          # v5.2: formation and the basic monolith read this block's reference measurement;
          # always assigned so a block never inherits the previous block's file.
          + f" AIC_V52_REFERENCE={Path(argv[1]).resolve() if os.environ.get('AIC_V52') == '1' else ''}"
          + (" AIC_V52=1" if os.environ.get('AIC_V52') == '1' else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
