"""Figures 1–5 for agent episodes; matplotlib is an optional, lazy dependency."""
from pathlib import Path
import csv
import itertools
import json

from experiments import agent_metrics as metrics


COLORS = ('#1f77b4', '#2ca02c', '#9467bd', '#8c564b', '#17becf', '#7f7f7f')


def _figure(*args, **kwargs):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    with plt.rc_context({'font.weight': 'normal', 'axes.titleweight': 'normal',
                         'axes.labelweight': 'normal', 'text.color': 'black'}):
        return plt.subplots(*args, **kwargs)


def _save(fig, out):
    from matplotlib.text import Text
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    # Includes legends, ticks and titles even when a caller has custom rcParams.
    for artist in fig.findobj(Text):
        artist.set_color('black')
        artist.set_fontweight('normal')
    fig.savefig(path, dpi=140)
    return fig


def _label(group):
    return f"{group['condition']} / {group['method']} / {group['timingMode']} / {group.get('ablation', 'ordinary')}"


def _condition_label(group):
    details = group.get('conditionDetails', {})
    lines = [group['condition'], group['method'], group['timingMode'], group.get('ablation', 'ordinary')]
    for key, unit in (('offeredLoadMbps', 'Mbps'), ('attenuationDb', 'dB')):
        if key in details:
            value = details[key]
            values = ', '.join(f'{k}:{v}' for k, v in value.items()) if isinstance(value, dict) else str(value)
            lines.append(f'{values} {unit}')
    return '\n'.join(lines)


def _plottable(sample, requirement):
    """A deadline KPI arrives as cumulative counters per deadline level; plot its ratio."""
    if not isinstance(sample, dict):
        return sample
    counted = metrics._deadline_counter_sample(sample, requirement.get('deadlineMs'))
    if counted is None:
        return None
    counters = counted[0]
    return counters['completed'] / counters['eligible'] if counters['eligible'] else None


def figure_trajectory(episode, out):
    requirements = metrics._requirements(episode)
    fig, axes = _figure(len(requirements)+1, 1, sharex=True,
                        figsize=(10, 2.3*(len(requirements)+1)), squeeze=False)
    axes = axes[:, 0]
    targets = metrics._targets(episode)
    for ax, (rid, req) in zip(axes, requirements.items()):
        key = req['kpi']+'@'+req['scope'].split('@', 1)[-1]
        points = [(metrics._elapsed(episode, s['t'])/1000, _plottable(s['kpis'][key], req))
                  for s in episode.get('serviceTrace', []) if key in s.get('kpis', {})]
        points = [p for p in points if p[1] is not None]
        # Categorical cell IDs use a stable local ordinal scale, not numeric interpolation.
        categorical = req['op'] == '=='
        values = list(dict.fromkeys([req['value']] + [p[1] for p in points] +
                                    [t['requirements'][rid] for t in targets.values()])) if categorical else []
        encode = (lambda v: values.index(v)) if categorical else (lambda v: v)
        if points:
            ax.plot([p[0] for p in points], [encode(p[1]) for p in points], color=COLORS[0], label='Observed')
        ax.axhline(encode(req['value']), linestyle='--', color='black', label='Original target')
        for trial in episode.get('trials', []):
            target = targets.get(trial.get('proposedTargetId'))
            start = metrics._elapsed(episode, trial.get('appliedAt'))
            end = metrics._elapsed(episode, trial.get('window', {}).get('end'))
            if target and start is not None and end is not None:
                ax.plot([start/1000, end/1000], [encode(target['requirements'][rid])]*2,
                        color=COLORS[1], linewidth=2, label='Active target' if not any(l.get_label() == 'Active target' for l in ax.lines) else None)
        if categorical:
            ax.set_yticks(range(len(values)), [str(v) for v in values])
        ax.set_ylabel(f"{req['owner']}\n{req['kpi']} ({req['unit']})")
        ax.legend(fontsize=7, loc='best')
    ax = axes[-1]
    for trial in episode.get('trials', []):
        elapsed = metrics._elapsed(episode, trial.get('appliedAt'))
        end = metrics._elapsed(episode, trial.get('window', {}).get('end'))
        if elapsed is not None:
            for panel in axes:
                panel.axvline(elapsed/1000, color='#7f7f7f', linestyle=':', alpha=.6)
            config = ', '.join(f'{k}={v}' for k, v in trial.get('configuration', {}).items())
            ax.annotate(f"{trial['controlId']}: {config}", (elapsed/1000, .65), fontsize=7, rotation=15)
        if end is not None:
            valid = trial.get('window', {}).get('valid', False)
            outcome = 'UNKNOWN' if not valid else ('PASS' if any(trial.get('success', {}).values()) else 'FAIL')
            ax.scatter([end/1000], [.2], color=COLORS[0], marker='o' if valid else 'x')
            ax.annotate(outcome, (end/1000, .22), fontsize=7)
    ax.set_ylim(0, 1.2)
    ax.set_yticks([])
    ax.set_ylabel('Controls / outcomes')
    ax.set_xlabel('Elapsed from t0 (s)')
    axes[0].set_title(f"OTA trajectory: {episode.get('episodeId', '')}")
    return _save(fig, out)


def figure_operating_conditions(summary, out):
    groups = summary['groups']
    fig, axes = _figure(2, 1, figsize=(11, 8), sharex=True)
    owners = sorted({owner for g in groups for owner in g['concession']['bestAttained']['D_o']})
    for index, g in enumerate(groups):
        r = g['resolution']
        if r['rate'] is not None:
            axes[0].errorbar(index, r['rate'], yerr=[[r['rate']-r['wilson95'][0]], [r['wilson95'][1]-r['rate']]],
                             fmt='o', color=COLORS[0])
            axes[0].annotate(f"{r['n']}/{r['N']}; excluded={r.get('excludedCount', 0)}", (index, r['rate']), xytext=(3, 5), textcoords='offset points', fontsize=8)
        for oi, owner in enumerate(owners):
            stats = g['concession']['bestAttained']['D_o'].get(owner)
            if stats and stats['mean'] is not None:
                x = index + .6 * ((oi+.5)/len(owners)-.5)
                axes[1].scatter(x, stats['mean'], color=COLORS[oi % len(COLORS)],
                                label=owner if not any(owner == label for label in axes[1].get_legend_handles_labels()[1]) else None)
                axes[1].annotate(f"n={stats['n']}", (x, stats['mean']),
                                 xytext=(0, 7), textcoords='offset points', ha='center', fontsize=7)
    axes[0].set_ylabel('Resolution rate (95% Wilson)')
    axes[0].set_ylim(-.05, 1.15)
    axes[1].set_ylabel('Best attained owner concession')
    axes[1].set_ylim(-.05, 1.1)
    if owners:
        axes[1].legend(fontsize=8)
    axes[1].set_xticks(range(len(groups)), [_condition_label(g) for g in groups], fontsize=7)
    axes[0].set_title('Operating conditions (conditions varied separately)')
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['condition', 'method', 'timingMode', 'episodeId', 'requirement', 'owner',
                         'deficit', 'deficitUnit', 'violationSeconds', 'coverage', 'incomplete'])
        for g in groups:
            for episode in g['serviceDeficit']:
                for rid, deficit in episode['requirements'].items():
                    writer.writerow([g['condition'], g['method'], g['timingMode'], episode['episodeId'], rid,
                                     deficit['owner'], deficit['cumulativeDeficit'], deficit['deficitUnit'],
                                     deficit['violationDurationSeconds'], deficit['coverage'], deficit['incomplete']])
    return _save(fig, out)


def figure_search_efficiency(summary, out):
    fig, axes = _figure(1, 2, figsize=(13, 5))
    for g, color in zip(summary['groups'], itertools.cycle(COLORS)):
        for curve, style in zip(g['qualityQualified'], itertools.cycle(('-', '--', ':', '-.'))):
            unresolved = curve['unresolvedFraction']
            label = f"{_label(g)}, a={curve['a']}, unresolved={unresolved:.0%}" if unresolved is not None else _label(g)
            label += f"; excluded={g.get('excludedCount', 0)}"
            for ax, axis in zip(axes, ('trialIndex', 'elapsedMs')):
                points = curve[axis]
                ax.step([p['x'] for p in points], [p['rate'] for p in points], where='post',
                        color=color, linestyle=style, label=label)
    for ax, label in zip(axes, ('Trial count', 'Elapsed from t0 (ms)')):
        ax.set_xlabel(label)
        ax.set_ylabel('Quality-qualified fraction of all episodes')
        ax.set_ylim(-.02, 1.05)
    axes[1].legend(fontsize=5, loc='best')
    return _save(fig, out)


def figure_timing_and_reuse(summary, out):
    fig, axes = _figure(2, 2, figsize=(12, 8))
    groups = summary['groups']
    for i, g in enumerate(groups):
        rows = g['resourceCost']['records']
        for ax, key in ((axes[0, 0], 'prepMs'), (axes[0, 1], 'decisionLatenciesMs')):
            values = g['resourceCost'][key]['values']
            if values:
                ax.boxplot([values], positions=[i], widths=.5,
                           medianprops={'color': 'black'}, flierprops={'markeredgecolor': 'black'})
        if g['timingMode'] == 'cold-start':
            axes[1, 0].scatter([i]*len(rows), [r['totalMs'] for r in rows], color=COLORS[i % len(COLORS)])
    # A shared T/C group can span methods; accumulate the whole actual sequence.
    rows = [r for g in groups for r in g['resourceCost']['records']]
    reuse_groups = sorted({r['reuseGroup'] for r in rows if r.get('reuseCount') is not None})
    for i, reuse_group in enumerate(reuse_groups):
        reuse = sorted([r for r in rows if r.get('reuseCount') is not None
                        and r['reuseGroup'] == reuse_group], key=lambda r: r['reuseCount'])
        totals, cumulative = [], reuse[0].get('priorPrepMs') or 0
        for r in reuse:
            cumulative += ((r.get('prepMs') or 0) + (r.get('totalMs') or 0)
                           if r['timingMode'] == 'prepared' else (r.get('totalMs') or 0))
            totals.append(cumulative)
        axes[1, 1].plot([r['reuseCount'] for r in reuse], totals,
                        label=reuse_group, color=COLORS[i % len(COLORS)])
    for ax, title in zip(axes.flat, ('Preparation wall time', 'Online decision latency', 'Cold-start elapsed cost', 'Cumulative cost over actual T/C reuse')):
        ax.set_title(title)
        ax.set_ylabel('ms')
    for ax in (axes[0, 0], axes[0, 1], axes[1, 0]):
        ax.set_xticks(range(len(groups)), [_label(g) for g in groups], rotation=25, ha='right', fontsize=6)
    if not any(g['timingMode'] == 'cold-start' for g in groups):
        axes[1, 0].text(.5, .5, 'No cold-start episodes recorded', transform=axes[1, 0].transAxes, ha='center')
    if not axes[1, 1].lines:
        axes[1, 1].text(.5, .5, 'No actual reuse counts recorded', transform=axes[1, 1].transAxes, ha='center')
    else:
        axes[1, 1].legend(fontsize=6)
    axes[1, 1].set_xlabel('Recorded reuse count')
    return _save(fig, out)


def figure_role_ablation(summary_full, summary_traj, out):
    """Caller supplies separate full-construction and identical-T/C experiments."""
    fig, axes = _figure(1, 2, figsize=(12, 5))
    for ax, summary, title in zip(axes, (summary_full, summary_traj),
                                  ('Full construction: equal source inputs', 'Trajectory only: byte-identical T/C')):
        groups = summary.get('groups', [])
        for i, g in enumerate(groups):
            if g['resolution']['rate'] is None:
                ax.annotate(f"NA; excluded={g.get('excludedCount', 0)}", (i, 0), fontsize=7)
                continue
            ax.bar(i, g['resolution']['rate'], color=COLORS[i % len(COLORS)])
            quality = g['concession']['bestAttained']['D_max']
            text = f"n={g['resolution']['n']}/{g['N']}; excluded={g.get('excludedCount', 0)}; Dmax=" + (f"{quality['mean']:.2f}" if quality['mean'] is not None else 'NA')
            ax.annotate(text, (i, g['resolution']['rate']), fontsize=7, rotation=10)
        ax.set_title(title)
        ax.set_ylim(0, 1.3)
        ax.set_ylabel('Resolution rate')
        ax.set_xticks(range(len(groups)), [_label(g) for g in groups], rotation=25, ha='right', fontsize=6)
        if not groups:
            ax.text(.5, .5, 'No ablation episodes recorded', transform=ax.transAxes, ha='center')
    return _save(fig, out)


def omega_coverage_table(summary, out):
    """Per-method Omega attainment and lost coverage, one row per pinned Omega.

    Digests are never pooled into one row, and every row repeats the exclusion
    count so a reader sees the denominator the figures beside it were computed on.
    A row is one Omega, one record schema and one selection size: every pooled
    figure here is a function of the selection, so `schemaVersion` and
    `selectedCount` together name which population the row describes, and two
    populations over the same Omega are two rows, never one.
    """
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['method', 'timingMode', 'ablation', 'omegaDigest',
                         'schemaVersion', 'selectedCount', 'N',
                         'omegaExcluded', 'omegaSize', 'referenceSize',
                         'bestAttainedRank', 'bestAttainedRequirements',
                         'bestAttainedInSelectedT', 'episodesWithAttainment',
                         'attainedOutsideSelectedT', 'judgedRecords', 'lostRecords',
                         'meanRankGapTvsOmega', 'meanRankGapReferenceVsOmega',
                         'meanMissedCount'])
        for arm in summary.get('overall', []):
            omega = arm.get('omega', {})
            for _key, block in sorted(omega.get('byDigest', {}).items()):
                best = block['bestAttained']
                writer.writerow([
                    arm['method'], arm['timingMode'], arm['ablation'],
                    block['omegaDigest'][:12], block['schemaVersion'],
                    block['selectedCount'],
                    block['N'], block['omegaExcludedCount'], block['omegaSize'],
                    block['referenceSize'], best['rank'] if best else '',
                    json.dumps(best['requirements'], sort_keys=True) if best else '',
                    block['bestAttainedInSelectedT'], block['episodesWithAttainment'],
                    block['attainedOutsideSelectedT'], block['judgedRecords'],
                    block['lostRecords'], block['rankGapVsOmega']['mean'],
                    block['referenceGapVsOmega']['mean'], block['missedCount']['mean']])
    return path


def render_all(episodes_dir, out_dir):
    """Return paths; ordinary episodes do not masquerade as trajectory ablations.

    Explicit condition.ablation labels identify full-construction and trajectory-only
    identical-T/C runs. Their pairing is the experiment runner's responsibility.
    """
    episodes = metrics.load_episodes(episodes_dir) if isinstance(episodes_dir, (str, Path)) else list(episodes_dir)
    if not episodes:
        raise ValueError('no episode records to plot')
    out = Path(out_dir)
    summary = metrics.summarize(episodes)
    full = metrics.summarize([e for e in episodes if e.get('condition', {}).get('ablation') == 'full-construction'])
    trajectory = metrics.summarize([e for e in episodes if e.get('condition', {}).get('ablation') == 'trajectory-only'])
    omega_coverage_table(summary, out / 'omega-coverage.csv')
    jobs = [('figure1-trajectory.png', figure_trajectory, (episodes[0],)),
            ('figure2-operating-conditions.png', figure_operating_conditions, (summary,)),
            ('figure3-search-efficiency.png', figure_search_efficiency, (summary,)),
            ('figure4-timing-and-reuse.png', figure_timing_and_reuse, (summary,)),
            ('figure5-role-ablation.png', figure_role_ablation, (full, trajectory))]
    import matplotlib.pyplot as plt
    paths = []
    for name, function, args in jobs:
        fig = function(*args, out/name)
        plt.close(fig)
        paths.append(str(out/name))
    return paths
