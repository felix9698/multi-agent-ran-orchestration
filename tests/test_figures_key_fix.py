#!/usr/bin/env python3
"""Regression: figure generation must not crash when the requested headline
method is absent from the metrics.

Root cause (observed live, git 270a2dd, `--phase-profile sec14-load
--env-driver offered-load`): the experiment loop completed and saved records +
metrics, but `generate_all_figures` crashed with `KeyError: 'adaptive'`. The
runner passes `headline_method=DEFAULT_HEADLINE_METHOD == 'adaptive'`, while
`compute_multi_method` keys the metrics dict by the methods that ACTUALLY appear
in the records - and that live run produced only `llm_with_history`. figures.py
indexed `metrics_by_key['adaptive']` unconditionally.

These tests replay the exact live artifacts (a read-only committed copy under
tests/fixtures/figures_key_fix/, faithful to what the operator captured) through
the SAME code path the runner uses, and assert the whole figure set is produced
without crashing. See docs/figures_key_fix.md.
"""

import dataclasses
import json
import logging
import os
import tempfile
import unittest

from experiments import figures
from experiments.figures import _resolve_headline_method, generate_all_figures
from experiments.metrics import (
    ClipEvent, EpisodeRecord, StepRecord, compute_multi_method,
)
from experiments.runner import DEFAULT_HEADLINE_METHOD

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "figures_key_fix")


def _load_steps(path):
    fields = {f.name for f in dataclasses.fields(StepRecord)}
    with open(path) as f:
        raw = json.load(f)
    return [StepRecord(**{k: v for k, v in d.items() if k in fields}) for d in raw]


def _load_episodes(path):
    fields = {f.name for f in dataclasses.fields(EpisodeRecord)}
    with open(path) as f:
        raw = json.load(f)
    out = []
    for d in raw:
        d = dict(d)
        d.pop("total_ms", None)            # derived property, not a ctor field
        if isinstance(d.get("clips"), list):
            d["clips"] = [ClipEvent(**c) if isinstance(c, dict) else c
                          for c in d["clips"]]
        out.append(EpisodeRecord(**{k: v for k, v in d.items() if k in fields}))
    return out


class TestFiguresHeadlineKeyContract(unittest.TestCase):
    """The live single-method fixture drives the exact crash scenario."""

    @classmethod
    def setUpClass(cls):
        cls.steps = _load_steps(os.path.join(_FIXTURE_DIR, "live_steps.json"))
        cls.episodes = _load_episodes(
            os.path.join(_FIXTURE_DIR, "live_episodes.json"))
        with open(os.path.join(_FIXTURE_DIR, "live_metrics.json")) as f:
            cls.saved_metrics = json.load(f)
        # metrics rebuilt exactly as ExperimentRunner.analyze() does
        cls.metrics = compute_multi_method(cls.steps, cls.episodes)

    def test_fixture_is_the_single_method_live_scenario(self):
        # the crash only happens because only one method is present and it is
        # NOT the runner default headline - lock that in so the fixture stays
        # representative.
        self.assertEqual(list(self.metrics.keys()), ["llm_with_history"])
        self.assertNotIn(DEFAULT_HEADLINE_METHOD, self.metrics)
        # reconstruction is faithful: theta* matches the saved artifact bit-for-bit
        self.assertEqual(
            self.metrics["llm_with_history"]["cost_estimate"]["theta_star"],
            self.saved_metrics["llm_with_history"]["cost_estimate"]["theta_star"])

    def test_generate_all_figures_survives_missing_adaptive_headline(self):
        # the exact regression: runner passes headline_method='adaptive' but only
        # 'llm_with_history' is present -> must NOT raise, must produce every fig.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs("experiments.figures", level="WARNING") as cm:
                figs = generate_all_figures(
                    self.steps, self.episodes, self.metrics, output_dir=tmp,
                    headline_method="adaptive")
            # the fallback is ANNOUNCED, never silent
            self.assertTrue(any("adaptive" in m and "llm_with_history" in m
                                for m in cm.output))
            self.assertEqual(
                set(figs),
                {"throughput_timeline", "metrics_table", "reliability_diagram",
                 "consecutive_rollback", "per_ue_kpi"})
            # every promised artifact was actually written to disk (png + pdf)
            for paths in figs.values():
                self.assertTrue(paths)
                for p in paths:
                    self.assertTrue(os.path.exists(p), p)

    def test_generate_all_figures_uses_the_real_runner_default(self):
        # tie the test to the ACTUAL runner contract: whatever
        # DEFAULT_HEADLINE_METHOD is, an absent one must still resolve+run.
        with tempfile.TemporaryDirectory() as tmp:
            figs = generate_all_figures(
                self.steps, self.episodes, self.metrics, output_dir=tmp,
                headline_method=DEFAULT_HEADLINE_METHOD)
        self.assertIn("per_ue_kpi", figs)
        self.assertTrue(figs["per_ue_kpi"])

    def test_present_headline_is_honoured_no_warning(self):
        # when the requested headline IS present, it is used verbatim and no
        # fallback warning is emitted.
        with tempfile.TemporaryDirectory() as tmp:
            logger = logging.getLogger("experiments.figures")
            with self.assertLogs(logger, level="WARNING") as cm:
                logger.warning("sentinel")   # assertLogs needs >=1 record
                figs = generate_all_figures(
                    self.steps, self.episodes, self.metrics, output_dir=tmp,
                    headline_method="llm_with_history")
            self.assertEqual(
                [m for m in cm.output if "not present" in m], [])
            self.assertIn("per_ue_kpi", figs)


class TestResolveHeadlineMethod(unittest.TestCase):
    """Unit coverage for the resolution contract."""

    def test_present_returns_itself(self):
        self.assertEqual(
            _resolve_headline_method({"a": {}, "b": {}}, "b"), "b")

    def test_absent_falls_back_to_first_key_with_warning(self):
        with self.assertLogs("experiments.figures", level="WARNING") as cm:
            got = _resolve_headline_method({"a": {}, "b": {}}, "adaptive")
        self.assertEqual(got, "a")
        self.assertTrue(any("adaptive" in m for m in cm.output))

    def test_none_request_returns_first_present_key(self):
        self.assertEqual(_resolve_headline_method({"x": {}}, None), "x")

    def test_empty_metrics_returns_none(self):
        self.assertIsNone(_resolve_headline_method({}, "adaptive"))
        self.assertIsNone(_resolve_headline_method({}, None))


class TestPerUeKpiGuard(unittest.TestCase):
    """Direct misuse of plot_per_ue_kpi fails CLEARLY, not with a bare KeyError."""

    def setUp(self):
        self.steps = _load_steps(os.path.join(_FIXTURE_DIR, "live_steps.json"))
        self.episodes = _load_episodes(
            os.path.join(_FIXTURE_DIR, "live_episodes.json"))
        self.metrics = compute_multi_method(self.steps, self.episodes)

    def test_missing_method_key_raises_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KeyError) as ctx:
                figures.plot_per_ue_kpi(self.metrics, "adaptive", output_dir=tmp)
        # the message names the missing method AND the available ones
        msg = str(ctx.exception)
        self.assertIn("adaptive", msg)
        self.assertIn("llm_with_history", msg)


if __name__ == "__main__":
    unittest.main()
