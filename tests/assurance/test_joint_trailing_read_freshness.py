"""A slow trailing-end read must not make the Kernel judge a live stream stale.

2026-09-15 OTA attempt 26: the live sitting reads the issued echo cohort over SSH at
``trailing-end`` (~5 s).  The joint runtime had made its last collection before that
read, so the Kernel judged a gap-free 1 Hz KPM stream 4.8 s after its newest sample
against a 1 s freshness bound and stopped three of four trials TELEMETRY_STALE.
"""

from unittest.mock import patch

from assurance.live.joint_runtime import SteeringParticipantSpec, build_joint_live_runtime
from assurance.objectives.joint import IntentSpec, SteeringAxisSpec, compose_joint
from gui.operator.sources.kernel_live import polling_plan
from tests.assurance.live_support import HOME_NCI, TARGET_NCI
from tests.assurance.test_live_pin_to_cell_driver import LiveRunFixture
from tools.g3ota.composition import LivePolicyBuilder, build_policy_type_discovery


class ASlowTrailingReadIsNotStale(LiveRunFixture):
    def run_trial(self, read_ms):
        self.build(stream_follows_policy=True)
        ue = str(self.identity.amf_ue_ngap_id)
        joint = compose_joint(
            intents=[IntentSpec("I1", "UELevelTarget", ue, HOME_NCI, TARGET_NCI)],
            steering_axes=[SteeringAxisSpec(ue, HOME_NCI, (HOME_NCI, TARGET_NCI))],
            deployment_binding=self.binding.r1.deployment, budget_trials=1)
        discovery = build_policy_type_discovery(
            self.testbed, policy_type_id=self.binding.r1.policy_type_id,
            capability_manifest=self.capability)

        def factory(kernel, composed, participant):
            minimum, maximum = composed.steering_measurements[ue]
            return LivePolicyBuilder(
                kernel=kernel,
                contracts={"measurement_min": minimum, "measurement_max": maximum,
                           "target": composed.bundle.target,
                           "case_policy": composed.bundle.case_policy,
                           "harm": composed.bundle.harm,
                           "actuator": composed.actuators_by_axis[participant.axis]},
                deployment=self.deployment, identity=self.identity,
                case_id="case/joint-trailing", policy_type_discovery=discovery,
                capability_manifest=self.capability,
                axis=participant.axis, scope_key=f"ue@{ue}")

        participant = SteeringParticipantSpec(
            ue_id=ue, identity=self.identity, policy_port=self.testbed,
            policy_builder_factory=factory)
        runtime = build_joint_live_runtime(
            joint=joint, binding=self.binding, steering=[participant],
            reader=self.reader, now=self.testbed.now,
            monotonic_ms=self.testbed.monotonic_ms, sleep_ms=self.testbed.sleep_ms,
            case_id="case/joint-trailing", counter_sample_loaders={})
        candidate = next(c for c in runtime.kernel.current_catalog().candidates
                         if c.parameters.get(participant.axis) == str(TARGET_NCI))
        plan = polling_plan(runtime.kernel)

        def on_phase(name):
            if name == "trailing-end":
                self.testbed.sleep_ms(read_ms)   # the SSH cohort read on the radio

        _, report = runtime.run_candidate(
            candidate.candidate_id, observation_polls=plan.observation_polls,
            cadence_ms=plan.cadence_ms, settle_ms=250, on_phase=on_phase)
        return report

    def test_a_five_second_trailing_read_is_judged_on_fresh_measurements(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            report = self.run_trial(5000)
        self.assertNotEqual("TELEMETRY_STALE", getattr(report.stop_reason, "value", None))
        self.assertEqual("SETTLED_SUCCESS", report.terminal_state.value)

    def test_the_same_trial_without_the_delay_is_the_reference(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            report = self.run_trial(0)
        self.assertEqual("SETTLED_SUCCESS", report.terminal_state.value)
