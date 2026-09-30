"""The live telnet actuation backend, driven against a scripted gNB.

Hermetic: :class:`~tests.assurance.xapp_live_support.ScriptedGnbTelnet` is a
callable that prints what the patched ``ci`` shell prints.  No socket, no
process, no radio -- but the grammar, the print formats and the refusal texts
are the ones in the checked-in handlers, so a codec that drifts from the C
fails here rather than on the testbed.

What each class pins:

* :class:`RoundTripTests` -- snapshot, apply, readback and rollback per
  knob-backed family, with the device state really moving and really returning;
* :class:`ParameterRefusalTests` -- an inadmissible value costs zero bytes;
* :class:`PermitBoundaryTests` -- so does a missing, wrong-kind, expired or
  foreign permit, and so does touching an axis outside a permit scope;
* :class:`FaultTests` -- a dropped connection, an off-grammar answer and a
  write that silently did not take all end fail-closed, with the rollback
  still able to run;
* :class:`BoundaryTests` -- what this path is not: not the steering path, not
  a slice actuator, and not registrable as an official-path gateway adapter.
"""

import unittest

from assurance.contracts.capability import ActuatorPath
from assurance.gateway.registry import GatewayAdapterRegistry
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayRefusal
from assurance.xapps import (
    CellPowerXApp, HardwareFreeConfigStore, PermitRequiredError,
    UeSchedulerXApp, XAppExecutionStatus, default_capability_manifests,
)
from assurance.xapps.assignment import XAppExecutionAssignment
from assurance.xapps.live_actuation import (
    NOT_ACTUATED_BY_TELNET, TELNET_ACTUATED_ACTIONS, LinkAdaptationXApp,
    LiveActuationError, NotActuatedByTelnetError, TelnetActuationTransport,
    TelnetGrammarError, TelnetTransportError, build_specialist_executor,
)

from tests.assurance.xapp_live_support import ScriptedGnbTelnet
from tests.assurance.xapp_support import (
    HEAVY_RNTI, NOW, SOURCE_CELL, TAKEN, TARGET_CELL, TARGET_RNTI,
    TARGET_UE, attribution_snapshot, deployment, permit,
)

DEADLINE = "2026-08-31T10:05:00.000000Z"


def _manifests():
    return {m.xapp_id: m for m in default_capability_manifests(deployment())}


def _assignment(action_id, xapp_id, parameters, selector, snapshot,
                *, the_permit):
    return XAppExecutionAssignment(
        assignment_id=f"assignment/live/{action_id}",
        plan_id="xapp-plan/live", step_id=f"step/live/{action_id}",
        xapp_id=xapp_id, action_id=action_id, parameters=parameters,
        target_selector=selector, snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.content_hash(), preconditions=(),
        deadline=DEADLINE, permit_ref=the_permit.content_hash())


def _gnb(**kwargs):
    kwargs.setdefault("ues", {TARGET_RNTI: {"pf": 1.0, "cap": 0},
                              HEAVY_RNTI: {"pf": 1.0, "cap": 0}})
    return ScriptedGnbTelnet(**kwargs)


def _transport(gnb, *, cell_id=SOURCE_CELL, clock=None):
    return TelnetActuationTransport(
        send=gnb, deployment=deployment(), cell_id=cell_id,
        endpoint="127.0.0.1:9091", clock=clock)


class _LiveCase(unittest.TestCase):
    """Shared wiring: one scripted gNB, one transport, one specialist."""

    xapp_id = ""

    def setUp(self):
        self.gnb = _gnb()
        self.transport = _transport(self.gnb)
        self.snapshot = attribution_snapshot()
        self.permit = permit()
        if self.xapp_id:
            self.executor = build_specialist_executor(
                manifest=_manifests()[self.xapp_id], backend=self.transport)

    def execute(self, assignment, *, the_permit=None, now=NOW):
        the_permit = self.permit if the_permit is None else the_permit
        with self.transport.permit_scope(assignment, permit=the_permit,
                                         kind=TokenKind.COMMIT, now=now):
            return self.executor.execute(assignment, permit=the_permit,
                                         snapshot=self.snapshot, now=now)

    def roll_back(self, assignment, *, now=NOW):
        rollback_permit = permit(kind=TokenKind.REVERSE_ROLLBACK)
        with self.transport.permit_scope(
                assignment, permit=rollback_permit,
                kind=TokenKind.REVERSE_ROLLBACK, now=now):
            return self.executor.rollback(assignment, permit=rollback_permit,
                                          now=now)


# --------------------------------------------------------------------------- #
# positive: one round trip per knob-backed family
# --------------------------------------------------------------------------- #

class RoundTripTests(_LiveCase):
    """snapshot -> apply -> readback -> rollback, on the real grammar."""

    def _round_trip(self, xapp_id, action_id, parameters, selector, *,
                    reads_live, expected_live, baseline=None):
        self.executor = build_specialist_executor(
            manifest=_manifests()[xapp_id], backend=self.transport)
        assignment = _assignment(action_id, xapp_id, parameters, selector,
                                 self.snapshot, the_permit=self.permit)
        before = reads_live()
        report = self.execute(assignment)
        self.assertIs(report.status, XAppExecutionStatus.SUCCEEDED,
                      report.detail)
        self.assertEqual(reads_live(), expected_live)
        self.assertEqual(list(self.transport.observed_baseline.values()),
                         [before if baseline is None else baseline])
        rollback = self.roll_back(assignment)
        self.assertIs(rollback.status, XAppExecutionStatus.ROLLBACK_SUCCEEDED)
        self.assertEqual(reads_live(), before)
        return report

    def test_ue_dl_prb_cap_round_trip(self):
        report = self._round_trip(
            "xapp/ue-scheduler", "ue-dl-prb-cap",
            {"rnti": TARGET_RNTI, "maxDlPrbs": 20},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            reads_live=lambda: self.gnb.ues[TARGET_RNTI]["cap"],
            expected_live=20)
        self.assertEqual(report.readback[f"ue/{TARGET_RNTI:#06x}/dlPrbCap"], 20)
        self.assertIn(f"ci prbcap 20 {TARGET_RNTI:x}", self.gnb.sent)

    def test_scheduler_priority_round_trip(self):
        report = self._round_trip(
            "xapp/ue-scheduler", "scheduler-priority",
            {"rnti": TARGET_RNTI, "pfWeight": 2.5},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            reads_live=lambda: self.gnb.ues[TARGET_RNTI]["pf"],
            expected_live=2.5)
        self.assertEqual(report.readback[f"ue/{TARGET_RNTI:#06x}/pfWeight"], 2.5)
        self.assertIn(f"ci sched_prio 2.500 {TARGET_RNTI:x}", self.gnb.sent)

    def test_dl_rf_attenuation_round_trip(self):
        report = self._round_trip(
            "xapp/cell-power", "dl-rf-attenuation",
            {"txAttenuationDb": 9.0}, {"cellId": SOURCE_CELL},
            reads_live=lambda: self.gnb.tx_att_db, expected_live=9.0)
        self.assertEqual(
            report.readback[f"cell/{SOURCE_CELL}/txAttenuationDb"], 9.0)
        self.assertIn("lowers DL transmit power",
                      report.readback["powerDirectionNote"])
        self.assertIn("ci rfatt 9.0", self.gnb.sent)

    def test_dl_mcs_bounds_round_trip(self):
        report = self._round_trip(
            "xapp/link-adaptation", "dl-mcs-bounds",
            {"maxDlMcs": 20, "minDlMcs": 4}, {"cellId": SOURCE_CELL},
            reads_live=lambda: (self.gnb.dl_min_mcs, self.gnb.dl_max_mcs),
            expected_live=(4, 20),
            baseline={"minDlMcs": 0, "maxDlMcs": 28})
        self.assertEqual(report.readback[f"cell/{SOURCE_CELL}/dlMcsBounds"],
                         {"maxDlMcs": 20, "minDlMcs": 4})
        self.assertIn("cell-wide", report.readback["boundsDirectionNote"])
        self.assertIn("ci mcs 20 4", self.gnb.sent)

    def test_a_zero_prb_cap_is_read_back_as_uncapped_not_as_a_missing_ue(self):
        """``prbcap`` lists a UE only while capped; 0 must not read as absent."""
        # 24 rather than 30: the frozen profile is a 24-PRB radio, so a prior
        # cap of 30 is not a configuration this deployment can be in
        # (assurance/actions/catalog.py UE_DL_PRB_CAP_APPLY_RANGE).
        self.gnb.ues[TARGET_RNTI]["cap"] = 24
        self._round_trip(
            "xapp/ue-scheduler", "ue-dl-prb-cap",
            {"rnti": TARGET_RNTI, "maxDlPrbs": 0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            reads_live=lambda: self.gnb.ues[TARGET_RNTI]["cap"],
            expected_live=0)

    def test_every_send_is_journalled_with_its_response(self):
        self.executor = build_specialist_executor(
            manifest=_manifests()["xapp/cell-power"], backend=self.transport)
        assignment = _assignment(
            "dl-rf-attenuation", "xapp/cell-power", {"txAttenuationDb": 15.0},
            {"cellId": SOURCE_CELL}, self.snapshot, the_permit=self.permit)
        self.execute(assignment)
        operations = [exchange.operation for exchange in
                      self.transport.exchanges]
        self.assertEqual(operations, ["READ", "APPLY", "READ"])
        self.assertTrue(all(exchange.outcome == "PARSED"
                            for exchange in self.transport.exchanges))
        self.assertEqual(self.transport.exchanges[1].command, "ci rfatt 15.0")
        self.assertEqual(self.transport.applied_axes,
                         (f"cell/{SOURCE_CELL}/txAttenuationDb",))


class UeLivenessTests(_LiveCase):
    """A cell-local RNTI is verified against the cell before anything moves."""

    xapp_id = "xapp/ue-scheduler"

    def test_an_absent_rnti_is_refused_with_one_read_and_no_write(self):
        del self.gnb.ues[TARGET_RNTI]
        assignment = _assignment(
            "scheduler-priority", "xapp/ue-scheduler",
            {"rnti": TARGET_RNTI, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            self.snapshot, the_permit=self.permit)
        with self.assertRaisesRegex(LiveActuationError, "not connected"):
            self.execute(assignment)
        self.assertEqual(self.gnb.sent, ["ci sched_prio"])
        self.assertEqual(self.transport.applied_axes, ())


# --------------------------------------------------------------------------- #
# negative: an inadmissible value never reaches the wire
# --------------------------------------------------------------------------- #

class ParameterRefusalTests(_LiveCase):
    """Out of range, or not expressible on the wire: refused before any send."""

    def _refuse(self, xapp_id, action_id, parameters, selector):
        self.executor = build_specialist_executor(
            manifest=_manifests()[xapp_id], backend=self.transport)
        assignment = _assignment(action_id, xapp_id, parameters, selector,
                                 self.snapshot, the_permit=self.permit)
        with self.assertRaises(LiveActuationError) as raised:
            self.execute(assignment)
        self.assertEqual(self.gnb.sent, [],
                         "a refused parameter must cost zero bytes")
        return str(raised.exception)

    def test_a_prb_cap_above_the_catalog_maximum_is_refused(self):
        detail = self._refuse(
            "xapp/ue-scheduler", "ue-dl-prb-cap",
            {"rnti": TARGET_RNTI, "maxDlPrbs": 300},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL})
        self.assertIn("above maximum", detail)

    def test_a_zero_pf_weight_is_refused(self):
        detail = self._refuse(
            "xapp/ue-scheduler", "scheduler-priority",
            {"rnti": TARGET_RNTI, "pfWeight": 0.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL})
        self.assertIn("below minimum", detail)

    def test_a_pf_weight_finer_than_the_wire_format_is_refused(self):
        detail = self._refuse(
            "xapp/ue-scheduler", "scheduler-priority",
            {"rnti": TARGET_RNTI, "pfWeight": 1.23456},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL})
        self.assertIn("3 decimals", detail)

    def test_an_attenuation_outside_the_knob_range_is_refused(self):
        detail = self._refuse(
            "xapp/cell-power", "dl-rf-attenuation",
            {"txAttenuationDb": 61.0}, {"cellId": SOURCE_CELL})
        self.assertIn("[0.0,60.0] dB", detail)

    def test_an_mcs_floor_above_its_ceiling_is_refused(self):
        detail = self._refuse(
            "xapp/link-adaptation", "dl-mcs-bounds",
            {"maxDlMcs": 10, "minDlMcs": 20}, {"cellId": SOURCE_CELL})
        self.assertIn("minDlMcs exceeds maxDlMcs", detail)

    def test_a_cell_wide_write_for_another_cell_is_refused(self):
        detail = self._refuse(
            "xapp/cell-power", "dl-rf-attenuation",
            {"txAttenuationDb": 9.0}, {"cellId": TARGET_CELL})
        self.assertIn("wrong cell", detail)

    def test_an_rnti_outside_what_fetch_rnti_accepts_is_refused(self):
        detail = self._refuse(
            "xapp/ue-scheduler", "scheduler-priority",
            {"rnti": 0xFFFE, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL})
        self.assertIn("fetch_rnti", detail)

    def test_a_gnb_side_refusal_is_an_error_not_a_silent_no_op(self):
        """The wire refuses what slipped past every local check."""
        transport = _transport(self.gnb)
        codec = transport._codecs["dl-rf-attenuation"]
        target = codec.target_for(f"cell/{SOURCE_CELL}/txAttenuationDb")
        self.assertEqual(codec.write_line(target, 9.0), "ci rfatt 9.0")
        self.assertEqual(self.gnb("ci rfatt 99.0"),
                         "attenuation 99.0 out of range [0,60] dB")


# --------------------------------------------------------------------------- #
# the permit boundary
# --------------------------------------------------------------------------- #

class PermitBoundaryTests(_LiveCase):
    """No permit, no send.  The check is the executor's, called not restated."""

    xapp_id = "xapp/ue-scheduler"

    def setUp(self):
        super().setUp()
        self.assignment = _assignment(
            "scheduler-priority", "xapp/ue-scheduler",
            {"rnti": TARGET_RNTI, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            self.snapshot, the_permit=self.permit)

    def _blocked(self, **kwargs):
        with self.assertRaises(PermitRequiredError) as raised:
            self.transport.authorise(self.assignment, **kwargs)
        self.assertEqual(self.gnb.sent, [])
        return str(raised.exception)

    def test_no_permit_at_all(self):
        detail = self._blocked(permit=None, kind=TokenKind.COMMIT, now=NOW)
        self.assertIn("blocked", detail)

    def test_a_wrong_kind_permit(self):
        detail = self._blocked(permit=permit(kind=TokenKind.PREPARE),
                               kind=TokenKind.COMMIT, now=NOW)
        self.assertIn("PREPARE", detail)

    def test_an_expired_lease(self):
        expired = permit(lease_expiry="2026-08-31T10:00:00.500000Z")
        assignment = _assignment(
            "scheduler-priority", "xapp/ue-scheduler",
            {"rnti": TARGET_RNTI, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            self.snapshot, the_permit=expired)
        with self.assertRaisesRegex(PermitRequiredError, "expired"):
            self.transport.authorise(assignment, permit=expired,
                                     kind=TokenKind.COMMIT, now=NOW)
        self.assertEqual(self.gnb.sent, [])

    def test_a_permit_issued_for_another_assignment(self):
        detail = self._blocked(permit=permit(sequence=7),
                               kind=TokenKind.COMMIT, now=NOW)
        self.assertIn("not the one", detail)

    def test_touching_an_axis_outside_a_permit_scope(self):
        for call in (lambda: self.transport.read(f"ue/{TARGET_RNTI:#06x}/pfWeight"),
                     lambda: self.transport.apply(
                         f"ue/{TARGET_RNTI:#06x}/pfWeight", 2.0)):
            with self.subTest(call=call):
                with self.assertRaisesRegex(PermitRequiredError,
                                            "outside a permit scope"):
                    call()
        self.assertEqual(self.gnb.sent, [])

    def test_an_assignment_for_another_xapp_costs_no_read(self):
        foreign = _assignment(
            "scheduler-priority", "xapp/cell-power",
            {"rnti": TARGET_RNTI, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            self.snapshot, the_permit=self.permit)
        with self.assertRaisesRegex(LiveActuationError, "addressed to"):
            self.transport.authorise(foreign, permit=self.permit,
                                     kind=TokenKind.COMMIT, now=NOW)
        self.assertEqual(self.gnb.sent, [])

    def test_an_action_the_bound_xapp_does_not_own_costs_no_read(self):
        foreign = _assignment(
            "dl-rf-attenuation", "xapp/ue-scheduler",
            {"txAttenuationDb": 9.0}, {"cellId": SOURCE_CELL},
            self.snapshot, the_permit=self.permit)
        with self.assertRaisesRegex(LiveActuationError, "does not own"):
            self.transport.authorise(foreign, permit=self.permit,
                                     kind=TokenKind.COMMIT, now=NOW)
        self.assertEqual(self.gnb.sent, [])

    def test_an_unbound_transport_cannot_open_a_scope(self):
        loose = _transport(self.gnb)
        with self.assertRaisesRegex(PermitRequiredError, "not bound"):
            loose.authorise(self.assignment, permit=self.permit,
                            kind=TokenKind.COMMIT, now=NOW)
        self.assertEqual(self.gnb.sent, [])

    def test_an_axis_outside_the_armed_action_is_refused(self):
        power = build_specialist_executor(
            manifest=_manifests()["xapp/cell-power"],
            backend=_transport(self.gnb))
        del power
        with self.transport.permit_scope(self.assignment, permit=self.permit,
                                         kind=TokenKind.COMMIT, now=NOW):
            self.gnb.sent.clear()
            with self.assertRaisesRegex(PermitRequiredError,
                                        "scope was opened for"):
                self.transport.read(f"cell/{SOURCE_CELL}/txAttenuationDb")
        self.assertEqual(self.gnb.sent, [])

    def test_a_lease_that_expires_while_the_scope_is_open_stops_the_next_send(self):
        instants = [TAKEN, TAKEN, "2026-08-31T11:00:00.000000Z"]

        def _clock():
            return instants.pop(0) if len(instants) > 1 else instants[0]

        transport = _transport(self.gnb, clock=_clock)
        executor = build_specialist_executor(
            manifest=_manifests()["xapp/ue-scheduler"], backend=transport)
        del executor
        transport.authorise(self.assignment, permit=self.permit,
                            kind=TokenKind.COMMIT, now=NOW)
        axis = f"ue/{TARGET_RNTI:#06x}/pfWeight"
        transport.read(axis)
        with self.assertRaisesRegex(PermitRequiredError, "lease expired"):
            transport.apply(axis, 2.0)
        self.assertEqual(self.gnb.ues[TARGET_RNTI]["pf"], 1.0)


# --------------------------------------------------------------------------- #
# faults
# --------------------------------------------------------------------------- #

class FaultTests(_LiveCase):
    """Dropped, garbled and silently-lost writes all end fail-closed."""

    xapp_id = "xapp/cell-power"

    def _assignment(self, attenuation=9.0):
        return _assignment(
            "dl-rf-attenuation", "xapp/cell-power",
            {"txAttenuationDb": attenuation}, {"cellId": SOURCE_CELL},
            self.snapshot, the_permit=self.permit)

    def test_a_dropped_connection_during_the_write_latches_and_rolls_back(self):
        self.gnb = _gnb(raise_on={"ci rfatt 9.0": 1})
        self.transport = _transport(self.gnb)
        self.executor = build_specialist_executor(
            manifest=_manifests()["xapp/cell-power"], backend=self.transport)
        assignment = self._assignment()
        with self.assertRaisesRegex(TelnetTransportError, "UNKNOWN"):
            self.execute(assignment)
        self.assertTrue(self.transport.latched)
        self.assertEqual(self.transport.unknown_writes,
                         (f"cell/{SOURCE_CELL}/txAttenuationDb",))
        self.assertEqual(self.transport.applied_axes, ())

        # A second apply is refused while the outcome is unknown ...
        with self.assertRaisesRegex(LiveActuationError, "latched"):
            self.execute(self._assignment(attenuation=10.0))
        # ... but the rollback that resolves it is not.
        rollback = self.roll_back(assignment)
        self.assertIs(rollback.status, XAppExecutionStatus.ROLLBACK_SUCCEEDED)
        self.assertEqual(self.gnb.tx_att_db, 12.0)

    def test_an_off_grammar_read_is_refused_and_writes_nothing(self):
        self.gnb = _gnb(garbage_for={"ci rfatt": "MAC layer busy, try again"})
        self.transport = _transport(self.gnb)
        self.executor = build_specialist_executor(
            manifest=_manifests()["xapp/cell-power"], backend=self.transport)
        with self.assertRaisesRegex(TelnetGrammarError,
                                    "current TX attenuation"):
            self.execute(self._assignment())
        self.assertEqual(self.gnb.tx_att_db, 12.0)
        self.assertFalse(self.transport.latched)
        self.assertEqual(self.transport.applied_axes, ())

    def test_an_off_grammar_acknowledgement_latches(self):
        self.gnb = _gnb(garbage_for={"ci rfatt 9.0": "ok"})
        self.transport = _transport(self.gnb)
        self.executor = build_specialist_executor(
            manifest=_manifests()["xapp/cell-power"], backend=self.transport)
        with self.assertRaises(TelnetGrammarError):
            self.execute(self._assignment())
        self.assertTrue(self.transport.latched)
        self.assertEqual(self.transport.applied_axes, ())

    def test_a_write_that_silently_did_not_take_is_not_counted_as_applied(self):
        self.gnb = _gnb(ignore_writes=("ci rfatt",))
        self.transport = _transport(self.gnb)
        self.executor = build_specialist_executor(
            manifest=_manifests()["xapp/cell-power"], backend=self.transport)
        with self.assertRaisesRegex(TelnetGrammarError, "acknowledged 12.0"):
            self.execute(self._assignment())
        self.assertEqual(self.gnb.tx_att_db, 12.0)
        self.assertEqual(self.transport.applied_axes, ())
        self.assertTrue(self.transport.latched)

    def test_a_transport_that_dies_on_the_snapshot_read_writes_nothing(self):
        self.gnb = _gnb(raise_on={"ci rfatt": 1})
        self.transport = _transport(self.gnb)
        self.executor = build_specialist_executor(
            manifest=_manifests()["xapp/cell-power"], backend=self.transport)
        with self.assertRaises(TelnetTransportError):
            self.execute(self._assignment())
        self.assertFalse(self.transport.latched)
        self.assertEqual(self.transport.applied_axes, ())


# --------------------------------------------------------------------------- #
# what this backend is not
# --------------------------------------------------------------------------- #

class BoundaryTests(unittest.TestCase):

    def setUp(self):
        self.gnb = _gnb()
        self.transport = _transport(self.gnb)
        self.manifests = _manifests()

    def test_steering_is_not_routed_through_telnet(self):
        with self.assertRaises(NotActuatedByTelnetError) as raised:
            build_specialist_executor(
                manifest=self.manifests["xapp/traffic-steering"],
                backend=self.transport)
        self.assertIn("A1", str(raised.exception))
        self.assertNotIn("cell-steering", TELNET_ACTUATED_ACTIONS)

    def test_slice_resource_has_no_telnet_knob_and_no_executor(self):
        with self.assertRaises(LiveActuationError) as raised:
            build_specialist_executor(
                manifest=self.manifests["xapp/slice-resource"],
                backend=self.transport)
        self.assertIn("no specialist executor", str(raised.exception))
        self.assertIn("NOT_ACTUATED_BY_TELNET",
                      NOT_ACTUATED_BY_TELNET["slice-prb-quota"])
        self.assertNotIn("slice-prb-quota", TELNET_ACTUATED_ACTIONS)

    def test_the_four_actuated_families_are_exactly_the_knob_backed_ones(self):
        self.assertEqual(dict(TELNET_ACTUATED_ACTIONS), {
            "ue-dl-prb-cap": "ci prbcap",
            "scheduler-priority": "ci sched_prio",
            "dl-rf-attenuation": "ci rfatt",
            "dl-mcs-bounds": "ci mcs",
        })

    def test_it_is_a_lab_setup_path_the_gateway_registry_refuses(self):
        self.assertIs(self.transport.actuator_path,
                      ActuatorPath.LAB_SETUP_PREPARATION)
        with self.assertRaises(GatewayRefusal):
            GatewayAdapterRegistry().register("telnet-knobs", self.transport)

    def test_an_unmappable_axis_is_refused_rather_than_dropped(self):
        with self.assertRaisesRegex(LiveActuationError, "no telnet knob"):
            self.transport._resolve("cell/1/somethingElse")

    def test_the_module_opens_no_transport(self):
        import pathlib
        import assurance.xapps.live_actuation as module
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("telnetlib", "socket", "requests", "urllib",
                          "subprocess", "http.client"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)


class BackendSwitchTests(unittest.TestCase):
    """One executor implementation, two backends, no duplicated logic."""

    def setUp(self):
        self.manifests = _manifests()
        self.snapshot = attribution_snapshot()
        self.permit = permit()

    def test_the_same_class_serves_both_backends(self):
        store = HardwareFreeConfigStore()
        hermetic = build_specialist_executor(
            manifest=self.manifests["xapp/ue-scheduler"], backend=store)
        live = build_specialist_executor(
            manifest=self.manifests["xapp/ue-scheduler"],
            backend=_transport(_gnb()))
        self.assertIsInstance(hermetic, UeSchedulerXApp)
        self.assertIsInstance(live, UeSchedulerXApp)
        self.assertIs(type(hermetic), type(live))
        self.assertIs(
            type(build_specialist_executor(
                manifest=self.manifests["xapp/cell-power"], backend=store)),
            CellPowerXApp)

    def test_the_hardware_free_path_is_unchanged_through_the_factory(self):
        store = HardwareFreeConfigStore()
        executor = build_specialist_executor(
            manifest=self.manifests["xapp/ue-scheduler"], backend=store)
        assignment = _assignment(
            "scheduler-priority", "xapp/ue-scheduler",
            {"rnti": TARGET_RNTI, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            self.snapshot, the_permit=self.permit)
        report = executor.execute(assignment, permit=self.permit,
                                  snapshot=self.snapshot, now=NOW)
        self.assertIs(report.status, XAppExecutionStatus.SUCCEEDED)
        self.assertEqual(store.read(f"ue/{TARGET_RNTI:#06x}/pfWeight"), 2.0)

    def test_link_adaptation_runs_hardware_free_too(self):
        store = HardwareFreeConfigStore()
        executor = build_specialist_executor(
            manifest=self.manifests["xapp/link-adaptation"], backend=store)
        self.assertIsInstance(executor, LinkAdaptationXApp)
        assignment = _assignment(
            "dl-mcs-bounds", "xapp/link-adaptation",
            {"maxDlMcs": 12, "minDlMcs": 0}, {"cellId": SOURCE_CELL},
            self.snapshot, the_permit=self.permit)
        report = executor.execute(assignment, permit=self.permit,
                                  snapshot=self.snapshot, now=NOW)
        self.assertIs(report.status, XAppExecutionStatus.SUCCEEDED)
        self.assertEqual(store.read(f"cell/{SOURCE_CELL}/dlMcsBounds"),
                         {"maxDlMcs": 12, "minDlMcs": 0})

    def test_a_link_adaptation_executor_refuses_a_foreign_manifest(self):
        from assurance.xapps.executors import ExecutorError
        with self.assertRaises(ExecutorError):
            LinkAdaptationXApp(manifest=self.manifests["xapp/cell-power"],
                               store=HardwareFreeConfigStore())


if __name__ == "__main__":
    unittest.main()
