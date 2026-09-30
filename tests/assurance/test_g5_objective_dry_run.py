"""Hardware-free drive plans for the two QoS steering families."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

from assurance.live.pin_to_cell_driver import LiveDriverError
from assurance.objectives.registry import (
    ASSURANCE_FAMILY_WIRE_KIND_MAPPING_VERSION,
)
from tools.g3ota.run_ota import run


class QoSObjectiveDryRunTests(unittest.TestCase):

    _FORBIDDEN_LIVE_CALLS = (
        "load_integration_values",
        "_pinned_capability",
        "KpmTail",
        "observe_live_ue",
        "clear_ue_scope",
        "build_r1_policy_port",
    )
    _FORBIDDEN_PROCESS_CALLS = (
        "socket.socket",
        "socket.create_connection",
        "subprocess.Popen",
        "subprocess.run",
        "os.system",
        "os.popen",
    )

    def _run(self, family: str) -> dict:
        output = io.StringIO()
        with ExitStack() as stack:
            for name in self._FORBIDDEN_LIVE_CALLS:
                stack.enter_context(patch(
                    f"tools.g3ota.run_ota.{name}",
                    side_effect=AssertionError(
                        f"dry-run crossed into live integration at {name}"
                    ),
                ))
            for path in self._FORBIDDEN_PROCESS_CALLS:
                stack.enter_context(patch(
                    path,
                    side_effect=AssertionError(
                        f"dry-run crossed into external I/O at {path}"
                    ),
                ))
            stack.enter_context(redirect_stdout(output))
            status = run(["--objective", family, "--dry-run"])
        self.assertEqual(status, 0)
        return json.loads(output.getvalue())

    def test_qos_target_dry_run_builds_a_cell_steering_drive_plan(self) -> None:
        plan = self._run("QoSTarget")
        self.assertEqual(plan["mode"], "HARDWARE_FREE_DRY_RUN")
        self.assertEqual(
            plan["wireKindMappingVersion"],
            ASSURANCE_FAMILY_WIRE_KIND_MAPPING_VERSION,
        )
        self.assertEqual(plan["wireKind"], "PIN_TO_CELL")
        self.assertEqual(plan["policyTypeId"], "AIC_UECellSteering_1.0.0")
        self.assertEqual(plan["configurationAxes"], ["servingCell"])
        self.assertEqual(
            plan["mandatoryPredicates"],
            ["dl-prb-headroom", "ue-throughput-floor"],
        )
        self.assertEqual(plan["externalCalls"], 0)

    def test_qos_and_tsp_dry_run_keeps_both_components_in_one_trial(self) -> None:
        plan = self._run("QoSandTSP")
        self.assertEqual(plan["wireKind"], "PIN_TO_CELL")
        self.assertTrue(plan["jointTrialRequired"])
        self.assertEqual(
            plan["componentPredicates"],
            {
                "QoSTarget": ["dl-prb-headroom", "ue-throughput-floor"],
                "TrafficSteeringPreference": [
                    "serving-cell-preferred-min",
                    "serving-cell-preferred-max",
                ],
            },
        )
        self.assertEqual(
            set(plan["mandatoryPredicates"]),
            {
                "dl-prb-headroom",
                "ue-throughput-floor",
                "serving-cell-preferred-min",
                "serving-cell-preferred-max",
            },
        )
        self.assertEqual(plan["targetOptionCount"], 1)
        self.assertEqual(plan["externalCalls"], 0)

    def test_qos_accepts_either_direction_and_prints_the_requested_plan(self) -> None:
        for target in (12345678, 87654321):
            with self.subTest(target=target), redirect_stdout(output := io.StringIO()):
                status = run([
                    "--objective", "QoSTarget", "--dry-run",
                    "--target-nci", str(target),
                ])
            self.assertEqual(status, 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["baselineConfiguration"]["servingCell"],
                             str(87654321 if target == 12345678 else 12345678))
            self.assertEqual(plan["targetConfiguration"]["servingCell"], str(target))

    def test_live_qos_crosses_the_old_structural_gate(self) -> None:
        for family in ("QoSTarget", "QoSandTSP"):
            with self.subTest(family=family), ExitStack() as stack:
                reached = "live integration values were requested"
                stack.enter_context(patch(
                    "tools.g3ota.run_ota.load_integration_values",
                    side_effect=AssertionError(reached),
                ))
                for path in self._FORBIDDEN_PROCESS_CALLS:
                    stack.enter_context(patch(
                        path,
                        side_effect=AssertionError(
                            f"live refusal crossed into external I/O at {path}"
                        ),
                    ))
                with self.assertRaisesRegex(AssertionError, reached):
                    run(["--objective", family])


if __name__ == "__main__":
    unittest.main()
