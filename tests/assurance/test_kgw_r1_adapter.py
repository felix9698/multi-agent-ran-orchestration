"""The R1 adapter: gateway commands onto the official O-RAN path, no live call.

Design section 9 fixes the path an objective effect may take, and Gate 2 is
hardware-free (design section 15).  So this file proves the *mapping* -- which
R1 request each gateway command produces, and what the adapter does with the
answer -- against the project's real ``oran.rapp.r1_client.R1Client`` driven by
an injected transport that never opens a socket.  ``socket.socket`` is replaced
for the whole module, so a live call would fail the test rather than reach the
testbed.

The claim that needs the most care is task section 7.6: "A1 create, E2 ACK 또는
command acceptance만으로 success를 선언하지 않고 계약된 actual effect readback과
correlated evidence를 요구한다."  The frozen ``AIC_UECellSteering_1.0.0`` status
object states the distinction in its own fields, and the adapter honours it: an
acceptance is evidence, a verified readback is effect.
"""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping
from unittest.mock import patch

from decision.intent_model import (
    ConstraintType, Intent, IntentPriority, IntentScope, IntentTarget, IntentType,
)
from oran.rapp.contract_support import _bundle_dir
from oran.rapp.policy_translator import (
    POLICY_TYPE_ID, PolicyTranslationContext, translate_intent,
)
from oran.rapp.r1_client import HttpResponse, R1Client, R1Refusal

from assurance.gateway.commands import GatewayOperation, build_command
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.plan import config_hash
from assurance.gateway.r1_adapter import R1Adapter, project_verified_readback
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayOutcome
from tests.assurance.kgw_support import TestClock, token

POLICY_BASE = "/a1-policy-management/v1"
_REAL_SOCKET = socket.socket


def setUpModule():
    """No test in this module may open a socket."""
    def refuse(*args, **kwargs):
        raise AssertionError("a Gate 2 test attempted a live network call")

    patcher = patch.object(socket, "socket", refuse)
    patcher.start()
    globals()["_socket_patcher"] = patcher


def tearDownModule():
    globals()["_socket_patcher"].stop()


class FakeDeployment:
    """A Non-RT RIC that keeps a serving cell, reachable only through R1."""

    def __init__(self, golden: Mapping[str, Any], bundle: Path) -> None:
        self.golden = golden
        self.requests: List[Dict[str, Any]] = []
        self.policies: Dict[str, Dict[str, Any]] = {}
        self.next_policy_id = 1
        #: Every readback request, so a test can show what the adapter hands
        #: a status-based readback rather than assuming the scope is enough.
        self.readback_calls: list[Dict[str, Any]] = []
        cells = golden["policy"]["steeringObjective"]["actionEnvelope"]["allowedCells"]
        self.home_cell, self.target_cell = cells[0], cells[1]
        self.serving_cell = self.home_cell
        self.policy_type = {
            "policyTypeId": POLICY_TYPE_ID,
            "policySchema": json.loads(
                (bundle / "AIC_UECellSteering_1.0.0.policy.schema.json").read_text()
            ),
            "statusSchema": json.loads(
                (bundle / "AIC_UECellSteering_1.0.0.status.schema.json").read_text()
            ),
        }
        #: Flipped by a test to make every transport call fail, so the
        #: client's bounded retries are exhausted rather than papering over it.
        self.fail_all = False
        #: The real PIN_TO_CELL producer: a DELETE ends the policy, the UE stays.
        self.delete_keeps_cell = False
        #: A UE that ignores a later steering write (the handover back fails).
        self.ignore_updates = False

    # -- the Transport protocol -------------------------------------------

    def request(self, method, url, *, headers, body, timeout) -> HttpResponse:
        path = url.split("127.0.0.1:9", 1)[-1].split("?", 1)[0]
        parsed = json.loads(body.decode("utf-8")) if body else None
        self.requests.append(
            {"method": method, "path": path, "version": headers.get("Version"),
             "body": parsed}
        )
        if self.fail_all:
            raise OSError("injected transport failure")
        version = {"Version": headers.get("Version", "")}
        if method == "GET" and path == f"{POLICY_BASE}/policy-types/{POLICY_TYPE_ID}":
            return HttpResponse(200, version, self.policy_type)
        if method == "POST" and path == f"{POLICY_BASE}/policies":
            policy_id = f"pol-{self.next_policy_id}"
            self.next_policy_id += 1
            self.policies[policy_id] = parsed["policyObject"]
            self._enforce(parsed["policyObject"])
            return HttpResponse(
                201, dict(version, Location=f"{POLICY_BASE}/policies/{policy_id}"), {}
            )
        if method == "PUT" and path.startswith(f"{POLICY_BASE}/policies/"):
            policy_id = path.rsplit("/", 1)[-1]
            self.policies[policy_id] = parsed
            if not self.ignore_updates:
                self._enforce(parsed)
            return HttpResponse(200, version, {})
        if method == "DELETE" and path.startswith(f"{POLICY_BASE}/policies/"):
            self.policies.pop(path.rsplit("/", 1)[-1], None)
            if not self.delete_keeps_cell:
                self.serving_cell = self.home_cell
            return HttpResponse(204, version, None)
        if method == "GET" and path.endswith("/status"):
            return HttpResponse(200, version, self._status())
        return HttpResponse(404, version, {"path": path})

    # -- the deployment ----------------------------------------------------

    def _enforce(self, policy_object: Mapping[str, Any]) -> None:
        self.serving_cell = policy_object["steeringObjective"]["actionEnvelope"][
            "allowedCells"
        ][0]

    def _status(self) -> Dict[str, Any]:
        status = json.loads(json.dumps(self.golden["appliedVerifiedStatus"]))
        status["aicStatus"]["readback"]["observedServingCell"] = self.serving_cell
        return status

    def acceptance_only_status(self) -> Dict[str, Any]:
        """An enforced policy whose episode carries no verified readback.

        The frozen golden ``noActionStatus``: schema-valid, accepted, and with
        nothing in it that observes the contracted effect.
        """
        return json.loads(json.dumps(self.golden["noActionStatus"]))

    def readback(
        self,
        *,
        scope: Mapping[str, Any],
        transaction_id: str,
        policy_id: str | None,
    ):
        """The contracted effect readback Gate 3 wires to KPM/O1 evidence.

        Records what it was asked with, so a test can show that the adapter
        hands over the transaction binding a status-based readback needs
        rather than the scope alone.
        """
        self.readback_calls.append(
            {
                "scope": dict(scope),
                "transactionId": transaction_id,
                "policyId": policy_id,
            }
        )
        return {"servingCell": self.serving_cell}


class R1Fixture(unittest.TestCase):
    """One adapter over one fake deployment, built the way Gate 3 will."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = _bundle_dir()
        cls.golden = json.loads(
            (cls.bundle / "golden/golden-vectors.1.0.0.json").read_text(encoding="utf-8")
        )["canonicalObjects"]

    def setUp(self):
        self._built: Dict[str, Dict[str, Any]] = {}
        self.deployment = FakeDeployment(self.golden, self.bundle)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = R1Client(
            api_root="http://127.0.0.1:9/",
            r_app_id="assurance-gateway",
            policy_evidence_push_base_uri="http://127.0.0.1:9/push",
            state_path=Path(directory.name) / "r1-state.json",
            transport=self.deployment,
            insecure_dev=True,
        )
        self.discovery = {
            "policyTypeIds": [POLICY_TYPE_ID],
            "policyTypeObject": self.deployment.policy_type,
        }
        self.adapter = R1Adapter(
            policy_port=self.client,
            policy_builder=self.build_policy,
            near_rt_ric_id="nearrt-1",
            policy_type_id=POLICY_TYPE_ID,
            readback_port=self.deployment.readback,
        )

    # -- the policy builder Gate 3 binds to translate_intent ---------------

    def build_policy(self, command: Mapping[str, Any]) -> Dict[str, Any]:
        cached = self._built.get(json.dumps(command["value"], sort_keys=True))
        if cached is not None:
            return json.loads(json.dumps(cached))
        policy = self.golden["policy"]
        intent = Intent(
            id=policy["trace"]["intentId"],
            type=IntentType.THROUGHPUT_GOAL,
            target=IntentTarget("throughput", ConstraintType.MIN, 1.0, unit="Mbps"),
            scope=IntentScope(ue_ids=["ue-fixture"]),
            priority=IntentPriority.HIGH,
        )
        context = PolicyTranslationContext(
            ue_id=policy["scope"]["ueId"],
            allowed_cells=[command["value"]],
            forbidden_cells=[],
            objective_kind="PIN_TO_CELL",
            improvement_threshold_prb=None,
            min_seconds_between_actuations=30,
            required_kpi_freshness_ms=3000,
            action_deadline_ms=10000,
            not_before=policy["validity"]["notBefore"],
            expires_at=policy["validity"]["expiresAt"],
            rollback_on=policy["rollbackPolicy"]["on"],
            rollback_timeout_ms=10000,
            intent_revision=1,
            policy_revision=1,
            correlation_id=policy["trace"]["correlationId"],
            producer_id=policy["trace"]["producerId"],
        )
        built = translate_intent(
            intent,
            policy_type_discovery=self.discovery,
            capability_manifest=self.golden["capabilityManifest"],
            context=context,
        )
        self._built[json.dumps(command["value"], sort_keys=True)] = built
        return json.loads(json.dumps(built))

    def command(self, operation: GatewayOperation, **kwargs):
        return build_command(
            token(TokenKind.COMMIT), operation, scope={"guAmfUeNgapId": "ue-1"}, **kwargs
        )

    def dispatch(self, operation: GatewayOperation, **kwargs):
        return self.adapter.dispatch(
            token=token(TokenKind.COMMIT), command=self.command(operation, **kwargs)
        )

    def paths(self, method: str) -> List[str]:
        return [r["path"] for r in self.deployment.requests if r["method"] == method]


class EachCommandMapsToOneR1Request(R1Fixture):

    def test_validate_discovers_and_builds_but_creates_nothing(self):
        result = self.dispatch(
            GatewayOperation.VALIDATE, axis="servingCell", value=self.deployment.target_cell
        )
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(
            self.paths("GET"), [f"{POLICY_BASE}/policy-types/{POLICY_TYPE_ID}"]
        )
        self.assertEqual(self.paths("POST"), [])
        self.assertEqual(self.deployment.policies, {})
        self.assertEqual(self.deployment.serving_cell, self.deployment.home_cell)

    def test_apply_creates_the_translated_policy_and_binds_it_to_the_transaction(self):
        result = self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        created = [r for r in self.deployment.requests if r["method"] == "POST"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["path"], f"{POLICY_BASE}/policies")
        self.assertEqual(created[0]["version"], "1.0.0")
        self.assertEqual(
            created[0]["body"],
            {
                "policyObject": self.build_policy(
                    self.command(
                        GatewayOperation.APPLY,
                        axis="servingCell",
                        value=self.deployment.target_cell,
                    )
                ),
                "nearRtRicId": "nearrt-1",
                "policyTypeId": POLICY_TYPE_ID,
            },
        )
        self.assertEqual(self.adapter.bound_policy("tx-1"), "pol-1")
        self.assertIn("r1:policy:pol-1", result.evidence_refs)

    def test_an_acceptance_carries_no_configuration_digest(self):
        """Design section 9: an A1 create is evidence, not effect."""
        result = self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        self.assertIsNone(result.observed_config_hash)

    def test_a_second_apply_on_one_transaction_updates_rather_than_re_creates(self):
        self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.home_cell
        )
        self.assertEqual(len(self.paths("POST")), 1)
        self.assertEqual(self.paths("PUT"), [f"{POLICY_BASE}/policies/pol-1"])
        self.assertEqual(self.adapter.bound_policy("tx-1"), "pol-1")

    def test_undo_and_halt_withdraw_the_bound_policy_and_release_the_scope(self):
        for operation in (GatewayOperation.UNDO, GatewayOperation.HALT):
            with self.subTest(operation=operation.value):
                self.setUp()
                self.dispatch(
                    GatewayOperation.APPLY,
                    axis="servingCell",
                    value=self.deployment.target_cell,
                )
                kwargs = (
                    {"axis": "servingCell", "value": self.deployment.home_cell}
                    if operation is GatewayOperation.UNDO
                    else {}
                )
                result = self.adapter.dispatch(
                    token=token(TokenKind.COMMIT),
                    command=self.command(operation, **kwargs),
                )
                self.assertIs(result.outcome, GatewayOutcome.ACKED)
                self.assertEqual(self.paths("DELETE"), [f"{POLICY_BASE}/policies/pol-1"])
                self.assertIsNone(self.adapter.bound_policy("tx-1"))
                self.assertEqual(self.deployment.policies, {})

    def test_withdrawing_nothing_calls_nothing(self):
        result = self.dispatch(GatewayOperation.HALT)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.deployment.requests, [])

    def test_the_policy_id_comes_only_from_the_token_binding(self):
        self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        for command in self.deployment.requests:
            self.assertNotIn("policyId", json.dumps(command["body"] or {}))
        other = self.adapter.dispatch(
            token=token(TokenKind.COMMIT, transaction_id="tx-2"),
            command=build_command(
                token(TokenKind.COMMIT, transaction_id="tx-2"),
                GatewayOperation.HALT,
                scope={"guAmfUeNgapId": "ue-1"},
            ),
        )
        self.assertIn("nothing to withdraw", other.detail)
        self.assertEqual(self.paths("DELETE"), [])


class TheProducersRollbackGateIsAskedAgain(R1Fixture):
    """2026-09-23: a 409 "no unique fresh KPM/header" on a rollback DELETE wrote
    nothing and is a momentary state of the KPM stream; 9 of them on 8 boards,
    never retried, each ended the board in a lockdown."""

    GATE = ("DELETE rollback gate failed: UE scope has no unique fresh KPM/header")

    def refusing(self, *messages):
        adapter = R1Adapter(
            policy_port=self.client, policy_builder=self.build_policy,
            near_rt_ric_id="nearrt-1", policy_type_id=POLICY_TYPE_ID,
            readback_port=self.deployment.readback, refusal_errors=(R1Refusal,),
        )
        adapter._gate_sleep = lambda seconds: None
        real, queue, self.attempts = self.client.delete_policy, list(messages), []

        def delete_policy(policy_id):
            self.attempts.append(policy_id)
            if queue:
                raise R1Refusal(queue.pop(0), status=409)
            return real(policy_id)

        self.client.delete_policy = delete_policy
        adapter.dispatch(token=token(TokenKind.COMMIT), command=self.command(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell))
        return adapter

    def test_the_gate_refusal_is_retried_until_the_delete_lands(self):
        adapter = self.refusing(self.GATE, self.GATE)
        result = adapter.dispatch(token=token(TokenKind.COMMIT),
                                  command=self.command(GatewayOperation.HALT))
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(len(self.attempts), 3)
        self.assertEqual(self.deployment.policies, {})

    def test_the_retries_are_bounded(self):
        adapter = self.refusing(*[self.GATE] * R1Adapter.GATE_RETRY_ATTEMPTS)
        result = adapter.dispatch(token=token(TokenKind.COMMIT),
                                  command=self.command(GatewayOperation.HALT))
        self.assertIsNot(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(len(self.attempts), R1Adapter.GATE_RETRY_ATTEMPTS)

    def test_any_other_refusal_is_not_retried(self):
        adapter = self.refusing("revision conflict")
        result = adapter.dispatch(token=token(TokenKind.COMMIT),
                                  command=self.command(GatewayOperation.HALT))
        self.assertIsNot(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(len(self.attempts), 1)


class AnAcceptanceIsNotAnEffect(R1Fixture):
    """Task section 7.6 and design section 9."""

    def test_a_read_without_a_contracted_readback_is_unknown(self):
        adapter = R1Adapter(
            policy_port=self.client,
            policy_builder=self.build_policy,
            near_rt_ric_id="nearrt-1",
            policy_type_id=POLICY_TYPE_ID,
        )
        result = adapter.dispatch(
            token=token(TokenKind.COMMIT), command=self.command(GatewayOperation.READ)
        )
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIn("acceptance is not one", result.detail)

    def test_a_read_reports_the_contracted_readback_digest(self):
        self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        result = self.dispatch(GatewayOperation.READ)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(
            result.observed_config_hash,
            config_hash({"servingCell": self.deployment.target_cell}),
        )

    def test_finalize_needs_a_verified_readback_in_the_status(self):
        self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        acknowledged = self.deployment.acceptance_only_status()
        self.assertEqual(acknowledged["enforceStatus"], "ENFORCED")
        self.assertIsNone(project_verified_readback(acknowledged))
        with patch.object(self.deployment, "_status", return_value=acknowledged):
            result = self.dispatch(GatewayOperation.FINALIZE)
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIn("verified readback", result.detail)

    def _expired(self, rollback_state: str) -> Dict[str, Any]:
        # 2026-09-19 board 110539: the handover was verified, then the policy
        # ran out its validity before FINALIZE -- NOT_ENFORCED/EXPIRED.
        status = self.deployment._status()
        status["enforceStatus"] = "NOT_ENFORCED"
        status["enforceReason"] = "OTHER_REASON"
        status["aicStatus"]["policyState"] = "EXPIRED"
        status["aicStatus"]["policyTerminal"] = True
        status["aicStatus"]["episodeTerminal"] = True
        status["aicStatus"]["rollback"] = {"state": rollback_state}
        return status

    def test_an_expired_policy_after_a_verified_readback_still_finalizes(self):
        self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        with patch.object(self.deployment, "_status",
                          return_value=self._expired("NOT_REQUESTED")):
            result = self.dispatch(GatewayOperation.FINALIZE)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(result.observed_config_hash,
                         config_hash({"servingCell": self.deployment.target_cell}))

    def test_an_expired_policy_with_a_rollback_does_not_finalize(self):
        self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        with patch.object(self.deployment, "_status",
                          return_value=self._expired("ROLLED_BACK_VERIFIED")):
            result = self.dispatch(GatewayOperation.FINALIZE)
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)

    def test_finalize_succeeds_on_a_verified_readback(self):
        self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        result = self.dispatch(GatewayOperation.FINALIZE)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(
            result.observed_config_hash,
            config_hash({"servingCell": self.deployment.target_cell}),
        )

    def test_the_projection_reads_the_status_fields_that_carry_the_distinction(self):
        verified = self.golden["appliedVerifiedStatus"]
        self.assertFalse(verified["aicStatus"]["control"]["resultIsEffectEvidence"])
        self.assertEqual(verified["aicStatus"]["control"]["result"], "ACK")
        self.assertEqual(
            project_verified_readback(verified),
            {"servingCell": verified["aicStatus"]["readback"]["observedServingCell"]},
        )


class ATransportFailureIsNeverAConfirmedNonEvent(R1Fixture):

    def test_a_failed_write_is_unknown_rather_than_rejected(self):
        self.deployment.fail_all = True
        result = self.dispatch(
            GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell
        )
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIsNot(result.outcome, GatewayOutcome.REJECTED)

    def test_a_failed_read_is_an_error_because_it_changed_nothing(self):
        adapter = R1Adapter(
            policy_port=self.client,
            policy_builder=self.build_policy,
            near_rt_ric_id="nearrt-1",
            policy_type_id=POLICY_TYPE_ID,
            readback_port=self._raising_readback,
        )
        result = adapter.dispatch(
            token=token(TokenKind.COMMIT), command=self.command(GatewayOperation.READ)
        )
        self.assertIs(result.outcome, GatewayOutcome.ERROR)

    @staticmethod
    def _raising_readback(*, scope, transaction_id, policy_id):
        raise OSError("injected readback failure")


class TheGatewayDrivesTheR1AdapterEndToEnd(R1Fixture):
    """The skeleton wired the way Gate 3 will wire it, with no live call."""

    def setUp(self):
        super().setUp()
        self.clock = TestClock()
        self.baseline = {"servingCell": self.deployment.home_cell}
        self.gateway = TokenBoundWriteGateway(
            adapters={"r1": self.adapter},
            safe_state=dict(self.baseline),
            clock=self.clock,
        )
        self.plan = {
            "adapter": "r1",
            "scope": {"guAmfUeNgapId": "ue-1"},
            "baselineConfig": dict(self.baseline),
            "steps": [{"axis": "servingCell", "value": self.deployment.target_cell}],
        }
        self.baseline_hash = config_hash(self.baseline)
        self.applied_hash = config_hash({"servingCell": self.deployment.target_cell})

    def test_prepare_through_finalize_over_the_official_path(self):
        prepared = self.gateway.prepare(
            token=token(TokenKind.PREPARE, sequence=0, expected=self.baseline_hash),
            plan=self.plan,
        )
        self.assertIs(prepared.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.paths("POST"), [], "prepare created a policy")

        ready = self.gateway.ready(
            token=token(TokenKind.READY, sequence=1, expected=self.baseline_hash)
        )
        self.assertIs(ready.outcome, GatewayOutcome.ACKED)

        committed = self.gateway.commit(
            token=token(TokenKind.COMMIT, sequence=2, expected=self.baseline_hash)
        )
        self.assertIs(committed.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.deployment.serving_cell, self.deployment.target_cell)
        self.assertEqual(self.paths("POST"), [f"{POLICY_BASE}/policies"])

        finalized = self.gateway.finalize_live(
            token=token(TokenKind.FINALIZE_LIVE, sequence=3, expected=self.applied_hash)
        )
        self.assertIs(finalized.outcome, GatewayOutcome.ACKED)

    def test_the_readback_is_asked_with_the_transaction_binding(self):
        """A status-based readback cannot be reached from the scope alone.

        Before a policy exists the question is "what is this UE on now" and
        there is no policy id to answer it with.  Once one is bound, the
        contracted readback is the one that policy's status carries, and
        reaching it needs the id.  Both are asked here.
        """
        self.gateway.prepare(
            token=token(TokenKind.PREPARE, sequence=0, expected=self.baseline_hash),
            plan=self.plan,
        )
        before = [
            call for call in self.deployment.readback_calls if call["policyId"] is None
        ]
        self.assertTrue(before)
        self.assertEqual(before[0]["transactionId"], "tx-1")
        self.assertEqual(before[0]["scope"], {"guAmfUeNgapId": "ue-1"})

        self.gateway.ready(
            token=token(TokenKind.READY, sequence=1, expected=self.baseline_hash)
        )
        self.gateway.commit(
            token=token(TokenKind.COMMIT, sequence=2, expected=self.baseline_hash)
        )

        bound = self.adapter.bound_policy("tx-1")
        self.assertIsNotNone(bound)
        after = [
            call
            for call in self.deployment.readback_calls
            if call["policyId"] is not None
        ]
        self.assertTrue(after)
        self.assertEqual({call["policyId"] for call in after}, {bound})

    def test_a_rollback_withdraws_the_policy_and_restores_the_cell(self):
        self.gateway.prepare(
            token=token(TokenKind.PREPARE, sequence=0, expected=self.baseline_hash),
            plan=self.plan,
        )
        self.gateway.ready(
            token=token(TokenKind.READY, sequence=1, expected=self.baseline_hash)
        )
        self.gateway.commit(
            token=token(TokenKind.COMMIT, sequence=2, expected=self.baseline_hash)
        )
        rolled = self.gateway.reverse_rollback(
            token=token(
                TokenKind.REVERSE_ROLLBACK, sequence=3, expected=self.applied_hash
            )
        )
        self.assertIs(rolled.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.deployment.serving_cell, self.deployment.home_cell)
        self.assertEqual(self.deployment.policies, {})
        self.assertIsNone(self.adapter.bound_policy("tx-1"))


class AHandoverIsUndoneByHandingBackNotByWithdrawing(TheGatewayDrivesTheR1AdapterEndToEnd):
    """2026-09-19 boards 093728 and 110539: DELETE left ue1 on the target cell."""

    def setUp(self):
        super().setUp()
        self.deployment.delete_keeps_cell = True
        self.adapter.restore_by_handover = True

    def commit(self, value):
        plan = dict(self.plan, steps=[{"axis": "servingCell", "value": value}])
        self.gateway.prepare(
            token=token(TokenKind.PREPARE, sequence=0, expected=self.baseline_hash), plan=plan)
        self.gateway.ready(token=token(TokenKind.READY, sequence=1, expected=self.baseline_hash))
        self.gateway.commit(token=token(TokenKind.COMMIT, sequence=2, expected=self.baseline_hash))

    def rollback(self, expected):
        return self.gateway.reverse_rollback(
            token=token(TokenKind.REVERSE_ROLLBACK, sequence=3, expected=expected))

    def test_the_rollback_pins_the_baseline_then_withdraws(self):
        self.commit(self.deployment.target_cell)
        self.assertIs(self.rollback(self.applied_hash).outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.deployment.serving_cell, self.deployment.home_cell)
        puts = [r for r in self.deployment.requests if r["method"] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"]["steeringObjective"]["actionEnvelope"]["allowedCells"],
                         [self.deployment.home_cell])
        self.assertEqual(self.deployment.policies, {})

    def test_a_halt_naming_the_baseline_hands_back_too(self):
        self.commit(self.deployment.target_cell)
        halt = build_command(token(TokenKind.STOP, sequence=3), GatewayOperation.HALT,
                             scope={"guAmfUeNgapId": "ue-1"})
        halt.update(axis="servingCell", value=self.deployment.home_cell)
        result = self.adapter.dispatch(token=token(TokenKind.STOP, sequence=3), command=halt)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.deployment.serving_cell, self.deployment.home_cell)
        self.assertEqual(self.paths("DELETE"), [f"{POLICY_BASE}/policies/pol-1"])

    def test_a_ue_already_at_its_baseline_is_simply_withdrawn(self):
        self.commit(self.deployment.home_cell)
        self.rollback(self.baseline_hash)
        self.assertEqual([r for r in self.deployment.requests if r["method"] == "PUT"], [])
        self.assertEqual(self.paths("DELETE"), [f"{POLICY_BASE}/policies/pol-1"])

    def test_a_ue_that_does_not_come_back_is_a_partial_apply(self):
        self.deployment.ignore_updates = True
        self.commit(self.deployment.target_cell)
        rolled = self.rollback(self.applied_hash)
        self.assertIs(rolled.outcome, GatewayOutcome.PARTIAL_APPLY)
        self.assertEqual(self.deployment.serving_cell, self.deployment.target_cell)
        self.assertEqual(self.paths("DELETE"), [], "withdrew a policy that never handed back")


    def test_a_hand_back_lost_at_halt_is_written_again_by_the_rollback(self):
        """2026-09-27 board 809: ue3 lost its link on the target cell as the HALT hand-back
        went out, re-established there, and the rollback only re-read -- lockdown."""
        self.commit(self.deployment.target_cell)
        self.deployment.ignore_updates = True                 # the HALT command is lost
        halt = build_command(token(TokenKind.STOP, sequence=3), GatewayOperation.HALT,
                             scope={"guAmfUeNgapId": "ue-1"})
        halt.update(axis="servingCell", value=self.deployment.home_cell)
        self.adapter.dispatch(token=token(TokenKind.STOP, sequence=3), command=halt)
        self.assertEqual(self.deployment.serving_cell, self.deployment.target_cell)
        self.deployment.ignore_updates = False                # re-established, reachable again
        rolled = self.gateway.reverse_rollback(
            token=token(TokenKind.REVERSE_ROLLBACK, sequence=4, expected=self.applied_hash))
        self.assertIs(rolled.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.deployment.serving_cell, self.deployment.home_cell)
        self.assertEqual(2, len([r for r in self.deployment.requests if r["method"] == "PUT"]))


class AHandBackRewriteClearsTheProducersRevision(AHandoverIsUndoneByHandingBackNotByWithdrawing):
    """2026-09-27 board 818: the UNDO re-write was refused AIC_STALE_REVISION -- the producer
    held a revision above the HALT's draft and a journal-less adapter could not know it."""

    def test_the_rewrite_goes_out_above_the_live_revision(self):
        original = self.adapter._build                         # noqa: SLF001
        seeds = []

        def seeded(command, **kwargs):
            seeds.append(kwargs)
            return original(command)
        self.adapter._build = seeded                           # noqa: SLF001
        self.adapter._builder_takes_last_revision = True       # noqa: SLF001
        self.commit(self.deployment.target_cell)
        self.deployment.ignore_updates = True
        halt = build_command(token(TokenKind.STOP, sequence=3), GatewayOperation.HALT,
                             scope={"guAmfUeNgapId": "ue-1"})
        halt.update(axis="servingCell", value=self.deployment.home_cell)
        self.adapter.dispatch(token=token(TokenKind.STOP, sequence=3), command=halt)
        self.deployment.ignore_updates = False
        live = {"trace": {"revision": 40, "fencingToken": 50}}
        with patch.object(self.adapter._port, "get_policy", return_value=live):   # noqa: SLF001
            rolled = self.gateway.reverse_rollback(
                token=token(TokenKind.REVERSE_ROLLBACK, sequence=4, expected=self.applied_hash))
        self.assertIn({"last_revision": 40, "last_fencing_token": 50}, seeds)
        self.assertIs(rolled.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.deployment.serving_cell, self.deployment.home_cell)


class APolicyTheProducerRefusedWithoutWritingIsReadThePrePolicyWay(R1Fixture):
    """2026-09-20, board 20260919T193249 trial 6: AIC_SCOPE_NOT_FOUND at ADMISSION,
    writeMayHaveOccurred false -- the status never says VERIFIED, and reading
    through it answered UNKNOWN until the trial locked down."""

    def refused_status(self, may_have_written):
        status = self.deployment._status()
        status["aicStatus"]["readback"] = None
        status["aicStatus"]["error"] = {"code": "AIC_SCOPE_NOT_FOUND", "retryable": True,
                                        "stage": "ADMISSION",
                                        "writeMayHaveOccurred": may_have_written}
        return status

    def test_a_refusal_that_wrote_nothing_reads_without_the_policy(self):
        self.dispatch(GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell)
        self.deployment.serving_cell = self.deployment.home_cell      # nothing reached the radio
        original = self.deployment.readback
        # Through the policy the readback cannot answer (its status never verifies).
        self.adapter._readback = (  # noqa: SLF001
            lambda **kw: None if kw["policy_id"] else original(**kw))
        with patch.object(self.client, "get_policy_status",
                          return_value=self.refused_status(False)):
            result = self.dispatch(GatewayOperation.READ)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(result.observed_config_hash,
                         config_hash({"servingCell": self.deployment.home_cell}))

    def test_a_handover_the_radio_never_made_is_read_from_the_counter(self):
        """2026-09-20 boards 132028 and 140425.

        The policy was created, gNB1 sent Handover Required, the AMF answered
        with a Handover Command -- and ue3 never appeared on the target cell.
        The policy status cannot verify a move that did not happen, so the
        corroborated readback answered nothing and the Kernel locked the board
        down.  The independent counter showed the UE on its baseline the whole
        time; that is what recovery has to prove, so the read falls back to it.
        """
        self.adapter.restore_by_handover = True    # the steering adapter, as joint_runtime builds it
        self.dispatch(GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell)
        self.deployment.serving_cell = self.deployment.home_cell   # the UE never moved
        original = self.deployment.readback
        self.adapter._readback = (  # noqa: SLF001
            lambda **kw: None if kw["policy_id"] else original(**kw))
        result = self.dispatch(GatewayOperation.READ)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(result.observed_config_hash,
                         config_hash({"servingCell": self.deployment.home_cell}))
        self.assertIn("the radio did not move", result.detail)

    def test_a_supplementary_axis_still_fails_closed_without_a_readback(self):
        """Only steering can be commanded and disobeyed; a cap takes effect where
        it is written, so a missing readback there stays an unknown."""
        self.adapter.restore_by_handover = False
        self.dispatch(GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell)
        self.adapter._readback = lambda **kw: None       # noqa: SLF001
        result = self.dispatch(GatewayOperation.READ)
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)

    def test_a_failure_that_may_have_written_still_reads_through_the_policy(self):
        self.dispatch(GatewayOperation.APPLY, axis="servingCell", value=self.deployment.target_cell)
        self.adapter._readback = lambda **kw: None       # noqa: SLF001 - never verifies
        with patch.object(self.client, "get_policy_status",
                          return_value=self.refused_status(True)):
            result = self.dispatch(GatewayOperation.READ)
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)          # fail-closed


class NoLiveCallHappensAnywhereInThisModule(unittest.TestCase):

    def test_the_socket_constructor_is_replaced_for_the_whole_module(self):
        self.assertIsNot(socket.socket, _REAL_SOCKET)
        with self.assertRaises(AssertionError):
            socket.socket()


if __name__ == "__main__":
    unittest.main()


class AUeBackOnItsBaselineIsProvenByTheCounterNotOnlyByThePolicy(
        AHandoverIsUndoneByHandingBackNotByWithdrawing):
    """2026-09-23: 38 hand-backs measured on the KPM stream -- 34 UEs came back, and
    14 of those were never seen through the policy status (re-registered under a
    new id, or the producer never verified the update).  The gateway reported
    "baseline not observed", left the policy at the RIC, and the trial locked down.
    """

    def _policy_path_blind(self):
        original = self.deployment.readback
        self.adapter._readback = (  # noqa: SLF001
            lambda **kw: None if kw["policy_id"] else original(**kw))

    def test_the_counter_proves_the_baseline_and_the_policy_is_withdrawn(self):
        self.commit(self.deployment.target_cell)
        self._policy_path_blind()
        rolled = self.rollback(self.applied_hash)
        self.assertIs(rolled.outcome, GatewayOutcome.ACKED, rolled.detail)
        self.assertEqual(self.deployment.serving_cell, self.deployment.home_cell)
        self.assertEqual(self.paths("DELETE"), [f"{POLICY_BASE}/policies/pol-1"],
                         "the UE is back; the policy must not be left at the RIC")

    def test_a_ue_that_really_did_not_come_back_is_still_not_withdrawn(self):
        self.deployment.ignore_updates = True       # the hand-back never happens
        self.commit(self.deployment.target_cell)
        self._policy_path_blind()
        rolled = self.rollback(self.applied_hash)
        self.assertIs(rolled.outcome, GatewayOutcome.PARTIAL_APPLY)
        self.assertEqual(self.paths("DELETE"), [], "withdrew a policy that never handed back")
