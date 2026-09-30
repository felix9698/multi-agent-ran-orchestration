# SPDX-License-Identifier: MIT
"""Which axis values can be *applied*, and which need a withdrawal.

A supplementary axis rests at a sentinel -- ``dlPrbCap`` at ``"0"``,
``pfWeight`` at ``"1.0"``.  These tests used to assert that BOTH were outside
the APPLY domain.  That was wrong for both, and the error cost the campaign its
depth: once any trial settled a supplementary policy, every remaining candidate
was marked ineligible on that axis alone and the sitting ended
CATALOG_EXHAUSTED after one counted trial.

What the deployment actually says:

  * ``scheduler-priority.wire_value("1.0")`` returns 1.0.  A neutral weight is a
    setting, not an absence -- and episode ``…211256``'s retention applied
    ``pfWeight@ue2`` 4.0 -> 1.0 and finalized (``partialApply: false``,
    ``CLOSED_PASS``).
  * the deployed producer's ``AIC_UeDlPrbCap_1.0.0`` schema reads
    ``"maxDlPrbs": {"minimum": 0, ... "0 removes the cap."}``; the live gate
    publishes ``RAN.UE.DlPrbCap = 0`` for every uncapped UE, so a zero cap has a
    readback to be corroborated against; ``oai_patches`` says ``0 = uncapped``;
    and ``assurance/actions/catalog.py`` already exempted the sentinel from the
    5..24 band while ``action102_support.wire_value`` did not.  Episode
    ``…221114`` trial 3 then returned ``dlPrbCap@ue2`` 18 -> 0 live and measured
    ue2 at 8.67 Mbps with a 1.0 deadline ratio.

So the routing question is no longer "is this a sentinel" but "would a policy
body carrying this value be built" -- ``_sentinel_is_applicable`` asks the
declaration.  A composite axis (mcs bounds, attenuation, slice quota) still has
no single wire scalar and keeps the conservative treatment.
"""

from __future__ import annotations

import unittest

from assurance.objectives.action102_support import (
    CAP_ACTION_ID, PRIORITY_ACTION_ID, SUPPLEMENTARY_ACTIONS,
)
from tools.liveconsole.agent import AgentSitting

needs_withdrawal = AgentSitting._axes_needing_withdrawal  # noqa: SLF001 - the unit under test


class TheContractedSentinels(unittest.TestCase):
    """The values this routing depends on are the contract's, not ours."""

    def test_the_cap_rests_uncapped_and_the_weight_rests_neutral(self) -> None:
        self.assertEqual("0", SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID].baseline)
        self.assertEqual("1.0", SUPPLEMENTARY_ACTIONS[PRIORITY_ACTION_ID].baseline)


class RetentionNamesWhatOnlyAWithdrawalCouldRestore(unittest.TestCase):

    def test_the_live_case_that_spent_a_permit_for_nothing(self) -> None:
        # The shape of episode …175355 trial 2: C0 asks every axis back to
        # baseline, the deployment holds one capped UE.
        wanted = {"servingCell@1156": "12345678", "servingCell@1153": "87654321",
                  "servingCell@1157": "12345678", "dlPrbCap@1156": "0",
                  "dlPrbCap@1153": "0", "dlPrbCap@1157": "0"}
        applied = dict(wanted, **{"dlPrbCap@1157": "6"})
        # Was ``("dlPrbCap@1157",)``.  The permit that episode spent was not
        # wasted on an inexpressible value -- 0 removes the cap and the producer
        # schema admits it; the refusal came from our own band check, which had
        # forgotten the sentinel exemption ``catalog.py`` already had.
        self.assertEqual((), needs_withdrawal(wanted, applied))

    def test_a_weight_back_to_neutral_is_an_ordinary_apply(self) -> None:
        """A neutral PF weight is a setting, not an absence.

        This test used to assert the opposite, and asserting it cost the
        campaign its depth: after one trial settled ``pfWeight@ue2``, every one
        of episode 2c70f79d's eleven candidates came back ``eligible=False`` on
        that axis alone and the sitting ended CATALOG_EXHAUSTED with a single
        counted trial.  The live record disproves the rule the test fixed --
        that same episode's retention applied ``pfWeight@ue2`` 4.0 -> 1.0 and
        finalized (``partialApply: false``, ``CLOSED_PASS``).  The declaration
        agrees: ``scheduler-priority.wire_value("1.0")`` returns 1.0 while
        ``ue-dl-prb-cap.wire_value("0")`` refuses ("an applied cap is 5..24
        PRB"), which is the difference the code now asks about instead of
        assuming every sentinel is unreachable.
        """
        self.assertEqual(
            (),
            needs_withdrawal({"pfWeight@131": "1.0"}, {"pfWeight@131": "0.5"}))

    def test_every_blocked_axis_is_named_not_just_the_first(self) -> None:
        wanted = {"dlPrbCap@131": "0", "dlPrbCap@132": "0", "pfWeight@131": "1.0"}
        applied = {"dlPrbCap@131": "6", "dlPrbCap@132": "12", "pfWeight@131": "2.0"}
        # Nothing: both axes of this deployment can carry their own baseline.
        self.assertEqual((), needs_withdrawal(wanted, applied))


class RetentionStillReAppliesEverythingElse(unittest.TestCase):
    """The check must not swallow the ordinary superseded-control case."""

    def test_an_axis_already_at_its_sentinel_needs_no_withdrawal(self) -> None:
        self.assertEqual((), needs_withdrawal({"dlPrbCap@131": "0"},
                                              {"dlPrbCap@131": "0"}))

    def test_moving_onto_a_cap_is_an_ordinary_apply(self) -> None:
        self.assertEqual((), needs_withdrawal({"dlPrbCap@131": "6"},
                                              {"dlPrbCap@131": "0"}))

    def test_changing_a_held_cap_is_an_ordinary_apply_again(self) -> None:
        # Three readings, each from measurement.  It began as an ordinary apply;
        # 2026-09-15 attempt 45 refuted that, because the settled binding owned
        # the scope and 18 -> 12 and 18 -> 6 were both REJECTED; and 2026-09-16
        # priced the refusal -- every episode of that day ending CATALOG_EXHAUSTED
        # or KERNEL_TERMINATED did so on one of these rejections, eight in all.
        # The adapter now lets a later transaction adopt the policy a finished
        # one left live, which the producer takes as an in-place update, so the
        # move is expressible and only the sentinel return is not.
        self.assertEqual((), needs_withdrawal({"dlPrbCap@131": "6"},
                                              {"dlPrbCap@131": "12"}))

    def test_a_held_cap_back_to_its_sentinel_is_an_ordinary_apply(self) -> None:
        """Removing a cap is a value the wire carries, not an absence.

        Renamed from ``..._is_still_a_withdrawal``.  Episode ``…221114`` trial 3
        returned ``dlPrbCap@ue2`` 18 -> 0 live and measured ue2 at 8.67 Mbps with
        a 1.0 deadline ratio, and the gate publishes ``RAN.UE.DlPrbCap = 0`` for
        every uncapped UE, so the write has a readback to be corroborated
        against.
        """
        self.assertEqual((), needs_withdrawal({"dlPrbCap@131": "0"},
                                              {"dlPrbCap@131": "12"}))

    def test_keeping_a_held_cap_is_not_a_change(self) -> None:
        self.assertEqual((), needs_withdrawal({"dlPrbCap@131": "12"},
                                              {"dlPrbCap@131": "12"}))

    def test_steering_is_not_a_sentinel_axis(self) -> None:
        # servingCell has no uncapped state to withdraw to; a cell is a cell.
        self.assertEqual((), needs_withdrawal({"servingCell@131": "12345678"},
                                              {"servingCell@131": "87654321"}))

    def test_an_axis_absent_from_the_applied_config_reads_as_its_sentinel(self) -> None:
        self.assertEqual((), needs_withdrawal({"dlPrbCap@131": "0"}, {}))


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()


class TheSearchDoesNotSpendATrialOnAWithdrawal(unittest.TestCase):
    """2026-09-15 attempt 27: after C04 settled live with a cap, two proposals asked
    the capped axis back to "0", each spent a trial on PREPARE REJECTED, and the pair
    ended the sitting as an execution failure."""

    def sitting(self):
        from types import SimpleNamespace
        sitting = AgentSitting.__new__(AgentSitting)
        configs = {
            "C04": {"servingCell@131": "12345678", "dlPrbCap@132": "18"},
            "C05": {"servingCell@131": "12345678", "dlPrbCap@132": "0"},
            "C06": {"servingCell@131": "87654321", "dlPrbCap@132": "18"},
        }
        sitting.controls = SimpleNamespace(
            candidate=lambda cid: SimpleNamespace(configuration=configs[cid]) if cid in configs else None)
        sitting.catalog_of_control = {cid: f"candidate/{cid}" for cid in configs}
        availability = {"C04": "CONSUMED", "C05": "AVAILABLE", "C06": "AVAILABLE"}
        entries = [SimpleNamespace(candidate=SimpleNamespace(candidate_id=f"candidate/{cid}"),
                                   availability=SimpleNamespace(value=availability[cid]))
                   for cid in configs]
        sitting.runtime = SimpleNamespace(
            catalog_entries=lambda: entries, trials=[],
            live_baseline=lambda: {"servingCell@131": "12345678", "dlPrbCap@132": "18"})
        # 2026-09-19: the sitting reads single points of the frozen domain.
        sitting.runtime.candidate_entry = lambda cid: next(
            (e for e in sitting.runtime.catalog_entries() if e.candidate.candidate_id == cid), None)
        sitting.runtime.spent_ids = lambda: {
            e.candidate.candidate_id for e in sitting.runtime.catalog_entries()
            if e.availability.value != "AVAILABLE"}
        sitting.request = SimpleNamespace(method="three-agent")
        sitting.retention_runtime = None
        sitting.retired_runtimes = []
        sitting.baselines = {"servingCell@131": "12345678", "dlPrbCap@132": "0"}
        return sitting

    def test_a_control_returning_a_settled_cap_to_its_sentinel_is_executable(self):
        """Renamed and inverted: C05 asks 18 -> 0, which the deployment can apply.

        This is the assertion that cost the most.  While it held, a settled cap
        made every candidate naming the uncapped baseline ineligible, and the
        search died with one counted trial.  With it corrected, episode
        ``…221114`` ran eight counted trials and ended BUDGET_EXHAUSTED -- the
        termination the budget is for -- instead of CATALOG_EXHAUSTED.
        """
        sitting = self.sitting()
        self.assertEqual((), sitting._withdrawal_blocked("C05"))
        self.assertEqual((), sitting._withdrawal_blocked("C06"))
        # Two, not one: C05 counts again now that the uncapped baseline is an
        # applied value.  That single number is the whole defect -- with C05
        # struck off, one settled cap could leave the search with nothing.
        self.assertEqual(2, sitting._eligible_remaining())
        status = sitting._candidate_status()
        # The status the model reads has to agree: C05 is offerable again and
        # carries no withdrawal-only axis, so the prompt no longer presents a
        # candidate the executor would refuse.
        self.assertTrue(status["C05"]["eligible"])
        self.assertNotIn("withdrawalOnlyAxes", status["C05"])
        self.assertTrue(status["C06"]["eligible"])

    def test_a_catalog_combination_no_control_names_is_not_eligible(self):
        # 2026-09-15 attempt 51: 421 unreachable combinations were reported as
        # eligible after every control of C was spent or held.
        from types import SimpleNamespace
        sitting = self.sitting()
        sitting.runtime.catalog_entries = lambda: [
            SimpleNamespace(candidate=SimpleNamespace(candidate_id="candidate/unnamed"),
                            availability=SimpleNamespace(value="AVAILABLE"))]
        self.assertEqual(0, sitting._eligible_remaining())
