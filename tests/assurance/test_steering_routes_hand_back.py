"""Defect 3 (2026-09-23 audit): every steering route hands the UE back before DELETE.

The 09-19 hand-back was set on the joint route alone; the pin_to_cell and
objective routes still DELETEd and left the UE on the target cell.  Hermetic.
"""
from __future__ import annotations

import unittest

from assurance.live.objective_runtime import build_live_objective_runtime
from assurance.live.pin_to_cell_driver import KpmUeAttributionReader, LiveCellTopology
from assurance.objectives import FAMILY_MODULES
from tests.assurance.test_g5_live_objective_runtime import (
    HOME_NCI, LIVE_SCOPE, TARGET_NCI, LiveObjectiveFixture, _Clock, _identity,
)
from tests.assurance.test_live_pin_to_cell_driver import LiveRunFixture


class ThePinToCellRouteHandsBack(LiveRunFixture):

    def test_the_adapter_restores_by_handover(self) -> None:
        self.assertTrue(self.build().adapter.restore_by_handover)


class TheObjectiveRouteHandsBack(LiveObjectiveFixture):

    def test_the_built_adapter_restores_by_handover(self) -> None:
        from assurance.contracts.live_binding import load_assurance_live_binding
        binding = load_assurance_live_binding("deployment/assurance-live-binding.1.0.0.json")
        clock = _Clock()
        runtime = build_live_objective_runtime(
            family_module=FAMILY_MODULES["TrafficSteeringPreference"](),
            scope=LIVE_SCOPE, binding=binding, policy_port=object(),
            policy_builder_factory=lambda kernel, bundle: (lambda command: {}),
            reader=KpmUeAttributionReader(
                read_new_lines=lambda: (),
                topology=LiveCellTopology(
                    plmn={"mcc": "208", "mnc": "95"},
                    nb_id_to_nci={0xE00: HOME_NCI, 0xB00: TARGET_NCI},
                    expected_epochs=dict(binding.kpm_expected_epochs))),
            identity=_identity(), now=clock.now, monotonic_ms=clock.monotonic_ms,
            sleep_ms=clock.sleep_ms, case_id="case/hand-back",
            counter_sample_loaders={"counter/rru-prb-dl": lambda: (),
                                    "counter/kpm-f3-drb-ue-thp-dl": lambda: ()})
        self.assertTrue(runtime.adapter.restore_by_handover)

    def test_an_override_keeps_its_own_behaviour(self) -> None:
        runtime = self.build("TrafficSteeringPreference")
        self.assertFalse(getattr(runtime.adapter, "restore_by_handover", False))


if __name__ == "__main__":
    unittest.main()
