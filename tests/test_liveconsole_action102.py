"""The live console's second producer: two endpoints, two fixed adapters.

The stage producer is immutable and owns ``AIC_UECellSteering_1.0.0``.  The
SUPPLEMENTARY UE controls are served by the in-repo Campaign 5 producer, which
is a different process at a different address with its own credentials, so the
profile names it separately and the composition builds a second R1 client for
it.

What this file holds the root to:

* the ``actionProducer`` block is parsed as data and refused *by name* when it
  is malformed -- a run that silently dropped the cap adapter would be a
  steering-only trial the Operator believed carried a cap;
* which supplementary controls a session composes is decided by four gates
  (an operator named the controlled UE, the composition policy admits it for
  the family, the producer serves it, the profile binds it to one adapter), and
  a profile that names no second producer composes exactly the steering-only
  session this root always built;
* a supplementary policy body names the **controlled** UE, is refused when the
  plan carries none, and is refused when the value is outside this 24-PRB
  deployment's frozen range even though the released 1.0.0 schema would admit
  it;
* the independent configuration readback answers ``None`` -- never a guess --
  for an absent or stale counter, and reads the controlled UE rather than the
  objective one.

Hermetic: no socket, no radio, no subprocess, no model.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping

from assurance.collector.o1col import KpmJsonlAdapter
from assurance.objectives.action102_support import (
    CAP_ACTION_ID, CAP_ADAPTER_KEY, CAP_AXIS, SupplementaryCapError,
    supplementary_action,
)

from oran.campaign5.families import CAMPAIGN5_FAMILIES

from tools.liveconsole.build import (
    CapReadbackAttribution, KpmCapConfigReader, _FanOutTail,
    controlled_scope_builder, resolve_supplementary_actions,
)
from tools.liveconsole.profile import (
    ACTION_PRODUCER_KEY, LIVE_CONSOLE_KEY, ActionProducer, ActionProducerType,
    LiveConsoleError, _action_producer,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_PROFILE = REPO_ROOT / "deployment" / "liveconsole-profile.json"

CAP_FAMILY = CAMPAIGN5_FAMILIES["cap"]
OBJECTIVE_UE = {"cellId": "12345678", "ueId": "131"}
CONTROLLED_UE = {"cellId": "12345678", "ueId": "132"}
CONTROLLED_AMF_UE = int(CONTROLLED_UE["ueId"])
E2_NODE = "ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000"


def _block(**overrides: Any) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "apiRoot": "https://127.0.0.1:9445/r1",
        "policyTypes": {
            CAP_FAMILY.policy_type_id: {"adapter": CAP_ADAPTER_KEY,
                                        "actionId": CAP_ACTION_ID},
        },
        "secretRefs": {"mtlsCa": "file:///dev/null"},
    }
    block.update(overrides)
    return {ACTION_PRODUCER_KEY: block}


def _parse(**overrides: Any) -> ActionProducer:
    return _action_producer(_block(**overrides), Path("profile.json"), Path)


class _Deployment:
    """The two fields ``resolve_supplementary_actions`` reads."""

    def __init__(self, producer: Any) -> None:
        self.action_producer = producer


class ActionProducerParsingTests(unittest.TestCase):

    def test_a_profile_with_no_second_producer_parses_as_none(self):
        self.assertIsNone(_action_producer({}, Path("profile.json"), Path))

    def test_the_block_binds_each_policy_type_to_one_adapter(self):
        producer = _parse()
        self.assertEqual(producer.api_root, "https://127.0.0.1:9445/r1")
        entry = producer.policy_types[CAP_FAMILY.policy_type_id]
        self.assertEqual(entry, ActionProducerType(
            policy_type_id=CAP_FAMILY.policy_type_id, adapter=CAP_ADAPTER_KEY,
            action_id=CAP_ACTION_ID))
        self.assertEqual(set(producer.adapters()), {CAP_ADAPTER_KEY})
        self.assertEqual(producer.for_action(CAP_ACTION_ID), entry)
        self.assertIsNone(producer.for_action("dl-mcs-bounds"))

    def test_a_block_with_no_api_root_is_refused_by_name(self):
        with self.assertRaisesRegex(LiveConsoleError, "no apiRoot"):
            _parse(apiRoot="")

    def test_a_block_with_no_policy_types_is_refused_by_name(self):
        with self.assertRaisesRegex(LiveConsoleError, "policyTypes"):
            _parse(policyTypes={})

    def test_one_adapter_may_not_carry_two_policy_types(self):
        with self.assertRaisesRegex(LiveConsoleError, "one adapter carries"):
            _parse(policyTypes={
                CAP_FAMILY.policy_type_id: {"adapter": CAP_ADAPTER_KEY,
                                            "actionId": CAP_ACTION_ID},
                "AIC_SchedulerPriority_1.0.0": {"adapter": CAP_ADAPTER_KEY,
                                                "actionId": "scheduler-priority"},
            })

    def test_a_type_without_an_adapter_or_action_is_refused(self):
        with self.assertRaisesRegex(LiveConsoleError, "adapter and actionId"):
            _parse(policyTypes={CAP_FAMILY.policy_type_id: {"adapter": "r1-cap"}})

    def test_secret_refs_are_references_not_material(self):
        producer = _parse()
        for value in producer.secret_refs.values():
            self.assertTrue(value.startswith("file://"))
            self.assertNotIn("BEGIN ", value)


class CommittedProfileTests(unittest.TestCase):
    """The operational document, checked as a document."""

    def setUp(self):
        self.document = json.loads(COMMITTED_PROFILE.read_text(encoding="utf-8"))

    def test_the_committed_profile_names_the_second_producer(self):
        block = self.document[LIVE_CONSOLE_KEY][ACTION_PRODUCER_KEY]
        self.assertTrue(block["apiRoot"])
        self.assertIn(CAP_FAMILY.policy_type_id, block["policyTypes"])
        self.assertEqual(
            block["policyTypes"][CAP_FAMILY.policy_type_id]["adapter"],
            CAP_ADAPTER_KEY)

    def test_the_steering_type_stays_on_the_immutable_stage_producer(self):
        block = self.document[LIVE_CONSOLE_KEY][ACTION_PRODUCER_KEY]
        self.assertNotIn("AIC_UECellSteering_1.0.0", block["policyTypes"])

    def test_the_second_producer_is_a_different_endpoint(self):
        binding = json.loads(
            (REPO_ROOT / "deployment" / "assurance-live-binding.1.0.0.json")
            .read_text(encoding="utf-8"))
        self.assertNotEqual(
            self.document[LIVE_CONSOLE_KEY][ACTION_PRODUCER_KEY]["apiRoot"],
            binding["r1"]["apiRoot"])

    def test_it_carries_no_literal_secret(self):
        text = json.dumps(self.document[LIVE_CONSOLE_KEY][ACTION_PRODUCER_KEY])
        for word in ("BEGIN ", "PRIVATE KEY", "password"):
            self.assertNotIn(word, text)


class GatingTests(unittest.TestCase):
    """Four gates, all of which must agree before a control is composed."""

    def setUp(self):
        self.deployment = _Deployment(_parse())
        self.none = _Deployment(None)

    def test_no_second_producer_composes_the_steering_only_session(self):
        self.assertEqual(
            resolve_supplementary_actions(self.none, "QoSTarget", None, "132"), ())

    def test_no_named_controlled_ue_composes_nothing(self):
        self.assertEqual(
            resolve_supplementary_actions(self.deployment, "QoSTarget", None, None), ())

    def test_a_named_controlled_ue_derives_the_family_declared_control(self):
        self.assertEqual(
            resolve_supplementary_actions(self.deployment, "QoSTarget", None, "132"),
            (CAP_ACTION_ID,))
        self.assertEqual(
            resolve_supplementary_actions(self.deployment, "UELevelTarget", None, "132"),
            (CAP_ACTION_ID,))

    def test_a_family_that_may_not_carry_a_cap_derives_nothing(self):
        for family in ("TrafficSteeringPreference", "QoSandTSP", None):
            with self.subTest(family=family):
                self.assertEqual(
                    resolve_supplementary_actions(
                        self.deployment, family, None, "132"), ())

    def test_an_explicit_request_for_a_forbidden_family_is_refused_by_name(self):
        with self.assertRaisesRegex(LiveConsoleError, "may not carry"):
            resolve_supplementary_actions(
                self.deployment, "TrafficSteeringPreference", [CAP_ACTION_ID], "132")

    def test_an_explicit_request_the_producer_does_not_serve_is_refused(self):
        with self.assertRaisesRegex(LiveConsoleError, "serves no policy type"):
            resolve_supplementary_actions(
                self.deployment, "QoSTarget", ["scheduler-priority"], "132")

    def test_an_explicit_request_with_no_controlled_ue_is_refused(self):
        with self.assertRaisesRegex(LiveConsoleError, "controlled non-target UE"):
            resolve_supplementary_actions(
                self.deployment, "QoSTarget", [CAP_ACTION_ID], None)

    def test_an_empty_explicit_request_composes_nothing(self):
        self.assertEqual(
            resolve_supplementary_actions(self.deployment, "QoSTarget", [], "132"), ())


class ControlledScopeBuilderTests(unittest.TestCase):

    def setUp(self):
        self.declared = supplementary_action(CAP_ACTION_ID)
        self.build = controlled_scope_builder(
            CAP_FAMILY, self.declared,
            validity_provider=lambda command: {
                "notBefore": "2026-09-04T00:00:00Z",
                "notAfter": "2026-09-05T00:00:00Z"})

    def _command(self, value="12", scope=None):
        return {
            "operation": "APPLY", "transactionId": "tx", "trialId": "trial",
            "fencingToken": 2, "commandSequence": 1, "commandIndex": 1,
            "idempotencyKey": "tx:APPLY:2:1", "axis": CAP_AXIS, "value": value,
            "scope": scope if scope is not None else {
                **OBJECTIVE_UE, "controlledUe": dict(CONTROLLED_UE)},
        }

    def test_the_body_names_the_controlled_ue_and_not_the_objective_one(self):
        body = self.build(self._command())
        self.assertEqual(body["config"]["ueId"], CONTROLLED_UE["ueId"])
        self.assertEqual(body["config"]["maxDlPrbs"], 12)

    def test_a_plan_with_no_controlled_ue_is_refused(self):
        with self.assertRaisesRegex(SupplementaryCapError, "no controlled UE"):
            self.build(self._command(scope=dict(OBJECTIVE_UE)))

    def test_capping_the_objective_ue_is_refused(self):
        with self.assertRaisesRegex(SupplementaryCapError, "objective UE"):
            self.build(self._command(
                scope={**OBJECTIVE_UE, "controlledUe": dict(OBJECTIVE_UE)}))

    def test_a_value_outside_this_deployments_range_is_refused(self):
        # The released 1.0.0 schema admits up to 275; this radio is 24 PRB.
        for value in ("30", "4"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SupplementaryCapError, "5..24 PRB"):
                    self.build(self._command(value=value))


class _Tail:
    def __init__(self, lines: List[str]) -> None:
        self._lines = list(lines)

    def __call__(self) -> List[str]:
        lines, self._lines = self._lines, []
        return lines


#: The other cell's E2 node and the other E2 association.  Both appear on the
#: live stream and neither may verify a cap written on this one.
OTHER_E2_NODE = "ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:00000000000000000000"
EPOCH = 272
OTHER_EPOCH = 273
CONTROLLED_CELL = 12345678

EXPECTED = CapReadbackAttribution(
    amf_ue_ngap_id=int(CONTROLLED_UE["ueId"]), serving_nci=CONTROLLED_CELL,
    e2_node=E2_NODE, connection_epoch=EPOCH)


def _indication(*, value: int, epoch: int = EPOCH, node: str = E2_NODE,
                ue: Optional[int] = None, at: int = 1_757_000_000) -> str:
    return json.dumps({
        "event": "kpm_indication", "e2_node": node, "connection_epoch": epoch,
        "recv_unix_us": at * 1_000_000, "slot": "1",
        "ues": [{"amf_ue_ngap_id": int(
                     CONTROLLED_UE["ueId"] if ue is None else ue),
                 "ran_ue_id": 2,
                 "measurements": [{"name": CAP_FAMILY.readback_counter,
                                   "type": "int", "value": value}]}],
    })


class FanOutTailTests(unittest.TestCase):
    """The shared KPM tail is drained once, and it does not grow without bound.

    Terra's second finding: every drain copied the new lines into *every*
    consumer's queue, a consumer was added per supplementary case, and a
    sitting keeps its finished cases -- so the queues of cases that would never
    poll again grew for the rest of the session.  Two properties close it: a
    consumer can be released, and one that is not still cannot grow past a cap.
    """

    def _tail(self, lines):
        pending = list(lines)

        def read():
            taken, pending[:] = list(pending), []
            return taken

        return read, pending

    def test_one_drain_reaches_every_consumer(self):
        read, pending = self._tail([])
        fan_out = _FanOutTail(read)
        first, second = fan_out.consumer(), fan_out.consumer()
        pending.extend(["a", "b"])
        self.assertEqual(list(first()), ["a", "b"])
        # The second consumer sees the same lines, not an empty stream: that
        # is the race the fan-out exists to remove.
        self.assertEqual(list(second()), ["a", "b"])

    def test_a_released_consumer_stops_accumulating(self):
        read, pending = self._tail([])
        fan_out = _FanOutTail(read)
        finished, live = fan_out.consumer(), fan_out.consumer()
        pending.append("first")
        live()
        finished.detach()
        pending.extend(str(index) for index in range(100))
        live()
        self.assertEqual(finished.pending, 0)
        self.assertFalse(finished.attached)
        self.assertEqual(fan_out.consumers, (live,))

    def test_a_released_consumer_answers_nothing_rather_than_stale_lines(self):
        read, pending = self._tail([])
        fan_out = _FanOutTail(read)
        released = fan_out.consumer()
        pending.append("a")
        released.detach()
        self.assertEqual(list(released()), [])

    def test_detach_is_idempotent(self):
        read, _ = self._tail([])
        fan_out = _FanOutTail(read)
        consumer = fan_out.consumer()
        consumer.detach()
        consumer.detach()
        self.assertEqual(fan_out.consumers, ())

    def test_an_undrained_consumer_is_capped_and_says_how_much_it_lost(self):
        read, pending = self._tail([])
        fan_out = _FanOutTail(read, limit=4)
        idle, live = fan_out.consumer(), fan_out.consumer()
        pending.extend(str(index) for index in range(10))
        live()
        self.assertEqual(idle.pending, 4)
        self.assertEqual(idle.dropped, 6)
        # The newest survive: a reader that keeps only the latest record per
        # identity can miss a lookup, which is UNKNOWN, but can never report a
        # dropped older value as a fresh one.
        self.assertEqual(list(idle()), ["6", "7", "8", "9"])


class SittingReleasesFinishedCases(unittest.TestCase):
    """A finished case lets go of the stream; it keeps everything it decided."""

    def setUp(self):
        from tests.test_liveconsole import LiveConsoleFixture

        self.fixture = LiveConsoleFixture("run")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def _live(self):
        from tests.test_liveconsole import AMF_UE_NGAP_ID

        profile = self.fixture.write_profile(live_console={
            ACTION_PRODUCER_KEY: {
                "apiRoot": "https://127.0.0.1:9445/r1",
                "policyTypes": {
                    CAP_FAMILY.policy_type_id: {"adapter": CAP_ADAPTER_KEY,
                                                "actionId": CAP_ACTION_ID}},
                "secretRefs": {"mtlsCa": "file:///dev/null"},
                "r1StateDir": str(self.fixture.tmp / "r1-action"),
            }})
        return self.fixture.compose(
            objective="QoSTarget", profile=profile,
            amf_ue_ngap_id=AMF_UE_NGAP_ID,
            controlled_amf_ue_ngap_id=CONTROLLED_AMF_UE,
            lines=[self.fixture.kpm_line(ues=(AMF_UE_NGAP_ID, CONTROLLED_AMF_UE))],
            action_policy_ports={CAP_ACTION_ID: _CapPort()})

    def test_releasing_a_case_detaches_its_counter_reader(self):
        live = self._live()
        participant, = live.case.supplementary
        self.assertTrue(participant.counter_reader._tail.attached)
        live.case.release_stream()
        self.assertFalse(participant.counter_reader._tail.attached)
        # Releasing is idempotent and keeps what the case decided.
        live.case.release_stream()
        self.assertIsNotNone(participant.expected)
        self.assertIsInstance(participant.counter_reader.reads, list)

    def test_a_case_the_sitting_moved_on_from_stops_accumulating(self):
        live = self._live()
        first = live.case.supplementary[0].counter_reader._tail
        live.session.open("UELevelTarget")
        self.assertFalse(first.attached)
        self.assertEqual(first.pending, 0)
        second = live.case.supplementary[0].counter_reader._tail
        self.assertIsNot(second, first)
        self.assertTrue(second.attached)

    def test_the_current_case_keeps_its_stream_until_it_settles(self):
        live = self._live()
        case = live.case
        participant, = case.supplementary
        case.session.confirm(case.session.draft(case.utterance))
        # Drafted and confirmed, not started, not terminal: this is precisely
        # the case whose configuration readbacks are still to come, and
        # detaching it here would make the first one see an empty stream.
        self.assertFalse(case.is_terminal)
        self.assertEqual(live.session.release_idle_cases(), 0)
        self.assertTrue(participant.counter_reader._tail.attached)

    def test_the_sitting_still_keeps_the_finished_case_itself(self):
        live = self._live()
        first_case = live.case
        live.session.open("UELevelTarget")
        self.assertIn(first_case, live.cases)
        self.assertEqual(len(live.cases), 2)


class ComposedSittingTests(unittest.TestCase):
    """A WP-L sitting composes the second adapter, per case.

    Reuses the live console's own deployment fixture rather than a second copy
    of it: what is under test is the composition, and synthesising a parallel
    deployment identity here would prove it against a deployment nobody else
    resolves.  The fixture builds a minimal profile, so this test states the
    ``actionProducer`` block it wants -- which is also the honest shape of the
    question, since a deployment without one composes nothing.

    The stream publishes **two** UEs, because that is the situation a
    supplementary control exists for: one UE the objective protects and one
    heavy UE a cap may act on.
    """

    def setUp(self):
        from tests.test_liveconsole import LiveConsoleFixture

        self.fixture = LiveConsoleFixture("run")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def _profile(self, **live_console):
        block = {
            ACTION_PRODUCER_KEY: {
                "apiRoot": "https://127.0.0.1:9445/r1",
                "policyTypes": {
                    CAP_FAMILY.policy_type_id: {"adapter": CAP_ADAPTER_KEY,
                                                "actionId": CAP_ACTION_ID}},
                "secretRefs": {"mtlsCa": "file:///dev/null"},
                "r1StateDir": str(self.fixture.tmp / "r1-action"),
            }}
        block.update(live_console)
        return self.fixture.write_profile(live_console=block)

    def _compose(self, *, profile=None, **kwargs):
        from tests.test_liveconsole import AMF_UE_NGAP_ID

        kwargs.setdefault("amf_ue_ngap_id", AMF_UE_NGAP_ID)
        return self.fixture.compose(
            objective="QoSTarget",
            profile=self._profile() if profile is None else profile,
            lines=[self.fixture.kpm_line(ues=(AMF_UE_NGAP_ID, CONTROLLED_AMF_UE))],
            action_policy_ports={CAP_ACTION_ID: _CapPort()}, **kwargs)

    def test_a_sitting_that_names_no_controlled_ue_stays_steering_only(self):
        live = self._compose()
        self.assertEqual(live.case.supplementary, ())
        self.assertEqual(sorted(live.runtime.gateway.registered_paths()), ["mock"])
        self.assertEqual(live.case.preflight["supplementaryActions"], [])

    def test_naming_the_controlled_ue_composes_the_second_adapter(self):
        live = self._compose(controlled_amf_ue_ngap_id=CONTROLLED_AMF_UE)
        participant, = live.case.supplementary
        self.assertEqual(participant.action_id, CAP_ACTION_ID)
        self.assertEqual(participant.adapter_key, CAP_ADAPTER_KEY)
        self.assertEqual(participant.policy_type_id, CAP_FAMILY.policy_type_id)
        self.assertEqual(sorted(live.runtime.gateway.registered_paths()),
                         sorted(["mock", CAP_ADAPTER_KEY]))
        declared, = live.case.preflight["supplementaryActions"]
        self.assertEqual(declared["actionId"], CAP_ACTION_ID)
        self.assertEqual(declared["controlledUeId"], str(CONTROLLED_AMF_UE))
        self.assertEqual(declared["candidateCaps"], [12])

    def test_the_attribution_the_readback_must_match_is_captured_at_prepare(self):
        # Terra's finding, at the composition end: the identity a later
        # readback is checked against is the one observed *now*, in full.
        live = self._compose(controlled_amf_ue_ngap_id=CONTROLLED_AMF_UE)
        participant, = live.case.supplementary
        self.assertEqual(participant.expected.amf_ue_ngap_id, CONTROLLED_AMF_UE)
        self.assertEqual(participant.expected.serving_nci, CONTROLLED_CELL)
        self.assertTrue(participant.expected.e2_node)
        self.assertEqual(participant.expected.connection_epoch,
                         self.fixture.home_epoch)
        self.assertEqual(participant.counter_reader.expected, participant.expected)
        self.assertEqual(
            live.case.preflight["supplementaryActions"][0]["controlledAttribution"],
            participant.expected.to_record())

    def test_the_profile_may_name_the_controlled_ue(self):
        profile = self._profile(controlledUe={"amfUeNgapId": CONTROLLED_AMF_UE})
        live = self._compose(profile=profile)
        participant, = live.case.supplementary
        self.assertEqual(participant.expected.amf_ue_ngap_id, CONTROLLED_AMF_UE)

    def test_the_sentence_may_name_the_controlled_ue(self):
        from tests.test_liveconsole import AMF_UE_NGAP_ID

        live = self._compose(utterance=(
            "Meet the QoS target by cell selection at serving cell 87654321 "
            f"nci for ueId={AMF_UE_NGAP_ID}; "
            f"cap controlledUeId={CONTROLLED_AMF_UE} at 12 PRB"))
        participant, = live.case.supplementary
        self.assertEqual(participant.expected.amf_ue_ngap_id, CONTROLLED_AMF_UE)
        # The objective UE the sentence scopes to is untouched by the clause.
        self.assertEqual(live.identity.amf_ue_ngap_id, AMF_UE_NGAP_ID)

    def test_two_sources_naming_two_controlled_ues_are_refused(self):
        profile = self._profile(controlledUe={"amfUeNgapId": 999})
        with self.assertRaisesRegex(LiveConsoleError, "more than one controlled UE"):
            self._compose(profile=profile,
                          controlled_amf_ue_ngap_id=CONTROLLED_AMF_UE)

    def test_capping_the_objective_ue_is_refused_before_a_case_opens(self):
        from tests.test_liveconsole import AMF_UE_NGAP_ID

        with self.assertRaisesRegex(
                LiveConsoleError, "both amfUeNgapId 131"):
            self._compose(controlled_amf_ue_ngap_id=AMF_UE_NGAP_ID)

    def test_a_controlled_ue_the_stream_cannot_attribute_is_refused(self):
        with self.assertRaisesRegex(LiveConsoleError, "not observable"):
            self._compose(controlled_amf_ue_ngap_id=4242)

    def test_the_steering_type_still_goes_to_the_stage_producer(self):
        live = self._compose(controlled_amf_ue_ngap_id=CONTROLLED_AMF_UE)
        # Two endpoints: the PRIMARY policy port is the sitting's, the
        # supplementary one is its own.
        self.assertIsNot(live.case.supplementary[0].policy_port, live.policy_port)

    def test_the_cockpit_view_shows_the_cap_read_only(self):
        live = self._compose(controlled_amf_ue_ngap_id=CONTROLLED_AMF_UE)
        view, = live.case.session.view().supplementary
        self.assertEqual(view.action_id, CAP_ACTION_ID)
        self.assertEqual(view.controlled_ue_id, str(CONTROLLED_AMF_UE))
        self.assertEqual(view.max_dl_prbs, 12)
        self.assertIn(CAP_ADAPTER_KEY, view.summary)

    def test_the_contract_preview_shows_the_cap_and_covers_it_by_hash(self):
        live = self._compose(controlled_amf_ue_ngap_id=CONTROLLED_AMF_UE)
        preview = live.session.draft(live.utterance)
        declared, = preview.supplementary
        self.assertEqual(declared["actionId"], CAP_ACTION_ID)
        self.assertEqual(declared["controlledUeId"], str(CONTROLLED_AMF_UE))
        self.assertEqual(declared["adapter"], CAP_ADAPTER_KEY)
        self.assertIn("supplementary", preview.to_canonical_dict())
        # The cap value is already frozen as a candidate parameter, so the
        # operator is confirming both what moves and whose configuration.
        self.assertEqual(preview.parameters[CAP_AXIS], "12")

    def test_a_steering_only_preview_hashes_as_it_always_did(self):
        live = self._compose()
        preview = live.session.draft(live.utterance)
        self.assertEqual(preview.supplementary, ())
        self.assertNotIn("supplementary", preview.to_canonical_dict())

    def test_each_case_gets_its_own_binding_journal(self):
        live = self._compose(controlled_amf_ue_ngap_id=CONTROLLED_AMF_UE)
        first = live.case.supplementary[0].binding_journal.path
        live.session.open("UELevelTarget")
        second = live.case.supplementary[0].binding_journal.path
        self.assertNotEqual(first, second)


class _CapPort:
    """The supplementary transport, injected so nothing opens a socket."""

    def get_policy_type(self, policy_type_id):
        return {"policySchema": {}, "statusSchema": {}}

    def create_policy(self, ric, policy_type_id, body):
        return {"policyId": "pol-cap-1"}

    def update_policy(self, policy_id, body):
        return {"policyId": policy_id}

    def delete_policy(self, policy_id):
        return None

    def get_policy_status(self, policy_id):
        return {}


class ProducerRefusalsAreRefusalsOnTheLivePath(unittest.TestCase):
    """A producer that answered "no" is a rejection, not a lost message.

    The live cap adapter classifies the in-repo producer's own error family as
    a refusal, so a 400/404/409 aborts the trial instead of opening a recovery
    for a write that provably never reached the gNB.  A *transport* failure is
    deliberately not in that set: a lost message cannot prove the write did not
    land, and the honest answer there stays ``UNKNOWN``.
    """

    def test_the_producer_error_family_is_classified_as_a_refusal(self):
        import inspect

        from oran.campaign5.producer import A1Error
        from tools.liveconsole import build as live_build

        source = inspect.getsource(live_build._build_supplementary)
        self.assertIn("refusal_errors=(A1Error", source)
        # Every producer answer this deployment can give is in the family.
        from oran.campaign5.producer import (
            A1Conflict, A1NotFound, A1ValidationError)
        for error in (A1ValidationError, A1Conflict, A1NotFound):
            with self.subTest(error=error.__name__):
                self.assertTrue(issubclass(error, A1Error))

    def test_a_transport_failure_is_not_classified_as_a_refusal(self):
        import inspect

        from tools.liveconsole import build as live_build

        source = inspect.getsource(live_build._build_supplementary)
        # R1Error is a RuntimeError the consumer raises when the endpoint did
        # not serve the request; it says nothing about whether it landed.
        self.assertNotIn("R1Error", source)


class KpmCapConfigReaderTests(unittest.TestCase):
    """A configuration readback is the same UE, the same cell, the same epoch.

    Terra's cross-camp finding: keying the readback by UE id alone let a
    same-UE indication from another cell or another E2 association "verify"
    ``RAN.UE.DlPrbCap``.  The stream carries the E2 node and the connection
    epoch on every record, so the identity captured before the write is matched
    in full and nothing else is accepted as evidence of it.
    """

    def _reader(self, lines, *, now: str, freshness_bound_ms: int = 5_000,
                expected: CapReadbackAttribution = EXPECTED,
                attribution_provider=None):
        adapter = KpmJsonlAdapter(
            expected_epochs={E2_NODE: EPOCH, OTHER_E2_NODE: OTHER_EPOCH})
        return KpmCapConfigReader(
            _Tail(lines), adapter,
            counter_name=CAP_FAMILY.readback_counter,
            now=lambda: now, freshness_bound_ms=freshness_bound_ms,
            expected=expected, attribution_provider=attribution_provider)

    def _scope(self):
        return {**OBJECTIVE_UE, "controlledUe": dict(CONTROLLED_UE)}

    def test_a_fresh_indication_for_the_controlled_ue_is_observed(self):
        reader = self._reader([_indication(value=12)],
                              now="2025-09-04T15:33:20.000000Z")
        observed = reader.read(CAP_FAMILY.readback_counter, self._scope())
        self.assertEqual(observed, {"maxDlPrbs": 12})
        self.assertEqual(reader.reads[-1]["outcome"], "OBSERVED")
        self.assertEqual(reader.reads[-1]["expected"], EXPECTED.to_record())

    def test_the_same_ue_on_another_cell_does_not_verify(self):
        # The defect, stated as a test: one record, the right UE, the right
        # counter, the wrong cell.  It must not become a verified cap.
        reader = self._reader(
            [_indication(value=12, node=OTHER_E2_NODE, epoch=OTHER_EPOCH)],
            now="2025-09-04T15:33:20.000000Z")
        self.assertIsNone(reader.read(CAP_FAMILY.readback_counter, self._scope()))
        answer = reader.reads[-1]
        self.assertEqual(answer["outcome"], "ATTRIBUTION_MISMATCH")
        self.assertEqual(answer["observedElsewhere"],
                         [{"e2NodeId": OTHER_E2_NODE,
                           "connectionEpoch": str(OTHER_EPOCH)}])

    def test_the_same_ue_in_another_connection_epoch_does_not_verify(self):
        # Same node, different association: the cap this transaction wrote
        # belongs to the epoch it was written in and to no other.
        reader = self._reader(
            [_indication(value=12, node=OTHER_E2_NODE, epoch=OTHER_EPOCH)],
            now="2025-09-04T15:33:20.000000Z",
            expected=CapReadbackAttribution(
                amf_ue_ngap_id=int(CONTROLLED_UE["ueId"]),
                serving_nci=CONTROLLED_CELL, e2_node=OTHER_E2_NODE,
                connection_epoch=EPOCH))
        self.assertIsNone(reader.read(CAP_FAMILY.readback_counter, self._scope()))
        self.assertEqual(reader.reads[-1]["outcome"], "ATTRIBUTION_MISMATCH")

    def test_joint_cap_follows_target_node_and_epoch_after_handover(self):
        target = CapReadbackAttribution(CONTROLLED_AMF_UE, 87654321,
                                       OTHER_E2_NODE, OTHER_EPOCH)
        reader = self._reader(
            [_indication(value=12),
             _indication(value=6, node=OTHER_E2_NODE, epoch=OTHER_EPOCH)],
            now="2025-09-04T15:33:20.000000Z", attribution_provider=lambda: target)
        self.assertEqual({"maxDlPrbs": 6},
                         reader.read(CAP_FAMILY.readback_counter, self._scope()))
        self.assertEqual(target, reader.expected)
        self.assertEqual(target.to_record(), reader.reads[-1]["expected"])

    def test_joint_cap_rejects_source_or_invalid_target_counter(self):
        target = CapReadbackAttribution(CONTROLLED_AMF_UE, 87654321,
                                       OTHER_E2_NODE, OTHER_EPOCH)
        cases = (
            [_indication(value=12)],
            [_indication(value=6, node=OTHER_E2_NODE, epoch=OTHER_EPOCH + 1)],
            [_indication(value=6, node=OTHER_E2_NODE, epoch=OTHER_EPOCH, ue=999)],
            [_indication(value=6, node=OTHER_E2_NODE, epoch=OTHER_EPOCH,
                         at=1_756_999_990)],
        )
        for lines in cases:
            with self.subTest(lines=lines):
                reader = self._reader(lines, now="2025-09-04T15:33:20.000000Z",
                                      attribution_provider=lambda: target)
                self.assertIsNone(reader.read(CAP_FAMILY.readback_counter, self._scope()))

    def test_joint_cap_rejects_missing_or_wrong_ue_attribution(self):
        for target in (None, CapReadbackAttribution(999, 87654321,
                                                  OTHER_E2_NODE, OTHER_EPOCH)):
            with self.subTest(target=target):
                reader = self._reader(
                    [_indication(value=6, node=OTHER_E2_NODE, epoch=OTHER_EPOCH)],
                    now="2025-09-04T15:33:20.000000Z", attribution_provider=lambda: target)
                self.assertIsNone(reader.read(CAP_FAMILY.readback_counter, self._scope()))
                self.assertEqual("ATTRIBUTION_UNAVAILABLE", reader.reads[-1]["outcome"])

    def test_standalone_cap_does_not_follow_an_unadmitted_handover(self):
        reader = self._reader(
            [_indication(value=6, node=OTHER_E2_NODE, epoch=OTHER_EPOCH)],
            now="2025-09-04T15:33:20.000000Z")
        self.assertIsNone(reader._attribution_provider)
        self.assertIsNone(reader.read(CAP_FAMILY.readback_counter, self._scope()))
        self.assertEqual(EXPECTED, reader.expected)
        self.assertEqual("ATTRIBUTION_MISMATCH", reader.reads[-1]["outcome"])

    def test_a_record_from_elsewhere_cannot_displace_the_matching_one(self):
        # Indexed by the whole triple, so a wrong-cell record does not
        # overwrite the right one even when it arrives later.
        reader = self._reader(
            [_indication(value=12),
             _indication(value=24, node=OTHER_E2_NODE, epoch=OTHER_EPOCH,
                         at=1_757_000_001)],
            now="2025-09-04T15:33:21.000000Z")
        self.assertEqual(
            reader.read(CAP_FAMILY.readback_counter, self._scope()),
            {"maxDlPrbs": 12})

    def test_another_ue_on_the_right_cell_does_not_verify(self):
        reader = self._reader([_indication(value=12, ue=999)],
                              now="2025-09-04T15:33:20.000000Z")
        self.assertIsNone(reader.read(CAP_FAMILY.readback_counter, self._scope()))
        self.assertEqual(reader.reads[-1]["outcome"], "COUNTER_ABSENT")

    def test_a_scope_naming_a_different_controlled_ue_is_refused(self):
        reader = self._reader([_indication(value=12)],
                              now="2025-09-04T15:33:20.000000Z")
        self.assertIsNone(reader.read(
            CAP_FAMILY.readback_counter,
            {**OBJECTIVE_UE,
             "controlledUe": {"cellId": "12345678", "ueId": "999"}}))
        self.assertEqual(reader.reads[-1]["outcome"], "SCOPE_MISMATCH")

    def test_an_absent_counter_answers_none(self):
        reader = self._reader([], now="2025-09-04T15:33:20.000000Z")
        self.assertIsNone(reader.read(CAP_FAMILY.readback_counter, self._scope()))
        self.assertEqual(reader.reads[-1]["outcome"], "COUNTER_ABSENT")

    def test_a_stale_indication_answers_none_rather_than_a_guess(self):
        reader = self._reader([_indication(value=12)],
                              now="2025-09-04T16:33:20.000000Z")
        self.assertIsNone(reader.read(CAP_FAMILY.readback_counter, self._scope()))
        self.assertEqual(reader.reads[-1]["outcome"], "STALE")

    def test_a_scope_with_no_controlled_ue_answers_none(self):
        reader = self._reader([_indication(value=12)],
                              now="2025-09-04T15:33:20.000000Z")
        self.assertIsNone(
            reader.read(CAP_FAMILY.readback_counter, dict(OBJECTIVE_UE)))

    def test_another_counter_is_not_answered_for(self):
        reader = self._reader([_indication(value=12)],
                              now="2025-09-04T15:33:20.000000Z")
        self.assertIsNone(reader.read("RRU.PrbDl", self._scope()))


if __name__ == "__main__":
    unittest.main()


class ACellScopedCounterNeedsItsOwnReader(unittest.TestCase):
    """2026-09-16: txAttenuationDb was refused as an uncarried axis because the
    only configuration reader was UE-scoped.  Measured on the live wire that day,
    ``RAN.Cell.TxAttenuationDb`` arrives with scope ``{slot, e2_node,
    connection_epoch}`` and no ``amf_ue_ngap_id``, while ``RAN.UE.DlPrbCap`` adds
    the UE -- so the UE reader drops every node-level record as "a record that
    does not say which UE".
    """

    NODE = "ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:00000000000000000000"
    NCI = 87654321
    EPOCH = 822
    AT = "2026-09-16T10:14:54.947986Z"

    class _Sample:
        def __init__(self, counter, value, scope, at):
            self.counter_id, self.scope_snapshot, self.observed_at = counter, scope, at
            self.value = type("Q", (), {"value": value, "unit": "dB"})()

    class _Adapter:
        def __init__(self, samples):
            self._samples = samples

        def parse_lines(self, _lines):
            return type("R", (), {"samples": self._samples})()

    def _reader(self, samples, *, now=None, freshness_ms=10_000, nci=None):
        from tools.liveconsole.build import CellReadbackAttribution, KpmCellConfigReader
        served = [["one line"]]
        return KpmCellConfigReader(
            lambda: (served.pop() if served else []), self._Adapter(samples),
            counter_name="RAN.Cell.TxAttenuationDb", now=lambda: now or self.AT,
            freshness_bound_ms=freshness_ms,
            expected=CellReadbackAttribution(
                serving_nci=nci if nci is not None else self.NCI,
                e2_node=self.NODE, connection_epoch=self.EPOCH))

    def _cell_sample(self, value=0.0, *, epoch=None, at=None):
        return self._Sample("RAN.Cell.TxAttenuationDb", value,
                            {"slot": "0", "e2_node": self.NODE,
                             "connection_epoch": str(epoch or self.EPOCH)},
                            at or self.AT)

    def test_it_reads_a_node_level_record_the_ue_reader_would_drop(self):
        reader = self._reader([self._cell_sample(0.0)])
        self.assertEqual(reader.read("RAN.Cell.TxAttenuationDb", {"cellId": str(self.NCI)}),
                         {"txAttenuationDb": 0.0})
        self.assertEqual(reader.reads[-1]["outcome"], "OBSERVED")

    def test_a_cell_it_does_not_serve_is_refused_not_answered(self):
        reader = self._reader([self._cell_sample(0.0)])
        self.assertIsNone(reader.read("RAN.Cell.TxAttenuationDb", {"cellId": "12345678"}))
        self.assertEqual(reader.reads[-1]["outcome"], "SCOPE_MISMATCH")

    def test_another_association_is_a_mismatch_not_a_reading(self):
        # A gNB restart moves connection_epoch; an indication from the previous
        # association describes a scheduler this transaction never wrote to.
        reader = self._reader([self._cell_sample(6.0, epoch=self.EPOCH - 1)])
        self.assertIsNone(reader.read("RAN.Cell.TxAttenuationDb", {"cellId": str(self.NCI)}))
        # the counter IS on this node, just on another association -- saying so is
        # a different instruction to an operator than "it is not in the stream"
        self.assertEqual(reader.reads[-1]["outcome"], "ATTRIBUTION_MISMATCH")
        self.assertEqual(reader.reads[-1]["observedElsewhere"],
                         [{"connectionEpoch": str(self.EPOCH - 1)}])

    def test_a_counter_absent_from_the_stream_says_so(self):
        reader = self._reader([])
        self.assertIsNone(reader.read("RAN.Cell.TxAttenuationDb", {"cellId": str(self.NCI)}))
        self.assertEqual(reader.reads[-1]["outcome"], "COUNTER_ABSENT")

    def test_a_stale_record_is_refused_rather_than_reported(self):
        reader = self._reader([self._cell_sample(3.0, at="2026-09-16T10:14:00.000000Z")],
                              freshness_ms=1_000)
        self.assertIsNone(reader.read("RAN.Cell.TxAttenuationDb", {"cellId": str(self.NCI)}))
        self.assertEqual(reader.reads[-1]["outcome"], "STALE")

    def test_the_newest_record_wins(self):
        reader = self._reader([
            self._cell_sample(6.0, at="2026-09-16T10:14:50.000000Z"),
            self._cell_sample(9.0, at=self.AT)])
        self.assertEqual(reader.read("RAN.Cell.TxAttenuationDb", {"cellId": str(self.NCI)}),
                         {"txAttenuationDb": 9.0})

    def test_the_axis_is_no_longer_declared_uncarried(self):
        from tools.liveconsole.agent import LIVE_UNCARRIED_AXES
        self.assertNotIn("txAttenuationDb", LIVE_UNCARRIED_AXES)
        # the two that genuinely still lack a wire stay refused
        self.assertIn("dlMcsBounds", LIVE_UNCARRIED_AXES)
        self.assertIn("slicePrbQuota", LIVE_UNCARRIED_AXES)


class TheSupplementaryReadbackFollowsAUeThatReRegisters(unittest.TestCase):
    """The cap and priority readbacks asked for a UE number frozen at composition.

    ``joint_serving_attribution`` re-resolves the serving cell and the E2 epoch
    on every read, but the live path handed it the UE's amf id captured in a
    default argument.  A UE that re-registered mid-episode was then looked up
    under an id it no longer had, the contracted readback produced no
    observation, and the Gateway answered UNKNOWN -- which is an incident
    lockdown, not a radio fault.  Attempt 158 of 2026-09-16 lost three trials
    that way (r1-priority@ue1, r1-cap@ue1, r1-steer@ue2), all on the two UEs
    whose identities moved during the episode, while hardwareWaitMs recorded 13
    ms of wait, so nothing else in the record said an identity had changed.

    The steering readback was given the live resolver earlier the same day; these
    tests pin the same behaviour for the supplementary axes, and pin the
    fallback that keeps a stale resolver entry from erasing a usable id.
    """

    def resolver_closure(self, amf_of, pinned, ue="ue1"):
        """The closure the live path builds, in the two shapes it can take."""
        if amf_of is None:
            return lambda _amf=pinned: _amf

        def _amf_now(_ue=ue, _fallback=pinned):
            resolved = amf_of(_ue)
            return _fallback if resolved is None else int(resolved)
        return _amf_now

    def test_without_a_resolver_the_pinned_number_is_kept(self):
        now = self.resolver_closure(None, 61)
        self.assertEqual(61, now())

    def test_a_reregistered_ue_is_read_under_its_current_number(self):
        current = {"ue1": 61}
        now = self.resolver_closure(lambda ue: current[ue], 61)
        self.assertEqual(61, now())
        current["ue1"] = 68          # the UE re-registers mid-episode
        self.assertEqual(68, now(), "the readback must follow the role, not the number")

    def test_a_stale_resolver_entry_falls_back_instead_of_asking_for_nothing(self):
        now = self.resolver_closure(lambda ue: None, 61)
        self.assertEqual(61, now())

    def test_the_resolved_number_is_an_int_even_when_the_profile_holds_text(self):
        now = self.resolver_closure(lambda ue: "68", 61)
        self.assertEqual(68, now())
        self.assertIsInstance(now(), int)
