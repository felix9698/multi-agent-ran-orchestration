"""A UE re-registered mid-sitting is followed through the real Kernel and Gateway.

docs/design/ue-identity-continuity.md: intents and candidates are sealed on the
UE host role; the AMF id is resolved when a trial needs it.  This drives a
hardware-free sitting whose ue2 is handed a new AMF UE NGAP id between trials
and checks that the sitting rebinds instead of refusing, without resetting the
budget or re-offering a spent candidate.
"""
import tempfile
import unittest
from unittest.mock import patch

from tools.hfconsole import agent_env
from tools.hfconsole.agent_env import EmulatedRan
from tools.liveconsole import agent as agent_module
from tools.liveconsole.agent import (
    AgentRequest, METHOD_DETERMINISTIC, build_agent_sitting, build_hardware_free_agent_sitting)

HOME, TARGET = "12345678", "87654321"
# The formal runner hands the sitting records (--intents-json), keyed by host role.
INTENTS = tuple({"intentId": f"I{n}", "owner": f"ue{n}-owner", "ueId": f"ue{n}", "priority": 1,
                 "weight": 1.0, "requirement": {"reqId": f"I{n}.r1", "kpi": "dlGoodputMbps",
                                                "op": ">=", "value": value, "bound": bound,
                                                "steps": 2, "unit": "Mbps"}}
                for n, value, bound in ((1, 6.0, 5.5), (2, 6.0, 5.5)))  # unattainable on 5 Mbps cells: every trial runs


class ARoleLabelledSittingRebindsAcrossReRegistration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name
        self.amf = {"ue1": 34, "ue2": 42}
        self.streams = []
        original_init = agent_env.EmulatedKpmStream.__init__

        def init(stream, *args, **kwargs):
            original_init(stream, *args, **kwargs)
            stream.amf_of = self.amf  # shared: re-registration is one assignment
            self.streams.append(stream)
        patches = [
            patch.object(agent_env.EmulatedKpmStream, "__init__", init),
            patch.object(agent_module, "role_identity_resolver",
                         lambda _doc, **_kw: (lambda label: int(label) if str(label).isdigit()
                                              else self.amf.get(str(label)))),
            patch("socket.socket", side_effect=AssertionError("network forbidden")),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def build(self, **request_kwargs):
        ran = EmulatedRan(ues={"ue1": HOME, "ue2": HOME}, cells={HOME: 5.0, TARGET: 5.0},
                          offered_load_mbps={"ue1": 4.0, "ue2": 4.0}, noise_sigma=0.0)
        request = AgentRequest(intents=INTENTS, method=METHOD_DETERMINISTIC, **request_kwargs)
        return build_hardware_free_agent_sitting(request, tmp_dir=self.tmp, ran=ran,
                                                 stamp="20260916T010000Z")

    def test_ids_resolve_from_the_role_and_the_catalog_carries_only_role_keys(self):
        sitting = self.build(budget_trials=2, axes=("servingCell",))
        self.assertEqual({"ue1": 34, "ue2": 42},
                         {ue: item.amf_ue_ngap_id for ue, item in sitting.identities.items()})
        for entry in sitting.runtime.catalog_entries():
            self.assertEqual({"servingCell@ue1", "servingCell@ue2"},
                             set(entry.candidate.parameters))

    def test_a_re_registration_between_trials_rebinds_and_keeps_budget_and_spent(self):
        sitting = self.build(budget_trials=3, axes=("servingCell",))
        catalog_before = [str(e.candidate.candidate_id) for e in sitting.runtime.catalog_entries()]
        original = agent_module.AgentSitting._rebind_if_reregistered
        calls = []

        def rebind(this):
            calls.append(this._trials_used_total())
            if this._trials_used_total() == 1 and self.amf["ue2"] == 42:
                self.amf["ue2"] = 43  # ue2 dropped and re-registered
                this.clock.sleep_ms(1000)
            return original(this)

        with patch.object(agent_module.AgentSitting, "_rebind_if_reregistered", rebind):
            sitting.confirm()
            sitting.run()
        rebinds = sitting.identity_rebinds
        self.assertEqual(1, len(rebinds), rebinds)
        record = rebinds[0]
        self.assertEqual("REBOUND", record["outcome"], record)
        self.assertEqual({"previousAmfUeNgapId": 42, "amfUeNgapId": 43},
                         {k: record["ues"]["ue2"][k] for k in ("previousAmfUeNgapId", "amfUeNgapId")})
        self.assertEqual(1, record["trialsBefore"])
        self.assertEqual(43, sitting.identities["ue2"].amf_ue_ngap_id)
        self.assertEqual(34, sitting.identities["ue1"].amf_ue_ngap_id)
        # One retired case; the catalog is the same set of candidate ids on both.
        self.assertEqual(1, len(sitting.retired_runtimes))
        self.assertEqual(catalog_before,
                         [str(e.candidate.candidate_id) for e in sitting.runtime.catalog_entries()])
        self.assertNotEqual(sitting.retired_runtimes[0].case_id, sitting.runtime.case_id)
        self.assertEqual(sitting.retired_runtimes[0].case_id, sitting.case_id)
        # The budget is the sitting's, not the case's; a spent candidate is never re-run.
        self.assertLessEqual(sitting._trials_used_total(), 3)
        run = [str(t["candidateId"]) for t in sitting._all_kernel_trials() if t.get("candidateId")]
        self.assertEqual(len(run), len(set(run)), run)
        episode = sitting.episode_record()
        self.assertEqual("REBOUND", episode["identityRebinds"][0]["outcome"])
        # The retired case's own event stream and ids are written beside the current one.
        import json
        from pathlib import Path
        from tools.liveconsole.agent import write_agent_evidence
        paths = write_agent_evidence(sitting)
        document = json.loads(Path(paths["episode"]).read_text())
        retired, = document["execution"]["retiredCases"]
        events = Path(paths["events"]).with_name(retired["eventsFile"])
        self.assertTrue(events.read_text().strip())
        self.assertNotEqual(events.read_text(), Path(paths["events"]).read_text())
        # The new case holds only the sitting's remaining trials, so it closes with it.
        self.assertEqual("EVIDENCE_INCOMPLETE", sitting.kernel_termination,
                         sitting.preflight.get("kernelTerminationRefusal"))


class RoleLabelsChangeNothingButTheLabel(unittest.TestCase):
    """The same emulated radio, sat once under ids and once under roles, must judge alike.

    This is what shows the per-UE KPM counters (throughput, cap and PF readback)
    still reach the Kernel when the contract names the UE by role.
    """

    def sit(self, labels, amf):
        from tools.hfconsole import agent_env as env
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        original_init = env.EmulatedKpmStream.__init__

        def init(stream, *args, **kwargs):
            original_init(stream, *args, **kwargs)
            stream.amf_of = amf
        a, b = labels
        intents = tuple(dict(record, ueId=label, owner=f"{label}-owner",
                             requirement=dict(record["requirement"], value=value, bound=bound))
                        for record, label, value, bound in
                        zip(INTENTS, labels, (3.0, 2.0), (2.5, 1.5)))
        ran = EmulatedRan(ues={a: HOME, b: HOME}, cells={HOME: 5.0, TARGET: 5.0},
                          offered_load_mbps={a: 4.0, b: 4.0}, noise_sigma=0.0)
        request = AgentRequest(intents=intents, method=METHOD_DETERMINISTIC, budget_trials=4,
                               axes=("servingCell", "dlPrbCap", "pfWeight"),
                               caps={a: (6, 12), b: (6, 12)}, pf_weights={a: (1.0, 4.0), b: (1.0, 4.0)})
        resolver = (lambda _doc, **_kw: (lambda label: int(label) if str(label).isdigit()
                                         else amf.get(str(label))))
        with patch.object(env.EmulatedKpmStream, "__init__", init), \
                patch.object(agent_module, "role_identity_resolver", resolver), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")):
            sitting = build_hardware_free_agent_sitting(request, tmp_dir=temporary.name, ran=ran,
                                                        stamp="20260916T020000Z")
            sitting.confirm()
            sitting.run()
        return sitting

    def test_ids_and_roles_run_the_same_trials_with_the_same_evidence(self):
        by_id = self.sit(("34", "42"), {})
        by_role = self.sit(("ue1", "ue2"), {"ue1": 34, "ue2": 42})
        rename = {"34": "ue1", "42": "ue2"}

        def row(trial):
            kernel = trial.kernel
            return (trial.control_id,
                    {k.split("@")[0] + "@" + rename.get(k.split("@")[1], k.split("@")[1]): v
                     for k, v in trial.configuration.items()},
                    kernel.get("terminalState"), kernel.get("outcome"),
                    kernel.get("measurementSufficiency"), trial.window_valid)

        self.assertTrue(by_id.grid.trials)
        self.assertEqual([row(t) for t in by_id.grid.trials], [row(t) for t in by_role.grid.trials])
        self.assertEqual((by_id.termination, by_id.kernel_termination),
                         (by_role.termination, by_role.kernel_termination))
        # Nothing the Kernel read for the role sitting was a gap the id sitting did not have.
        def gaps(sitting):
            found = {}
            for event in sitting.runtime.event_store.iterate():
                if event.event_kind == "RawSampleIngested":
                    sample = event.payload.get("sample", event.payload)
                    key = str(sample["counterId"]).replace("@34", "@ue1").replace("@42", "@ue2")
                    found.setdefault(key, []).append(bool(sample.get("missingIntervals")))
            return found
        id_gaps, role_gaps = gaps(by_id), gaps(by_role)
        self.assertIn("counter/joint@ue1/ue-dl-prb-cap", role_gaps)
        self.assertFalse(all(id_gaps["counter/joint@ue1/ue-dl-prb-cap"]))
        self.assertEqual(id_gaps, role_gaps)

class TheModelsSpeakRolesToo(unittest.TestCase):
    """The three-agent path validates model answers against role-keyed axes and scopes."""

    def test_role_keyed_target_and_control_answers_map_onto_the_frozen_catalog(self):
        import json
        from tests import test_agent_sitting as fixture

        def roles(document):
            text = json.dumps(document)
            for number, role in (("131", "ue1"), ("132", "ue2")):
                text = text.replace("@" + number, "@" + role).replace('"%s"' % number, '"%s"' % role)
            return json.loads(text)

        resolver, models = fixture.ScriptedResolver.by_role(
            {"target": [roles(fixture.T_ANSWER)], "control": [roles(fixture.C_ANSWER)]},
            method=fixture.METHOD_THREE_AGENT)
        amf = {"ue1": 34, "ue2": 42}
        original_init = agent_env.EmulatedKpmStream.__init__

        def init(stream, *args, **kwargs):
            original_init(stream, *args, **kwargs)
            stream.amf_of = amf
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(agent_env.EmulatedKpmStream, "__init__", init), \
                patch.object(agent_module, "role_identity_resolver",
                             lambda _doc, **_kw: (lambda label: amf.get(str(label)))), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")):
            ran = EmulatedRan(ues={"ue1": HOME, "ue2": HOME}, cells={HOME: 5.0, TARGET: 5.0},
                              offered_load_mbps={"ue1": 4.0, "ue2": 4.0}, noise_sigma=0.0)
            request = AgentRequest(intents=tuple(dict(record, requirement=dict(
                                       record["requirement"], value=value, bound=bound))
                                   for record, value, bound in zip(INTENTS, (3.0, 1.5), (2.0, 1.0))),
                                   method=fixture.METHOD_THREE_AGENT, budget_trials=2,
                                   role_models=models.to_record())
            sitting = build_hardware_free_agent_sitting(
                fixture.fixture_scale(request, list(ran.ues)), tmp_dir=tmp, ran=ran,
                role_resolver=resolver, stamp="20260916T030000Z")
            self.assertEqual(("C0", "C1", "C2"), sitting.controls.control_ids)
            self.assertEqual([], sitting.preflight["unmappedControls"])
            self.assertEqual([], sitting.preflight["droppedCandidates"])
            sitting.confirm()
            sitting.run()
        accepted = {call.role: call.accepted for call in sitting.agents.calls}
        self.assertTrue(accepted["target"] and accepted["control"])
        # 2026-09-20: a passing trial is returned to the baseline (PASS_RESET), not finalized.
        self.assertEqual("PASS_RESET", sitting.grid.trials[0].kernel.get("outcome"))

class AHardwareDisconnectIsWaitedOutNotCountedAsAFailure(ARoleLabelledSittingRebindsAcrossReRegistration):
    """ue2 vanishes (no AMF id, no KPM, its configuration unreadable) and comes back as 43."""

    def disconnect(self, *, during_trial=None, between_after_trial=None, back_after_ms=5000,
                   never_back=False, back_on=None, who="ue2", back_id=None,
                   stop_when_waiting=False):
        from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult
        state = {"trials": 0, "back_at": None}
        amf = self.amf
        adapters = []
        original_adapter_init = agent_env.EmulatedActuationAdapter.__init__

        def adapter_init(adapter, *args, **kwargs):
            original_adapter_init(adapter, *args, **kwargs)
            adapters.append(adapter)
        original_read = agent_env.EmulatedActuationAdapter._read

        def read(adapter, reference, detail):
            if adapter.ue == who and amf[who] is None:
                return GatewayResult(outcome=GatewayOutcome.ERROR, evidence_refs=(reference,),
                                     detail="UE context gone")
            return original_read(adapter, reference, detail)

        original_advance = agent_env.EmulatedRan.advance

        def advance(ran, ms):
            original_advance(ran, ms)
            if state.get("drop_at") is not None and ran.ms >= state["drop_at"]:
                amf[who], state["drop_at"] = None, None
                state["back_at"] = None if never_back else ran.ms + back_after_ms + 60_000
            if state["back_at"] is not None and ran.ms >= state["back_at"] and amf[who] is None:
                amf[who] = back_id or {"ue1": 35, "ue2": 43}[who]
                if back_on is not None:  # the UE attached to the other cell this time
                    ran.ues[who] = back_on
                    for adapter in adapters:
                        if adapter.axis == f"servingCell@{who}":
                            adapter.apply_drift({f"servingCell@{who}": back_on})

        original_run_trial = agent_module.AgentSitting._run_trial

        def run_trial(sitting, *args, **kwargs):
            state["trials"] += 1
            if state["trials"] == during_trial:
                state["drop_at"] = sitting.clock.ran.ms + 1000
            result = original_run_trial(sitting, *args, **kwargs)
            if state["trials"] == between_after_trial:
                amf[who] = None
                state["back_at"] = None if never_back else sitting.clock.ran.ms + back_after_ms
            return result

        original_await = agent_module.AgentSitting._await_reregistration

        def await_(sitting, ues, reserve_ms):
            if stop_when_waiting:
                sitting.stopped = True
            if state["back_at"] is not None:
                state["back_at"] = min(state["back_at"], sitting.clock.ran.ms + back_after_ms)
            return original_await(sitting, ues, reserve_ms)

        for item in (patch.object(agent_env.EmulatedActuationAdapter, "__init__", adapter_init),
                     patch.object(agent_env.EmulatedActuationAdapter, "_read", read),
                     patch.object(agent_env.EmulatedRan, "advance", advance),
                     patch.object(agent_module.AgentSitting, "_run_trial", run_trial),
                     patch.object(agent_module.AgentSitting, "_await_reregistration", await_)):
            item.start()
            self.addCleanup(item.stop)
        return state

    def test_an_unrelated_ue_vanishing_does_not_lock_the_trial_down(self):
        """2026-09-23 오너 지시: "무관한 ue가 떨어져도 그냥 떨어진 부분은 제외하고 다시 붙여서
        진행해".  Trial 2 moves ue2 only; ue1 vanishes.  Until then the joint recovery read
        failed as a whole on ue1 and the lockdown stood with ue2 perfectly readable.  Now the
        read excludes ue1 -- this trial never wrote to it -- confirms ue2's own recovery
        directly, and the sitting goes on.
        """
        sitting = self.build(budget_trials=3, axes=("servingCell",))
        self.disconnect(during_trial=2, who="ue1")
        sitting.confirm()
        sitting.run()
        second = sitting.grid.trials[1]
        self.assertNotEqual("INCIDENT_LOCKDOWN", second.kernel.get("terminalState"),
                            "ue2's recovery was readable; ue1 was never touched")
        self.assertNotEqual("EXECUTION_FAILURE", sitting.termination)

    def test_a_disconnect_lockdown_on_the_last_trial_ends_on_the_budget_without_waiting(self):
        sitting = self.build(budget_trials=2, axes=("servingCell",))
        self.disconnect(during_trial=2, never_back=True)
        sitting.confirm()
        sitting.run()
        self.assertEqual("INCIDENT_LOCKDOWN", sitting.grid.trials[-1].kernel.get("terminalState"))
        self.assertEqual("BUDGET_EXHAUSTED", sitting.termination, sitting.termination_detail)
        disconnect, = sitting.hardware_disconnects
        self.assertNotIn("waitedMs", disconnect)  # nothing left to try, so nothing was waited for
        self.assertTrue(disconnect["trials"][-1]["excused"])

    def test_a_ue_back_under_the_same_id_revokes_the_exemption(self):
        sitting = self.build(budget_trials=3, axes=("servingCell",))
        self.disconnect(during_trial=2, back_id=42)
        sitting.confirm()
        sitting.run()
        self.assertEqual("EXECUTION_FAILURE", sitting.termination)
        entry = sitting.hardware_disconnects[0]["trials"][-1]
        self.assertEqual((entry["excused"], entry["excuseRevoked"]), (False, "same-id"))
        self.assertFalse(sitting._disconnect_trial_ids())
        self.assertEqual("retention is blocked by an unresolved recovery incident",
                         sitting.retained.get("detail"))
        unresolved = [item for item in sitting.episode_record()["completion"]["unresolved"] if "trialId" in item]
        self.assertFalse(any(item["hardwareDisconnect"] for item in unresolved))

    def test_an_operator_stop_while_waiting_is_the_stated_reason(self):
        sitting = self.build(budget_trials=3, axes=("servingCell",))
        self.disconnect(between_after_trial=1, never_back=True, stop_when_waiting=True)
        sitting.confirm()
        sitting.run()
        self.assertEqual("OPERATOR_STOP", sitting.termination, sitting.termination_detail)

    def test_a_lockdown_the_disconnect_caused_waits_rebinds_and_continues(self):
        sitting = self.build(budget_trials=3, axes=("servingCell",))
        self.disconnect(during_trial=2)
        sitting.confirm()
        sitting.run()
        states = [t.kernel.get("terminalState") for t in sitting.grid.trials]
        self.assertIn("INCIDENT_LOCKDOWN", states, states)
        self.assertEqual(3, len(sitting.grid.trials), states)
        self.assertEqual("BUDGET_EXHAUSTED", sitting.termination, sitting.termination_detail)
        disconnect, = sitting.hardware_disconnects
        self.assertTrue(disconnect["reregistered"], disconnect)
        self.assertEqual({"previousAmfUeNgapId": 42, "amfUeNgapId": 43}, disconnect["ues"]["ue2"])
        self.assertEqual(["INCIDENT_LOCKDOWN"], [t["terminalState"] for t in disconnect["trials"]])
        self.assertGreater(disconnect["waitedMs"], 0)
        rebound, = sitting.identity_rebinds
        self.assertEqual("REBOUND", rebound["outcome"])
        self.assertEqual(1, len(sitting.retired_runtimes))
        # The trial after the rebind ran on the new case; the locked trial stays in the record.
        self.assertNotIn("/rebind-1:", sitting.grid.trials[1].kernel["trialId"])
        self.assertIn("/rebind-1:", sitting.grid.trials[2].kernel["trialId"])
        episode = sitting.episode_record()
        unresolved = episode["completion"]["unresolved"]
        locked = [item for item in unresolved if "trialId" in item]
        self.assertTrue(locked and all(item["hardwareDisconnect"] for item in locked), unresolved)
        # Retention is no longer blocked by a lockdown the disconnect explains.
        self.assertNotEqual("retention is blocked by an unresolved recovery incident",
                            sitting.retained.get("detail"))
        self.assertEqual(disconnect, episode["hardwareDisconnects"][0])

    def test_a_ue_gone_between_trials_is_waited_for_before_the_next_trial(self):
        sitting = self.build(budget_trials=3, axes=("servingCell",))
        self.disconnect(between_after_trial=1)
        sitting.confirm()
        sitting.run()
        states = [t.kernel.get("terminalState") for t in sitting.grid.trials]
        self.assertNotIn("INCIDENT_LOCKDOWN", states)
        self.assertEqual(3, len(sitting.grid.trials))
        disconnect, = sitting.hardware_disconnects
        self.assertTrue(disconnect["reregistered"])
        # The trial the drop followed is named for the footnote; its verdict stands as judged.
        self.assertEqual(["SETTLED_NON_SUCCESS"], [t["terminalState"] for t in disconnect["trials"]])
        self.assertEqual("REBOUND", sitting.identity_rebinds[0]["outcome"])
        self.assertEqual(43, sitting.identities["ue2"].amf_ue_ngap_id)

    def test_a_ue_back_on_the_other_cell_opens_the_new_case_on_that_cell(self):
        sitting = self.build(budget_trials=3, axes=("servingCell",))
        self.disconnect(between_after_trial=1, back_on=TARGET)
        sitting.confirm()
        sitting.run()
        rebound, = sitting.identity_rebinds
        self.assertEqual("REBOUND", rebound["outcome"])
        self.assertEqual(TARGET, rebound["initialConfiguration"]["servingCell@ue2"])
        self.assertEqual(3, len(sitting.grid.trials))
        after = [t.kernel for t in sitting.grid.trials[1:]]
        self.assertTrue(all(k.get("terminalState", "").startswith("SETTLED_") for k in after), after)

    def test_a_ue_that_never_returns_ends_the_sitting_at_its_deadline_not_as_a_failure(self):
        # B is relieved of the wait, so B never runs out while waiting: what ends it is
        # the wait's own cap, and since 2026-09-23 that is HARDWARE_UNAVAILABLE -- a
        # hardware reason no longer mixed into the DEADLINE count.
        sitting = self.build(budget_trials=3, axes=("servingCell",), deadline_ms=400_000)
        self.disconnect(between_after_trial=1, never_back=True)
        sitting.confirm()
        sitting.run()
        self.assertEqual(1, len(sitting.grid.trials))
        self.assertEqual("HARDWARE_UNAVAILABLE", sitting.termination, sitting.termination_detail)
        disconnect, = sitting.hardware_disconnects
        self.assertFalse(disconnect["reregistered"])
        self.assertEqual([], sitting.identity_rebinds)
        self.assertIn("hardware disconnect", sitting.retained["detail"])

class TheLiveCapParticipantAddressesTheNewIdAfterARebind(unittest.TestCase):
    """The live composition (R1 cap adapter, KPM readback), not the emulator's overrides."""

    def test_rebind_recomposes_the_cap_participant_on_the_new_amf_id(self):
        import json
        from oran.campaign5.families import CAMPAIGN5_FAMILIES
        from assurance.objectives.action102_support import CAP_ACTION_ID
        from tools.hfconsole.agent_env import (
            EmulatedClock, EmulatedKpiObserver, EmulatedKpmStream, EmulatedPolicyPort,
            HermeticDeployment, POLICY_TYPE_ID)
        family = CAMPAIGN5_FAMILIES["cap"]
        amf = {"ue1": 34}
        cleared = []
        with tempfile.TemporaryDirectory() as directory, \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch.object(agent_module, "role_identity_resolver",
                             lambda _doc, **_kw: (lambda label: amf.get(str(label)))):
            ran = EmulatedRan(ues={"ue1": HOME}, cells={HOME: 5.0, TARGET: 5.0}, noise_sigma=0.0)
            clock = EmulatedClock(ran)
            stream = EmulatedKpmStream(clock, ran)
            stream.amf_of = amf
            profile = HermeticDeployment.write(directory, ues=ran.ues, cells=ran.cells)

            def lines():
                records = [json.loads(line) for line in stream()]
                for record in records:
                    record["ues"][0]["measurements"] = [
                        {"name": family.readback_counter, "type": "int", "value": ran.caps["ue1"]}]
                return [json.dumps(record) for record in records]

            record = dict(INTENTS[0], requirement=dict(INTENTS[0]["requirement"], value=3.0, bound=2.0))
            sitting = build_agent_sitting(
                profile, AgentRequest(intents=(record,), method=METHOD_DETERMINISTIC,
                                      axes=("servingCell", "dlPrbCap"), caps={"ue1": (6, 12)},
                                      restrict_catalog_to_candidates=False),
                read_new_lines=lines, policy_port=EmulatedPolicyPort(POLICY_TYPE_ID),
                action_policy_ports={CAP_ACTION_ID: object()}, ports=clock,
                scope_clearer=lambda *args, **kwargs: cleared.append(kwargs) or (),
                kpi_observer=EmulatedKpiObserver(ran), stamp="20260916T010000Z")
            try:
                catalog = [str(e.candidate.candidate_id) for e in sitting.runtime.catalog_entries()]
                self.assertEqual(34, sitting.identities["ue1"].amf_ue_ngap_id)
                amf["ue1"] = 35  # re-registered
                clock.sleep_ms(200)
                # Inside the case nothing moves: its transactions still address the id it was
                # composed over (a rebind, between trials, is the only way to the new one).
                old_participant = sitting.supplementary[-1]
                scope = {"controlledUe@ue1": {"ueId": "ue1", "cellId": HOME}}
                command = {"operation": "APPLY", "transactionId": "tx0", "trialId": "trial",
                           "fencingToken": 2, "commandSequence": 1, "commandIndex": 1,
                           "idempotencyKey": "tx0:APPLY:2:1", "axis": "dlPrbCap@ue1",
                           "value": "6", "scope": scope}
                self.assertEqual("34", old_participant.adapter._build(command)["config"]["ueId"])
                cleared.clear()
                sitting._rebind_if_reregistered()
                rebound, = sitting.identity_rebinds
                self.assertEqual("REBOUND", rebound["outcome"], rebound)
                self.assertEqual(35, sitting.identities["ue1"].amf_ue_ngap_id)
                # Both scopes freed: the old id's (its context is gone) and the new one's.
                self.assertEqual([(34, True), (35, False)],
                                 [(item["amf_ue_ngap_id"], item["withdraw_verified"]) for item in cleared])
                self.assertEqual(catalog, [str(e.candidate.candidate_id)
                                           for e in sitting.runtime.catalog_entries()])
                participant = sitting.supplementary[-1]
                self.assertEqual("dlPrbCap@ue1", participant.axis)
                scope = {"controlledUe@ue1": {"ueId": "ue1", "cellId": HOME}}
                self.assertEqual({"maxDlPrbs": ran.caps["ue1"]},
                                 participant.counter_reader.read(family.readback_counter, scope))
                body = participant.adapter._build({
                    "operation": "APPLY", "transactionId": "tx", "trialId": "trial",
                    "fencingToken": 2, "commandSequence": 1, "commandIndex": 1,
                    "idempotencyKey": "tx:APPLY:2:1", "axis": "dlPrbCap@ue1",
                    "value": "6", "scope": scope})
                self.assertEqual("35", body["config"]["ueId"])
            finally:
                for item in sitting.supplementary:
                    item.close()


    def test_a_re_registration_during_composition_leaves_one_id_per_ue(self):
        """2026-09-20, board 20260919T193249: the cap participant re-pinned ue1 to its
        new id while waiting, the steering participant kept the old one, and the
        steering policy went out for a UE that no longer existed."""
        import json
        from oran.campaign5.families import CAMPAIGN5_FAMILIES
        from assurance.objectives.action102_support import CAP_ACTION_ID
        from tools.hfconsole.agent_env import (
            EmulatedClock, EmulatedKpiObserver, EmulatedKpmStream, EmulatedPolicyPort,
            HermeticDeployment, POLICY_TYPE_ID)
        family = CAMPAIGN5_FAMILIES["cap"]
        amf = {"ue1": 34}
        real_wait = agent_module._await_addressable
        moved = []

        def wait_and_move(**kwargs):
            if not moved:            # the UE re-registers while composition waits on it
                moved.append(True)
                amf["ue1"] = 35
            return real_wait(**kwargs)

        with tempfile.TemporaryDirectory() as directory, \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch.object(agent_module, "_await_addressable", wait_and_move), \
                patch.object(agent_module, "role_identity_resolver",
                             lambda _doc, **_kw: (lambda label: amf.get(str(label)))):
            ran = EmulatedRan(ues={"ue1": HOME}, cells={HOME: 5.0, TARGET: 5.0}, noise_sigma=0.0)
            clock = EmulatedClock(ran)
            stream = EmulatedKpmStream(clock, ran)
            stream.amf_of = amf

            def lines():
                records = [json.loads(line) for line in stream()]
                for record in records:
                    record["ues"][0]["measurements"] = [
                        {"name": family.readback_counter, "type": "int", "value": ran.caps["ue1"]}]
                return [json.dumps(record) for record in records]

            record = dict(INTENTS[0], requirement=dict(INTENTS[0]["requirement"], value=3.0, bound=2.0))
            sitting = build_agent_sitting(
                HermeticDeployment.write(directory, ues=ran.ues, cells=ran.cells),
                AgentRequest(intents=(record,), method=METHOD_DETERMINISTIC,
                             axes=("servingCell", "dlPrbCap"), caps={"ue1": (6, 12)},
                             restrict_catalog_to_candidates=False),
                read_new_lines=lines, policy_port=EmulatedPolicyPort(POLICY_TYPE_ID),
                action_policy_ports={CAP_ACTION_ID: object()}, ports=clock,
                scope_clearer=lambda *args, **kwargs: (),
                kpi_observer=EmulatedKpiObserver(ran), stamp="20260920T010000Z")
            try:
                self.assertTrue(moved)
                self.assertEqual(35, sitting.identities["ue1"].amf_ue_ngap_id)
                steering_pins = {str(builder.deployment.ue_scope_id)
                                 for builder in sitting.policy_builders
                                 if hasattr(getattr(builder, "deployment", None), "ue_scope_id")}
                self.assertEqual({"35"}, steering_pins)
            finally:
                for item in sitting.supplementary:
                    item.close()


if __name__ == "__main__":
    unittest.main()
