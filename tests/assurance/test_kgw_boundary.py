"""The Write Gateway boundary: one door in, one path out, and no live call.

Design section 9: "The Write Gateway may orchestrate the registered O-RAN
clients/adapters, but it cannot silently replace the path with direct gNB
control."  Task section 7.3 adds the other half -- the GUI and the agents get no
direct E2/xApp write, no PRB/MCS/scheduler change, no SSH/Telnet, no process
lifecycle and no USRP power control.

A boundary that is only a convention is checked by whoever reviews the next
diff.  These tests check it mechanically: which modules may import an adapter,
what the gateway exposes, what a command must carry, and what the package is
allowed to import at all.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any, List

import assurance.gateway as gateway_package
from assurance.contracts.capability import ActuatorPath
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.mock_adapter import MockActuationAdapter
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import (
    GatewayOutcome,
    GatewayRefusal,
    GatewayResult,
    WriteGateway,
    WriteGatewayAdapter,
)
from tests.assurance.kgw_support import (
    BASELINE,
    SAFE_STATE,
    GatewayFixture,
    TestClock,
    token,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "assurance"
GATEWAY_ROOT = PACKAGE_ROOT / "gateway"

#: Modules that hold an adapter -- the only things that speak a transport.
ADAPTER_MODULES = {
    "assurance.gateway.mock_adapter",
    "assurance.gateway.r1_adapter",
    "assurance.gateway.registry",
}

#: Anything here inside ``assurance/gateway/`` would mean the gateway can reach
#: the equipment without going through a registered adapter.
FORBIDDEN_IN_THE_GATEWAY = {
    "socket", "subprocess", "telnetlib", "paramiko", "requests", "http",
    "urllib", "asyncio", "ssl", "oran.rapp", "oran.nonrt", "oran.o1",
    "executor", "collectors", "coordinator", "gui", "decision", "experiments",
}


def _imports(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names.append(node.module or "")
    return names


class OnlyTheGatewayCanReachAnAdapter(unittest.TestCase):

    def test_no_module_outside_the_gateway_package_imports_an_adapter(self):
        violations = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            if path.parent == GATEWAY_ROOT:
                continue
            for name in _imports(path):
                if name in ADAPTER_MODULES:
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{name}")
        self.assertEqual(violations, [], f"adapter reachable outside the gateway: {violations}")

    def test_the_package_does_not_re_export_an_adapter(self):
        for name in gateway_package.__all__:
            exported = getattr(gateway_package, name)
            with self.subTest(exported=name):
                self.assertFalse(
                    isinstance(exported, type)
                    and isinstance(getattr(exported, "actuator_path", None), ActuatorPath),
                    f"{name} is an adapter and must not be re-exported",
                )
        self.assertNotIn("MockActuationAdapter", gateway_package.__all__)
        self.assertNotIn("R1Adapter", gateway_package.__all__)

    def test_a_gateway_exposes_names_and_paths_but_never_an_adapter(self):
        adapter = MockActuationAdapter(config=BASELINE)
        gateway = TokenBoundWriteGateway(
            adapters={"mock": adapter}, safe_state=SAFE_STATE
        )
        for name in dir(gateway):
            if name.startswith("_"):
                continue
            value = getattr(gateway, name)
            with self.subTest(attribute=name):
                self.assertNotIsInstance(value, MockActuationAdapter)
        self.assertEqual(
            gateway.registered_paths(), {"mock": ActuatorPath.OFFICIAL_ORAN_DYNAMIC}
        )

    def test_a_lab_setup_adapter_cannot_be_built_into_a_gateway(self):
        class LabSetup:
            actuator_path = ActuatorPath.LAB_SETUP_PREPARATION

            def dispatch(self, *, token, command):  # pragma: no cover
                raise AssertionError("never dispatched")

        with self.assertRaises(GatewayRefusal):
            TokenBoundWriteGateway(
                adapters={"labsetup": LabSetup()}, safe_state=SAFE_STATE
            )

    def test_an_object_that_is_not_an_adapter_cannot_be_registered(self):
        with self.assertRaises(GatewayRefusal):
            TokenBoundWriteGateway(adapters={"nope": object()}, safe_state=SAFE_STATE)


class TheGatewayItselfCannotSpeakToEquipment(unittest.TestCase):
    """Design section 15: a hardware-free gate reports zero live calls.

    The cheapest proof is that no module in the gateway package can even
    import a transport -- the R1 adapter takes its client as an injected port
    for exactly this reason.
    """

    def test_no_gateway_module_imports_a_transport_or_a_runtime_package(self):
        violations = []
        for path in sorted(GATEWAY_ROOT.glob("*.py")):
            for name in _imports(path):
                for banned in FORBIDDEN_IN_THE_GATEWAY:
                    if name == banned or name.startswith(banned + "."):
                        violations.append(f"{path.relative_to(REPO_ROOT)}:{name}")
        self.assertEqual(violations, [], f"the gateway can reach a transport: {violations}")

    def test_the_r1_adapter_takes_its_client_rather_than_importing_one(self):
        source = (GATEWAY_ROOT / "r1_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("import oran", source)
        self.assertIn("policy_port", source)
        self.assertIn("readback_port", source)


class EveryCommandCarriesItsPermit(GatewayFixture, unittest.TestCase):
    """Nothing reaches an adapter that a Kernel token did not authorise."""

    def test_every_dispatched_command_is_derived_from_the_token(self):
        self.build()
        self.commit_applied()
        self.do_finalize()
        self.do_stop(sequence=4)
        self.do_rollback(sequence=5)
        self.assertTrue(self.adapter.commands)
        for command in self.adapter.commands:
            with self.subTest(command=command["operation"]):
                self.assertEqual(command["transactionId"], "tx-1")
                self.assertEqual(command["trialId"], "trial-1")
                self.assertEqual(command["fencingToken"], 1)
                self.assertIn("commandSequence", command)
                self.assertTrue(command["idempotencyKey"].startswith("tx-1:"))
                self.assertEqual(command["scope"], {"guAmfUeNgapId": "ue-1"})

    def test_a_command_carries_no_endpoint_and_no_free_text(self):
        self.build()
        self.commit_applied()
        allowed = {
            "operation", "transactionId", "trialId", "fencingToken",
            "commandSequence", "commandIndex", "idempotencyKey", "scope",
            "axis", "value",
        }
        for command in self.adapter.commands:
            self.assertEqual(set(command) - allowed, set())

    def test_every_frozen_operation_takes_its_token_by_keyword_only(self):
        import inspect

        for name in (
            "prepare", "ready", "commit", "stop", "reverse_rollback",
            "reread_configuration", "confirm_recovery", "emergency_safe_state",
            "finalize_live",
        ):
            signature = inspect.signature(getattr(WriteGateway, name))
            with self.subTest(operation=name):
                parameter = signature.parameters["token"]
                self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)


class AdaptersHonourTheirFrozenContract(unittest.TestCase):

    def test_both_adapters_satisfy_the_runtime_checkable_protocol(self):
        from assurance.gateway.r1_adapter import R1Adapter

        adapters: List[Any] = [
            MockActuationAdapter(config=BASELINE),
            R1Adapter(
                policy_port=object(),
                policy_builder=lambda command: {},
                near_rt_ric_id="ric",
                policy_type_id="type",
            ),
        ]
        for adapter in adapters:
            with self.subTest(adapter=type(adapter).__name__):
                self.assertIsInstance(adapter, WriteGatewayAdapter)
                self.assertIs(adapter.actuator_path, ActuatorPath.OFFICIAL_ORAN_DYNAMIC)

    def test_an_adapter_that_misbehaves_cannot_crash_the_gateway(self):
        class Raising:
            actuator_path = ActuatorPath.OFFICIAL_ORAN_DYNAMIC

            def dispatch(self, *, token, command):
                raise RuntimeError("downstream exploded")

        gateway = TokenBoundWriteGateway(
            adapters={"raising": Raising()},
            safe_state=SAFE_STATE,
            clock=TestClock(),
        )
        result = gateway.prepare(
            token=token(TokenKind.PREPARE),
            plan={
                "adapter": "raising",
                "scope": {"guAmfUeNgapId": "ue-1"},
                "baselineConfig": dict(BASELINE),
                "steps": [{"axis": "servingCell", "value": "cell-2"}],
            },
        )
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIn("could not be read", result.detail)

    def test_an_adapter_that_returns_the_wrong_type_is_an_error_not_a_success(self):
        class Wrong:
            actuator_path = ActuatorPath.OFFICIAL_ORAN_DYNAMIC

            def dispatch(self, *, token, command):
                return "fine"

        gateway = TokenBoundWriteGateway(
            adapters={"wrong": Wrong()},
            safe_state=SAFE_STATE,
            clock=TestClock(),
        )
        result = gateway.prepare(
            token=token(TokenKind.PREPARE),
            plan={
                "adapter": "wrong",
                "scope": {"guAmfUeNgapId": "ue-1"},
                "baselineConfig": dict(BASELINE),
                "steps": [{"axis": "servingCell", "value": "cell-2"}],
            },
        )
        self.assertIsInstance(result, GatewayResult)
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)


if __name__ == "__main__":
    unittest.main()


class AnUnreadableConfigurationSaysWhichParticipantCouldNotRead(unittest.TestCase):
    """2026-09-16 attempt 130 ended ``EXECUTION_FAILURE`` on two ``PREPARE`` reads.

    Both said only "prepare: UNKNOWN the configuration could not be read; nothing was
    staged".  The evidence named three participants (``r1-steer@ue1/ue2/ue3``) against
    the nine a healthy read dispatches in attempt 124, so one of them failed and the
    read stopped -- but *which* one, and the detail its own adapter produced (the
    readback distinguishes "no observation", "no verified readback within the
    contracted deadline" and "status verified but the stream did not corroborate"),
    were both discarded.  The episode could not be diagnosed from its own evidence.
    """

    class _Reader:
        """An adapter that reads back whatever it was constructed with."""

        actuator_path = ActuatorPath.OFFICIAL_ORAN_DYNAMIC

        def __init__(self, name, result):
            self.name, self._result = name, result

        def dispatch(self, *, token, command):
            return self._result

    def _gateway(self, adapters):
        return TokenBoundWriteGateway(
            adapters=adapters, safe_state=SAFE_STATE, clock=TestClock()
        )

    def _prepare(self, gateway, adapter, steps):
        return gateway.prepare(
            token=token(TokenKind.PREPARE),
            plan={
                "adapter": adapter,
                "scope": {"guAmfUeNgapId": "ue-1"},
                "baselineConfig": dict(BASELINE),
                "steps": steps,
            },
        )

    def test_the_lone_participant_is_named_with_its_own_detail(self):
        gateway = self._gateway({"solo": self._Reader("solo", GatewayResult(
            outcome=GatewayOutcome.UNKNOWN,
            evidence_refs=("solo:read:1",),
            detail="the contracted readback did not produce an observation"))})
        result = self._prepare(gateway, "solo",
                               [{"axis": "servingCell", "value": "cell-2"}])
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIn("could not be read", result.detail)
        self.assertIn("solo", result.detail)
        self.assertIn("the contracted readback did not produce an observation",
                      result.detail)

    def test_in_a_composition_the_failing_participant_is_named_not_the_whole_read(self):
        good = GatewayResult(
            outcome=GatewayOutcome.ACKED,
            evidence_refs=("first:read:1",),
            observed_config={"servingCell": "cell-1"},
            observed_config_hash="unused",
            detail="configuration read back")
        bad = GatewayResult(
            outcome=GatewayOutcome.UNKNOWN,
            evidence_refs=("second:read:1",),
            detail="status verified but the stream did not corroborate")
        gateway = self._gateway({"first": self._Reader("first", good),
                                 "second": self._Reader("second", bad)})
        result = self._prepare(gateway, "first", [
            {"axis": "servingCell", "value": "cell-2", "adapter": "first"},
            {"axis": "queuePriority", "value": 7, "adapter": "second"},
        ])
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIn("second", result.detail)
        self.assertIn("the stream did not corroborate", result.detail)
        # The participant that answered is not the one blamed.
        self.assertNotIn("first answered", result.detail)

    def test_a_participant_that_acks_without_naming_its_axes_says_so(self):
        good = GatewayResult(
            outcome=GatewayOutcome.ACKED,
            evidence_refs=("first:read:1",),
            observed_config={"servingCell": "cell-1"},
            observed_config_hash="unused",
            detail="configuration read back")
        silent = GatewayResult(
            outcome=GatewayOutcome.ACKED,
            evidence_refs=("second:read:1",),
            observed_config=None,
            observed_config_hash="a-digest-of-half-the-surface",
            detail="configuration read back")
        gateway = self._gateway({"first": self._Reader("first", good),
                                 "second": self._Reader("second", silent)})
        result = self._prepare(gateway, "first", [
            {"axis": "servingCell", "value": "cell-2", "adapter": "first"},
            {"axis": "queuePriority", "value": 7, "adapter": "second"},
        ])
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIn("second", result.detail)
        self.assertIn("named no axes", result.detail)

    def test_a_commit_whose_reread_fails_names_the_participant_too(self):
        """Four of the eighteen lockdowns on record say 'apply dispatched, readback
        unavailable; 9/9 acknowledged' -- every axis acknowledged and the reread still
        undecidable.  That is the same unreadable configuration as the PREPARE case and
        it is the one that costs a trial, so it carries the reason as well."""
        from assurance.gateway.mock_adapter import FaultInjection
        from tests.assurance.kgw_support import GatewayFixture, PLAN

        fixture = GatewayFixture()
        gateway, adapter = fixture.build()
        self.assertIs(fixture.do_prepare().outcome, GatewayOutcome.ACKED)
        fixture.do_ready()
        # set_faults resets the read counter, so this says "one more read works":
        # commit's pre-apply read succeeds and the reread after the apply fails.
        # That reread is the branch that produced the four lockdowns -- every axis
        # acknowledged, nothing readable afterwards.
        adapter.set_faults(FaultInjection(unreadable_after_reads=1))
        result = fixture.do_commit()
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIn("readback unavailable", result.detail)
        self.assertIn("mock", result.detail)
