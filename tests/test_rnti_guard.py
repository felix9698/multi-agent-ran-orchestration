"""P8: RNTI integrity guard - RRC re-establishment during a trial changes
the RNTI; per-UE apply/restore must detect it (stock reestab counter),
reinterpret the stale RNTI, and fail safely when reinterpretation is
impossible.
"""

import unittest

from coordinator.intent_coordinator import IntentCoordinator
from experiments.emulation import SimExecutor, SimTelnetGNB


def _make_executor(ues_gnb1=(0x4601,)):
    sims = {"gnb1": SimTelnetGNB(pci=0), "gnb2": SimTelnetGNB(pci=1)}
    for r in ues_gnb1:
        sims["gnb1"].add_ue(r)
    sims["gnb2"].add_ue(0x4699)
    return SimExecutor(sims), sims


class SnapshotBaselineTest(unittest.TestCase):

    def test_snapshot_records_reestab_count(self):
        ex, sims = _make_executor()
        snap = ex.snapshot()
        self.assertEqual(snap["gnb1"]["reestab_count"], 0)
        sims["gnb1"].reestablish(0x4601, 0x9b3e)
        self.assertEqual(ex.snapshot()["gnb1"]["reestab_count"], 1)

    def test_counter_unavailable_disables_guard(self):
        # a gNB without the counter keeps legacy behavior (no guard)
        class _NoCounter(SimTelnetGNB):
            def exec_cmd(self, cmd):
                if "get_reestab_count" in cmd:
                    return "unknown ci subcommand 'get_reestab_count'"
                return super().exec_cmd(cmd)

        sims = {"gnb1": _NoCounter(pci=0), "gnb2": SimTelnetGNB(pci=1)}
        sims["gnb1"].add_ue(0x4601)
        ex = SimExecutor(sims)
        self.assertIsNone(ex.snapshot()["gnb1"]["reestab_count"])
        self.assertTrue(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))


class RestoreGuardTest(unittest.TestCase):

    def test_reestablishment_detected_and_restored_to_new_rnti(self):
        # Acceptance (a): trial sets a per-UE cap, the UE re-establishes
        # under a new RNTI, restore reinterprets and resets the NEW RNTI.
        ex, sims = _make_executor()
        snap = ex.snapshot()                                # baseline count 0
        self.assertTrue(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))
        sims["gnb1"].reestablish(0x4601, 0x9b3e)            # count -> 1
        self.assertTrue(ex.restore(snap))
        # device: new RNTI exists, uncapped (fresh context stays neutral)
        self.assertEqual(sims["gnb1"].ues[0x9b3e]["prb_cap"], 0)
        self.assertNotIn(0x4601, sims["gnb1"].ues)
        # mirror migrated to the new RNTI
        self.assertNotIn(0x4601, ex.states["gnb1"].ue_prb_cap)
        # guard event recorded with the remap
        ev = ex.last_rnti_guard_events[-1]
        self.assertEqual((ev["old"], ev["new"]), (0x4601, 0x9b3e))
        # audit covered the remapped key and matched
        self.assertTrue(ex.last_restore_report["gnb1"]["prb@9b3e"][2])

    def test_unresolvable_reestablishment_fails_safe(self):
        # Acceptance (b): with 2 UEs on the cell, the stale RNTI cannot be
        # disambiguated -> restore returns False and the reason is recorded.
        ex, sims = _make_executor(ues_gnb1=(0x4601, 0x4602))
        snap = ex.snapshot()
        self.assertTrue(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))
        sims["gnb1"].reestablish(0x4601, 0x9b3e)
        # a second unknown UE appears too -> listing diff is ambiguous
        sims["gnb1"].add_ue(0x7777)
        self.assertFalse(ex.restore(snap))
        ev = ex.last_rnti_guard_events[-1]
        self.assertIsNone(ev["new"])
        self.assertIn("could not reinterpret", ev["reason"])

    def test_multi_axis_per_ue_rollback_after_remap(self):
        # regression (Codex finding): a UE with BOTH sched-priority and PRB
        # state must not have the second axis wipe the first restore - the
        # remap recorded on the first axis rekeys the second axis's
        # snapshot union so each logical UE is processed exactly once.
        ex, sims = _make_executor()
        self.assertTrue(ex.apply_axis("gnb1", "sched_priority", 2.0,
                                      rnti=0x4601))
        self.assertTrue(ex.apply_axis("gnb1", "prb", 30, rnti=0x4601))
        snap = ex.snapshot()          # sched 2.0 + prb 30 @4601 in snapshot
        self.assertTrue(ex.apply_axis("gnb1", "sched_priority", 3.0,
                                      rnti=0x4601))
        self.assertTrue(ex.apply_axis("gnb1", "prb", 12, rnti=0x4601))
        sims["gnb1"].reestablish(0x4601, 0x9b3e)
        self.assertTrue(ex.restore(snap))
        self.assertAlmostEqual(sims["gnb1"].ues[0x9b3e]["pf_weight"], 2.0)
        self.assertEqual(sims["gnb1"].ues[0x9b3e]["prb_cap"], 30)   # not 0!
        self.assertTrue(ex.last_restore_report["gnb1"]["prb@9b3e"][2])
        self.assertTrue(
            ex.last_restore_report["gnb1"]["sched_priority@9b3e"][2])

    def test_snapshotted_per_ue_value_restored_under_new_rnti(self):
        # pre-trial per-UE state exists in the snapshot under the OLD RNTI;
        # after a remap the restore must re-impose it on the NEW RNTI and
        # the audit must compare under the remapped key.
        ex, sims = _make_executor()
        self.assertTrue(ex.apply_axis("gnb1", "prb", 30, rnti=0x4601))
        snap = ex.snapshot()                    # cap 30 @4601 in snapshot
        self.assertTrue(ex.apply_axis("gnb1", "prb", 12, rnti=0x4601))
        sims["gnb1"].reestablish(0x4601, 0x9b3e)
        self.assertTrue(ex.restore(snap))
        self.assertEqual(sims["gnb1"].ues[0x9b3e]["prb_cap"], 30)
        self.assertTrue(ex.last_restore_report["gnb1"]["prb@9b3e"][2])


class ApplyGuardTest(unittest.TestCase):

    def test_apply_axis_remaps_stale_rnti(self):
        ex, sims = _make_executor()
        ex.snapshot()                                # observe baseline
        sims["gnb1"].reestablish(0x4601, 0x9b3e)
        self.assertTrue(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))
        self.assertEqual(sims["gnb1"].ues[0x9b3e]["prb_cap"], 24)
        self.assertEqual(ex.states["gnb1"].ue_prb_cap.get(0x9b3e), 24)

    def test_apply_axis_fails_safe_when_ambiguous(self):
        ex, sims = _make_executor(ues_gnb1=(0x4601, 0x4602))
        ex.snapshot()
        sims["gnb1"].reestablish(0x4601, 0x9b3e)
        sims["gnb1"].add_ue(0x7777)
        self.assertFalse(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))


class CoordinatorHooksTest(unittest.TestCase):

    def _coordinator_with(self, ex):
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.ue_serving_gnb = {"ue1": "gnb1"}
        c.ue_rnti = {"ue1": 0x4601}
        c.executor = ex
        ex.rnti_resolver = c._resolve_stale_rnti
        ex.on_rnti_remap = c._on_rnti_remap
        return c

    def test_registry_resolver_answers_first(self):
        # the registry already knows the post-reestab RNTI: the guard uses
        # it without device probes
        ex, sims = _make_executor()
        c = self._coordinator_with(ex)
        ex.snapshot()
        sims["gnb1"].reestablish(0x4601, 0x9b3e)
        c.ue_rnti["ue1"] = 0x9b3e                    # operator re-registered
        self.assertTrue(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))
        self.assertEqual(sims["gnb1"].ues[0x9b3e]["prb_cap"], 24)

    def test_remap_updates_coordinator_cache(self):
        ex, sims = _make_executor()
        c = self._coordinator_with(ex)
        ex.snapshot()
        sims["gnb1"].reestablish(0x4601, 0x9b3e)
        self.assertTrue(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))
        self.assertEqual(c.ue_rnti["ue1"], 0x9b3e)   # cache kept coherent


if __name__ == "__main__":
    unittest.main()
