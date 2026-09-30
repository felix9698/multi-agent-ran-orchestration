"""One Campaign 5 family, over the official path, driven from a shell.

All six xApp actions were already wired ``rApp -> A1 -> A1-P -> xApp ->
E2SM-RC``; what was missing was a way to run one, so exercising a cap over the
air meant firing the xApp directly, off the path the paper claims.
:mod:`tools.campaign5.live_run` is that entry point, and this file drives it
end to end with the transport and the KPM stream injected -- the same two seams
the Cockpit's live root exposes, and the same consequence: an injected port
makes the composition ``MOCK``, and the run document says so.

What is asserted is what makes the driver safe rather than merely working:

* a stale or absent UE attribution refuses **before** anything is written;
* a producer refusal is ``REJECTED`` with zero writes, never a lost message;
* an acknowledgement is not an effect -- a policy the counter does not
  corroborate is ``UNKNOWN`` and the run reverses anyway;
* the baseline is read back before the scope is released, and the durable
  binding says ``RESTORED`` rather than "the DELETE returned";
* the run reports no Kernel decision axis, because it ran no Kernel case.

Hermetic: no socket, no radio, no subprocess.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from assurance.gateway.r1_binding_journal import BindingState

from oran.campaign5.families import CAMPAIGN5_FAMILIES
from oran.campaign5.producer import A1Conflict, A1ValidationError

from tools.campaign5 import live_run
from tools.campaign5.live_run import Campaign5LiveError, run_live_action
from tools.liveconsole.profile import ACTION_PRODUCER_KEY, LiveConsoleError

CAP = CAMPAIGN5_FAMILIES["cap"]
PRIORITY = CAMPAIGN5_FAMILIES["priority"]
MCS = CAMPAIGN5_FAMILIES["mcs"]
POWER = CAMPAIGN5_FAMILIES["power"]

BASELINE_PRBS = 24
TARGET_PRBS = 12


class _Radio:
    """What the scheduler is actually holding, as one number.

    The point of a corroborated readback is that the producer's claim and the
    equipment's counter are two observations, so a fixture in which the counter
    is a constant can only ever test the half that fails.  This is the other
    half: the A1-P port moves it on a create and puts it back on a delete, and
    the KPM stream reports it.
    """

    def __init__(self, value: int) -> None:
        self.value = int(value)
        self.history: List[int] = [int(value)]

    def set(self, value: int) -> None:
        self.value = int(value)
        self.history.append(int(value))


class _Port:
    """The A1-P transport, injected so nothing opens a socket.

    It behaves as the in-repo producer does for the parts this driver depends
    on: a policy id per create, a delete that removes it, and a status object
    whose ``readback`` is what the corroboration compares against.  Given a
    :class:`_Radio` it also moves the scheduler, which is what makes a verified
    apply and a verified restore reachable at all.
    """

    def __init__(self, *, family=CAP, refuse: Optional[type] = None,
                 verified: bool = True, applied: Optional[Dict[str, Any]] = None,
                 radio: Optional[_Radio] = None) -> None:
        self.family = family
        self.refuse = refuse
        self.verified = verified
        self.applied = applied
        self.radio = radio
        self.created: List[Dict[str, Any]] = []
        self.deleted: List[str] = []

    def get_policy_type(self, policy_type_id):
        # The real advertisement, from the real producer: the discovery gate
        # compares three digests, and a stub that returned empty schemas would
        # be testing that the gate can be fooled rather than that it holds.
        from oran.campaign5.producer import Campaign5PolicyProducer

        return Campaign5PolicyProducer().get_policytype(policy_type_id)

    def create_policy(self, ric, policy_type_id, body):
        if self.refuse is not None:
            raise self.refuse("injected producer refusal")
        self.created.append(dict(body))
        if self.radio is not None:
            self.radio.set(body["config"][self.family.value_fields[0]])
        return {"policyId": f"pol-{self.family.key}-1"}

    def update_policy(self, policy_id, body):
        self.created.append(dict(body))
        return {"policyId": policy_id}

    def delete_policy(self, policy_id):
        self.deleted.append(policy_id)
        if self.radio is not None:
            self.radio.set(self.radio.history[0])
        return None

    def get_policy_status(self, policy_id):
        if not self.created:
            return {}
        config = dict(self.created[-1]["config"])
        if self.applied is not None:
            config.update(self.applied)
        if not self.verified:
            return {"enforceStatus": "NOT_ENFORCED",
                    "aicStatus": {"episodeState": "APPLIED_UNVERIFIED",
                                  "readback": {"result": "NOT_AVAILABLE"}}}
        return {"enforceStatus": "ENFORCED",
                "aicStatus": {"episodeState": "APPLIED_VERIFIED",
                              "readback": {"result": "VERIFIED",
                                           self.family.observed_key: config}}}


class _Driven(unittest.TestCase):
    """The live console's own deployment fixture, driven by this driver.

    Reused rather than rebuilt: what is under test is the driver, and a second
    synthesised deployment identity would prove it against a deployment nobody
    else resolves.
    """

    def setUp(self):
        from tests.test_liveconsole import LiveConsoleFixture

        self.fixture = LiveConsoleFixture("run")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.clock = _clock()

    def profile(self, family=CAP, **overrides):
        block = {
            ACTION_PRODUCER_KEY: {
                "apiRoot": "https://127.0.0.1:9445/r1",
                "policyTypes": {
                    family.policy_type_id: {
                        "adapter": family.adapter_name,
                        "actionId": family.catalog_action_id}},
                "secretRefs": {"mtlsCa": "file:///dev/null"},
                "r1StateDir": str(self.fixture.tmp / f"r1-{family.key}"),
            }}
        block.update(overrides)
        return self.fixture.write_profile(live_console=block)

    def counter_line(self, value, *, counter=None, at_ms=0, ues=None,
                     node=None, epoch=None):
        """One KPM indication carrying a configuration counter for a UE."""
        from tests.test_liveconsole import (
            AMF_UE_NGAP_ID, BASE_INSTANT, HOME_E2_NODE)
        from datetime import timedelta

        received = BASE_INSTANT + timedelta(milliseconds=int(at_ms))
        return json.dumps({
            "event": "kpm_indication",
            "kpm_msg_format": 3,
            "e2_node": node or HOME_E2_NODE,
            "connection_epoch": int(
                self.fixture.home_epoch if epoch is None else epoch),
            "recv_unix_us": int(received.timestamp() * 1_000_000),
            "ues": [{"amf_ue_ngap_id": int(identifier),
                     "measurements": [{"name": counter or CAP.readback_counter,
                                       "type": "int", "value": int(value)}]}
                    for identifier in (ues or (AMF_UE_NGAP_ID,))],
        })

    def drive(self, *, family=CAP, values=None, port=None, lines=None,
              profile=None, radio=None, **kwargs):
        """One run against a gNB that publishes what it holds.

        ``lines`` swaps that for a fixed list, which is how a test asks for a
        counter that does *not* follow the policy -- the "an acknowledgement is
        not an effect" case.
        """
        from tests.test_liveconsole import AMF_UE_NGAP_ID

        kwargs.setdefault("amf_ue_ngap_id", AMF_UE_NGAP_ID)
        if lines is not None:
            stream = _stream(list(lines))
        else:
            radio = radio if radio is not None else _Radio(_baseline_of(family))
            stream = self.live_stream(radio, family=family)
        if port is None:
            port = _Port(family=family, radio=radio)
        return run_live_action(
            profile_path=str(profile if profile is not None
                             else self.profile(family)),
            family_key=family.key,
            values=values if values is not None else {"maxDlPrbs": TARGET_PRBS},
            hold_s=0,
            ports=self.clock,
            policy_port=port,
            read_new_lines=stream,
            evidence_dir=self.fixture.tmp / "evidence",
            **kwargs)

    def default_lines(self):
        """Attribution and the configuration counter, both fresh."""
        from tests.test_liveconsole import AMF_UE_NGAP_ID

        return [self.fixture.kpm_line(ues=(AMF_UE_NGAP_ID,)),
                self.counter_line(BASELINE_PRBS)]

    def live_stream(self, radio, *, family=CAP):
        """A gNB that keeps publishing what it is holding, as one does.

        A fixed list of lines would go stale the moment the driver's clock
        advanced past the freshness window -- a property of the fixture, not of
        the code -- and could never show a value changing.  This mints one
        attribution and one counter indication per drain, at the clock's
        current instant, carrying whatever the radio currently holds.
        """
        from tests.test_liveconsole import AMF_UE_NGAP_ID

        def read() -> Sequence[str]:
            return (self.fixture.kpm_line(ues=(AMF_UE_NGAP_ID,),
                                          at_ms=self.clock.ms, offset_ms=0),
                    self.counter_line(radio.value, at_ms=self.clock.ms,
                                      counter=family.readback_counter))

        return read


def _clock():
    from tests.test_liveconsole import _Clock

    return _Clock()


def _stream(lines: List[str]):
    """A tail that hands out what has been published and then nothing."""
    remaining = list(lines)

    def read() -> Sequence[str]:
        taken = tuple(remaining)
        del remaining[:]
        return taken

    read.publish = remaining.append  # type: ignore[attr-defined]
    return read


class TheFamilyIsAddressedThroughItsOwnAdapter(_Driven):

    def test_a_cap_run_creates_the_policy_and_reverses_it(self):
        radio = _Radio(BASELINE_PRBS)
        port = _Port(radio=radio)
        document = self.drive(port=port, radio=radio)
        self.assertEqual(document["family"], "cap")
        self.assertEqual(document["policyTypeId"], CAP.policy_type_id)
        self.assertEqual(document["adapter"], "r1-cap")
        # Style 2, action 102, RAN parameters 211/212 -- from the family, not
        # from this test's imagination.
        self.assertEqual(document["rc"], {"style": 2, "actionId": 102,
                                          "ranParameterIds": [211, 212]})
        self.assertEqual(
            [kind for kind, _outcome in
             document["settlement"]["gatewayOperations"]],
            ["PREPARE", "READY", "COMMIT", "REVERSE_ROLLBACK"])
        self.assertEqual(port.created[0]["config"]["maxDlPrbs"], TARGET_PRBS)
        self.assertEqual(port.deleted, ["pol-cap-1"])

    def test_the_ue_comes_from_the_stream_and_the_scope_names_it(self):
        from tests.test_liveconsole import AMF_UE_NGAP_ID, HOME_NCI

        document = self.drive()
        self.assertEqual(document["scope"],
                         {"cellId": str(HOME_NCI), "ueId": str(AMF_UE_NGAP_ID)})
        self.assertEqual(document["observedUe"]["amfUeNgapId"], AMF_UE_NGAP_ID)
        self.assertEqual(document["expectedAttribution"]["amfUeNgapId"],
                         AMF_UE_NGAP_ID)

    def test_the_baseline_is_the_value_the_counter_actually_held(self):
        document = self.drive()
        self.assertEqual(document["baselineConfig"], {"maxDlPrbs": BASELINE_PRBS})
        self.assertEqual(document["targetConfig"], {"maxDlPrbs": TARGET_PRBS})

    def test_a_priority_run_uses_its_own_type_counter_and_adapter(self):
        radio = _Radio(4)
        port = _Port(family=PRIORITY, radio=radio)
        document = self.drive(
            family=PRIORITY, values={"pfWeight": 8}, port=port, radio=radio,
            profile=self.profile(PRIORITY))
        self.assertEqual(document["policyTypeId"], PRIORITY.policy_type_id)
        self.assertEqual(document["adapter"], "r1-priority")
        self.assertEqual(document["rc"]["actionId"], 103)
        self.assertEqual(document["baselineConfig"], {"pfWeight": 4})
        self.assertEqual(port.created[0]["config"]["pfWeight"], 8)
        self.assertTrue(document["settlement"]["appliedVerified"])

    def test_the_composition_is_mock_because_its_ports_were_injected(self):
        document = self.drive()
        self.assertEqual(document["sessionMode"], "MOCK")
        self.assertEqual(sorted(document["injectedPorts"]),
                         ["kpmTail", "policyPort"])


class TheEffectIsCorroboratedOrItIsUnknown(_Driven):

    def test_a_counter_that_agrees_settles_applied_and_restored(self):
        radio = _Radio(BASELINE_PRBS)
        document = run_live_action(
            profile_path=str(self.profile()), family_key="cap",
            values={"maxDlPrbs": TARGET_PRBS}, hold_s=0, ports=self.clock,
            policy_port=_Port(radio=radio),
            read_new_lines=self.live_stream(radio),
            evidence_dir=self.fixture.tmp / "evidence",
            amf_ue_ngap_id=_ue())
        self.assertEqual(radio.history, [BASELINE_PRBS, TARGET_PRBS,
                                         BASELINE_PRBS])
        self.assertTrue(document["settlement"]["appliedVerified"])
        self.assertTrue(document["settlement"]["restoredVerified"])
        self.assertEqual(document["settlement"]["outcome"],
                         "APPLIED_VERIFIED_AND_RESTORED")
        self.assertEqual(document["binding"]["state"],
                         BindingState.RESTORED.value)
        self.assertFalse(document["binding"]["holdsScope"])

    def test_an_acknowledged_policy_the_counter_does_not_confirm_is_unknown(self):
        # ACK is not effect: the producer says ENFORCED, the counter still
        # reads the baseline, and the run refuses to call that applied.
        document = self.drive(lines=self.default_lines(), port=_Port())
        self.assertFalse(document["settlement"]["appliedVerified"])
        self.assertIn("COMMIT", [kind for kind, _ in
                                 document["settlement"]["gatewayOperations"]])
        self.assertNotIn(
            "ACKED",
            [outcome for kind, outcome in
             document["settlement"]["gatewayOperations"] if kind == "COMMIT"])
        # And it reverses anyway: a write that may have landed is an obligation.
        kinds = [kind for kind, _ in document["settlement"]["gatewayOperations"]]
        self.assertIn("REVERSE_ROLLBACK", kinds)
        # And when the reversal cannot read the configuration either, the run
        # halts rather than walking away from a policy it may have created.
        self.assertIn("STOP", kinds)
        self.assertEqual(document["settlement"]["outcome"],
                         "UNRESOLVED_WITHDRAWAL_OUTSTANDING")

    def test_every_readback_answer_is_kept_with_the_identity_it_required(self):
        document = self.drive(lines=self.default_lines(), port=_Port())
        self.assertTrue(document["readbackLog"])
        for entry in document["readbackLog"]:
            self.assertEqual(entry["counter"], CAP.readback_counter)
            self.assertEqual(entry["expected"]["amfUeNgapId"], _ue())

    def test_a_counter_from_another_epoch_does_not_verify(self):
        from tests.test_liveconsole import AMF_UE_NGAP_ID

        lines = [self.fixture.kpm_line(ues=(AMF_UE_NGAP_ID,)),
                 self.counter_line(BASELINE_PRBS)]
        document = self.drive(
            port=_Port(),
            lines=lines + [self.counter_line(TARGET_PRBS, epoch=999)])
        self.assertFalse(document["settlement"]["appliedVerified"])


class ARefusalIsARefusal(_Driven):

    def test_a_producer_refusal_is_rejected_with_no_write(self):
        for error in (A1ValidationError, A1Conflict):
            with self.subTest(error=error.__name__):
                # A fresh instant per subtest: the run's durable state is keyed
                # by its timestamp, and two runs at one instant would share a
                # binding journal and refuse each other on scope -- which is
                # correct behaviour and not what this asserts.
                self.clock.sleep_ms(2000)
                port = _Port(refuse=error)
                document = self.drive(port=port)
                self.assertEqual(document["settlement"]["outcome"], "REJECTED")
                self.assertEqual(document["settlement"]["refusal"], "REJECTED")
                self.assertEqual(document["writeCounts"]["refused"], 1)
                self.assertEqual(document["writeCounts"]["applies"], 0)
                self.assertEqual(port.created, [])
                self.assertEqual(port.deleted, [])

    def test_an_unobservable_ue_refuses_before_anything_is_written(self):
        with self.assertRaises(LiveConsoleError) as caught:
            self.drive(lines=[])
        message = str(caught.exception)
        self.assertIn("not observable on the live KPM stream", message)
        # The refusal states the window and what the reader actually saw, so a
        # silent stream and a stale one are told apart.
        self.assertIn("freshness window", message)

    def test_a_named_ue_that_is_not_on_the_stream_is_refused_by_name(self):
        with self.assertRaises(LiveConsoleError):
            self.drive(amf_ue_ngap_id=999999)

    def test_no_baseline_reading_refuses_rather_than_writing_blind(self):
        # The counter is absent, so there is no value a restore could aim at.
        with self.assertRaises(Campaign5LiveError) as caught:
            self.drive(lines=[self.fixture.kpm_line()], port=_Port())
        self.assertIn("no reading", str(caught.exception))
        self.assertIn("--baseline", str(caught.exception))

    def test_a_stated_baseline_is_recorded_as_stated(self):
        document = self.drive(lines=[self.fixture.kpm_line()], port=_Port(),
                              baseline={"maxDlPrbs": BASELINE_PRBS})
        self.assertEqual(document["baselineConfig"], {"maxDlPrbs": BASELINE_PRBS})

    def test_a_profile_with_no_action_producer_is_refused_by_name(self):
        with self.assertRaises(Campaign5LiveError) as caught:
            run_live_action(
                profile_path=str(self.fixture.write_profile()),
                family_key="cap", values={"maxDlPrbs": TARGET_PRBS},
                hold_s=0, ports=self.clock, policy_port=_Port(),
                read_new_lines=_stream(self.default_lines()),
                evidence_dir=self.fixture.tmp / "evidence")
        self.assertIn("actionProducer", str(caught.exception))

    def test_a_family_the_producer_does_not_serve_is_refused_by_name(self):
        with self.assertRaises(Campaign5LiveError) as caught:
            self.drive(family=PRIORITY, values={"pfWeight": 8},
                       profile=self.profile(CAP), port=_Port(family=PRIORITY))
        self.assertIn(PRIORITY.catalog_action_id, str(caught.exception))

    def test_an_unknown_family_is_refused_with_the_list(self):
        with self.assertRaises(Campaign5LiveError) as caught:
            run_live_action(profile_path=str(self.profile()),
                            family_key="quota", values={}, hold_s=0)
        self.assertIn("cap", str(caught.exception))


class TheWireCannotCorroborateEveryFamily(_Driven):
    """Two families cannot be verified on this JSONL, and say so.

    ``mcs`` moves two value leaves against one scalar counter record, and
    ``power``'s three-component counter loses its labels.  Both are properties
    of the deployed wire, and both are reported rather than guessed around:
    guessing a leaf would make every mcs run look verified.
    """

    def test_a_two_leaf_family_refuses_the_counter_rather_than_guessing(self):
        reader = live_run.KpmFamilyConfigReader(
            _stream([]), None, family=MCS, now=self.clock.now,
            freshness_bound_ms=8000, expected=_attribution())
        self.assertIsNone(reader.single_leaf)
        self.assertIsNone(reader.read(MCS.readback_counter, {"cellId": "1"}))
        self.assertEqual([entry["outcome"] for entry in reader.reads],
                         ["MULTI_LEAF_COUNTER_UNSUPPORTED"])

    def test_a_single_leaf_family_reads_its_own_leaf(self):
        reader = live_run.KpmFamilyConfigReader(
            _stream([]), None, family=CAP, now=self.clock.now,
            freshness_bound_ms=8000, expected=_attribution())
        self.assertEqual(reader.single_leaf, "maxDlPrbs")

    def test_power_reads_the_cell_counter_by_nb_id_and_pins_the_epoch(self):
        # 2026-09-25: the cell record names its node as KPM spells it; the topology gives the
        # nb id.  The first association answered is pinned; once another appears nothing is
        # answered -- it is not the scheduler this run wrote to.
        from types import SimpleNamespace as NS

        from tools.liveconsole.build import CapReadbackAttribution

        now = self.clock.now()

        def sample(node, epoch, value):
            return NS(counter_id=POWER.readback_counter, observed_at=now,
                      scope_snapshot={"e2_node": node, "connection_epoch": epoch},
                      value=NS(value=value))

        batches = [[sample("plmn=20895;nb=2816/22", "7", 10.0), sample("plmn=20895;nb=3584/22", "7", 8.0)],
                   [sample("plmn=20895;nb=2816/22", "8", 13.0)]]
        adapter = NS(parse_lines=lambda lines: NS(samples=batches.pop(0)))
        stream = _stream(["a"])
        reader = live_run.KpmFamilyConfigReader(
            stream, adapter, family=POWER, now=self.clock.now, freshness_bound_ms=8000,
            expected=CapReadbackAttribution(amf_ue_ngap_id=0, serving_nci=87654321,
                                            e2_node="2816", connection_epoch=0))
        self.assertEqual(reader.read(POWER.readback_counter, {"cellId": "87654321"}),
                         {"txAttenuationDb": 10.0})
        stream.publish("b")
        self.assertIsNone(reader.read(POWER.readback_counter, {"cellId": "87654321"}))
        self.assertIn("EPOCH_CHANGED", [row["outcome"] for row in reader.reads])

    def test_power_uses_the_reader_it_is_given(self):
        import inspect

        from tools.campaign5 import route

        source = inspect.getsource(route.build_official_adapter)
        self.assertIn('cell_power_reader and kpm_reader is not None', source)
        self.assertIn("cell_power_reader=True", inspect.getsource(live_run.run_live_action))


class TheRunSaysWhatItIsAndWhatItIsNot(_Driven):

    def test_it_claims_no_kernel_axis_because_it_ran_no_kernel_case(self):
        document = self.drive()
        self.assertIsNone(document["axes"])
        self.assertIn("evaluator", document["axesNote"])
        self.assertEqual(document["permitIssuer"], "campaign5-live-run")
        self.assertIn("Kernel", document["permitIssuerNote"])

    def test_the_permits_carry_a_monotonic_fence_and_a_bounded_lease(self):
        document = self.drive()
        permits = document["permits"]
        self.assertEqual([entry["tokenKind"] for entry in permits],
                         ["PREPARE", "READY", "COMMIT", "REVERSE_ROLLBACK"])
        for entry in permits:
            self.assertGreater(entry["leaseExpiry"], entry["issuedAt"])
            self.assertTrue(entry["expectedConfigHash"])

    def test_the_write_counts_come_from_the_adapter_journal(self):
        document = self.drive()
        counts = document["writeCounts"]
        self.assertEqual(set(counts),
                         {"applies", "withdrawals", "refused", "unknown"})
        self.assertEqual(counts["applies"], 1)
        self.assertEqual(counts["withdrawals"], 1)

    def test_every_transport_call_is_recorded(self):
        document = self.drive()
        methods = [call["method"] for call in document["transportCalls"]]
        self.assertIn("create_policy", methods)
        self.assertIn("delete_policy", methods)


class TheEvidenceIsWrittenBesideTheLiveConsoleRuns(_Driven):

    def test_both_halves_are_written_with_the_declared_schema(self):
        document = self.drive()
        run_path = Path(document["evidence"]["run"])
        events_path = Path(document["evidence"]["events"])
        self.assertTrue(run_path.is_file())
        self.assertTrue(events_path.is_file())
        self.assertTrue(run_path.name.startswith("CAMPAIGN5-A1-cap-"))
        recorded = json.loads(run_path.read_text(encoding="utf-8"))
        self.assertEqual(recorded["schemaVersion"], "campaign5-a1-run/1.0.0")
        self.assertEqual(recorded["family"], "cap")

    def test_the_events_file_is_the_operation_journal_and_says_so(self):
        # Not a Kernel event stream, and it does not pretend to be: this path
        # has no Kernel case, so the append-only record it does have is the
        # adapter's, and that is what is written.
        document = self.drive()
        self.assertEqual(document["eventsKind"], "r1-operation-journal")
        lines = [json.loads(line) for line in
                 Path(document["evidence"]["events"]).read_text(
                     encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(lines)
        self.assertEqual({entry["adapter"] for entry in lines}, {"r1-cap"})
        self.assertIn("CREATE", {entry["operation"] for entry in lines})
        self.assertIn("DELETE", {entry["operation"] for entry in lines})

    def test_the_printed_block_names_the_path_and_the_outcome(self):
        import contextlib
        import io

        document = self.drive()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            live_run.print_run(document)
        printed = buffer.getvalue()
        self.assertIn("campaign5 A1 [MOCK] cap", printed)
        self.assertIn(CAP.policy_type_id, printed)
        self.assertIn("style 2 action 102", printed)
        self.assertIn("not evaluated", printed)


class TheCommandLine(unittest.TestCase):

    def test_every_family_has_a_value_flag_for_every_leaf(self):
        for key, family in CAMPAIGN5_FAMILIES.items():
            with self.subTest(family=key):
                leaves = [leaf for _flag, leaf in
                          live_run.FAMILY_VALUE_FLAGS[key]]
                self.assertEqual(leaves, list(family.value_fields))

    def test_a_family_run_without_its_value_is_refused(self):
        code = live_run.main(["--profile", "x.json", "--family", "cap"])
        self.assertEqual(code, 2)

    def test_a_two_leaf_family_refuses_a_single_stated_baseline(self):
        code = live_run.main(["--profile", "x.json", "--family", "mcs",
                              "--mcs-min", "4", "--mcs-max", "20",
                              "--baseline", "9"])
        self.assertEqual(code, 2)

    def test_a_missing_profile_is_refused_before_anything_is_composed(self):
        code = live_run.main(["--profile", "does-not-exist.json",
                              "--family", "cap", "--cap", "12"])
        self.assertEqual(code, 3)


def _baseline_of(family):
    """The value the fixture's radio starts at, per family."""
    return {"cap": BASELINE_PRBS, "priority": 4, "mcs": 9, "power": 0}[family.key]


def _ue():
    from tests.test_liveconsole import AMF_UE_NGAP_ID

    return AMF_UE_NGAP_ID


def _attribution():
    from tools.liveconsole.build import CapReadbackAttribution

    return CapReadbackAttribution(amf_ue_ngap_id=_ue(), serving_nci=1,
                                  e2_node="node", connection_epoch=1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
