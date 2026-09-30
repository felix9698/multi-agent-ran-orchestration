"""Matched experiment blocks over the existing T/C sitting composition roots."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Mapping
from uuid import uuid4

from assurance.coordination.agents import METHOD_BASIC_MONOLITH, METHODS, SYSTEM_PROMPTS
from assurance.core.addressing import content_hash
from experiments import agent_figures, agent_metrics
from tools.hfconsole.agent_env import CONDITIONS, EmulatedRan
from tools.liveconsole.agent import (AgentRequest, build_agent_sitting,
                                    build_hardware_free_agent_sitting,
                                    write_agent_evidence)

ROLES = ('target', 'control', 'trajectory', 'monolith')
ABLATIONS = {'full': 'full-construction', 'trajectory': 'trajectory-only'}


def _external_failure(record, kind):
    # Explicit attribution only: an ERROR or missing samples is not evidence
    # of an external failure. Method-caused errors always remain outcomes.
    for failure in record.get('failures', []):
        if (failure.get('kind') == kind and failure.get('cause') == 'external'
                and failure.get('methodCaused') is not True):
            return str(failure.get('reason') or kind)
    return None


EXCLUSION_RULES = {
    name: (lambda record, kind=name: _external_failure(record, kind))
    for name in ('external-equipment-failure', 'logging-failure')}


def _hashes(record):
    return {key: content_hash(record[key]) for key in ('T', 'C')}


def _intent(i, owner, ue, priority, value, limit, op='>=', weight=None):
    steps = 2 if limit is not None and limit != value else 0
    return {'weight': weight, 'intentId': i, 'owner': owner, 'ueId': ue, 'priority': priority,
            'sentence': f'{owner}: UE {ue} DL goodput {op} {value} Mbps, relaxable in {steps} steps to {limit if steps else value}',
            'requirement': {'reqId': f'{i}.r1', 'kpi': 'dlGoodputMbps',
                            'scope': f'ue@{ue}', 'op': op, 'value': value,
                            'unit': 'Mbps', 'steps': steps,
                            'bound': limit if steps else value}}


@dataclass(frozen=True)
class Scenario:
    name: str
    intents: tuple[Mapping[str, Any], ...]
    ues: Mapping[str, str]
    cells: Mapping[str, float]
    unsupported_requirements: tuple[Mapping[str, Any], ...] = ()
    caps: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    condition_intents: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)

    axis_exposure: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        def weighted(intents):
            rows = deepcopy(tuple(intents))
            highest = max((i.get('priority', 1) for i in rows), default=1)
            for intent in rows:
                if intent.get('weight') is None:
                    intent['weight'] = float(1 + highest - intent.get('priority', 1))
            return rows
        object.__setattr__(self, 'intents', weighted(self.intents))
        object.__setattr__(self, 'condition_intents', {
            condition: weighted(intents) for condition, intents in self.condition_intents.items()})

    def to_record(self):
        return {'name': self.name, 'intents': list(self.intents),
                'initialServingCells': dict(self.ues), 'cellCapacityMbps': dict(self.cells),
                'caps': dict(self.caps), 'axisExposure': dict(self.axis_exposure), 'conditionIntents': dict(self.condition_intents),
                'unsupportedRequirements': list(self.unsupported_requirements)}


SCENARIOS = {
    'three-ue': Scenario(
        'three-ue', (
            _intent('I1', 'ue1-video', '131', 1, 3.0, 2.0),
            _intent('I2', 'ue2-map', '132', 2, 3.0, 2.0),
            _intent('I3', 'ue3-incumbent', '133', 3, 3.0, 2.5)),
        {'131': '12345678', '132': '12345678', '133': '87654321'},
        {'12345678': 5.0, '87654321': 5.0},
        ({'intentId': 'I4', 'owner': 'ue1-command', 'ueId': '131',
          'requirementKind': 'deadline-success', 'supported': False,
          'reason': 'Tagged echo deadline/reliability observer is outside this build'},),
        {'132': (6, 12)}, condition_intents={
            'light-load': (
                _intent('I1', 'ue1-video', '131', 1, 1.2, 1.0),
                _intent('I2', 'ue2-map', '132', 2, 1.2, 1.0),
                _intent('I3', 'ue3-incumbent', '133', 3, 1.2, 1.1)),
            'relaxable-multiservice': (
                _intent('I1', 'ue1-video', '131', 1, 3.5, 2.0),
                _intent('I2', 'ue2-map', '132', 2, 3.5, 2.0),
                _intent('I3', 'ue3-incumbent', '133', 3, 3.5, 2.5))}),
    'two-ue': Scenario(
        'two-ue', (
            _intent('I1', 'ue1-priority', '131', 1, 3.0, 2.0),
            _intent('I2', 'ue2-service', '132', 2, 1.5, 1.0),
            _intent('I3', 'fairness-policy', '132', 3, 1.0, 1.5, '<=')),
        {'131': '12345678', '132': '12345678'},
        {'12345678': 5.0, '87654321': 5.0}, caps={'132': (6, 12)}),
}


# Deliberately bounded ladders for matched scenarios: every UE still has cap/PF.
for _name, _scenario in tuple(SCENARIOS.items()):
    SCENARIOS[_name] = replace(_scenario, caps={ue: (0, 12) for ue in _scenario.ues},
        axis_exposure={"axes": ["servingCell", "dlPrbCap", "pfWeight"],
                       "pf_weights": {ue: (1.0, 2.0) for ue in _scenario.ues}})
_base = SCENARIOS['two-ue']
SCENARIOS['wide-axes'] = replace(_base, name='wide-axes', axis_exposure={
    **_base.axis_exposure, "axes": ["all"],
    "mcs_bounds": {cell: ("0..28", "0..16") for cell in _base.cells},
    "tx_attenuations": {cell: ("0.0", "6.0") for cell in _base.cells},
    "slice_quotas": {"1": ("0:1:100", "0:1:60")}})


# A headless intake exercise: I2 deliberately lacks relaxation authorization.
_incomplete = deepcopy(SCENARIOS['two-ue'].intents)
_incomplete[1]['requirement'].pop('steps')
_incomplete[1]['requirement'].pop('bound')
_incomplete[1]['sentence'] = 'ue2-service: UE 132 DL goodput >= 1.5 Mbps'
SCENARIOS['intake-incomplete'] = Scenario(
    'intake-incomplete', _incomplete, SCENARIOS['two-ue'].ues,
    SCENARIOS['two-ue'].cells, caps=SCENARIOS['two-ue'].caps,
    axis_exposure=SCENARIOS['two-ue'].axis_exposure)


def block_settings(budget, settings=None, calibration=None):
    """One shared set of executor settings for every method in a block."""
    result = {'trialsK': budget, 'deadlineMs': 600000, 'horizonMs': 900000,
              'stopAfterRelaxedSuccess': False, 'retention': 'bestAttained',
              'unselectedFunctionRule': 'baseline', 'generation': {},
              'observation': {
                  'dlGoodputMbps': {'settleMs': 1500, 'windowMs': 1500,
                      'statistic': 'mean', 'minCoverage': 0.5, 'validityMs': 60000},
                  'servingCell': {'settleMs': 1500, 'windowMs': 1500,
                      'statistic': 'last', 'minCoverage': 0.5, 'validityMs': 10000}}}
    observation = result['observation']
    result.update(deepcopy(settings or {}))
    result['observation'] = {**observation, **result['observation']}
    if calibration is not None:
        result['latencyCalibrationPath'] = str(Path(calibration).resolve())
    return result


def _answers(value):
    if isinstance(value, (str, Path)):
        return json.loads(Path(value).read_text(encoding='utf-8'))
    return deepcopy(value or {})


def parse_models(value='mock:deterministic'):
    """One backend for every role, or comma-separated role=backend pairs."""
    if isinstance(value, Mapping):
        result = dict(value)
    elif '=' not in value:
        result = {role: value.strip() or None for role in ROLES}
    else:
        result = {}
        for pair in value.split(','):
            role, separator, model = pair.partition('=')
            if not separator:
                raise ValueError('models must be a name or role=name pairs')
            result[role.strip()] = model.strip() or None
    if set(result) - set(ROLES):
        raise ValueError('unknown model role: ' + ', '.join(sorted(set(result) - set(ROLES))))
    return {role: result.get(role) for role in ROLES}


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _write(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def run_block(scenario, condition, methods, repetition, models, budget, seed, out_dir,
              *, live_profile=None, load_scale=1.0, block=None, settings=None, answers=None,
              calibration=None, axis_exposure=None, ablation='full',
              exclusion_rule=None, attempt=0):
    """Return records in randomized execution order, sharing one initial-state seed.

    LIVE delegates deployment and observation to T4; workload replay and initial
    lab state must already be supplied by the live profile/operator.
    """
    if ablation not in ABLATIONS:
        raise ValueError('ablation must be full or trajectory')
    if ablation == 'trajectory':
        # exp_metrics.md figure 5 compares "trajectory decisions with
        # byte-identical T/C".  A method that has no T and no C by definition
        # -- the basic monolith works without a grid (section 1) -- cannot hold
        # one identical, and it records the configurations it chose as its own
        # C, which is why an injected pair does not survive it.  Excluding it
        # here is the comparison's own boundary, stated rather than discovered
        # as a hash mismatch.
        # It also must not simply be handed the pair: SINGLE_CALL.md's boundary
        # rules forbid giving the basic monolith our T, C, grid or rankings at
        # all, so injecting a prepared pair into it would break the comparison
        # in the other direction too.
        without_grid = [name for name in methods if name == METHOD_BASIC_MONOLITH]
        methods = [name for name in methods if name not in without_grid]
        if not methods:
            raise ValueError('the trajectory ablation needs a method that consumes a '
                             'prepared T and C; the basic monolith has neither')
    if exclusion_rule is not None and exclusion_rule not in EXCLUSION_RULES:
        raise ValueError('unknown predeclared exclusion rule: ' + exclusion_rule)
    scenario = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    if condition not in CONDITIONS:
        raise ValueError(f'unknown condition: {condition}')
    if not math.isfinite(load_scale) or load_scale < 0:
        raise ValueError('load scale must be finite and nonnegative')
    order = list(methods)
    if not order or len(set(order)) != len(order) or set(order) - set(METHODS):
        raise ValueError('methods must contain distinct supported method names')
    models = parse_models(models)
    random.Random(seed).shuffle(order)
    block = repetition if block is None else block
    load = {ue: CONDITIONS[condition] * load_scale for ue in scenario.ues}
    settings = block_settings(budget, settings, calibration)
    answers = _answers(answers)
    exposure = {"caps": dict(scenario.caps), **deepcopy(scenario.axis_exposure)}
    for key, value in deepcopy(axis_exposure or {}).items():
        exposure[key] = {**exposure.get(key, {}), **value} if isinstance(value, Mapping) else value
    records = []
    prepared = None
    prepared_hashes = None
    preparation = None
    reuse_group = f'{scenario.name}/{condition}/b{block}/r{repetition}/attempt{attempt}/{uuid4().hex}'

    def compose(request, episode_id, pair=None):
        kwargs = {'prepared': deepcopy(pair)} if pair is not None else {}
        if live_profile is None:
            ran = EmulatedRan(ues=scenario.ues, cells=scenario.cells,
                              offered_load_mbps=load, seed=seed)
            return build_hardware_free_agent_sitting(
                request, ran=ran, seed=seed, condition=condition,
                tmp_dir=Path(out_dir) / 'runtime' / episode_id, stamp=episode_id, **kwargs)
        return build_agent_sitting(live_profile, request, stamp=episode_id, **kwargs)

    for position, method in enumerate(order):
        episode_id = f'{scenario.name}-{condition}-b{block}-r{repetition}-{method}-{uuid4().hex[:12]}'
        request = AgentRequest(
            intents=scenario.condition_intents.get(condition, scenario.intents),
            method=method, role_models=models, settings=deepcopy(settings),
            answers=deepcopy(answers),
            budget_trials=budget, deadline_ms=600000, horizon_ms=900000, **exposure,
            cells=tuple(int(cell) for cell in scenario.cells),
            condition={'name': f'{scenario.name}/{condition}', 'operatingCondition': condition,
                       'scenario': scenario.name, 'offeredLoadMbps': load,
                       'loadScale': load_scale, 'ablation': ABLATIONS[ablation]},
            block=block, repetition=repetition, episode_id=episode_id)
        if ablation == 'trajectory' and prepared is None:
            source = compose(replace(request, method='three-agent'), episode_id + '-preparation')
            prepared = deepcopy((source.contract, source.controls))
            prepared_hashes = _hashes({'T': prepared[0].to_record(), 'C': prepared[1].to_record()})
            preparation = {'method': 'three-agent', 'timing': dict(source.timing),
                           'resourceCost': source.resource_cost(),
                           'calls': [call.to_record() for call in source.agents.calls]}
        sitting = compose(request, episode_id, prepared)
        sitting.confirm()
        sitting.run()
        if prepared_hashes is not None and _hashes(sitting.episode().to_record()) != prepared_hashes:
            raise ValueError('trajectory-only sitting changed the injected T/C')
        evidence = write_agent_evidence(sitting)
        record = json.loads(Path(evidence['episode']).read_text(encoding='utf-8'))
        record.update(schemaVersion='agent-episode/1.3.0', scenario=scenario.name, seed=seed,
                      attempt=attempt, exclusionRule=exclusion_rule, methodOrder=order,
                      methodPosition=position, sittingSettings=deepcopy(settings),
                      axisExposure=deepcopy(exposure),
                      unsupportedRequirements=list(scenario.unsupported_requirements),
                      evidence=evidence)
        record.setdefault('condition', {}).update(ablation=ABLATIONS[ablation])
        if 'T' in record and 'C' in record:
            record['tcHashes'] = _hashes(record)
        if preparation is not None:
            record['preparation'] = deepcopy(preparation)
            record.setdefault('resourceCost', {}).update(
                reuseCount=position + 1, reuseGroup=reuse_group,
                priorPrepMs=preparation['resourceCost']['prepMs'])
        if exclusion_rule:
            reason = EXCLUSION_RULES[exclusion_rule](record)
            if reason:
                record['excluded'] = {'rule': exclusion_rule, 'reason': reason, 'at': _now()}
        if ablation == 'trajectory' and without_grid:
            record.setdefault('condition', {})['methodsWithoutGrid'] = list(without_grid)
        records.append(record)
    if ablation == 'trajectory':
        if any(record.get('tcHashes') != prepared_hashes for record in records):
            raise ValueError('trajectory-only requires byte-identical prepared T/C in every episode')
    # Validate the complete matched block before publishing its episode files.
    for record in records:
        _write(Path(out_dir) / 'episodes' / f"{record['episodeId']}.json", record)
    return records


def run_matrix(scenarios=('three-ue',), conditions=('contention-boundary',),
               methods=('three-agent', 'internal-monolith', 'basic-monolith'),
               repetitions=3, models='mock:deterministic', budget=8, seed=0,
               out_dir='agentexp', *, live_profile=None, load_scale=1.0,
               settings=None, answers=None, calibration=None, axis_exposure=None,
               ablation='full', exclusion_rule=None):
    """Run the Cartesian matrix and aggregate only this invocation's episodes."""
    if ablation not in ABLATIONS:
        raise ValueError('ablation must be full or trajectory')
    if exclusion_rule is not None and exclusion_rule not in EXCLUSION_RULES:
        raise ValueError('unknown predeclared exclusion rule: ' + exclusion_rule)
    if repetitions < 1:
        raise ValueError('repetitions must be positive')
    scenarios = [scenarios] if isinstance(scenarios, (str, Scenario)) else list(scenarios)
    conditions = [conditions] if isinstance(conditions, str) else list(conditions)
    methods = list(methods)
    models = parse_models(models)
    settings = block_settings(budget, settings, calibration)
    answers = _answers(answers)
    manifest = {'schemaVersion': 'agent-experiment-manifest/1.2.0',
                'ablation': ABLATIONS[ablation], 'exclusionRule': exclusion_rule,
                'exclusionProtocol': {'maxBlockRepeats': 1, 'failureCause': 'external',
                    'failureKinds': list(EXCLUSION_RULES), 'methodCausedExcluded': False},
                'settings': settings, 'answers': answers, 'axisExposureOverrides': deepcopy(axis_exposure or {}),
                'startedAt': _now(), 'sessionMode': 'LIVE' if live_profile is not None else 'MOCK',
                'models': models, 'methods': methods, 'seed': seed,
                'budgetTrials': settings['trialsK'], 'deadlineBMs': settings['deadlineMs'],
                'horizonHMs': settings['horizonMs'],
                'binDeltaMs': 5000, 'qualityThresholdsA': [0, 0.25, 0.5, 1],
                'repetitions': repetitions, 'loadScale': load_scale,
                'conditions': conditions,
                'scenarios': [(SCENARIOS[s] if isinstance(s, str) else s).to_record()
                              for s in scenarios],
                'promptVersions': {role: hashlib.sha256(prompt.encode()).hexdigest()
                                   for role, prompt in SYSTEM_PROMPTS.items()},
                'promptSource': 'orc_task/SINGLE_CALL.md', 'promptRevised': '2026-09-07', 'blocks': []}
    out = Path(out_dir)
    _write(out / 'manifest.json', manifest)
    records = []
    for scenario in scenarios:
        for condition in conditions:
            for repetition in range(repetitions):
                block = len(manifest['blocks'])
                block_seed = seed + block
                for attempt in range(2):
                    try:
                        rows = run_block(scenario, condition, methods, repetition, models,
                                         budget, block_seed, out, live_profile=live_profile,
                                         load_scale=load_scale, block=block, settings=settings, answers=answers,
                                         axis_exposure=axis_exposure, ablation=ablation,
                                         exclusion_rule=exclusion_rule, attempt=attempt)
                    except Exception as error:
                        # `run_block` 은 **완전한 매칭 블록만** 공개한다 -- 중간에 예외가 나면
                        # 이미 완주한 판의 episode 파일도 쓰이지 않는다.  그 설계는 옳지만,
                        # 그렇게 사라진 블록이 **어디에도 남지 않아 분모를 알 수 없었다**:
                        # 2026-09-21 실측으로 보드 593개 중 episode 기록은 322개(54%)뿐인데
                        # 리포트는 N=322 를 모집단이라고 적었다.  불안정해서 죽은 판이
                        # 체계적으로 빠지므로 모든 비율이 낙관 쪽으로 치우친다.
                        # 판을 공개하지는 않되 **시도했다는 사실은 남긴다.**
                        manifest['blocks'].append({
                            'block': block, 'attempt': attempt, 'seed': block_seed,
                            'settings': deepcopy(settings), 'methodOrder': list(methods),
                            'episodeIds': [], 'excludedEpisodeIds': [], 'repeatRequired': False,
                            'aborted': {'error': f'{type(error).__name__}: {error}'[:400],
                                        'at': _now()}})
                        _write(out / 'manifest.json', manifest)
                        raise
                    records.extend(rows)
                    excluded = [row['episodeId'] for row in rows if row.get('excluded')]
                    manifest['blocks'].append({'block': block, 'attempt': attempt,
                        'seed': block_seed, 'settings': deepcopy(settings),
                        'methodOrder': [row['method'] for row in rows],
                        'episodeIds': [row['episodeId'] for row in rows],
                        'excludedEpisodeIds': excluded, 'repeatRequired': bool(excluded) and attempt == 0})
                    _write(out / 'manifest.json', manifest)
                    if not excluded:
                        break
    summary = agent_metrics.summarize(records)
    _write(out / 'metrics.json', summary)
    manifest['figures'] = agent_figures.render_all(records, out / 'figures')
    manifest['endedAt'] = _now()
    manifest['episodeCount'] = len(records)
    manifest['excludedCount'] = sum(bool(row.get('excluded')) for row in records)
    _write(out / 'manifest.json', manifest)
    return records
