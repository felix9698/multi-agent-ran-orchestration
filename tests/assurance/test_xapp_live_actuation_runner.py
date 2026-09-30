"""The Phase-3 live-run entry: one assignment, one dwell, always a rollback.

Hermetic: the gNB is :class:`~tests.assurance.xapp_live_support.ScriptedGnbTelnet`
and the Kernel is a stub whose only job is to issue real
:class:`~assurance.gateway.token.KernelToken` objects, so the permit path under
test is the real one (a token with a fence, a lease and an idempotency key)
without opening a case.  No socket is created: ``tools.xapp_live.transport`` is
never constructed here, only ``run_one_assignment``'s injected ``send``.
"""

import json
import unittest
from datetime import timedelta

from assurance.contracts.capability import ActuatorPath
from assurance.core.timebase import format_utc, parse_utc
from assurance.gateway.token import KernelToken, TokenKind
from assurance.xapps import XAppExecutionStatus
from assurance.xapps.live_actuation import NotActuatedByTelnetError
from tools.xapp_live.run_xapp import (
    KernelPermitSource, LiveRunError, LiveXAppRequest, _load,
    _typed_parameters, lab_deployment, run_one_assignment,
)

from tests.assurance.xapp_live_support import ScriptedGnbTelnet
from tests.assurance.xapp_support import (
    CONFIG_HASH, HEAVY_RNTI, NOW, SOURCE_CELL, TARGET_RNTI, TARGET_UE,
    attribution_snapshot, deployment,
)


class _StubKernel:
    """Issues real Kernel tokens; records what was asked for."""

    def __init__(self, lease_ms: int = 600_000) -> None:
        self.issued = []
        self._lease_ms = lease_ms

    def issue_token(self, trial_id, *, token_kind, now):
        token = KernelToken(
            token_kind=token_kind, transaction_id="txn/live-xapp-1",
            trial_id=trial_id, fencing_token=len(self.issued),
            command_sequence=len(self.issued),
            lease_expiry=format_utc(parse_utc(now)
                                    + timedelta(milliseconds=self._lease_ms)),
            expected_config_hash=CONFIG_HASH,
            idempotency_key=f"idem/live-xapp/{len(self.issued)}",
            issued_at=now)
        self.issued.append(token)
        return token


def _gnb(**kwargs):
    kwargs.setdefault("ues", {TARGET_RNTI: {"pf": 1.0, "cap": 0},
                              HEAVY_RNTI: {"pf": 1.0, "cap": 0}})
    return ScriptedGnbTelnet(**kwargs)


def _request(action_id="scheduler-priority", xapp_id="xapp/ue-scheduler",
             parameters=None, selector=None):
    if parameters is None:
        parameters = {"rnti": TARGET_RNTI, "pfWeight": 2.0}
    if selector is None:
        selector = {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL}
    return LiveXAppRequest(
        xapp_id=xapp_id, action_id=action_id, parameters=parameters,
        target_selector=selector, cell_id=SOURCE_CELL,
        endpoint="127.0.0.1:9091")


def _run(request=None, *, gnb=None, permit_source=None, snapshot=None,
         **kwargs):
    gnb = gnb if gnb is not None else _gnb()
    snapshot = snapshot if snapshot is not None else attribution_snapshot()
    source = permit_source if permit_source is not None \
        else KernelPermitSource(kernel=_StubKernel(), trial_id="case/live:trial:1")
    return gnb, run_one_assignment(
        request if request is not None else _request(),
        send=gnb, permit_source=source,
        snapshot_provider=lambda: snapshot,
        deployment=deployment(), clock=lambda: NOW, **kwargs)


class RoundTripTests(unittest.TestCase):

    def test_apply_dwell_rollback(self):
        seen = []

        def sampler(report):
            seen.append(report.status)
            return {"downlinkMbps": 9.7}

        gnb, outcome = _run(while_applied=sampler)
        self.assertTrue(outcome.applied, outcome.refusal)
        self.assertTrue(outcome.restored)
        self.assertEqual(seen, [XAppExecutionStatus.SUCCEEDED])
        self.assertEqual(outcome.dwell, {"downlinkMbps": 9.7})
        self.assertEqual(outcome.baseline,
                         {f"ue/{TARGET_RNTI:#06x}/pfWeight": 1.0})
        self.assertEqual(outcome.applied_axes,
                         (f"ue/{TARGET_RNTI:#06x}/pfWeight",))
        self.assertIs(outcome.rollback.status,
                      XAppExecutionStatus.ROLLBACK_SUCCEEDED)
        self.assertEqual(gnb.ues[TARGET_RNTI]["pf"], 1.0)

    def test_the_dwell_runs_while_the_change_is_really_live(self):
        live = []
        gnb = _gnb()
        _, outcome = _run(gnb=gnb,
                          while_applied=lambda report: live.append(
                              gnb.ues[TARGET_RNTI]["pf"]))
        self.assertEqual(live, [2.0])
        self.assertTrue(outcome.applied)
        self.assertEqual(gnb.ues[TARGET_RNTI]["pf"], 1.0)

    def test_the_cell_power_family_runs_through_the_same_entry(self):
        gnb, outcome = _run(_request(
            action_id="dl-rf-attenuation", xapp_id="xapp/cell-power",
            parameters={"txAttenuationDb": 6.0},
            selector={"cellId": SOURCE_CELL}))
        self.assertTrue(outcome.applied, outcome.refusal)
        self.assertTrue(outcome.restored)
        self.assertEqual(gnb.tx_att_db, 12.0)

    def test_both_permits_come_from_the_kernel(self):
        kernel = _StubKernel()
        _, outcome = _run(permit_source=KernelPermitSource(
            kernel=kernel, trial_id="case/live:trial:1"))
        self.assertTrue(outcome.applied)
        self.assertEqual([token.token_kind for token in kernel.issued],
                         [TokenKind.COMMIT, TokenKind.REVERSE_ROLLBACK])
        self.assertEqual([token.fencing_token for token in kernel.issued],
                         [0, 1])

    def test_the_outcome_is_json_and_says_what_kind_of_evidence_this_is(self):
        _, outcome = _run()
        record = outcome.to_canonical_dict()
        text = json.dumps(record, sort_keys=True)
        self.assertIn("LAB_TELNET_RESEARCH_MEASUREMENT", text)
        self.assertEqual(record["actuatorPath"],
                         ActuatorPath.LAB_SETUP_PREPARATION.value)
        self.assertEqual([exchange["operation"] for exchange
                          in record["exchanges"]],
                         ["PROBE", "READ", "APPLY", "READ", "PROBE", "APPLY",
                          "READ"])
        self.assertTrue(record["restored"])


class RefusalTests(unittest.TestCase):
    """Nothing runs without a permit, a snapshot, or an owned action."""

    def test_no_permit_source_at_all(self):
        gnb = _gnb()
        with self.assertRaisesRegex(LiveRunError, "no permit source"):
            run_one_assignment(
                _request(), send=gnb, permit_source=None,
                snapshot_provider=attribution_snapshot,
                deployment=deployment(), clock=lambda: NOW)
        self.assertEqual(gnb.sent, [])

    def test_a_permit_source_that_does_not_return_a_kernel_token(self):
        class _Forged:
            def acquire(self, kind, *, now):
                return {"tokenKind": kind.value, "now": now}

        gnb = _gnb()
        with self.assertRaisesRegex(LiveRunError, "did not return a Kernel token"):
            run_one_assignment(
                _request(), send=gnb, permit_source=_Forged(),
                snapshot_provider=attribution_snapshot,
                deployment=deployment(), clock=lambda: NOW)
        self.assertEqual(gnb.sent, [])

    def test_a_kernel_permit_source_needs_a_kernel(self):
        with self.assertRaisesRegex(LiveRunError, "Assurance Kernel"):
            KernelPermitSource(kernel=object(), trial_id="case/x:trial:1")

    def test_a_snapshot_this_entry_did_not_get_from_the_collector(self):
        gnb = _gnb()
        with self.assertRaisesRegex(LiveRunError, "CommonKpiSnapshot"):
            run_one_assignment(
                _request(), send=gnb,
                permit_source=KernelPermitSource(
                    kernel=_StubKernel(), trial_id="case/live:trial:1"),
                snapshot_provider=lambda: {"ue": "attached"},
                deployment=deployment(), clock=lambda: NOW)
        self.assertEqual(gnb.sent, [])

    def test_an_action_the_named_xapp_does_not_own(self):
        gnb = _gnb()
        with self.assertRaisesRegex(LiveRunError, "does not own"):
            _run(_request(action_id="dl-rf-attenuation"), gnb=gnb)
        self.assertEqual(gnb.sent, [])

    def test_an_xapp_the_registry_does_not_know(self):
        gnb = _gnb()
        with self.assertRaisesRegex(LiveRunError, "unknown xApp"):
            _run(_request(xapp_id="xapp/invented"), gnb=gnb)
        self.assertEqual(gnb.sent, [])

    def test_steering_is_refused_by_this_entry(self):
        gnb = _gnb()
        with self.assertRaises(NotActuatedByTelnetError):
            _run(_request(action_id="cell-steering",
                          xapp_id="xapp/traffic-steering",
                          parameters={"targetPrimaryCellId": 87654321},
                          selector={"objectiveUeId": TARGET_UE}), gnb=gnb)
        self.assertEqual(gnb.sent, [])

    def test_an_xapp_outside_the_coordinated_live_set_needs_saying_so(self):
        request = _request(action_id="dl-mcs-bounds",
                           xapp_id="xapp/link-adaptation",
                           parameters={"maxDlMcs": 12, "minDlMcs": 0},
                           selector={"cellId": SOURCE_CELL})
        gnb = _gnb()
        with self.assertRaisesRegex(LiveRunError, "coordinated live set"):
            _run(request, gnb=gnb)
        self.assertEqual(gnb.sent, [])

        gnb, outcome = _run(request, outside_coordinated_live_set=True)
        self.assertTrue(outcome.applied, outcome.refusal)
        self.assertFalse(outcome.coordinated_live_set)
        self.assertTrue(outcome.restored)
        self.assertEqual((gnb.dl_min_mcs, gnb.dl_max_mcs), (0, 28))

    def test_a_refused_parameter_leaves_nothing_to_roll_back(self):
        gnb, outcome = _run(_request(
            parameters={"rnti": TARGET_RNTI, "pfWeight": 500.0}))
        self.assertFalse(outcome.applied)
        self.assertIn("above maximum", outcome.refusal)
        self.assertEqual(gnb.sent, [])
        self.assertIsNone(outcome.rollback)
        self.assertIn("nothing to restore", outcome.rollback_skipped_reason)
        self.assertTrue(outcome.restored)


class _DiesOn:
    """A gNB that answers normally until one specific command, then dies."""

    def __init__(self, gnb, prefix):
        self._gnb = gnb
        self._prefix = prefix

    def __call__(self, line):
        if line.startswith(self._prefix):
            raise OSError("the shell went away before the restore")
        return self._gnb(line)


class AlwaysRollsBackTests(unittest.TestCase):
    """The rollback is in a ``finally``, not on the happy path."""

    def test_a_dwell_that_raises_still_rolls_back(self):
        gnb = _gnb()

        def boom(report):
            raise RuntimeError("the throughput sampler died")

        with self.assertRaisesRegex(RuntimeError, "sampler died"):
            _run(gnb=gnb, while_applied=boom)
        self.assertEqual(gnb.ues[TARGET_RNTI]["pf"], 1.0)

    def test_an_unknown_write_is_rolled_back_and_reported(self):
        gnb = _gnb(raise_on={f"ci sched_prio 2.000 {TARGET_RNTI:x}": 1})
        _, outcome = _run(gnb=gnb)
        self.assertFalse(outcome.applied)
        self.assertIn("UNKNOWN", outcome.refusal)
        self.assertEqual(outcome.unknown_writes,
                         (f"ue/{TARGET_RNTI:#06x}/pfWeight",))
        self.assertIs(outcome.rollback.status,
                      XAppExecutionStatus.ROLLBACK_SUCCEEDED)
        self.assertTrue(outcome.restored)
        self.assertEqual(gnb.ues[TARGET_RNTI]["pf"], 1.0)

    def test_a_rollback_that_cannot_run_is_recorded_not_swallowed(self):
        _, outcome = _run(gnb=_DiesOn(_gnb(), "ci sched_prio 1.000"))
        self.assertTrue(outcome.applied)
        self.assertFalse(outcome.restored)
        self.assertIn("the shell went away", outcome.rollback_refusal)


class CliHelperTests(unittest.TestCase):

    def test_parameters_are_typed_from_the_frozen_catalog(self):
        values = _typed_parameters(
            "scheduler-priority", ["rnti=0x2222", "pfWeight=2.5"],
            deployment())
        self.assertEqual(values, {"rnti": 0x2222, "pfWeight": 2.5})

    def test_a_parameter_the_contract_does_not_declare_is_refused(self):
        with self.assertRaisesRegex(LiveRunError, "no parameter"):
            _typed_parameters("scheduler-priority", ["gain=3"], deployment())

    def test_a_value_of_the_wrong_type_is_refused(self):
        with self.assertRaisesRegex(LiveRunError, "not a integer"):
            _typed_parameters("ue-dl-prb-cap",
                              ["rnti=0x2222", "maxDlPrbs=lots"], deployment())

    def test_the_lab_deployment_records_the_loopback_truth(self):
        binding = lab_deployment("telnet://127.0.0.1:9091")
        self.assertEqual(binding.transport_security.value, "NONE")

    def test_a_permit_source_reference_must_resolve(self):
        with self.assertRaisesRegex(LiveRunError, "not 'package.module"):
            _load("tools.xapp_live.run_xapp")
        with self.assertRaisesRegex(LiveRunError, "has no"):
            _load("tools.xapp_live.run_xapp:nothing_here")


if __name__ == "__main__":
    unittest.main()
