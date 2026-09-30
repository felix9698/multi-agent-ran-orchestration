#!/usr/bin/env python3
"""Run matched agent episodes and regenerate the paper metrics and figures."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.agent_episodes import CONDITIONS, SCENARIOS, EXCLUSION_RULES, parse_models, run_matrix
from assurance.coordination.agents import METHODS
from tools.campaign5.calibrate_agent_latency import calibrate
from tools.liveconsole.agent import ClarificationNeeded
from tools.liveconsole import LiveConsoleError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', default='three-ue', help='comma-separated scenarios, or all')
    parser.add_argument('--condition', default='contention-boundary', help='comma-separated conditions, or all')
    parser.add_argument('--methods', default='three-agent,internal-monolith,basic-monolith')
    parser.add_argument('--models', '--model', default='mock:deterministic')
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--budget', type=int, default=16)
    parser.add_argument('--ablation', choices=('full', 'trajectory'), default='full')
    parser.add_argument('--exclusion-rule', choices=tuple(EXCLUSION_RULES))
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--load-scale', type=float, default=1.0)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--answers', type=Path, help='JSON intake answers keyed by intent ID')
    parser.add_argument('--settings', type=Path, help='JSON sitting settings shared by all methods')
    parser.add_argument('--observe', type=Path, help='JSON per-KPI observation rules')
    parser.add_argument('--generation', type=Path, help='JSON generation options keyed by prompt role')
    parser.add_argument('--unselected-rule', choices=('baseline', 'keep-current'))
    parser.add_argument('--retain', type=int, help='number of control candidates')
    parser.add_argument('--calibration', type=Path, help='existing latency calibration table')
    parser.add_argument('--calibrate', action='store_true', help='calibrate named models before the matrix')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--axes', help='comma-separated axis kinds, or all; default: scenario exposure')
    parser.add_argument('--max-catalog', type=int, help='stated catalog ceiling (executor default 4096)')
    for kind in ('cap', 'pf', 'mcs', 'atten', 'slice'):
        parser.add_argument(f'--{kind}-axis', action='append', help='scope:value,value ladder override')
    args = parser.parse_args(argv)
    if args.live and (args.profile is None or not args.profile.is_file()):
        parser.error('--live requires an existing --profile file')
    if args.profile is not None and not args.live:
        parser.error('--profile requires --live')
    def names(value, available):
        selected = list(available) if value == 'all' else [s.strip() for s in value.split(',')]
        if not selected or set(selected) - set(available):
            parser.error(f'choose from {", ".join(available)}')
        return selected
    scenarios = names(args.scenario, SCENARIOS)
    conditions = names(args.condition, CONDITIONS)
    methods = names(args.methods, METHODS)
    try:
        exposure = {}
        if args.axes is not None:
            exposure['axes'] = tuple(part.strip() for part in args.axes.split(','))
        if args.max_catalog is not None:
            exposure['max_catalog_cardinality'] = args.max_catalog
        for flag, field, convert in (
                ('cap', 'caps', int), ('pf', 'pf_weights', float),
                ('mcs', 'mcs_bounds', str), ('atten', 'tx_attenuations', str),
                ('slice', 'slice_quotas', str)):
            entries = getattr(args, flag + '_axis')
            if entries:
                exposure[field] = {}
                for entry in entries:
                    scope, separator, values = entry.partition(':')
                    if not separator or not scope.strip() or not values.strip():
                        raise ValueError(f'--{flag}-axis requires scope:value,value')
                    exposure[field][scope.strip()] = tuple(convert(v.strip()) for v in values.split(','))
        models = parse_models(args.models)
        settings = json.loads(args.settings.read_text()) if args.settings else {}
        for field, path in (('observation', args.observe), ('generation', args.generation)):
            if path:
                settings[field] = json.loads(path.read_text())
        if args.unselected_rule is not None:
            settings['unselectedFunctionRule'] = args.unselected_rule
        if args.retain is not None:
            settings['retain'] = args.retain
        calibration = args.calibration
        if args.calibrate:
            named = list(dict.fromkeys(model for model in models.values()
                                      if model and model != 'deterministic'))
            calibration = calibrate(named, ('1000', '4000'), 3, args.out)
        records = run_matrix(scenarios, conditions, methods, args.repetitions, models,
                             args.budget, args.seed, args.out,
                             live_profile=args.profile if args.live else None,
                             load_scale=args.load_scale, settings=settings,
                             answers=args.answers, calibration=calibration, axis_exposure=exposure,
                             ablation=args.ablation, exclusion_rule=args.exclusion_rule)
    except (ValueError, OSError, ClarificationNeeded, LiveConsoleError) as exc:
        parser.error(str(exc))
    print(f'Wrote {len(records)} episodes, metrics.json, manifest.json and figures to {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
