"""Exercise the real joint R1 readback wiring with independent fake KPM."""

import unittest
from unittest.mock import patch
from dataclasses import replace
from types import SimpleNamespace

from assurance.live.joint_runtime import SteeringParticipantSpec, build_joint_live_runtime
from assurance.objectives.joint import IntentSpec, SteeringAxisSpec, compose_joint
from tests.assurance.live_support import HOME_NCI, TARGET_NCI
from tests.assurance.test_live_pin_to_cell_driver import LiveRunFixture
from tools.g3ota.composition import LivePolicyBuilder, build_policy_type_discovery


class JointServingCellReadback(LiveRunFixture):
    def test_two_joint_policy_bodies_do_not_collide_and_retries_are_identical(self):
        self.build()
        ids = [str(self.identity.amf_ue_ngap_id), str(self.identity.amf_ue_ngap_id + 1)]
        candidate = replace(self.runtime.kernel.current_catalog().candidates[0],
                            parameters={f"servingCell@{ue}": str(TARGET_NCI) for ue in ids})
        trial_id = "case/joint:trial:1"
        state = {"trials": {trial_id: {"trialId": trial_id, "caseId": "case/joint",
                                     "candidateId": candidate.candidate_id}},
                 "transactions": {"tx": {"issuedAt": "2026-08-21T09:00:00.000000Z",
                                          "leaseExpiry": "2026-08-21T09:01:00.000000Z"}}}
        kernel = SimpleNamespace(current_catalog=lambda: SimpleNamespace(candidates=[candidate]),
                                 reduced_state=lambda: state)
        discovery = build_policy_type_discovery(
            self.testbed, policy_type_id=self.binding.r1.policy_type_id,
            capability_manifest=self.capability)
        bodies = []
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            for ue in ids:
                axis = f"servingCell@{ue}"
                builder = LivePolicyBuilder(
                    kernel=kernel, contracts=self.runtime.contracts,
                    deployment=replace(self.deployment, ue_scope_id=ue),
                    identity=replace(self.identity, amf_ue_ngap_id=int(ue)),
                    case_id="case/joint", policy_type_discovery=discovery,
                    capability_manifest=self.capability, axis=axis, scope_key=f"ue@{ue}")
                command = {"scope": {f"ue@{ue}": {"ueId": ue}}, "axis": axis,
                           "value": str(TARGET_NCI), "operation": "APPLY",
                           "trialId": trial_id, "transactionId": "tx"}
                body = builder(command)
                self.assertEqual(body, builder(command))
                bodies.append(body)
        self.assertNotEqual(bodies[0]["trace"]["idempotencyKey"],
                            bodies[1]["trace"]["idempotencyKey"])

    def joint_readback(self, *, stream_follows_policy):
        self.build(stream_follows_policy=stream_follows_policy)
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
                case_id="case/joint-readback", policy_type_discovery=discovery,
                capability_manifest=self.capability,
                axis=participant.axis, scope_key=f"ue@{ue}")

        participant = SteeringParticipantSpec(
            ue_id=ue, identity=self.identity, policy_port=self.testbed,
            policy_builder_factory=factory)
        runtime = build_joint_live_runtime(
            joint=joint, binding=self.binding, steering=[participant],
            reader=self.reader, now=self.testbed.now,
            monotonic_ms=self.testbed.monotonic_ms, sleep_ms=self.testbed.sleep_ms,
            case_id="case/joint-readback", counter_sample_loaders={})
        # A producer-verified handover and independent KPM use the same cell.
        # Exercise the readback constructed by the real composition root,
        # without substituting the hardware-free adapter override.
        created = self.testbed.create_policy("ric", self.binding.r1.policy_type_id, {})
        self.testbed.sleep_ms(self.testbed.apply_latency_ms + 1000)
        adapter = runtime.adapters[participant.key].inner
        return adapter._readback(
            scope={"ueId": ue}, transaction_id="tx/joint-readback",
            policy_id=created["policyId"]), participant.axis

    def test_matching_status_and_kpm_are_projected_to_the_scoped_axis(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            observed, axis = self.joint_readback(stream_follows_policy=True)
        self.assertEqual({axis: str(TARGET_NCI)}, observed)

    def test_verified_status_without_matching_kpm_stays_unknown(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            observed, _ = self.joint_readback(stream_follows_policy=False)
        self.assertIsNone(observed)


class TheSteeringScopeMayNameTheUeInEitherIdSpace(unittest.TestCase):
    """A joint sitting scopes by role label; a numeric sitting by amfUeNgapId.

    ``assurance/objectives/joint.py`` writes ``scope["ue@ue1"] = {"ueId": "ue1"}``
    and the agent's composition root keys the builder ``scope_key="ue@ue1"``, so
    the value that comes back is the ROLE LABEL.  The guard compared it only
    against ``identity.amf_ue_ngap_id`` -- a different id space -- so every
    steering command a joint sitting ever issued was refused at PREPARE with
    "the command's scope names a different UE than the observed identity".
    That is why the v4 condition never moved a cell at all.  An earlier note
    said "one cell change"; that counted a trial's REQUESTED configuration, and
    the guard had refused it.  Counted by the OBSERVED ``servingCell`` it is
    zero (2026-09-17: episode …1843 trial 2, c4630d27 trial 4, 0039b54c trial 6
    -- every one "prepare: REJECTED ... names a different UE").

    The guard's job is "this scope names MY UE", not "the scope uses the numeric
    space", so both spellings are accepted and everything else still refused.
    """

    def _builder(self):
        builder = LivePolicyBuilder.__new__(LivePolicyBuilder)
        builder.identity = SimpleNamespace(amf_ue_ngap_id=1)
        builder.scope_key = "ue@ue1"
        builder.axis = "servingCell@ue1"
        return builder

    def _call(self, scope):
        return self._builder()({"scope": scope, "axis": "servingCell@ue1",
                                "value": "12345678", "transactionId": "tx"})

    def _refused_on_scope(self, scope) -> bool:
        """True when the SCOPE guard refused; later stages raise other errors."""
        try:
            self._call(scope)
        except Exception as exc:  # noqa: BLE001 - the stage is what is under test
            return "names a different UE" in str(exc)
        return False

    def test_the_role_label_a_joint_sitting_writes_is_accepted(self) -> None:
        self.assertFalse(self._refused_on_scope({"ue@ue1": {"ueId": "ue1"}}))

    def test_the_numeric_id_a_plain_sitting_writes_is_still_accepted(self) -> None:
        self.assertFalse(self._refused_on_scope({"ue@ue1": {"ueId": 1}}))

    def test_another_ue_is_still_refused(self) -> None:
        self.assertTrue(self._refused_on_scope({"ue@ue1": {"ueId": "ue2"}}))

    def test_a_scope_without_this_builders_key_is_still_refused(self) -> None:
        self.assertTrue(self._refused_on_scope({"ue@ue9": {"ueId": "ue9"}}))

    def test_the_refusal_says_what_it_compared(self) -> None:
        """The bare sentence cost a night: it fires for two different faults."""
        try:
            self._call({"ue@ue9": {"ueId": "ue9"}})
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
        else:
            self.fail("the scope guard did not refuse")
        self.assertIn("ue@ue1", message)      # this builder's key
        self.assertIn("None", message)        # what the lookup yielded
        self.assertIn("ue@ue9", message)      # what the scope actually carried


class TheSteeringAdapterRecordsWhatItIssued(unittest.TestCase):
    """조종 축도 동작 저널을 받아야 한다.

    보조 축들은 처음부터 `<키>-operations.jsonl` 을 갖고 있었는데 조종만 없었다.
    그래서 2026-09-17 에 프로듀서의 `503 AIC_INTERNAL_ERROR` 를 **셀 수는 있어도
    잴 수는 없었다** — 유일하게 실패하는 축이 유일하게 기록이 없는 축이었다.
    (그 503 은 **UE 를 기준선 셀로 되돌리는 쓰기에서만** 난다: 복귀 10건 중 4건,
    이탈 204건과 무쓰기 237건은 0건, 213판에서 Fisher p=1.2e-07. 처음에는 "판의
    뒷자리" 로 보였는데 **복귀는 이탈 뒤에만 가능하니 늘 뒷자리**여서 생긴 그림자였다.
    프로듀서가 거절하기 전에 느려지는지는 쓰기 시간이 있어야만 말할 수 있다.)

    바인딩 저널은 **일부러** 넘기지 않는다 — 조종이 판을 넘어 durable 해지면
    조인 조립이 원하지 않는 상태가 생긴다.
    """

    def test_the_spec_carries_an_operation_journal_to_the_adapter(self):
        from dataclasses import fields, replace
        from assurance.gateway.r1_operation_journal import InMemoryR1OperationJournal
        from assurance.live.joint_runtime import SteeringParticipantSpec

        names = {f.name for f in fields(SteeringParticipantSpec)}
        self.assertIn("operation_journal", names)
        self.assertNotIn("binding_journal", names,
                         "조종은 판을 넘어 durable 해지면 안 된다")

        journal = InMemoryR1OperationJournal()
        spec = SteeringParticipantSpec(
            ue_id="ue1", identity=None, policy_port=None,
            policy_builder_factory=lambda *a, **k: None)
        self.assertIsNone(spec.operation_journal)
        self.assertIs(replace(spec, operation_journal=journal).operation_journal,
                      journal)

    def test_the_composition_root_attaches_one_per_steering_key(self):
        """합성 루트가 보조 축과 **같은 디렉터리·같은 이름 규칙**으로 단다."""
        import inspect
        from tools.liveconsole import agent as composition
        source = inspect.getsource(composition)
        self.assertIn('f"{spec.key}-operations.jsonl"', source)
        # 저널을 만드는 자리에서 바인딩 저널을 함께 넘기지 않는지
        block = source.split('f"{spec.key}-operations.jsonl"')[0][-600:]
        self.assertNotIn("binding_journal", block)
