"""Render all required figures and inspect the actual text artists."""
import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest

from experiments import agent_figures as figures
from experiments import agent_metrics as metrics

FIXTURES = Path(__file__).parent / 'fixtures' / 'agent_episodes'


@unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'matplotlib is optional')
class AgentFiguresTests(unittest.TestCase):
    def test_five_figures_and_style(self):
        from matplotlib.text import Text
        from matplotlib.colors import to_rgba
        import matplotlib.pyplot as plt
        episodes = metrics.load_episodes(FIXTURES)
        summary = metrics.summarize(episodes)
        jobs = [(figures.figure_trajectory, (episodes[0],)),
                (figures.figure_operating_conditions, (summary,)),
                (figures.figure_search_efficiency, (summary,)),
                (figures.figure_timing_and_reuse, (summary,)),
                (figures.figure_role_ablation, (summary, summary))]
        with tempfile.TemporaryDirectory() as tmp:
            for i, (function, args) in enumerate(jobs, 1):
                path = Path(tmp) / f'figure{i}.png'
                fig = function(*args, path)
                self.assertGreater(path.stat().st_size, 1000)
                for text in fig.findobj(Text):
                    self.assertEqual(text.get_fontweight(), 'normal')
                    self.assertEqual(to_rgba(text.get_color()), to_rgba('black'))
                plt.close(fig)
            self.assertIn('deficitUnit', (Path(tmp) / 'figure2.csv').read_text())

    def test_reuse_cost_uses_actual_counts_and_cold_time_once(self):
        import copy
        import matplotlib.pyplot as plt
        rows = metrics.load_episodes(FIXTURES)[:1]
        rows.append(copy.deepcopy(rows[0]))
        for count, row in enumerate(rows):
            row['timing']['timingMode'] = 'cold-start'
            row['resourceCost'].update(reuseCount=count, reuseGroup='shared-TC', priorPrepMs=200)
        with tempfile.TemporaryDirectory() as tmp:
            fig = figures.figure_timing_and_reuse(metrics.summarize(rows), Path(tmp)/'reuse.png')
            self.assertEqual(list(fig.axes[3].lines[0].get_ydata()), [15200, 30200])
            plt.close(fig)

    def test_render_all_outputs_and_companion_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = figures.render_all(FIXTURES, tmp)
            self.assertEqual(len(paths), 5)
            self.assertTrue(all(Path(p).exists() for p in paths))
            self.assertTrue((Path(tmp) / 'figure2-operating-conditions.csv').exists())
            with (Path(tmp) / 'omega-coverage.csv').open(encoding='utf-8') as stream:
                omega = list(csv.DictReader(stream))
            self.assertEqual({e['method'] for e in metrics.load_episodes(FIXTURES)},
                             {row['method'] for row in omega})
            for row in omega:  # the denominator travels with every Omega figure
                self.assertEqual('0', row['omegaExcluded'])
                self.assertTrue(row['omegaSize'] and row['referenceSize'])
                self.assertIn('meanRankGapReferenceVsOmega', row)
                # a reader can tell which population the row describes
                self.assertTrue(row['schemaVersion'].startswith('agent-episode/'))
                self.assertGreater(int(row['selectedCount']), 0)


if __name__ == '__main__':
    unittest.main()
