#!/usr/bin/env python3
"""Form T and C once, off the radio, so only connection and Trajectory stay live.

Target needs the intents and the owner's authorization. Control needs T, the
function catalog, the compatibility rules, the network state, the predictor's
effect evidence and the construction policy. None of that needs a UE attached:
with three UEs the only thing an attach decides is which numeric amfUeNgapId each
host holds, and prepare_tc.rekey substitutes those at submission.

The inputs are taken from a completed episode so they are the same shapes the
live composition builds, and the evidence table is built at the same limit the
live path uses (the catalog cardinality), so the board is formed under the
conditions it will be used in.

    python3 form_board_offline.py <episode.json> <out.json>
"""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

def route_to_the_proxy():
    """Validate inherited configuration (historical helper name retained)."""
    from atomic_formal_run_guarded import llm_configuration_preflight

    model = os.environ.get('AIC_ROLE_MODEL') or 'claude-sonnet'
    llm_configuration_preflight(model=model)
    return model

from assurance.coordination.agents import (          # noqa: E402
    ControlInputs, RoleAgents, RoleModels, TargetInputs)
from assurance.coordination.tc import (               # noqa: E402
    Authorization, CompatibilityRules, FunctionCatalog, Intent)
from assurance.coordination.predictor import (        # noqa: E402
    JointEffectPredictor, NetworkState)


def board(episode, model):
    intents = tuple(Intent.from_record(item) for item in episode['intents'])
    authorization = Authorization.from_record(episode['T']['authorization'])
    catalog = FunctionCatalog.from_record(episode['C']['functionCatalog'])
    space = {k: tuple(v) for k, v in (episode['C']['actionSpace'] or {}).items()}
    policy = dict(episode['C']['constructionPolicy'] or {})

    ues = {ue: dict(entry) for ue, entry in
           (episode['T'].get('provenance', {}).get('ues') or {}).items()}
    if not ues:                       # rebuild it from the axis keys and the baseline
        ues = {axis.split('@', 1)[1]: {} for axis in space if axis.startswith('servingCell@')}
    baseline = {axis: values[0] for axis, values in space.items()}
    cells = sorted({v for axis, values in space.items()
                    if axis.startswith('servingCell@') for v in values})
    state = NetworkState.from_record({
        'ues': {ue: {'servingCell': baseline.get('servingCell@' + ue, cells[0])}
                for ue in ues},
        'cells': {c: {'capacityMbps': 3.0} for c in cells},
        'appliedConfiguration': baseline,
        'prbTotal': 38,
        'unselectedFunctionRule': policy.get('unselectedFunctionRule', 'baseline')})
    predictor = JointEffectPredictor(state)
    # the live path predicts the whole product, not a sample of it
    limit = int(policy.get('catalogCardinality') or 216)
    evidence = predictor.effect_evidence(space, baseline, (), limit=limit)

    agents = RoleAgents(models=RoleModels(target=model, control=model,
                                          trajectory=model),
                        predictor=predictor)
    contract, target_call = agents.form_targets(TargetInputs(
        intents=intents, authorization=authorization))
    controls, control_call = agents.form_controls(ControlInputs(
        target_contract=contract, function_catalog=catalog,
        compatibility=CompatibilityRules.from_record(
            episode['C'].get('compatibility') or {}),
        network_state=state.to_record() if hasattr(state, 'to_record') else {},
        effect_evidence=evidence, construction_policy=policy))
    return contract, controls, target_call, control_call, space


def main(argv):
    if len(argv) != 2:
        raise SystemExit(__doc__)
    episode = json.loads(Path(argv[0]).read_text(encoding='utf-8'))
    model = route_to_the_proxy()
    contract, controls, tcall, ccall, space = board(episode, model)
    for name, call in (('Target', tcall), ('Control', ccall)):
        if not getattr(call, 'accepted', False):
            raise SystemExit(
                f'{name} was refused and the deterministic rule formed that half of the '
                f'board, which defeats the point of forming it with the model: '
                f'{getattr(call, "fallback_reason", "no reason recorded")}')

    t_record, c_record = contract.to_record(), controls.to_record()
    refused = []
    for candidate in c_record.get('candidates') or []:
        for axis, value in (candidate.get('configuration') or {}).items():
            if axis not in space:
                refused.append(f"{candidate.get('controlId')}: no axis {axis}")
            elif str(value) not in tuple(str(x) for x in space[axis]):
                refused.append(f"{candidate.get('controlId')}: {axis}={value} not admitted")
    # The episode record does not carry ueHosts -- that lives in the runner's
    # profile.json -- so derive it from the intents, whose owner names say which
    # host each ue id belongs to. Without it prepare_tc has nothing to swap and
    # the injected board keeps the ids it was formed with.
    owner_host = {'ue1-video': 'ue1', 'ue1-command': 'ue1',
                  'ue2-map': 'ue2', 'ue3-incumbent': 'ue3'}
    ue_hosts = {}
    for intent in episode['intents']:
        host = owner_host.get(intent.get('owner'))
        if host:
            ue_hosts[str(intent['ueId'])] = host
    if sorted(set(ue_hosts.values())) != ['ue1', 'ue2', 'ue3']:
        raise SystemExit(f'could not tell which host each ue id belongs to: {ue_hosts}')

    document = {
        'T': t_record, 'C': c_record,
        'ueHosts': ue_hosts,
        'formedOffline': {
            'from': episode.get('episodeId'), 'model': model,
            'targetMs': getattr(tcall, 'latency_ms', None),
            'controlMs': getattr(ccall, 'latency_ms', None),
            'targetAccepted': getattr(tcall, 'accepted', None),
            'controlAccepted': getattr(ccall, 'accepted', None),
            'inadmissible': refused}}
    Path(argv[1]).write_text(json.dumps(document, indent=1), encoding='utf-8')
    print(json.dumps({
        'wrote': argv[1],
        'targetAlternatives': len(t_record.get('alternatives') or []),
        'controlCandidates': len(c_record.get('candidates') or []),
        'targetAccepted': getattr(tcall, 'accepted', None),
        'controlAccepted': getattr(ccall, 'accepted', None),
        'targetMs': round(getattr(tcall, 'latency_ms', 0) or 0),
        'controlMs': round(getattr(ccall, 'latency_ms', 0) or 0),
        'inadmissible': refused}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
