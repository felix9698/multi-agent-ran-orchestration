"""Reproducible, stdlib-only metrics for agent-episode/1.0.0 .. 1.3.0 records.

Times are milliseconds unless a field explicitly says seconds. Service integrals
use seconds, so e.g. Mbps shortages integrate to Mbit. Missing values are None.
The JSON summary has groups (condition/method/timingMode) and equal-weight overall
rates; timing modes are never pooled. No episode is silently excluded.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
import hashlib
from dataclasses import replace
import json
import math
from pathlib import Path
import random
from statistics import fmean, median

from assurance.coordination.intake import KpiObservationRule
from assurance.coordination.tc import (KPI_DEADLINE_RATIO, PASS, Authorization, JointCondition,
                                       Preference, TargetContract, TargetValidationError,
                                       concession_of as target_concession,
                                       expand_targets, judge_target, mode_constraint_from_record,
                                       observation_at_deadline, preference_key, verdict_of)
from experiments.metrics import mean_std


def load_episodes(paths_or_dir):
    """Read a file, directory (recursively), or iterable of files/directories."""
    paths = [paths_or_dir] if isinstance(paths_or_dir, (str, Path)) else paths_or_dir
    result = []
    for item in paths:
        path = Path(item)
        for file in sorted(path.rglob('*.json')) if path.is_dir() else [path]:
            record = json.loads(file.read_text(encoding='utf-8'))
            # A CLOSED allow-list: an unlisted version is dropped with no error
            # and no warning, so N just shrinks. Any writer-side version bump
            # must land here in the same change or the metrics report N = 0.
            if isinstance(record, dict) and record.get('schemaVersion') in ('agent-episode/1.0.0', 'agent-episode/1.1.0', 'agent-episode/1.2.0', 'agent-episode/1.3.0'):
                result.append(record)
    return result


def _ms(value):
    if isinstance(value, (int, float)):
        return float(value)
    if value is None or not str(value).strip():
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000


def _elapsed(episode, value):
    start = _ms(episode.get('timing', {}).get('t0'))
    end = _ms(value)
    return end - start if start is not None and end is not None else None


def _service_elapsed(episode, point):
    """Elapsed service time of one trace point, on the clock H is measured on.

    Owner 2026-09-23 ("볼드모트"): an our-side interruption -- a UE the bed dropped,
    a source that stopped, a handover past its normal time -- does not exist.  Its
    samples are not used (``None``: not a shortfall, not a missing bin), and every
    later point is moved earlier by the excised time before it, so [t0, t0+H) is H
    of usable service.  The hold extensions that refilled a judged window left the
    timeline with the rest, so their samples are left out here too.  An episode
    with no ``excision`` record reads exactly as before.
    """
    elapsed = _elapsed(episode, point.get('t'))
    excision = episode.get('excision') or {}
    if elapsed is None or not excision:
        return elapsed
    if point.get('excised') or point.get('holdExtension'):
        return None
    t0, at = _ms(episode.get('timing', {}).get('t0')), _ms(point.get('t'))
    spans = sorted((_ms(item.get('start')), _ms(item.get('end')))
                   for item in excision.get('intervals') or ()
                   if _ms(item.get('start')) is not None and _ms(item.get('end')) is not None
                   and _ms(item.get('end')) > _ms(item.get('start')))
    # Merged first: a roll-back (2026-09-24, "recovery") often overlaps an observer
    # gap in the trace, and the same seconds must not be removed twice.
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    # Each merged run is relieved at most ``runCapMs`` (the sitting's one-wait cap,
    # 2026-09-24) -- the same number B was relieved of; older records carry none.
    cap = excision.get('runCapMs')
    removed = 0.0
    for start, end in merged:
        if start < at <= end:
            return None
        part = max(0.0, min(end, at) - max(start, t0))
        removed += part if cap is None else min(part, float(cap))
    return elapsed - removed


def _requirements(episode):
    return {i['requirement']['reqId']: dict(i['requirement'], owner=i['owner'])
            for i in episode.get('intents', [])}


def q_r(original, limit, value, op='>='):
    """Normalized concession; unauthorized relaxation raises ValueError.

    Strengthening costs zero. Equality/categorical requirements stay unchanged.
    """
    if value == original:
        return 0.0
    if op == '==':
        raise ValueError('categorical relaxation needs an explicit concession mapping')
    direction = 1 if op == '>=' else -1
    if op not in ('>=', '<='):
        raise ValueError('unsupported requirement operator: ' + op)
    loss = direction * (float(original) - float(value))
    if loss <= 0:
        return 0.0
    span = 0 if limit is None else direction * (float(original) - float(limit))
    if span <= 0 or loss > span + 1e-12:
        raise ValueError('target exceeds authorized relaxation')
    return loss / span


def _finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def concession_of(episode, target):
    """Recompute q_r, D_o, D_mean and D_max, preserving raw thresholds."""
    requirements = _requirements(episode)
    auth = episode.get('T', {}).get('authorization', {})
    values = target.get('requirements', {})
    per_req, owners, raw = {}, defaultdict(list), {}
    for rid, req in requirements.items():
        a = auth.get(rid, {})
        original = a.get('original', req['value'])
        limit = a.get('bound', a.get('limit', req.get('bound', req.get('relaxLimit'))))
        if req.get('relaxable') is False:
            limit = original
        value = values[rid]
        per_req[rid] = q_r(original, limit, value, a.get('op', req['op']))
        # 완화가 **인가되지 않은** 요구는 owner 의 D 에 들어가지 않는다.  그런 요구의
        # q_r 은 정의상 언제나 0 이므로, 평균에 섞으면 그 owner 가 실제로 양보한 만큼을
        # 기계적으로 깎는다 -- 2026-09-21 전수: 현행 액션공간 이후 D 가 있는 64판 중
        # **33판(52%)** 에서 이 한 줄 때문에 `ue2-map` 이 정확히 절반이 됐고, 그중 일부는
        # `D_max` 가 1.0 대 0.5 로 **순서까지** 뒤집혔다.  런타임(`tc.py:2445`)은 처음부터
        # `if entry.relaxable:` 로 걸러 왔다 -- 두 구현이 달라 어느 쪽을 읽느냐로 수가
        # 달라지고 있었다.
        if req.get('relaxable') is not False:
            owners[req['owner']].append(per_req[rid])
        raw[rid] = {'original': original, 'limit': limit, 'value': value, 'unit': req['unit']}
        # Authorization.to_record() is a reqId map, not the model-input wrapper.
        # D is a separate ceiling concession, even when reliability is fixed.
        original_d = a.get('deadlineMs', req.get('deadlineMs'))
        deadlines = target.get('deadlines') or {}
        if original_d is None and rid not in deadlines:
            continue
        deadline = deadlines.get(rid, original_d)
        bound_d = a.get('deadlineBound', req.get('deadlineBound'))
        steps_d = a.get('deadlineSteps', req.get('deadlineSteps'))
        original_number, deadline_number = _finite_number(original_d), _finite_number(deadline)
        bound_number = _finite_number(bound_d)
        if (original_number is None or original_number <= 0
                or deadline_number is None or deadline_number <= 0):
            raise ValueError('deadline needs a positive original and target')
        extendable = (bound_number is not None and bound_number > original_number
                      and (steps_d is None or (_finite_number(steps_d) or 0) > 0))
        if not extendable:
            bound_d = original_d
            if deadline_number != original_number:
                raise ValueError('deadline must remain unchanged without authorization')
        deadline_id = rid + '#deadline'
        per_req[deadline_id] = q_r(original_d, bound_d, deadline, '<=')
        if extendable:                     # 마감도 같은 규칙: 인가된 축만 센다
            owners[req['owner']].append(per_req[deadline_id])
        raw[deadline_id] = {'original': original_d, 'limit': bound_d,
                            'value': deadline, 'unit': 'ms'}
    per_owner = {owner: fmean(qs) for owner, qs in owners.items() if qs}
    # 양보할 축이 하나도 없으면 D 는 **0 이 아니라 없다.**  0 으로 적으면 "아무것도
    # 양보하지 않았다"(가장 좋은 결과)로 읽히는데, 실제로는 양보라는 차원 자체가 없어
    # 잴 것이 없었다는 뜻이다 -- 달성률 0% 인 판이 D=0 으로 보이는 경로가 여기다.
    return {'perRequirement': per_req, 'perOwner': per_owner,
            'mean': fmean(per_owner.values()) if per_owner else None,
            'max': max(per_owner.values()) if per_owner else None, 'thresholds': raw}


def _targets(episode):
    contract = episode.get('T') or {}
    return {t['targetId']: t for t in [contract.get('t0')] + contract.get('alternatives', []) if t}


def _deadline_evidence_supports(episode, target, trial):
    """Check only deadline evidence; keep verdict-only legacy records readable."""
    if 'kpis' not in trial:
        return True
    kpis = trial.get('kpis') or {}
    auth = (episode.get('T') or {}).get('authorization', {})
    for rid, req in _requirements(episode).items():
        if req['kpi'] != KPI_DEADLINE_RATIO:
            continue
        if not isinstance(kpis, Mapping):
            return False
        original = auth.get(rid, {}).get('deadlineMs', req.get('deadlineMs'))
        deadline = (target.get('deadlines') or {}).get(rid, original)
        key = req['kpi'] + '@' + req['scope'].split('@', 1)[-1]
        measured = observation_at_deadline(kpis.get(key), deadline, original)
        wanted = _finite_number(target.get('requirements', {}).get(rid))
        if measured is None or wanted is None:
            return False
        if ((req['op'] == '>=' and measured < wanted - 1e-9)
                or (req['op'] == '<=' and measured > wanted + 1e-9)
                or (req['op'] == '==' and abs(measured - wanted) > 1e-9)):
            return False
    return True


def _valid_windows(episode):
    """Complete measured windows within K and B: (trial, index, elapsedMs).

    One gate, so a window that may not score a success may not score an Omega
    attainment either. Never merged; an excluded episode yields nothing.
    """
    if episode.get('excluded'):
        return []
    budget = episode.get('budget', {})
    candidates = {c['controlId'] for c in (episode.get('C') or {}).get('candidates', [])}
    rows = []
    counted = 0
    explicit_counting = any('counted' in t for t in episode.get('trials', []))
    boundaries = [stamp for b in episode.get('boundaries', [])
                  if b.get('kind') in ('intent', 'policy', 'exogenous')
                  for stamp in (_ms(b.get('at')),)
                  if stamp is not None]
    for trial in episode.get('trials', []):
        window = trial.get('window', {})
        elapsed = _elapsed(episode, window.get('end'))
        initial = trial.get('trialIndex') == 0
        if trial.get('counted', not initial):
            counted += 1
        elif not initial:
            continue
        index = (0 if initial else counted) if explicit_counting else trial['trialIndex']
        if initial and elapsed is not None:
            elapsed = 0
        if trial.get('beforeBoundary') is not None:
            continue
        if boundaries and (_ms(window.get('end')) is None or _ms(window.get('end')) < max(boundaries)):
            continue
        if (window.get('valid') is not True or elapsed is None or elapsed < 0
                or (episode.get('schemaVersion') != 'agent-episode/1.1.0'
                    and elapsed > (budget.get('deadlineBMs') or math.inf))
                or index > budget.get('trialsK', math.inf)
                or (candidates and trial.get('controlId') not in candidates)):
            continue
        rows.append((trial, index, elapsed))
    return rows


def valid_successes(episode):
    """Individual complete successful windows within K and B, never merged."""
    targets = _targets(episode)
    found = []
    for trial, index, elapsed in _valid_windows(episode):
        for tid, target in targets.items():
            verdicts = trial.get('verdicts', {}).get(tid)
            success = (all(verdicts.get(rid) == 'PASS' for rid in _requirements(episode))
                       if verdicts is not None else trial.get('success', {}).get(tid) is True)
            if not success:
                continue
            try:
                quality = concession_of(episode, target)
                if not _deadline_evidence_supports(episode, target, trial):
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            if quality['max'] is not None:
                found.append({'targetId': tid, 'controlId': trial.get('controlId'),
                              'trialIndex': index, 'elapsedMs': 0 if index == 0 else elapsed,
                              'concession': quality, 'cost': target.get('cost'),
                              'levels': target.get('levels', {})})
    return sorted(found, key=lambda r: (r['elapsedMs'], r['trialIndex']))


def _pinned_authorization(episode):
    """The episode's own signed authorization, with what shapes and ranks Omega."""
    record = episode.get('T') or {}
    rows = record.get('authorization')
    if not isinstance(rows, Mapping) or not rows:
        raise ValueError('episode carries no pinned authorization; Omega is undefined')
    authorization = Authorization.from_record(rows)
    if not authorization.requirements:
        raise ValueError('the pinned authorization names no requirement')
    # Both carvings are as much of the pinned domain as the bounds are: a joint
    # condition is a predicate on one observed KPI, and the mode constraints carve
    # across axes and across owners, which a single-KPI predicate structurally
    # cannot. Dropping either rebuilds a *larger* Omega than the episode ran under,
    # and since target ids are positional -- assigned after the ranking sort --
    # every id past a vector that should not be there names a different vector.
    # Authorization() skips whatever it cannot read, so both are parsed here and
    # the episode is refused instead, naming the entry. They are read by one loop
    # so the two can never drift into different postures.
    def pinned(field, parse, what):
        read = []
        for entry in record.get(field) or ():
            item = parse(entry)
            if item is None:
                raise ValueError('unreadable pinned %s %r; dropping it would widen '
                                 'Omega and shift every target id' % (what, entry))
            read.append(item)
        return tuple(read)

    return Authorization(
        authorization.requirements,
        joint_conditions=pinned('jointConditions', JointCondition.from_record,
                                'joint condition'),
        mode_constraints=pinned('modeConstraints', mode_constraint_from_record,
                                'mode constraint'),
        preference=Preference.from_record(record.get('preference') or {}))


def omega_targets(episode):
    """Omega: the episode's *pinned* authorization, expanded into every authorized vector.

    The domain is read from the record's own T.authorization and expanded by the
    contract's own expand_targets, so every method that ran under that
    authorization is scored against the identical Omega. A missing or unusable
    authorization raises: an empty Omega would silently flatter whichever arm
    prepared the least.
    """
    authorization = _pinned_authorization(episode)
    try:
        return expand_targets(authorization)
    except TargetValidationError:
        # v4.7 without a ladder (owner 2026-09-25): 21 levels per coordinate cannot be
        # expanded, and the runtime judged the arm's own T instead (tc.omega_contract).
        # Score against that same T, so trial.success ids name these targets.
        return replace(TargetContract.from_record(episode['T']), authorization=authorization)


#: Deliberately outside the identity. ``scope`` is the measurement binding, and
#: the RNTI behind it moves on every UE reattachment (ue@386, ue@390, ue@400 in
#: one campaign) while the owners, ops, bounds, steps, levels and weights stay
#: put: Omega is a set of authorized thresholds, not of radio ids. ``costRule``
#: and ``tieBreak`` are prose the prompt renders differently run to run, and
#: neither orders anything -- cost_of is recorded but is explicitly not the
#: ranking. Comparing them refused 88 distinct digests over 134 real episodes,
#: and a guard that refuses every block is one that gets switched off.
_BINDING_ONLY = ('scope',)
_LABEL_ONLY = ('costRule', 'tieBreak')


def _canonical_authorization(authorization):
    """The pinned structure as the contract itself normalizes it, order-independent.

    Everything that shapes Omega (each requirement's op, original, bound, steps,
    levels, unit, kpi, owner, weight and deadline fields, and the joint conditions
    and mode constraints that carve combinations out) or ranks it (preference rule
    and ownerPriority) is kept; only the measurement binding and the prose labels
    are dropped. Both carvings are a conjunction, so neither is ordered: they are
    sorted here, or the same domain would digest two ways.
    """
    record = authorization.to_full_record()
    record['requirements'] = {
        req_id: {key: value for key, value in entry.items() if key not in _BINDING_ONLY}
        for req_id, entry in record['requirements'].items()}
    record['preference'] = {key: value for key, value in record['preference'].items()
                            if key not in _LABEL_ONLY}
    for field in ('jointConditions', 'modeConstraints'):
        record[field] = sorted(record[field],
                               key=lambda item: json.dumps(item, sort_keys=True))
    return record


def _differences(reference, candidate, path=''):
    """Every field on which two canonical structures disagree, named by path."""
    if isinstance(reference, Mapping) and isinstance(candidate, Mapping):
        found = []
        for key in sorted(set(reference) | set(candidate)):
            where = str(key) if not path else path + '.' + str(key)
            if key in reference and key in candidate:
                found += _differences(reference[key], candidate[key], where)
            else:
                found.append(where)
        return found
    return [] if reference == candidate else [path]


def _episode_name(episode, index):
    return episode.get('episodeId') or 'episode[' + str(index) + ']'


def require_shared_omega(episodes):
    """Refuse to compare a block whose episodes were not pinned to one Omega.

    Returns a digest of the single pinned structure: every requirement with its
    bound, op, owner, kpi, steps, unit, weight, levels and deadline fields, plus
    the joint conditions and mode constraints that carve Omega and the rule and
    ownerPriority that rank it, all normalized by the contract itself so key
    order cannot decide
    this. The measurement binding and the prose labels are deliberately not part
    of the identity; :data:`_BINDING_ONLY` says why.

    Raises on the first episode that disagrees, naming it and the fields, because
    an arm scored against a different authorization -- or ranked by a different
    preference -- is not a comparison at all. Differing domains are never
    intersected or reconciled: that would silently flatter whichever arm was
    handed the smaller one, and this project takes a loud failure over that.
    """
    rows = list(episodes)
    if not rows:
        raise ValueError('an empty block pins no authorization; Omega is undefined')
    reference = _canonical_authorization(_pinned_authorization(rows[0]))
    for index, episode in enumerate(rows[1:], start=1):
        differences = _differences(
            reference, _canonical_authorization(_pinned_authorization(episode)))
        if differences:
            raise ValueError(
                'episodes %r and %r were pinned to different authorizations, so they '
                'share no Omega and may not be compared; %s differ'
                % (_episode_name(rows[0], 0), _episode_name(episode, index),
                   ', '.join(differences)))
    return hashlib.sha256(json.dumps(reference, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _signature(requirements, deadlines):
    """A target by the values it asks for, so T and Omega match across target ids."""
    def cell(value):
        number = _finite_number(value)
        return round(number, 9) if number is not None else str(value)
    return (tuple(sorted((str(k), cell(v)) for k, v in dict(requirements or {}).items())),
            tuple(sorted((str(k), cell(v)) for k, v in dict(deadlines or {}).items())))


def _selected_signatures(episode, authorization):
    """Which Omega vectors the method carried into its T, by value and not by id."""
    originals = {rid: entry.deadline_ms for rid, entry in authorization.requirements.items()
                 if entry.has_deadline}
    selected = {}
    for tid, target in _targets(episode).items():
        deadlines = dict(originals, **{str(k): v for k, v in (target.get('deadlines') or {}).items()})
        selected[_signature(target.get('requirements'), deadlines)] = tid
    return selected


def _described(target, authorization, rank, selected):
    if target is None:
        return None
    quality = target_concession(target, authorization)
    return {'targetId': target.target_id, 'rank': rank[target.target_id],
            'preferenceKey': list(preference_key(target, authorization)),
            'requirements': dict(target.requirements), 'deadlines': dict(target.deadlines),
            'D_max': quality['max'], 'D_mean': quality['mean'], 'perOwner': quality['perOwner'],
            'selectedAs': selected.get(_signature(target.requirements, target.deadlines))}


def _omega_evaluation(episode, reference_size):
    """Each valid measured vector's best satisfied member of Omega, of T and of the
    deterministic first-`reference_size`-by-preference subset, in one ranked pass."""
    contract = omega_targets(episode)
    authorization = contract.authorization
    ranked = contract.ranked()
    selected = _selected_signatures(episode, authorization)
    reference = {_signature(t.requirements, t.deadlines) for t in ranked[:reference_size]}
    d_max = {t.target_id: target_concession(t, authorization)['max'] for t in ranked}
    rows = []
    for trial, index, elapsed in _valid_windows(episode):
        kpis = trial.get('kpis')
        judged = isinstance(kpis, Mapping)
        best = {'omega': None, 'selected': None, 'reference': None}
        attained = []
        # No early exit: qualified success needs the smallest D_max over every
        # attained member of Omega, which the preference-best need not be.
        for target in ranked if judged else ():
            if verdict_of(judge_target(kpis, target, authorization)) != PASS:
                continue
            attained.append(target)
            signature = _signature(target.requirements, target.deadlines)
            best['omega'] = best['omega'] or target
            if signature in selected:
                best['selected'] = best['selected'] or target
            if signature in reference:
                best['reference'] = best['reference'] or target
        # A window with no measured vector is unjudgeable, never a failure.
        rows.append(({'trialIndex': index, 'controlId': trial.get('controlId'),
                      'elapsedMs': 0 if index == 0 else elapsed, 'judged': judged,
                      'originalAttained': any(t.target_id == contract.t0.target_id for t in attained),
                      'minAttainedDmax': min((d_max[t.target_id] for t in attained), default=None)},
                     best))
    return {'contract': contract, 'authorization': authorization, 'ranked': ranked,
            'rank': {t.target_id: i for i, t in enumerate(ranked)},
            'selected': selected, 'reference': reference, 'rows': rows}


#: Dmax thresholds of the shared outcome evaluator (amendment v3.1 section 5).
OMEGA_DMAX_THRESHOLDS = (0, 0.5, 1)


def omega_attainment(episode, reference_size=10):
    """Score every valid measured vector against the whole of Omega, not just the prepared T.

    Counts the best target the measurement actually satisfied even when the method
    never carried it into T, so an arm is neither flattered by having selected a
    small T nor charged for what it declined to prepare. Ranking is the episode's
    own preference rule, which is the rule that method chose.
    """
    evaluation = _omega_evaluation(episode, reference_size)
    authorization, rank, selected = (evaluation['authorization'], evaluation['rank'],
                                     evaluation['selected'])
    records = []
    for row, best in evaluation['rows']:
        attained = _described(best['omega'], authorization, rank, selected)
        records.append(dict(row, attained=attained,
                            inSelectedT=bool(attained and attained['selectedAs'])))
    best = min((r for r in records if r['attained']),
               key=lambda r: (r['attained']['preferenceKey'], r['trialIndex']), default=None)
    return {'episodeId': episode.get('episodeId'), 'method': episode.get('method'),
            'omegaSize': len(evaluation['ranked']), 'selectedCount': len(selected),
            'preference': evaluation['contract'].preference.to_record(),
            'judgedRecords': sum(r['judged'] for r in records), 'records': records,
            # Section 5: success against all of Omega, whatever T the method prepared.
            'originalSuccess': any(r['originalAttained'] for r in records),
            'qualifiedSuccess': [{'a': a, 'success': any(
                r['minAttainedDmax'] is not None and r['minAttainedDmax'] <= a + 1e-12
                for r in records)} for a in OMEGA_DMAX_THRESHOLDS],
            # The active preference's best, kept apart: not necessarily the smallest D_max.
            'best': best['attained'] if best else None,
            'bestInSelectedT': bool(best and best['inSelectedT'])}


def lost_target_coverage(episode, reference_size=10):
    """How much of Omega the method's selected T never carried, and what it cost.

    `missed` is every authorized vector absent from T, with its rank and normalized
    concession: how good what was discarded actually was. Per measured record the
    best supported member of T is compared with the best in Omega and with the
    deterministic first-`reference_size`-by-preference subset, which is the cheap
    diagnostic reference on those same records. A selected row that is not a member
    of Omega is reported, never dropped.
    """
    evaluation = _omega_evaluation(episode, reference_size)
    authorization, ranked = evaluation['authorization'], evaluation['ranked']
    rank, selected = evaluation['rank'], evaluation['selected']
    missed = [_described(t, authorization, rank, selected) for t in ranked
              if _signature(t.requirements, t.deadlines) not in selected]
    authorized = {_signature(t.requirements, t.deadlines) for t in ranked}
    records = []
    for row, best in evaluation['rows']:
        in_omega = _described(best['omega'], authorization, rank, selected)
        in_t = _described(best['selected'], authorization, rank, selected)
        records.append(dict(row, bestInOmega=in_omega, bestInT=in_t,
                            bestInReference=_described(best['reference'], authorization, rank, selected),
                            lost=bool(in_omega and (in_t is None or in_t['rank'] != in_omega['rank'])),
                            rankGap=in_t['rank']-in_omega['rank'] if in_t and in_omega else None))
    return {'episodeId': episode.get('episodeId'), 'method': episode.get('method'),
            'omegaSize': len(ranked), 'selectedCount': len(selected),
            'referenceSize': min(reference_size, len(ranked)),
            'unmatchedSelected': sorted(tid for sig, tid in selected.items() if sig not in authorized),
            'missedCount': len(missed), 'bestMissedRank': missed[0]['rank'] if missed else None,
            'missed': missed, 'judgedRecords': sum(r['judged'] for r in records),
            'lostRecords': sum(r['lost'] for r in records), 'records': records}


def _included(episodes):
    return [e for e in episodes if not e.get('excluded')]


def _exclusions(episodes):
    records = [{'episodeId': e.get('episodeId'), **e['excluded']}
               for e in episodes if e.get('excluded')]
    return {'excludedCount': len(records), 'exclusions': records}


def trial_count(episode):
    return sum(t.get('counted', t.get('trialIndex') != 0) is True
               for t in episode.get('trials', []))


#: Trial fields read for exposure, first present wins. Pre-v3.1 records carry
#: only appliedAt, elapsedMs (ms since t0 at completion) and kernel.terminalState;
#: the nested/explicit names are the ones the executor is adding for v3.1.
_EXPOSURE_START = (('application', 'startedAt'), ('applicationStartedAt',), ('appliedAt',),
                   ('window', 'start'))
_EXPOSURE_ACCEPTED = (('completion', 'acceptedAt'), ('acceptance', 'at'), ('acceptedAt',))
_EXPOSURE_RESTORED = (('recovery', 'restoredAt'), ('recovery', 'confirmedAt'),
                      ('completion', 'restoredAt'), ('restoredAt',), ('restorationConfirmedAt',))
#: Every other stamp that proves the trial was still open at least that long.
_EXPOSURE_SEEN = (('recovery', 'failedAt'), ('recovery', 'timedOutAt'), ('recovery', 'requestedAt'),
                  ('completion', 'at'), ('finalObservationAt',), ('window', 'end'))
_RECOVERY_FAILED = {'FAILED', 'FAILURE', 'TIMEOUT', 'TIMED_OUT', 'UNKNOWN', 'UNRESOLVED',
                    'INCIDENT_LOCKDOWN', 'LOCKDOWN', 'RECOVERY_FAILURE'}


def _field(record, path):
    for key in path:
        record = record.get(key) if isinstance(record, Mapping) else None
    return record


def _stamp(episode, trial, paths):
    for path in paths:
        value = _field(trial, path)
        if value not in (None, ''):
            return _elapsed(episode, value), '.'.join(path)
    return None, None


def trial_exposure(episode):
    """Per-trial exposure, application start through acceptance or confirmed restoration.

    Waits, settling, observation, partial application and rollback all sit inside
    that span. A trial without a confirmed end (recovery timeout, lockdown, unknown)
    reports only ``exposureLowerBoundMs`` up to its last recorded stamp, with
    ``restorationConfirmed: false``; it is never complete. A pre-v3.1 record ends at
    ``elapsedMs``: SETTLED_SUCCESS is acceptance and SETTLED_NON_SUCCESS is the
    Kernel's recovery-verified settlement; INCIDENT_LOCKDOWN or no state is not.

    The trial-count shortage bound holds only when every trial completed; this
    reports that condition and does not implement a cumulative-shortage budget gate.
    """
    rows = []
    for trial in episode.get('trials', []):
        if trial.get('counted', trial.get('trialIndex') != 0) is not True:
            continue
        kernel = trial.get('kernel') or {}
        state = kernel.get('terminalState')
        recovery = trial.get('recovery') if isinstance(trial.get('recovery'), Mapping) else {}
        status = str(recovery.get('status') or '').upper()
        failed = (recovery.get('confirmed') is False or status in _RECOVERY_FAILED
                  or state == 'INCIDENT_LOCKDOWN')
        start, start_field = _stamp(episode, trial, _EXPOSURE_START)
        accepted_at, end_field = _stamp(episode, trial, _EXPOSURE_ACCEPTED)
        restored_at = None
        if accepted_at is None and not failed:
            restored_at, end_field = _stamp(episode, trial, _EXPOSURE_RESTORED)
        legacy_end = _finite_number(trial.get('elapsedMs'))
        if legacy_end is not None and (start is None or legacy_end < start):
            legacy_end = None  # an unset 0.0 default is no completion stamp
        if accepted_at is None and restored_at is None and not failed and legacy_end is not None:
            if state == 'SETTLED_SUCCESS':
                accepted_at, end_field = legacy_end, 'elapsedMs'
            elif state == 'SETTLED_NON_SUCCESS':
                restored_at, end_field = legacy_end, 'elapsedMs'
        end = accepted_at if accepted_at is not None else restored_at
        complete = end is not None and start is not None
        seen = [s for s in [end, legacy_end] + [_stamp(episode, trial, (p,))[0] for p in _EXPOSURE_SEEN]
                if s is not None]
        rows.append({'trialIndex': trial.get('trialIndex'), 'controlId': trial.get('controlId'),
                     'startMs': start, 'endMs': end, 'startField': start_field, 'endField': end_field,
                     'accepted': accepted_at is not None, 'restorationConfirmed': restored_at is not None,
                     'recoveryFailed': failed, 'complete': complete,
                     'exposureMs': end - start if complete else None,
                     'exposureLowerBoundMs': (max(0.0, max(seen) - start)
                                              if start is not None and seen else None),
                     'terminalState': state, 'outcome': kernel.get('outcome'),
                     'stopReason': kernel.get('stopReason')})
    # `rows` 가 비면 `all([])` 은 True 다.  그대로 두면 **계수된 시행이 하나도 없는 판**이
    # `exposureMs 0` · `completionConfirmed True` · "한계 성립" 으로 보고된다 --
    # 아무것도 증명하지 않은 판이 안전 주장의 근거 수를 채우고 방식별 평균 라디오
    # 노출 시간을 0 쪽으로 끌어내린다 (2026-09-21 실측: 판 16개).  공허참은 증거가
    # 아니므로 시행이 없으면 아무것도 확인되지 않은 것으로 적는다.
    complete = bool(rows) and all(r['complete'] for r in rows)
    return {'episodeId': episode.get('episodeId'), 'trials': rows,
            'exposureMs': sum(r['exposureMs'] for r in rows) if complete else None,
            'exposureLowerBoundMs': (sum(r['exposureLowerBoundMs'] for r in rows)
                                     if all(r['exposureLowerBoundMs'] is not None for r in rows)
                                     else None),
            'completionConfirmed': complete,
            'recoveryFailures': [r['trialIndex'] for r in rows if r['recoveryFailed']],
            'unknownCompletion': [r['trialIndex'] for r in rows
                                  if not r['complete'] and not r['recoveryFailed']],
            'trialCountShortageBound': ('conditional on bounded trial/recovery completion; '
                                        + ('holds' if complete else 'not established')
                                        + '; no cumulative-shortage budget gate is implemented')}


def _rate(n, total, excluded_count=0):
    if not total:
        return {'n': n, 'N': total, 'excludedCount': excluded_count, 'rate': None, 'wilson95': [None, None]}
    p, z = n / total, 1.959963984540054
    denom = 1 + z*z/total
    center = (p + z*z/(2*total)) / denom
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denom
    return {'n': n, 'N': total, 'excludedCount': excluded_count, 'rate': p, 'wilson95': [max(0, center-half), min(1, center+half)]}


def resolution_rate(episodes):
    rows = _included(episodes)
    return _rate(sum(bool(valid_successes(e)) for e in rows), len(rows), len(episodes)-len(rows))


def original_target_attainment(episodes):
    def rate(rows):
        excluded = len(rows) - len(_included(rows))
        rows = _included(rows)
        return _rate(sum(any(s['targetId'] == (e.get('T') or {}).get('t0', {}).get('targetId', 'T0')
                             for s in valid_successes(e)) for e in rows), len(rows), excluded)
    result = rate(episodes)
    cases = sorted({e.get('condition', {}).get('caseType') for e in episodes} - {None})
    result['byCaseType'] = {case: rate([e for e in episodes if e.get('condition', {}).get('caseType') == case])
                            for case in cases}
    return result


def _stats(values):
    values = [v for v in values if v is not None]
    avg, std = mean_std(values) if values else (None, None)
    return {'n': len(values), 'mean': avg, 'std': std, 'median': median(values) if values else None,
            'values': values}


def omega_coverage(episodes, reference_size=10):
    """Omega attainment and lost coverage, grouped by the Omega each episode pins.

    Two arms pinned to different authorizations were scored against different
    Omegas, so their figures are never pooled: each digest reports its own N, its
    own |Omega| and its own deterministic first-`reference_size`-by-preference
    reference. No cardinality is assumed -- every size is read from the record,
    so a domain that shrinks when joint conditions start carving it still reports.

    A block is one Omega, one record schema *and* one selection size. Every
    figure pooled here is a function of the selection: a schema bump marks a
    selection rule change, and `selectedCount` separates the populations a
    schema bump cannot -- two arms that narrowed the same Omega differently
    were not asked the same question, and one average over both describes
    neither. Each block names its own `omegaDigest`, `schemaVersion` and
    `selectedCount`, and none of the three is ever pooled into one row.

    Per digest: the best member of Omega any measurement actually satisfied (and
    whether the method had carried it into T at all), and, on those same records,
    how far the best supported member of T and the cheap reference subset each sat
    behind the best in Omega. Both sides are visible, so selection is asked to
    justify its cost and what it discarded.

    An episode whose pinned authorization is missing or unusable can be scored
    against no Omega. It is named in `omegaExclusions` and counted in
    `omegaExcludedCount`, which is repeated beside every digest's figures so the
    denominator is legible: such an episode is not a zero and not a pass, and one
    of them never takes down the rest of the aggregation.
    """
    blocks, excluded = defaultdict(list), []
    for index, episode in enumerate(_included(episodes)):
        try:
            digest = require_shared_omega([episode])
            attained = omega_attainment(episode, reference_size)
            lost = lost_target_coverage(episode, reference_size)
        except (ValueError, KeyError, TypeError) as error:
            excluded.append({'episodeId': _episode_name(episode, index),
                             'method': episode.get('method'), 'reason': str(error)})
            continue
        # The block identity is the Omega, the record schema and the selection
        # size. Every field pooled below is a function of the selection: a
        # schema bump marks a selection rule change, and selectedCount
        # separates the populations a schema bump cannot -- two arms that
        # narrowed the same Omega differently were not asked the same question,
        # so their figures describe neither of them once averaged together.
        # The key stays a string: by_digest is serialized into metrics.json,
        # where a tuple key would not round-trip. selectedCount sits in the
        # middle because it cannot contain '/' and the version can.
        blocks['%s/%d/%s' % (digest, attained['selectedCount'],
                             episode.get('schemaVersion') or 'unknown')].append({
            'episodeId': attained['episodeId'], 'method': attained['method'],
            'omegaSize': attained['omegaSize'], 'selectedCount': attained['selectedCount'],
            'referenceSize': lost['referenceSize'],
            'bestAttained': attained['best'], 'bestInSelectedT': attained['bestInSelectedT'],
            'originalSuccess': attained['originalSuccess'],
            'qualifiedSuccess': attained['qualifiedSuccess'],
            'judgedRecords': attained['judgedRecords'], 'lostRecords': lost['lostRecords'],
            'missedCount': lost['missedCount'], 'bestMissedRank': lost['bestMissedRank'],
            'unmatchedSelected': lost['unmatchedSelected'],
            # Per measured record, how far behind the best in Omega the method's
            # own T sat, and how far the no-LLM reference subset sat.
            'rankGaps': [r['rankGap'] for r in lost['records']],
            'referenceGaps': [r['bestInReference']['rank'] - r['bestInOmega']['rank']
                              if r['bestInReference'] and r['bestInOmega'] else None
                              for r in lost['records']]})
    by_digest = {}
    for key, rows in sorted(blocks.items()):
        # The digest is hex and the count is decimal, so neither holds a '/':
        # the first two are this function's separators and the version, which
        # is the one component that may itself contain '/', is the remainder.
        digest, selected_count, version = key.split('/', 2)
        scored = [r for r in rows if r['bestAttained']]
        best = min(scored, key=lambda r: r['bestAttained']['preferenceKey'], default=None)
        by_digest[key] = {
            'omegaDigest': digest, 'schemaVersion': version,
            'selectedCount': int(selected_count),
            'N': len(rows), 'omegaExcludedCount': len(excluded),
            'omegaSize': rows[0]['omegaSize'], 'referenceSize': rows[0]['referenceSize'],
            'episodesWithAttainment': len(scored),
            'originalSuccess': _rate(sum(r['originalSuccess'] for r in rows), len(rows)),
            'qualifiedSuccess': [dict({'a': a}, **_rate(sum(
                next(q['success'] for q in r['qualifiedSuccess'] if q['a'] == a) for r in rows),
                len(rows))) for a in OMEGA_DMAX_THRESHOLDS],
            'attainedOutsideSelectedT': sum(not r['bestInSelectedT'] for r in scored),
            'bestAttained': best['bestAttained'] if best else None,
            'bestAttainedEpisodeId': best['episodeId'] if best else None,
            'bestAttainedInSelectedT': bool(best and best['bestInSelectedT']),
            'judgedRecords': sum(r['judgedRecords'] for r in rows),
            'lostRecords': sum(r['lostRecords'] for r in rows),
            'missedCount': _stats([r['missedCount'] for r in rows]),
            'bestMissedRank': _stats([r['bestMissedRank'] for r in rows]),
            'rankGapVsOmega': _stats([g for r in rows for g in r['rankGaps']]),
            'referenceGapVsOmega': _stats([g for r in rows for g in r['referenceGaps']]),
            'unmatchedSelected': sorted({t for r in rows for t in r['unmatchedSelected']}),
            'episodes': rows}
    return {'referenceSize': reference_size, 'omegaExcludedCount': len(excluded),
            'omegaExclusions': excluded, 'digestCount': len(by_digest), 'byDigest': by_digest}


def substituted_answer(episode):
    """이 판의 어느 역할 호출이 **결정론 답으로 대체**됐나.

    대체가 일어나면 그 뒤 시행은 그 방식이 고른 것이 아니라 코드가 고른 것을 집행한다.
    그러니 그 판의 D 는 **그 방식의 양보가 아니다.**  그렇다고 판을 버려서도 안 된다 --
    대체를 부른 사유는 둘 다 방식 귀책이고(`exp_metrics.md` 5절: 방법 자체의 오류는
    결과에 포함), 2026-09-21 실측 29판의 사유는 "답이 두 번 연속 관측 유효기간을 넘겨
    도착"(26건)과 "인가되지 않은 완화 제안"(3건)이다.  그래서 **분모에는 남기고 D 에서만
    뺀다** -- 그 전에는 29판 중 8판이 그냥 성공으로 세어졌다.
    """
    for call in (episode.get('calls') or []):
        if call.get('fallbackReason'):
            return f"{call.get('role')}: {str(call['fallbackReason'])[:120]}"
    return None


def concession_summary(episodes):
    exclusions = _exclusions(episodes)
    episodes = _included(episodes)
    substituted = {id(e): substituted_answer(e) for e in episodes}
    result = {}
    for field in ('bestAttained', 'retained'):
        records = []
        for e in episodes:
            successes = valid_successes(e)
            stored = e.get(field) or {}
            if field == 'retained':
                successes = [s for s in successes if stored.get('qualified') is True
                             and s['targetId'] == stored.get('targetId')
                             and s['controlId'] == stored.get('controlId')]
            best = min(successes, key=lambda s: (
                s['cost'] if s.get('cost') is not None else 0,
                s['concession']['max'], s['concession']['mean']), default=None)
            quality = best['concession'] if best else None
            if substituted.get(id(e)):
                quality = None          # 대체된 답의 양보는 그 방식의 양보가 아니다
            cached = stored.get('concession')
            mismatch = cached is not None and (quality is None or any(
                cached.get(k) != quality.get(k) for k in ('perOwner', 'mean', 'max')))
            records.append({'episodeId': e.get('episodeId'), 'targetId': best['targetId'] if best else None,
                            'concession': quality, 'cost': best.get('cost') if best else None,
                            'levels': best.get('levels', {}) if best else {}, 'storedConcession': cached, 'storedMismatch': mismatch})
        owners = sorted({o for r in records if r['concession'] for o in r['concession']['perOwner']})
        substitutions = sorted(v for v in substituted.values() if v)
        result[field] = {'n': sum(r['concession'] is not None for r in records),
                         'N': len(episodes), **exclusions,
                         # 분모에는 남지만 D 를 내지 못하는 판.  n 과 N 의 차이를
                         # "측정 실패" 로만 읽으면 이 방식 귀책 실패가 안 보인다.
                         'substitutedAnswerCount': len(substitutions),
                         'substitutedAnswers': substitutions,
                         'records': records,
                         'D_mean': _stats([r['concession']['mean'] for r in records if r['concession']]),
                         'D_max': _stats([r['concession']['max'] for r in records if r['concession']]),
                         'D_o': {o: _stats([r['concession']['perOwner'].get(o) for r in records if r['concession']])
                                 for o in owners}}
    return result


def quality_qualified_success(episodes, thresholds_a):
    exclusions = _exclusions(episodes)
    episodes = _included(episodes)
    successes = [valid_successes(e) for e in episodes]
    curves = []
    for a in thresholds_a:
        qualified = [[s for s in ss if s['concession']['max'] <= a] for ss in successes]
        curve = {'a': a, 'N': len(episodes), **exclusions}
        for axis, cutoff in (('trialIndex', 'trialsK'), ('elapsedMs', 'deadlineBMs')):
            points = sorted({0} | {s[axis] for ss in successes for s in ss}
                            | {e.get('budget', {}).get(cutoff) or 0 for e in episodes})
            curve[axis] = [{'x': x, **_rate(sum(any(s[axis] <= x for s in ss) for ss in qualified), len(episodes), exclusions['excludedCount'])}
                           for x in points]
        curve['unresolvedFraction'] = 1 - sum(bool(ss) for ss in qualified)/len(episodes) if episodes else None
        curves.append(curve)
    return curves


def first_success_cost(episodes):
    exclusions = _exclusions(episodes)
    episodes = _included(episodes)
    rows = []
    for e in episodes:
        ss = valid_successes(e)
        first = ss[0] if ss else {}
        rows.append({'episodeId': e.get('episodeId'), 'trialIndex': first.get('trialIndex'),
                     'trialCount': trial_count(e),
                     'elapsedMs': first.get('elapsedMs'), 'bestConcession':
                     min((s['concession'] for s in ss), key=lambda c: (c['max'], c['mean']), default=None),
                     'followUpMs': _elapsed(e, e.get('timing', {}).get('end')),
                     'termination': e.get('termination', {})})
    return {'records': rows, **exclusions, 'trialCount': _stats([r['trialCount'] for r in rows]),
            'successfulOnlyTrialIndex': _stats([r['trialIndex'] for r in rows]),
            'successfulOnlyElapsedMs': _stats([r['elapsedMs'] for r in rows])}


def resource_cost(episodes):
    rows = []
    for e in episodes:
        timing, calls, cost = e.get('timing', {}), e.get('calls', []), e.get('resourceCost', {})
        prep_start, prep_end = _ms(timing.get('prepStart')), _ms(timing.get('prepEnd'))
        row = dict(cost)
        row.update(episodeId=e.get('episodeId'), timingMode=timing.get('timingMode', 'prepared'),
                   excluded=e.get('excluded'))
        row.setdefault('prepMs', prep_end-prep_start if prep_end is not None and prep_start is not None else None)
        row.setdefault('totalMs', _elapsed(e, timing.get('end')))
        row.setdefault('llmCalls', len(calls))
        for key in ('inputTokens', 'outputTokens'):
            row.setdefault(key, sum(c.get(key) or 0 for c in calls))
        row.setdefault('decisionLatenciesMs', [c.get('latencyMs') for c in calls if c.get('phase') != 'formation'])
        row['calls'] = calls  # Preserve retries, failed decisions, tools and repair provenance.
        row['reuseCount'] = cost.get('reuseCount', e.get('reuseCount'))
        row['priorPrepMs'] = cost.get('priorPrepMs', e.get('priorPrepMs'))
        row['reuseGroup'] = cost.get('reuseGroup', e.get('reuseGroup', 'recorded-sequence'))
        rows.append(row)
    result = {key: _stats([r.get(key) for r in rows])
              for key in ('prepMs', 'totalMs', 'llmCalls', 'inputTokens', 'outputTokens', 'reuseCount', 'priorPrepMs')}
    result['decisionLatenciesMs'] = _stats([v for r in rows for v in r['decisionLatenciesMs']])
    result['records'] = rows
    result.update(_exclusions(episodes))
    result['population'] = 'all started episodes, including excluded costs actually incurred'
    return result


def decision_staleness(episodes):
    """Selection-call staleness and measured recovery time, already in totalMs.

    Retries are decisions too; formation/intake are excluded. Missing v1 fields
    contribute no stale flags and no recovery windows. Never add recovery time
    again to resourceCost, whose elapsed clock already includes it.
    """
    calls = [call for episode in episodes for call in episode.get('calls', [])
             if call.get('phase') == 'selection']
    recovery = []
    for episode in episodes:
        windows = episode.get('reobservations',
                              episode.get('execution', {}).get('reobservations', []))
        for window in windows:
            start, end = _ms(window.get('observedAt')), _ms(window.get('windowEnd'))
            if start is not None and end is not None:
                recovery.append(max(0, end - start))
    stale = sum(bool(call.get('staleAtArrival')) for call in calls)
    return {'decisions': len(calls), 'staleDecisions': stale,
            'fraction': stale / len(calls) if calls else None,
            'reobservationCount': len(recovery), 'reobservationMs': sum(recovery),
            'reobservationDurationsMs': _stats(recovery),
            'timeAccounting': 'included in resourceCost.totalMs; not added again'}


def _sent_options(call):
    '''What the wire actually carried for a call, and what it did not.

    2026-09-23 decision section 4: "Treat ``sentOptions`` as authoritative. If
    ``reasoningEffort`` and ``jsonMode`` are not transmitted, report them as
    unsupported/not sent."  Grouping by ``call['options']`` -- the *request* --
    reads a campaign as using enforced JSON and high reasoning when the backend
    sent neither.  Measured: every live call records ``sentOptions.maxTokens``
    plus a ``notSent`` block naming ``jsonMode`` and ``reasoningEffort``.

    A call whose generations carry no ``sentOptions`` (an older record, or a
    call that never reached a backend) reports ``None`` rather than falling back
    to the request, which is the substitution this function exists to stop.
    '''
    for generation in (call.get('generations') or []):
        sent = generation.get('sentOptions')
        if isinstance(sent, dict):
            return sent
    return None


def generation_options_summary(episodes):
    """Per model and role: what was asked for, what was sent, and what was not."""
    groups = defaultdict(list)
    for episode in episodes:
        for call in episode.get('calls', []):
            sent = _sent_options(call)
            groups[(call.get('model'), call.get('role'),
                    json.dumps(call.get('options') or {}, sort_keys=True),
                    json.dumps(sent, sort_keys=True) if sent is not None else None)].append(call)
    rows = []
    for (model, role, requested, sent), calls in sorted(groups.items(), key=lambda p: str(p[0])):
        sent_options = json.loads(sent) if sent is not None else None
        rows.append({
            'model': model, 'role': role,
            'requestedOptions': json.loads(requested),
            'sentOptions': sent_options,
            # 표에서 바로 읽히게 따로 뽑는다 -- 이것이 "고추론·JSON 강제로 돌렸다" 는
            # 서술을 막는 줄이다.
            'notSent': (sent_options or {}).get('notSent') if sent_options else None,
            'sentOptionsUnknown': sent_options is None,
            # 결정 §4 "Record truncation": 출력 토큰이 전송된 한도에 닿은 생성 수.
            # 기록 이전의 판이거나 한쪽 수를 모르면 따로 센다 -- 0 으로 치지 않는다.
            'truncatedAtLimit': sum(1 for c in calls for g in (c.get('generations') or [])
                                    if g.get('truncatedAtLimit') is True),
            'truncationUnknown': sum(1 for c in calls for g in (c.get('generations') or [])
                                     if g.get('truncatedAtLimit') is None),
            'calls': len(calls), 'latencyMs': _stats([c.get('latencyMs') for c in calls]),
            'staleDecisions': sum(bool(c.get('staleAtArrival')) for c in calls)})
    return rows


def _deadline_counter_sample(value, original_deadline):
    """Select D0's cumulative counters; untagged legacy counters describe D0 only."""
    deadline = _finite_number(original_deadline)
    if not isinstance(value, Mapping) or deadline is None or deadline <= 0:
        return None
    source = value.get('source')
    stamp = _finite_number(value.get('observedAtMs'))
    if 'byDeadlineMs' in value:
        levels = value['byDeadlineMs']
        if not isinstance(levels, Mapping) or source is None:
            return None
        value = next((row for key, row in levels.items()
                      if _finite_number(key) == deadline), None)
    if source is not None and (not isinstance(source, Mapping) or stamp is None
            or any(source.get(key) in (None, '') for key in ('sessionId', 'flowId', 'clockId'))):
        return None
    if not isinstance(value, Mapping):
        return None
    counters = {key: _finite_number(value.get(key)) for key in ('issued', 'eligible', 'completed')}
    if any(v is None for v in counters.values()):
        return None
    if not 0 <= counters['completed'] <= counters['eligible'] <= counters['issued']:
        return None
    return counters, source, stamp


def _ratio_bin(series, start, end, interval, minimum, original_deadline):
    """Counter delta at D0, with the service metric's [start, end) slot coverage.

    The last sample at/before start is the baseline; a sample exactly at end
    closes the delta but never fills a missing coverage slot. Boundary readings
    must be within one declared cadence; their actual interval is reported rather
    than interpolated. Missing counters, resets or source changes stay unknown.
    """
    before = [(stamp, value) for stamp, value in series if stamp <= start]
    rows = before[-1:] + [(stamp, value) for stamp, value in series if start < stamp <= end]
    parsed = [(stamp, _deadline_counter_sample(value, original_deadline)) for stamp, value in rows]
    slots = {int((stamp-start) // interval) for stamp, entry in parsed
             if start <= stamp < end and entry is not None}
    coverage = min(1.0, len(slots) / math.ceil((end-start)/interval))
    bounds = {'counterStartMs': before[-1][0] if before else None,
              'counterEndMs': rows[-1][0] if len(rows) >= 2 else None}
    if (not before or len(rows) < 2 or coverage < minimum
            or start - rows[0][0] > interval + 1e-6
            or end - rows[-1][0] > interval + 1e-6
            or any(entry is None for _stamp, entry in parsed)):
        return None, coverage, bounds
    entries = [entry for _stamp, entry in parsed]
    source = entries[0][1]
    if any(entry[1] != source for entry in entries):
        return None, coverage, bounds
    for previous, current in zip(entries, entries[1:]):
        if (any(current[0][key] < previous[0][key] for key in previous[0])
                or (source is not None and current[2] <= previous[2])):
            return None, coverage, bounds
    base, last = entries[0][0], entries[-1][0]
    eligible = last['eligible'] - base['eligible']
    completed = last['completed'] - base['completed']
    if eligible <= 0 or completed > eligible:
        return None, coverage, bounds
    # Reuse the live window's delta statistic after validation. Its legacy reset
    # fallback is intentionally unreachable here, including for old flat traces.
    rule = KpiObservationRule(kpi=KPI_DEADLINE_RATIO, window_ms=end-start,
                              statistic='ratio', min_coverage=minimum)
    value, _ = rule.aggregate([(stamp, entry[0]) for stamp, entry in parsed], end, interval)
    return _finite_number(value), coverage, bounds


def _integrated_unit(unit):
    """Mbps over seconds is Mbit (amendment section 6); other units stay unit*s."""
    unit = str(unit)
    return unit[:-2] + 'it' if unit.endswith('bps') else unit + '*s'


def _merged(intervals):
    merged = []
    for start, end in intervals:
        if merged and merged[-1]['endMs'] == start:
            merged[-1]['endMs'] = end
        else:
            merged.append({'startMs': start, 'endMs': end})
    return merged


#: The run-length semantics live in the assurance package: it is on the live
#: path and may not import ``experiments`` (tests/assurance/test_seams.py), so
#: the dependency runs this way and one implementation serves both verdicts.
from assurance.coordination.intake import (  # noqa: E402
    continuity_verdict, low_delivery_runs)


def delivery_continuity(episode, kpi_key, floor, max_run_bins, bin_ms=1000,
                        horizon_ms=None):
    """The longest run of consecutive low-delivery bins, with its two bounds.

    A mean over a window says nothing about how the shortfall was distributed:
    the same mean is one long outage or a scatter of dips, and only the second
    is tolerable for sustained delivery.  This reads the same 1-s stream
    ``service_deficit`` already slots the service trace into (``bin_ms``,
    1000 ms by default) and reports the longest run of bins strictly below
    ``floor``.

    Missing bins are the reason there are two numbers rather than one.  A
    second that was never collected is not a delivered second and is not a lost
    one, so it cannot decide a run by itself:

    * ``longestRun`` counts only observed low bins and lets a missing bin break
      a run -- the largest run the record *proves*;
    * ``longestPossibleRun`` reads every missing bin as low -- the largest run
      the record *permits*.

    The verdict follows from the two: ``FAIL`` when the proven run already
    exceeds ``max_run_bins``, ``PASS`` when even the permitted run does not,
    and ``UNKNOWN`` in between, where the gap decides it.  This keeps the rule
    the redesign states -- invalid collection stays unknown and is never a
    continuity violation, while a genuine break is a failed requirement and not
    an invalid observation.

    ``floor`` is in the KPI's own unit (for a 0.5 L1 floor at L1 = 8 Mbps, pass
    4.0).  Returns ``None`` values rather than inventing a horizon when the
    episode declares none.
    """
    horizon = horizon_ms if horizon_ms is not None else (
        episode.get('budget', {}).get('horizonHMs') or 0)
    bin_ms = int(bin_ms)
    if bin_ms <= 0 or horizon < 0 or horizon % bin_ms:
        raise ValueError('horizon must be a nonnegative integer multiple of the bin length')
    count = int(horizon / bin_ms)
    if not count:
        return {'binMs': bin_ms, 'floor': floor, 'maxRunBins': max_run_bins,
                'longestRun': None, 'longestPossibleRun': None,
                'verdict': 'UNKNOWN', 'observedBins': 0, 'missingIntervals': [],
                'runs': []}
    # One state per bin: True low, False delivered, None never collected.  A
    # later sample of the same bin replaces an earlier one, which is what the
    # deficit slots already do.
    state = [None] * count
    for point in episode.get('serviceTrace', []):
        elapsed = _service_elapsed(episode, point)
        value = point.get('kpis', {}).get(kpi_key)
        if elapsed is None or not 0 <= elapsed < horizon:
            continue
        if value is None or not point.get('valid', True):
            continue
        state[int(elapsed // bin_ms)] = float(value) < float(floor)

    proven, permitted, found = low_delivery_runs(
        [None if item is None else (0.0 if item else float(floor) + 1.0) for item in state],
        floor)
    runs = [{'startMs': row['startBin'] * bin_ms, 'bins': row['bins']} for row in found]
    verdict = continuity_verdict(proven, permitted, max_run_bins)
    missing = _merged([(index * bin_ms, (index + 1) * bin_ms)
                       for index, item in enumerate(state) if item is None])
    return {'binMs': bin_ms, 'floor': floor, 'maxRunBins': max_run_bins,
            'longestRun': proven, 'longestPossibleRun': permitted,
            'verdict': verdict, 'observedBins': sum(1 for i in state if i is not None),
            'missingIntervals': missing, 'runs': runs}


def service_deficit(episode, bin_delta_ms=None):
    """Service shortage over [t0, t0+H) against the ORIGINAL requirement.

    H is ``budget.horizonHMs``; without it nothing is invented and every total is
    None. Bins (``binDeltaMs``) carry the declared per-KPI statistic, coverage and
    verdict as before. The shortage integral itself uses the finest stream in the
    record: each sample interval (``sampleIntervalMs``, default 1 s) for sampled
    KPIs, each bin's counter-delta cohort for ratios. A mean over an interval gives
    the exact integral at that resolution and only a lower bound on finer-time
    positive shortage.

    ``shortageLowerBound`` charges missing intervals zero; ``shortageUpperBound``
    charges each missing interval of length Delta original*Delta (floors only; a
    ceiling has no finite worst case). ``missingIntervals`` names them.
    ``cumulativeDeficit`` (= ``fullHorizonShortage``) is reported only when no part
    of the horizon is missing: a partly missing stream is unavailable, never a
    single number with the gap filled by zero. Units: Mbps integrates to Mbit.
    Categorical predicates report violation seconds with no deficit.
    """
    budget = episode.get('budget', {})
    delta = bin_delta_ms or budget.get('binDeltaMs', 5000)
    horizon = budget.get('horizonHMs') or 0
    if delta <= 0 or horizon < 0 or horizon % delta:
        raise ValueError('horizon must be a nonnegative integer multiple of bin length')
    rules = episode.get('measurementRules', {})
    reducers = {'mean': fmean, 'min': min, 'max': max, 'median': median,
                'last': lambda values: values[-1]}
    count = int(horizon / delta)
    result = {}
    for rid, req in _requirements(episode).items():
        rule = rules.get(req['kpi'], rules)
        interval = rule.get('sampleIntervalMs', rules.get('sampleIntervalMs', 1000))
        minimum = rule.get('minCoverage', rule.get('minValidCoverage', 1.0))
        statistic = rule.get('statistic', 'mean')
        ratio = statistic == 'ratio'
        numeric = req['op'] in ('>=', '<=')
        if ((statistic not in reducers and not ratio) or interval <= 0
                or (ratio and not numeric)):
            raise ValueError('unsupported service statistic or sample interval')
        key = req['kpi'] + '@' + req['scope'].split('@', 1)[-1]
        samples = [dict() for _ in range(count)]
        series = []
        for point in episode.get('serviceTrace', []):
            elapsed = _service_elapsed(episode, point)
            value = point.get('kpis', {}).get(key)
            if ratio:
                if elapsed is not None and elapsed <= horizon:
                    series.append((elapsed, value if point.get('valid', True) else None))
            elif elapsed is not None and 0 <= elapsed < horizon and value is not None and point.get('valid', True):
                samples[int(elapsed // delta)][int((elapsed % delta) // interval)] = value
        series.sort(key=lambda item: item[0])
        original_deadline = (episode.get('T') or {}).get('authorization', {}).get(rid, {}).get(
            'deadlineMs', req.get('deadlineMs'))

        def short(value):
            return max(0, req['value']-value if req['op'] == '>=' else value-req['value'])

        bins, deficit, violation, covered = [], 0.0, 0.0, 0
        missing, worst = [], 0.0
        for index, slots in enumerate(samples):
            value, shortage, failing = None, None, None
            counter_bounds = {}
            start = index*delta
            if ratio:
                value, coverage, counter_bounds = _ratio_bin(
                    series, start, start+delta, interval, minimum, original_deadline)
                valid = value is not None
                # A valid counter delta covers the whole bin; an invalid one none of it.
                pieces = [] if valid else [(start, start+delta)]
            else:
                coverage = min(1.0, len(slots) / math.ceil(delta/interval))
                valid = bool(slots) and coverage >= minimum
                pieces = [(start + slot*interval, start + min((slot+1)*interval, delta))
                          for slot in range(math.ceil(delta/interval)) if slot not in slots]
                if numeric:
                    deficit += sum(short(v) * (min((s+1)*interval, delta) - s*interval)/1000
                                   for s, v in slots.items())
            missing += pieces
            worst += sum(end - begin for begin, end in pieces)/1000
            if valid:
                covered += 1
                if not ratio:
                    vals = list(slots.values())
                    value = reducers[statistic](vals) if numeric else vals[-1]
                if numeric:
                    shortage = short(value)
                    failing = shortage > 0
                    if ratio:
                        deficit += shortage * delta/1000
                else:
                    failing = (value != req['value'] if statistic == 'last'
                               else any(v != req['value'] for v in vals))
                violation += delta/1000 if failing else 0
            bins.append({'startMs': start, 'endMs': start+delta, 'coverage': coverage,
                         'valid': valid, 'value': value, 'shortage': shortage, 'violation': failing,
                         **counter_bounds})
        complete = bool(count) and not missing
        full = deficit if numeric and complete else None
        result[rid] = {'owner': req['owner'], 'unit': req['unit'],
                       'deficitUnit': _integrated_unit(req['unit']) if numeric else None,
                       'horizonMs': horizon or None,
                       'shortageResolutionMs': (delta if ratio else min(interval, delta)) if numeric else None,
                       'fullHorizonShortage': full, 'cumulativeDeficit': full,
                       'shortageLowerBound': deficit if numeric and count else None,
                       'shortageUpperBound': (deficit + req['value'] * worst
                                              if numeric and count and req['op'] == '>=' else None),
                       'missingIntervals': _merged(missing),
                       'violationDurationSeconds': violation if covered else None,
                       'coverage': fmean(b['coverage'] for b in bins) if bins else 0,
                       'validBins': covered, 'totalBins': count, 'incomplete': not complete,
                       'bins': bins}
    return result


def hardware_disconnects(episodes):
    """Footnote counts: UE disconnects the testbed hardware caused, excluded from judgement.

    The service they interrupted is already missing, never zero (``service_deficit``);
    this only says how often and for how long, and whether a control preceded it.
    """
    events = [(e.get('episodeId'), item) for e in episodes
              for item in (e.get('hardwareDisconnects') or ())]
    waited = [float(item['waitedMs']) for _, item in events
              if isinstance(item.get('waitedMs'), (int, float))]
    # Composition can wait for a UE before the sitting exists, so that time is in no
    # disconnect record at all -- attempt 129 waited 64 s for ue3 and its episode still
    # listed no disconnects.  The footnote needs the whole cost, so both are summed.
    composition = [float(((e.get('hardwareWaitMs') or {}).get('compositionMs')) or 0.0)
                   for e in episodes]
    return {'episodes': len({episode for episode, _ in events}), 'count': len(events),
            'compositionWaitMsTotal': sum(composition),
            'episodesWithCompositionWait': sum(1 for ms in composition if ms > 0),
            'reregistered': sum(1 for _, item in events if item.get('reregistered') is True),
            'notReregistered': sum(1 for _, item in events if item.get('reregistered') is False),
            'waitedMsTotal': sum(waited) if waited else 0.0,
            'afterTrial': sum(1 for _, item in events if item.get('trials')),
            'events': [{'episodeId': episode, 'ues': sorted(item.get('ues') or ()),
                        'detectedAt': item.get('detectedAt'), 'waitedMs': item.get('waitedMs'),
                        'reregistered': item.get('reregistered'),
                        'trialStates': [t.get('terminalState') for t in item.get('trials') or ()]}
                       for episode, item in events]}


def walkover_summary(episodes):
    """에이전트가 개입하기 **전에** 이미 최선이었던 판이 몇 개인가.

    2026-09-21 실측: 양보값이 기록된 181판 중 **90판(50%)** 이 `bestAttained.trialIndex == 0`
    -- 초기 측정이 최선이고, 8분과 LLM 호출 8회가 그것을 넘지 못했다.  그 수가 지표 어디에도
    없어 해석할 수 없었다.

    `t0Success` 는 **여기서 세지 않는다.**  처음엔 부전승 지표로 넣었는데 그 필드의 뜻은
    "트라이얼 0 에서 성공" 이 아니라 **"원 목표 T0 를 어느 시행에서든 달성했다"** 이고
    (`tools/liveconsole/agent.py:3801`), 실측으로 `bestAttained.concession.max == 0` 과
    정확히 일치한다(321판 중 6판, 예외 0).  그것은 이미 `original_target_attainment()` 이
    보고하므로, 같은 것을 다른 출처로 한 번 더 세면 두 수가 언젠가 갈린다.

    여기서 아무것도 빼거나 고치지 않는다 -- 부전승도 결과이고, 그것을 제외할지는 실험
    설계의 몫이다.  다만 **보이지 않으면 해석할 수 없으므로** 세어서 내보낸다.
    """
    exclusions = _exclusions(episodes)
    rows = _included(episodes)
    best_at_t0 = sum(1 for e in rows
                     if isinstance(e.get('bestAttained'), dict)
                     and e['bestAttained'].get('trialIndex') == 0)
    measured = sum(1 for e in rows if isinstance(e.get('bestAttained'), dict))
    return {'N': len(rows), **exclusions,
            #: 양보값이 기록된 판 중에서만 센다 -- 분모가 다르므로 아래와 섞지 말 것.
            'bestAtTrial0': _rate(best_at_t0, measured, exclusions['excludedCount']),
            'concessionMeasured': _rate(measured, len(rows), exclusions['excludedCount'])}


def counting_policy(episode):
    '''Which counting policies the episode actually ran under (2026-09-23).

    ``formalReferenceTrial`` charges the reference observation to ``N_max`` and
    gives it a real attainment time; ``retainOnImprovement`` leaves a qualifying
    configuration applied instead of resetting to C0.  Both change *what is
    counted*, so episodes that differ in them are different experiments and must
    not be summed (codex audit #8).  A record written before the keys existed
    ran with both off, which is what ``False`` says.
    '''
    return (bool(episode.get('formalReferenceTrial', False)),
            bool(episode.get('retainOnImprovement', False)))


def _quality(rows):
    '''Section 5.3, or the reason it could not be computed for this cohort.'''
    from experiments.agent_quality import quality_summary
    try:
        summary = quality_summary(rows)
    except ValueError as exc:            # no pinned authorization: Omega is undefined
        return {'unavailable': str(exc)}
    summary.pop('episodes', None)        # per-episode rows are for the report, not the summary
    return summary


def campaign_of(episode):
    '''The campaign an episode belongs to, as the episode itself records it.

    ``condition.name`` is the corpus name, shared by every campaign run on that
    corpus; two campaigns under the same counting policy would otherwise pool
    (2026-09-23: the partial ``blocks18-v46`` and the restarted
    ``blocks18-v46r``).  An episode written before the key existed says ``''``.
    '''
    campaign = str((episode.get('condition') or {}).get('campaign') or '')
    # 같은 캠페인 안에서 라이브 경로 코드가 바뀌면(sourceDigest) 다른 실험이다 -- 합치지 않고
    # 판본 앞 12자로 가른다.  캠페인 중 동결 규칙을 어기면 결과에서 저절로 두 줄로 드러난다
    # (2026-09-23: 하루에 판본이 네 번 바뀌었다).  해시가 없는 옛 판은 캠페인 이름만.
    digest = str(episode.get('sourceDigest') or '')[:12]
    return f'{campaign}@{digest}' if campaign and digest else campaign


def summarize(episodes, *, thresholds_a=(0, 0.25, 0.5, 1.0), reference_size=10):
    episodes = list(episodes)
    grouped = defaultdict(list)
    for e in episodes:
        grouped[(e.get('condition', {}).get('name', 'unknown'), e['method'],
                 e.get('timing', {}).get('timingMode', 'prepared'),
                 e.get('condition', {}).get('ablation', 'ordinary'),
                 counting_policy(e), campaign_of(e))].append(e)
    arms = defaultdict(list)
    groups = []
    for (condition, method, mode, ablation, policy, campaign), rows in sorted(grouped.items()):
        arms[(method, mode, ablation, policy, campaign)].extend(rows)
        included = _included(rows)
        groups.append({'condition': condition, 'conditionDetails': rows[0].get('condition', {}),
                       'method': method, 'timingMode': mode, 'ablation': ablation,
                       'formalReferenceTrial': policy[0], 'retainOnImprovement': policy[1],
                       'campaign': campaign,
                       # 결정 §5.3 (2026-09-23): 초기상태 4분류 · T_q(q=0,1,71) ·
                       # mean(min(T_q,B)) · 시행 효율 · 최종 선호.  별도 모듈이다 --
                       # 여기 해시 감시가 지키는 기존 통계를 건드리지 않으려고.
                       'quality': _quality(rows),
                       'N': len(included), **_exclusions(rows),
                       'resolution': resolution_rate(rows), 'originalTarget': original_target_attainment(rows),
                       'concession': concession_summary(rows), 'qualityQualified': quality_qualified_success(rows, thresholds_a),
                       'decisionStaleness': decision_staleness(included),
                       'generationOptions': generation_options_summary(included),
                       'omega': omega_coverage(rows, reference_size),
                       'firstSuccess': first_success_cost(rows), 'resourceCost': resource_cost(rows),
                       'walkover': walkover_summary(rows),
                       'serviceDeficit': [{'episodeId': e.get('episodeId'), 'requirements': service_deficit(e)} for e in included],
                       # All started episodes: an excluded one still exposed the radio.
                       'trialExposure': [trial_exposure(e) for e in rows],
                       'hardwareDisconnects': hardware_disconnects(rows)})
    overall = []
    # `overall` 도 셈 정책을 가른다 -- 그룹만 갈라 놓고 여기서 다시 합치면 기준 시행의
    # 비용과 유지 정책이 다른 판이 한 수로 섞인다 (codex 감사 #8).
    _key = lambda g: (g['method'], g['timingMode'], g['ablation'],
                      (g['formalReferenceTrial'], g['retainOnImprovement']), g['campaign'])
    for method, mode, ablation, policy, campaign in sorted({_key(g) for g in groups}):
        gs = [g for g in groups if _key(g) == (method, mode, ablation, policy, campaign)]
        excluded_count = sum(g['excludedCount'] for g in gs)
        def average(values):
            values = [v for v in values if v is not None]
            return fmean(values) if values else None
        def equal_stats(stats):
            available = [s for s in stats if s['mean'] is not None]
            return {'mean': fmean(s['mean'] for s in available) if available else None,
                    'n': sum(s['n'] for s in stats), 'conditionsWithData': len(available),
                    'conditionCount': len(gs)}
        concession = {}
        for field in ('bestAttained', 'retained'):
            cs = [g['concession'][field] for g in gs]
            owners = sorted({o for c in cs for o in c['D_o']})
            concession[field] = {'n': sum(c['n'] for c in cs),
                'D_mean': equal_stats([c['D_mean'] for c in cs]),
                'D_max': equal_stats([c['D_max'] for c in cs]),
                'D_o': {o: equal_stats([c['D_o'].get(o, _stats([])) for c in cs]) for o in owners}}
        curves = []
        for a in thresholds_a:
            source = [next(c for c in g['qualityQualified'] if c['a'] == a) for g in gs]
            curve = {'a': a, 'excludedCount': excluded_count,
                     'unresolvedFraction': average(c['unresolvedFraction'] for c in source)}
            for axis in ('trialIndex', 'elapsedMs'):
                points = sorted({p['x'] for c in source for p in c[axis]})
                curve[axis] = [{'x': x, 'excludedCount': excluded_count, 'rate': average(
                    max((p for p in c[axis] if p['x'] <= x), key=lambda p: p['x'])['rate']
                    for c in source)} for x in points]
            curves.append(curve)
        overall.append({'method': method, 'timingMode': mode, 'ablation': ablation,
                        'formalReferenceTrial': policy[0], 'retainOnImprovement': policy[1],
                        'campaign': campaign,
                        'excludedCount': excluded_count, 'conditionCount': len(gs),
                        'N': sum(g['N'] for g in gs), 'weighting': 'equal-condition',
                        'resolutionRate': average(g['resolution']['rate'] for g in gs),
                        'originalTargetRate': average(g['originalTarget']['rate'] for g in gs),
                        'concession': concession, 'qualityQualified': curves,
                        'omega': omega_coverage(arms[(method, mode, ablation, policy, campaign)], reference_size),
                        'resourceCost': {key: equal_stats([g['resourceCost'][key] for g in gs])
                            for key in ('prepMs', 'totalMs', 'llmCalls', 'inputTokens', 'outputTokens',
                                        'decisionLatenciesMs', 'reuseCount', 'priorPrepMs')},
                        'firstSuccess': {key: equal_stats([g['firstSuccess'][key] for g in gs])
                            for key in ('successfulOnlyTrialIndex', 'successfulOnlyElapsedMs')}})
    return {'schemaVersion': 'agent-metrics/1.0.0', 'thresholdsA': list(thresholds_a), **_exclusions(episodes), 'groups': groups, 'overall': overall}


def paired_block_bootstrap(episodes, metric, method_a, method_b, n=2000, seed=0):
    """A minus B; sample matched blocks within each condition, equal condition weight.

    metric is an episode->number callable or resolution/originalTarget/D_max.
    Null metric pairs are reported as unmatched; timing modes require separate calls.
    The compared arms must share one pinned Omega: a mixed block raises rather than
    returning a number.
    """
    exclusions = _exclusions(episodes)
    episodes = _included(episodes)
    if len({e.get('condition', {}).get('ablation', 'ordinary') for e in episodes}) > 1:
        raise ValueError('bootstrap each ablation separately')
    modes = {e.get('timing', {}).get('timingMode', 'prepared') for e in episodes}
    if len(modes) > 1:
        raise ValueError('bootstrap each timing mode separately')
    # One board per comparison. Episodes pinned to different authorizations rank
    # targets differently, so differencing two arms across them compares two
    # boards while reporting it as one method result -- the number that would
    # reach a paper. Refuse instead, naming the episode and the field; a block
    # naming neither method is simply empty, not a mixed board.
    compared = [e for e in episodes if e.get('method') in (method_a, method_b)]
    if compared:
        require_shared_omega(compared)
    def value(e):
        ss = valid_successes(e)
        if callable(metric):
            return metric(e)
        if metric == 'resolution':
            return float(bool(ss))
        if metric == 'originalTarget':
            return float(any(s['targetId'] == e.get('T', {}).get('t0', {}).get('targetId', 'T0') for s in ss))
        if metric == 'D_max':
            return min((s['concession']['max'] for s in ss), default=None)
        raise ValueError('unknown bootstrap metric: ' + str(metric))
    pairs = defaultdict(dict)
    for e in episodes:
        if e['method'] in (method_a, method_b):
            key = (e.get('condition', {}).get('name', 'unknown'), e.get('block', 0), (e.get('repetition', 0), e.get('attempt', 0)))
            pairs[key][e['method']] = value(e)
    conditions = defaultdict(lambda: defaultdict(list))
    matched = 0
    for (condition, block, _), pair in pairs.items():
        if pair.get(method_a) is not None and pair.get(method_b) is not None:
            conditions[condition][block].append(pair[method_a]-pair[method_b])
            matched += 1
    blocks = [[fmean(v) for v in b.values()] for b in conditions.values()]
    if not blocks:
        return {'difference': None, **exclusions, 'ci95': [None, None], 'matchedPairs': 0, 'unmatchedPairs': len(pairs)}
    if n <= 0:
        raise ValueError('bootstrap repetitions must be positive')
    rng = random.Random(seed)
    draws = sorted(fmean(fmean(rng.choices(b, k=len(b))) for b in blocks) for _ in range(n))
    def percentile(p):
        pos = (len(draws)-1)*p
        lo = int(pos)
        return draws[lo] + (draws[min(lo+1, len(draws)-1)]-draws[lo])*(pos-lo)
    return {'difference': fmean(fmean(b) for b in blocks), **exclusions, 'ci95': [percentile(.025), percentile(.975)],
            'matchedPairs': matched, 'unmatchedPairs': len(pairs)-matched, 'n': n, 'seed': seed,
            'methodA': method_a, 'methodB': method_b, 'weighting': 'equal-condition, paired-block'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('episodes_dir')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summarize(load_episodes(args.episodes_dir)), indent=2, allow_nan=False)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
