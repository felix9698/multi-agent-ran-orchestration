#!/usr/bin/env python3
"""Batch E (P0-15): real history ablation.

Covers the validated HistoryMode enum, the STRICT structured HistoryRecord (no
coercion), the defensive immutable reservoir snapshot, the DETERMINISTIC
relevance selector (mode-coerced, partitioned by proposer+model+regime, with
canonical axis-family overlap that replaces last-five recency), the explicit
evidence-record-id contract, and the coordinator integration.
"""

import copy
import json
import unittest

from coordinator.intent_coordinator import IntentCoordinator
from coordinator.history import (
    HistoryMode, HistoryPolicy, HistoryResetScope, HistoryRecord,
    HistoryValidationError, snapshot_records, restore_records, select_relevant,
    canonical_axis_family,
)
from coordinator.proposer import ProposerContext
from decision.intent_model import (
    ConstraintType, Intent, IntentScope, IntentTarget, IntentType,
)


def _rec(sig="sig-1", axes=("prb", "mcs"), regime="regimeX",
         proposer="A", model="modelA", ev="ev-1", created="2026-01-01",
         outcome="commit_original"):
    return HistoryRecord(
        intent_signature=sig, action_axes=tuple(axes), operating_regime=regime,
        proposer_id=proposer, model_version=model, evidence_id=ev,
        terminal_outcome=outcome, created_at=created)


def _intent(value=8.0, ues=("ue1",)):
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=value, unit="Mbps"),
                  scope=IntentScope(ue_ids=list(ues)))


class _FakeCal:
    def __init__(self):
        self.contexts = []

    def set_operating_context(self, model, regime):
        self.contexts.append((model, regime))


def _hist_coord(mode=HistoryMode.ONLINE_RESERVOIR, proposer="A",
                model="modelA", regime="regimeX"):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.history_reservoir = []
    c.max_history = 100
    c.history_mode = mode
    c._frozen_history = tuple()
    c._last_intent_signature = None
    c._proposer_ctx = ProposerContext(
        proposer_id=proposer, backend_name=proposer, model_version=model)
    c.current_phase = regime
    c._episode_context = (model, regime)          # _frozen_context returns this
    c._calibration_context_ok = True
    c.calibrator = _FakeCal()
    return c


# ---------------------------------------------------------------------------
# HistoryMode / policy / scope: fail-closed coercion
# ---------------------------------------------------------------------------
class EnumCoerceTest(unittest.TestCase):

    def test_history_mode_coerce(self):
        self.assertIs(HistoryMode.coerce("disabled"), HistoryMode.DISABLED)
        self.assertIs(HistoryMode.coerce("FROZEN_RESERVOIR"),
                      HistoryMode.FROZEN_RESERVOIR)
        for bad in ("", "on", "recency", None, 3):
            with self.assertRaises(ValueError):
                HistoryMode.coerce(bad)

    def test_policy_and_scope_coerce(self):
        self.assertIs(HistoryPolicy.coerce("online"), HistoryPolicy.ONLINE)
        self.assertIs(HistoryResetScope.coerce("MODEL"),
                      HistoryResetScope.MODEL)
        for bad in ("", "weekly", None):
            with self.assertRaises(ValueError):
                HistoryPolicy.coerce(bad)
            with self.assertRaises(ValueError):
                HistoryResetScope.coerce(bad)

    def test_set_history_mode_rejects_invalid(self):
        c = _hist_coord()
        with self.assertRaises(ValueError):
            c.set_history_mode("recency-window")
        self.assertIs(c.history_mode, HistoryMode.ONLINE_RESERVOIR)


# ---------------------------------------------------------------------------
# HistoryRecord: STRICT schema (no coercion)
# ---------------------------------------------------------------------------
class HistoryRecordTest(unittest.TestCase):

    def test_valid_record_roundtrips_json_and_deepcopy(self):
        r = _rec()
        d = r.to_dict()
        json.dumps(d)
        self.assertEqual(HistoryRecord.from_dict(d), r)
        self.assertEqual(copy.deepcopy(r), r)
        self.assertEqual(r.partition_key(), ("A", "modelA", "regimeX"))

    def test_frozen_is_immutable(self):
        with self.assertRaises(Exception):
            _rec().evidence_id = "tampered"

    def test_missing_required_fields_fail_closed(self):
        base = dict(intent_signature="s", action_axes=("prb",),
                    operating_regime="r", proposer_id="A",
                    model_version="m", evidence_id="e")
        for kw in ("intent_signature", "operating_regime", "proposer_id",
                   "model_version", "evidence_id"):
            bad = dict(base)
            bad[kw] = ""
            with self.assertRaises(HistoryValidationError):
                HistoryRecord(**bad)

    def test_from_dict_rejects_wrong_types_no_coercion(self):
        good = _rec().to_dict()
        # proposer_id = 123 must be REJECTED, not coerced to "123"
        bad = dict(good, proposer_id=123)
        with self.assertRaises(HistoryValidationError):
            HistoryRecord.from_dict(bad)
        # action_axes = [1] (int item) rejected, not coerced to ["1"]
        bad = dict(good, action_axes=[1])
        with self.assertRaises(HistoryValidationError):
            HistoryRecord.from_dict(bad)
        # bool-as-string rejected
        bad = dict(good, proposer_id=True)
        with self.assertRaises(HistoryValidationError):
            HistoryRecord.from_dict(bad)
        # action_axes must be a JSON array, not a bare string
        bad = dict(good, action_axes="prb")
        with self.assertRaises(HistoryValidationError):
            HistoryRecord.from_dict(bad)

    def test_from_dict_rejects_unknown_and_missing_keys(self):
        good = _rec().to_dict()
        with self.assertRaises(HistoryValidationError):
            HistoryRecord.from_dict(dict(good, surprise="x"))
        missing = dict(good)
        del missing["evidence_id"]
        with self.assertRaises(HistoryValidationError):
            HistoryRecord.from_dict(missing)

    def test_from_dict_rejects_malformed_timestamp(self):
        good = _rec().to_dict()
        with self.assertRaises(HistoryValidationError):
            HistoryRecord.from_dict(dict(good, created_at="not-a-timestamp"))
        with self.assertRaises(HistoryValidationError):
            _rec(created="13/25/2026 99:99")


# ---------------------------------------------------------------------------
# Axis-family normalization
# ---------------------------------------------------------------------------
class AxisFamilyTest(unittest.TestCase):

    def test_family_mapping(self):
        self.assertEqual(canonical_axis_family("bs1_prb"), "prb")
        self.assertEqual(canonical_axis_family("gnb1.cell.power_offset"),
                         "power")
        self.assertEqual(canonical_axis_family("bs1_power_offset"), "power")
        self.assertEqual(canonical_axis_family("mcs_offset"), "mcs")
        self.assertEqual(canonical_axis_family("sched_priority"),
                         "sched_priority")
        self.assertEqual(canonical_axis_family("prb"), "prb")


# ---------------------------------------------------------------------------
# Defensive immutable snapshot / restore
# ---------------------------------------------------------------------------
class SnapshotTest(unittest.TestCase):

    def test_snapshot_is_immutable_and_restore_is_fresh(self):
        live = [_rec(ev="ev-1"), _rec(ev="ev-2")]
        snap = snapshot_records(live)
        self.assertIsInstance(snap, tuple)
        live.append(_rec(ev="ev-3"))
        self.assertEqual(len(snap), 2)
        restored = restore_records(snap)
        restored.append(_rec(ev="ev-9"))
        self.assertEqual(len(snap), 2)

    def test_snapshot_rejects_unsnapshotable(self):
        with self.assertRaises(HistoryValidationError):
            snapshot_records([object()])


# ---------------------------------------------------------------------------
# Deterministic relevance selector
# ---------------------------------------------------------------------------
class SelectorTest(unittest.TestCase):

    def test_invalid_mode_fails_closed(self):
        # a garbage mode must RAISE, never behave like enabled
        with self.assertRaises(ValueError):
            select_relevant([_rec()], "sig-1", ("prb",), "regimeX", "A",
                            "modelA", mode="garbage")

    def test_partition_isolates_proposer_model_and_regime(self):
        pool = [
            _rec(ev="keep", proposer="A", model="modelA", regime="regimeX"),
            _rec(ev="other-proposer", proposer="B", model="modelA",
                 regime="regimeX"),
            _rec(ev="other-model", proposer="A", model="modelB",
                 regime="regimeX"),
            _rec(ev="other-regime", proposer="A", model="modelA",
                 regime="regimeY"),
        ]
        out = select_relevant(pool, "sig-1", ("prb", "mcs"), "regimeX",
                              "A", "modelA")
        self.assertEqual([r.evidence_id for r in out], ["keep"])

    def test_axis_family_overlap_breaks_the_reproduced_tie(self):
        # reviewer's exact probe: a real bs1_prb record must outrank a
        # bs1_power_offset record for a prb query, NOT lose on evidence-id.
        pool = [
            _rec(ev="a", sig="other", axes=("bs1_power_offset",)),
            _rec(ev="z", sig="other", axes=("bs1_prb",)),
        ]
        out = select_relevant(pool, "sig-1", ("prb",), "regimeX", "A",
                              "modelA")
        # prb-matching 'z' first (family overlap), then 'a' - NOT ['a','z']
        self.assertEqual([r.evidence_id for r in out], ["z", "a"])

    def test_signature_match_then_overlap_then_stable_tiebreak(self):
        pool = [
            _rec(ev="lo", sig="other", axes=("prb",), created="2026-01-02"),
            _rec(ev="sig", sig="sig-1", axes=(), created="2026-01-03"),
            _rec(ev="hi", sig="other", axes=("prb", "mcs"),
                 created="2026-01-02"),
        ]
        out = select_relevant(pool, "sig-1", ("prb", "mcs"), "regimeX", "A",
                              "modelA")
        self.assertEqual([r.evidence_id for r in out], ["sig", "hi", "lo"])

    def test_disabled_returns_empty(self):
        out = select_relevant([_rec()], "sig", ("prb",), "regimeX", "A",
                              "modelA", mode=HistoryMode.DISABLED)
        self.assertEqual(out, [])


# ---------------------------------------------------------------------------
# Coordinator integration
# ---------------------------------------------------------------------------
class CoordinatorHistoryTest(unittest.TestCase):

    def test_no_history_prompt_contains_no_records(self):
        c = _hist_coord(mode=HistoryMode.DISABLED)
        c.history_reservoir = [_rec()]
        self.assertIsNone(c._history_for_prompt(_intent()))

    def test_history_toggle_changes_only_history_input(self):
        c = _hist_coord(mode=HistoryMode.ONLINE_RESERVOIR)
        sig = c._intent_content_hash(_intent())
        c.history_reservoir = [_rec(sig=sig, proposer="A", model="modelA",
                                    regime="regimeX")]
        self.assertTrue(c._history_for_prompt(_intent()))
        c.set_history_mode(HistoryMode.DISABLED)
        self.assertIsNone(c._history_for_prompt(_intent()))

    def test_history_retrieval_uses_relevance_selector(self):
        c = _hist_coord(mode=HistoryMode.ONLINE_RESERVOIR)
        sig = c._intent_content_hash(_intent())
        c.history_reservoir = [
            _rec(ev="match", sig=sig, proposer="A", model="modelA"),
            _rec(ev="wrong-proposer", sig=sig, proposer="B", model="modelA"),
        ]
        out = c._history_for_prompt(_intent())
        self.assertEqual([r["evidence_id"] for r in out], ["match"])

    def test_frozen_mode_reads_snapshot_and_never_learns(self):
        c = _hist_coord(mode=HistoryMode.FROZEN_RESERVOIR)
        sig = c._intent_content_hash(_intent())
        c.set_frozen_history(snapshot_records(
            [_rec(ev="frozen-1", sig=sig, proposer="A", model="modelA")]))
        out = c._history_for_prompt(_intent())
        self.assertEqual([r["evidence_id"] for r in out], ["frozen-1"])
        c._last_intent_signature = sig
        c._add_to_history(_commit_result("evid-1"))
        self.assertEqual(c.history_reservoir, [])
        self.assertEqual(len(c._frozen_history), 1)

    def test_append_references_only_evidence_record_id(self):
        c = _hist_coord(mode=HistoryMode.ONLINE_RESERVOIR)
        c._last_intent_signature = "sig-1"
        c._add_to_history(_commit_result("evid-77"))
        self.assertEqual(len(c.history_reservoir), 1)
        r = c.history_reservoir[0]
        self.assertEqual(r.evidence_id, "evid-77")   # the evidence-record id
        self.assertEqual(r.model_version, "modelA")
        self.assertEqual(r.proposer_id, "A")

    def test_no_evidence_record_id_means_no_append(self):
        # a bundle without evidence_record_id: trial/episode ids are NOT a
        # fallback -> nothing appended (no final evidence identity).
        c = _hist_coord(mode=HistoryMode.ONLINE_RESERVOIR)
        c._last_intent_signature = "sig-1"
        c._add_to_history({
            "episode_id": "ep-1", "terminal_outcome": "commit_original",
            "evidence": {"episode_id": "ep-1", "actuation_trial_id": "trial-9",
                         "pending_intent_hash": "pi"}})
        self.assertEqual(c.history_reservoir, [])

    def test_noncommit_evidence_still_learns_with_record_id(self):
        c = _hist_coord(mode=HistoryMode.ONLINE_RESERVOIR)
        c._last_intent_signature = "sig-1"
        c._add_to_history({
            "episode_id": "ep-2",
            "terminal_outcome": "pending_not_admitted",
            "terminal_reason": "no_acceptable_alternative",
            "evidence": {"episode_id": "ep-2", "evidence_record_id": "evid-2",
                         "actuation_trial_id": None,
                         "calibration_context": {
                             "operating_regime_id": "regimeX"}}})
        self.assertEqual(len(c.history_reservoir), 1)
        self.assertEqual(c.history_reservoir[0].evidence_id, "evid-2")
        self.assertEqual(c.history_reservoir[0].action_axes, tuple())

    def test_no_signature_means_no_record(self):
        c = _hist_coord(mode=HistoryMode.ONLINE_RESERVOIR)
        c._last_intent_signature = None
        c._add_to_history(_commit_result("evid-3"))
        self.assertEqual(c.history_reservoir, [])

    def test_disabled_mode_never_appends(self):
        c = _hist_coord(mode=HistoryMode.DISABLED)
        c._last_intent_signature = "sig-1"
        c._add_to_history(_commit_result("evid-4"))
        self.assertEqual(c.history_reservoir, [])

    def test_snapshot_restore_clear_reset(self):
        c = _hist_coord()
        c.history_reservoir = [_rec(ev="ev-1")]
        snap = c.history_snapshot()
        c.clear_history()
        self.assertEqual(c.history_reservoir, [])
        c.restore_history_snapshot(snap)
        self.assertEqual(len(c.history_reservoir), 1)
        c.history_reservoir.append(_rec(ev="ev-2"))
        self.assertEqual(len(snap), 1)               # no alias
        c.set_frozen_history(snapshot_records([_rec(ev="fz")]))
        c.reset_history()
        self.assertEqual(c.history_reservoir, [])
        self.assertEqual(c._frozen_history, tuple())

    def test_no_cross_partition_leak_after_model_switch(self):
        c = _hist_coord(mode=HistoryMode.ONLINE_RESERVOIR,
                        proposer="A", model="modelA", regime="regimeX")
        sig = c._intent_content_hash(_intent())
        c.history_reservoir = [
            _rec(ev="A-X", sig=sig, proposer="A", model="modelA",
                 regime="regimeX"),
            _rec(ev="B-Y", sig=sig, proposer="B", model="modelB",
                 regime="regimeY"),
        ]
        self.assertEqual(
            [r["evidence_id"] for r in c._history_for_prompt(_intent())],
            ["A-X"])
        # switch the pinned proposer/model + regime to B/modelB/regimeY
        c._proposer_ctx = ProposerContext(proposer_id="B", backend_name="B",
                                          model_version="modelB")
        c._episode_context = ("modelB", "regimeY")
        c.current_phase = "regimeY"
        self.assertEqual(
            [r["evidence_id"] for r in c._history_for_prompt(_intent())],
            ["B-Y"])

    def test_invalid_record_build_fails_closed(self):
        c = _hist_coord(mode=HistoryMode.ONLINE_RESERVOIR)
        c._last_intent_signature = "sig-1"

        class _BoomCtx:
            def __getattr__(self, k):
                raise RuntimeError("ctx boom")
        c._proposer_ctx = _BoomCtx()
        c._add_to_history(_commit_result("evid-x"))   # must swallow, not raise
        self.assertEqual(c.history_reservoir, [])


def _commit_result(evid):
    """A finalized COMMIT result carrying an explicit evidence-record id."""
    return {
        "episode_id": "ep-c", "terminal_outcome": "commit_original",
        "terminal_reason": "commit_verified",
        "evidence": {"episode_id": "ep-c", "evidence_record_id": evid,
                     "actuation_trial_id": "trial-c",
                     "canonical_action": {"prb": 12, "mcs": 20},
                     "calibration_context": {"operating_regime_id": "regimeX"}}}


if __name__ == "__main__":
    unittest.main()
