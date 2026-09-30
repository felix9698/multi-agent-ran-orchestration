"""The Gate 3 live path, run end to end against a clock-driven deployment.

Hardware-free but not timing-free.  Every assertion here is about something
that can only go wrong once an acknowledgement and its effect are separated in
time: the readback that has to wait for the handover, the reporting grid that
has to line up with the Kernel's observation window, the corroboration that
refuses a status nobody else saw, and the permit lease all of it has to fit
inside.

The policy bodies are validated against the frozen ``AIC_UECellSteering_1.0.0``
schema by the real translator, so a body this suite accepts is a body the
deployment accepts.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Mapping

from assurance.contracts.live_binding import load_assurance_live_binding
from assurance.core.axes import (
    EvidenceCellStatus,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.states import TrialState
from assurance.gateway.plan import config_hash
from assurance.live.pin_to_cell_driver import (
    COUNTER_ID,
    LiveDriverError,
    LivePinToCellDeployment,
    LiveTiming,
    build_live_pin_to_cell_runtime,
    live_contract_set,
    serving_cell_projection,
)
from assurance.live import KpmUeAttributionReader

from oran.rapp.contract_support import _bundle_dir

from tests.assurance.live_support import (
    AMF_UE_NGAP_ID,
    EXPECTED_EPOCHS,
    HOME_NCI,
    TARGET_NCI,
    FakeTestbed,
)
from tools.g3ota.composition import (
    LivePolicyBuilder,
    build_policy_type_discovery,
    live_topology,
    observe_live_ue,
)

POLICY_TYPE_ID = "AIC_UECellSteering_1.0.0"


def _golden_capability() -> Mapping[str, Any]:
    bundle = _bundle_dir()
    return json.loads(
        (bundle / "golden/golden-vectors.1.0.0.json").read_text(encoding="utf-8")
    )["canonicalObjects"]["capabilityManifest"]


def _binding_document(source: Path) -> Dict[str, Any]:
    """The committed live binding's shape, with this run's epochs."""
    secrets = {
        "mtlsCa": "file:/run/secrets/ca.crt",
        "mtlsClientCertificate": "file:/run/secrets/client.crt",
        "mtlsClientPrivateKey": "file:/run/secrets/client.key",
        "oauthToken": "file:/run/secrets/token",
    }
    return {
        "schemaVersion": "assurance-live-binding/1.0.0",
        "bindingId": "oran-lab-live-dry-run",
        "sources": [
            {
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ],
        "r1": {
            "apiRoot": "https://192.168.50.1:18443/r1",
            "nearRtRicId": "near-rt-ric-lics-lab-001",
            "policyTypeId": POLICY_TYPE_ID,
            "secretRefs": dict(secrets),
            "polling": {"cadenceMs": 1000, "deadlineMs": 20000},
        },
        "a1p": {
            "apiRoot": "https://192.168.50.1:9444/A1-P/v2",
            "secretRefs": dict(secrets),
        },
        "o1": {
            "httpsRoot": "https://192.168.50.1:8443/o1",
            "netconf": "ssh://192.168.50.1:830",
            "sftp": "sftp://192.168.50.1:2022/pm",
            "pmDirectory": "/var/lib/oran/pm",
            "secretRefs": dict(secrets),
        },
        "kpm": {
            "jsonlPath": "/var/lib/oran/kpm.jsonl",
            "expectedEpochs": dict(EXPECTED_EPOCHS),
        },
        "e2Nodes": ["0x00000e00", "0x00000b00"],
        "cells": [HOME_NCI, TARGET_NCI],
        "plmn": {"mcc": "208", "mnc": "95"},
    }


class LiveRunFixture(unittest.TestCase):
    """One clock-driven live case, wired exactly the way the runner wires it."""

    def build(self, *, timing: LiveTiming = None, **testbed) -> Any:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / "deployment-record.json"
        source.write_text('{"session":"dry-run"}\n', encoding="utf-8")
        path = root / "binding.json"
        path.write_text(json.dumps(_binding_document(source)), encoding="utf-8")
        self.binding = load_assurance_live_binding(path)
        self.capability = _golden_capability()
        self.testbed = FakeTestbed(**testbed)
        self.timing = timing or LiveTiming()
        topology = live_topology(self.binding, self.capability)
        self.reader = KpmUeAttributionReader(
            read_new_lines=self.testbed.read_new_lines, topology=topology
        )
        identity = observe_live_ue(
            self.reader,
            now=self.testbed.now,
            sleep_ms=self.testbed.sleep_ms,
            freshness_ms=self.timing.freshness_bound_ms,
        )
        self.identity = identity
        self.deployment = LivePinToCellDeployment(
            home_nci=identity.serving_nci,
            target_nci=TARGET_NCI,
            ue_scope_id=str(identity.amf_ue_ngap_id),
            topology=topology,
            r1_deployment=self.binding.r1.deployment,
        )
        discovery = build_policy_type_discovery(
            self.testbed,
            policy_type_id=POLICY_TYPE_ID,
            capability_manifest=self.capability,
        )
        self.runtime = build_live_pin_to_cell_runtime(
            deployment=self.deployment,
            timing=self.timing,
            binding=self.binding,
            policy_port=self.testbed,
            policy_builder_factory=lambda kernel, contracts: LivePolicyBuilder(
                kernel=kernel,
                contracts=contracts,
                deployment=self.deployment,
                identity=identity,
                case_id="case/live-pin-to-cell",
                policy_type_discovery=discovery,
                capability_manifest=self.capability,
            ),
            reader=self.reader,
            identity=identity,
            now=self.testbed.now,
            monotonic_ms=self.testbed.monotonic_ms,
            sleep_ms=self.testbed.sleep_ms,
            case_id="case/live-pin-to-cell",
        )
        return self.runtime

    def run_case(self, **kwargs) -> Any:
        runtime = self.build(**kwargs)
        return runtime.path.run_trial(
            runtime.candidate_id(),
            cell_id=runtime.cell_id,
            observation_ticks=4,
            tick_ms=self.timing.cadence_ms,
            settle_ms=self.timing.cadence_ms,
        )

    def evaluation(self, report: Any) -> Mapping[str, Any]:
        return self.runtime.kernel.reduced_state()["trials"][report.trial_id][
            "evaluation"
        ]


class TheIdentityIsObservedNotConfigured(LiveRunFixture):

    def test_the_ue_identity_comes_off_the_live_indication(self) -> None:
        self.build()

        self.assertEqual(self.identity.amf_ue_ngap_id, AMF_UE_NGAP_ID)
        self.assertEqual(self.identity.serving_nci, HOME_NCI)
        self.assertEqual(
            self.identity.ue_id(),
            {
                "guAmfUeNgapId": {
                    "guAmI": {
                        "plmnId": {"mcc": "208", "mnc": "95"},
                        "amfRegionId": "01",
                        "amfSetId": "040",
                        "amfPointer": "04",
                    },
                    "amfUeNgapId": AMF_UE_NGAP_ID,
                }
            },
        )

    def test_an_unobservable_ue_refuses_before_anything_is_addressed(self) -> None:
        testbed = FakeTestbed()
        testbed.read_new_lines()  # drain, then let the stream go silent
        reader = KpmUeAttributionReader(
            read_new_lines=lambda: (),
            topology=live_topology(
                load_assurance_live_binding(self._binding_path()), _golden_capability()
            ),
        )

        with self.assertRaises(LiveDriverError):
            observe_live_ue(
                reader,
                now=testbed.now,
                sleep_ms=testbed.sleep_ms,
                freshness_ms=4000,
                attempts=2,
            )

    def test_an_indication_from_another_association_is_not_this_ue(self) -> None:
        """A re-associated gNB is a different connection, not the same node."""
        topology = live_topology(
            load_assurance_live_binding(self._binding_path()), _golden_capability()
        )
        stale = FakeTestbed()
        lines = [
            line.replace('"connection_epoch":173', '"connection_epoch":161')
            for line in stale.read_new_lines()
        ]
        reader = KpmUeAttributionReader(
            read_new_lines=lambda: tuple(lines), topology=topology
        )

        self.assertEqual(reader.refresh(), ())
        self.assertEqual(reader.rejected_records, len(lines))

    def test_a_current_node_and_epoch_cannot_attribute_the_other_nodes_cell(self) -> None:
        topology = live_topology(
            load_assurance_live_binding(self._binding_path()), _golden_capability())
        for own, other in ((0xE00, 0xB00), (0xB00, 0xE00)):
            with self.subTest(nb_id=own):
                lines = []
                for line in FakeTestbed(serving_nb_id=own).read_new_lines():
                    record = json.loads(line)
                    record['nb_id'] = other
                    lines.append(json.dumps(record))
                reader = KpmUeAttributionReader(
                    read_new_lines=lambda: tuple(lines), topology=topology)
                self.assertEqual((), reader.refresh())
                self.assertEqual(len(lines), reader.rejected_records)

    def test_both_correct_node_epoch_and_nb_pairs_still_attribute_their_cells(self) -> None:
        topology = live_topology(
            load_assurance_live_binding(self._binding_path()), _golden_capability())
        for nb_id, nci in ((0xE00, HOME_NCI), (0xB00, TARGET_NCI)):
            with self.subTest(nb_id=nb_id):
                testbed = FakeTestbed(serving_nb_id=nb_id)
                reader = KpmUeAttributionReader(
                    read_new_lines=testbed.read_new_lines, topology=topology)
                observations = reader.refresh()
                self.assertTrue(observations)
                self.assertTrue(all(row.serving_nci == nci for row in observations))
                self.assertEqual(0, reader.rejected_records)

    def test_an_unparseable_or_ambiguous_node_cannot_supply_the_nb_identity(self) -> None:
        from dataclasses import replace
        topology = live_topology(
            load_assurance_live_binding(self._binding_path()), _golden_capability())
        record = json.loads(FakeTestbed().read_new_lines()[0])
        for node in ('unbound-node', 'nb=3584', 'nb=0xE00/00',
                     'nb=3584/00;nb=2816/00'):
            with self.subTest(node=node):
                record['e2_node'] = node
                declared = replace(topology, expected_epochs={node: record['connection_epoch']})
                reader = KpmUeAttributionReader(
                    read_new_lines=lambda: (json.dumps(record),), topology=declared)
                self.assertEqual((), reader.refresh())
                self.assertEqual(1, reader.rejected_records)

    def _binding_path(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / "record.json"
        source.write_text("{}", encoding="utf-8")
        path = root / "binding.json"
        path.write_text(json.dumps(_binding_document(source)), encoding="utf-8")
        return path


class AnAcknowledgementIsNotAnEffect(LiveRunFixture):

    def test_a_verified_handover_finalizes_live(self) -> None:
        report = self.run_case()

        self.assertEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS)
        self.assertEqual(report.evidence_status, EvidenceCellStatus.CLOSED_PASS.value)
        self.assertEqual(self.testbed.serving_nb_id, 0x00000B00)

    def test_the_success_rests_on_both_observations(self) -> None:
        self.run_case()
        corroborated = [
            read
            for read in self.runtime.readback.reads
            if read["detail"] == "status-and-stream"
        ]

        self.assertTrue(corroborated)
        for read in corroborated:
            self.assertEqual(read["observed"], {"servingCell": str(TARGET_NCI)})

    def test_an_accepted_policy_whose_effect_never_landed_is_not_a_success(
        self,
    ) -> None:
        """The producer says ``VERIFIED``; nothing else ever saw it.

        This is the case the whole corroboration exists for, and it must not be
        reported as a deployed change.
        """
        report = self.run_case(stream_follows_policy=False)

        self.assertNotEqual(report.outcome, TrialOutcome.SUCCESS)
        self.assertIn(
            "status verified but the stream did not corroborate",
            [read["detail"] for read in self.runtime.readback.reads],
        )

    def test_a_quarantined_episode_is_a_failure_terminal_not_a_success(self) -> None:
        report = self.run_case(episode_outcome="QUARANTINED")

        self.assertNotEqual(report.outcome, TrialOutcome.SUCCESS)
        self.assertNotEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)


class TheWindowLinesUpWithTheKernel(LiveRunFixture):

    def test_the_hold_is_judged_from_complete_windows(self) -> None:
        report = self.run_case()
        evaluation = self.evaluation(report)

        self.assertEqual(
            evaluation["measurementSufficiency"],
            MeasurementSufficiency.SUFFICIENT.value,
        )
        self.assertEqual(
            evaluation["executionValidity"], ExecutionValidity.VALID.value
        )
        self.assertTrue(evaluation["holdComplete"])
        self.assertEqual(
            evaluation["predicateVerdicts"],
            {
                "serving-cell-is-pinned-throughout": PredicateVerdict.PASS.value,
                "serving-cell-is-pinned-only": PredicateVerdict.PASS.value,
            },
        )

    def test_the_reporting_grid_is_anchored_on_the_observation_window(self) -> None:
        report = self.run_case()
        trial = self.runtime.kernel.reduced_state()["trials"][report.trial_id]
        emitted = self.runtime.collector.emitted()

        self.assertEqual(emitted[0].observed_at, trial["observationStartedAt"])
        self.assertEqual(self.runtime.collector.anchor, trial["observationStartedAt"])
        for sample in emitted:
            self.assertEqual(sample.counter_id, COUNTER_ID)
            self.assertEqual(sample.cadence_ms, self.timing.cadence_ms)
            self.assertEqual(sample.value.unit, "nci")
            self.assertEqual(sample.scope_snapshot, {"ueId": str(AMF_UE_NGAP_ID)})
            self.assertEqual(sample.missing_intervals, ())

    def test_a_silent_stream_is_a_gap_and_never_the_previous_value(self) -> None:
        runtime = self.build()
        trial_id, report = runtime.path.begin_trial(runtime.candidate_id())
        self.assertIsNone(report)
        # The indications stop the moment the window opens.
        runtime.reader._read_new_lines = lambda: ()  # noqa: SLF001 - fault injection
        self.testbed.lines = []
        runtime.path.observe(ticks=8, tick_ms=self.timing.cadence_ms)
        gapped = [
            sample for sample in runtime.collector.emitted() if sample.missing_intervals
        ]

        self.assertTrue(gapped)
        for sample in gapped:
            self.assertEqual(sample.value.value, 0)
            self.assertEqual(
                sample.missing_intervals[0].reason,
                "no KPM UE attribution indication within two cadences",
            )


class ThePolicyBodyIsDerivedAndValidated(LiveRunFixture):

    def test_every_behaviour_bearing_value_traces_to_the_frozen_epoch(self) -> None:
        self.run_case()
        builder = self.runtime  # the bodies live on the builder the factory made
        bodies = self._bodies()
        applied = [entry for entry in bodies if entry["operation"] == "APPLY"]

        self.assertEqual(len(applied), 1)
        policy = applied[0]["policyObject"]
        request = applied[0]["actuationRequest"]
        self.assertEqual(policy["steeringObjective"]["kind"], "PIN_TO_CELL")
        self.assertEqual(
            policy["steeringObjective"]["actionEnvelope"]["allowedCells"],
            [{"plmnId": {"mcc": "208", "mnc": "95"}, "cId": {"ncI": TARGET_NCI}}],
        )
        self.assertEqual(
            policy["scope"]["ueId"]["guAmfUeNgapId"]["amfUeNgapId"], AMF_UE_NGAP_ID
        )
        self.assertEqual(
            policy["constraints"]["actionDeadlineMs"],
            self.timing.enforced_timeout_ms,
        )
        self.assertEqual(
            policy["constraints"]["requiredKpiFreshnessMs"],
            self.timing.freshness_bound_ms,
        )
        self.assertEqual(policy["rollbackPolicy"]["timeoutMs"],
                         self.timing.enforced_timeout_ms)
        self.assertEqual(
            request["derivationRules"]["actionDeadlineMs"],
            "harm_bound_enforced_timeout",
        )
        del builder

    def test_the_policy_never_outlives_the_permit_that_authorised_it(self) -> None:
        """Validity is the lease, not a second clock.

        Compared against the request the body was built from rather than
        against the transaction record as it stands now: later permits of the
        same transaction -- the reread and the finalize -- carry their own
        leases, and reading the current one would be checking the wrong
        authorisation.  What matters is that the *policy* never named a life
        longer than the permit that authorised the write.
        """
        self.run_case()
        applied = [e for e in self._bodies() if e["operation"] == "APPLY"][0]
        request, policy = applied["actuationRequest"], applied["policyObject"]
        issued = [
            envelope.payload
            for envelope in self.runtime.event_store.iterate()
            if envelope.event_kind == "TokenIssued"
            and envelope.payload.get("transactionId") == applied["transactionId"]
            and envelope.payload.get("tokenKind") == "COMMIT"
        ]

        self.assertEqual(policy["validity"]["notBefore"], request["notBefore"])
        self.assertEqual(policy["validity"]["expiresAt"], request["expiresAt"])
        self.assertEqual(
            [entry["leaseExpiry"] for entry in issued], [request["expiresAt"]]
        )

    def test_a_command_scoped_to_another_ue_is_refused(self) -> None:
        runtime = self.build()
        builder = self._builder()
        command = {
            "operation": "APPLY",
            "transactionId": "tx",
            "trialId": "trial",
            "scope": {"ueId": "999"},
            "axis": "servingCell",
            "value": str(TARGET_NCI),
        }

        with self.assertRaises(LiveDriverError):
            builder(command)
        del runtime

    def _builder(self) -> Any:
        # The adapter holds the late-bound builder; reach it the way the
        # gateway does rather than keeping a second reference in the fixture.
        return self.runtime.adapter._build  # noqa: SLF001 - inspection

    def _bodies(self) -> Any:
        closure = self._builder().__closure__
        bound = next(cell.cell_contents for cell in closure
                     if isinstance(cell.cell_contents, dict) and "build" in cell.cell_contents)
        return bound["build"].bodies


class TheConfigurationSurfaceIsTheCellIdentity(LiveRunFixture):

    def test_the_projection_reads_only_a_verified_readback(self) -> None:
        verified = {
            "aicStatus": {
                "readback": {
                    "result": "VERIFIED",
                    "observedServingCell": {
                        "plmnId": {"mcc": "208", "mnc": "95"},
                        "cId": {"ncI": TARGET_NCI},
                    },
                }
            }
        }
        acknowledged = {
            "aicStatus": {
                "control": {"result": "ACK", "resultIsEffectEvidence": False},
                "readback": {"result": "PENDING"},
            }
        }

        self.assertEqual(
            serving_cell_projection(verified), {"servingCell": str(TARGET_NCI)}
        )
        self.assertIsNone(serving_cell_projection(acknowledged))

    def test_the_baseline_the_permit_names_is_the_observed_home_cell(self) -> None:
        runtime = self.build()

        self.assertEqual(
            config_hash(runtime.deployment.baseline_config),
            config_hash({"servingCell": str(HOME_NCI)}),
        )
        self.assertEqual(
            runtime.deployment.applied_config, {"servingCell": str(TARGET_NCI)}
        )


class TheContractFamilyIsCrossConsistent(unittest.TestCase):

    def test_the_live_family_validates_as_a_set(self) -> None:
        from assurance.contracts import validate_family_set
        from assurance.contracts.capability import (
            DeploymentBinding,
            TransportSecurity,
        )

        deployment = LivePinToCellDeployment(
            home_nci=HOME_NCI,
            target_nci=TARGET_NCI,
            ue_scope_id="131",
            topology=__import__(
                "assurance.live.pin_to_cell_driver", fromlist=["LiveCellTopology"]
            ).LiveCellTopology(
                plmn={"mcc": "208", "mnc": "95"},
                nb_id_to_nci={0xE00: HOME_NCI, 0xB00: TARGET_NCI},
                expected_epochs=dict(EXPECTED_EPOCHS),
            ),
            r1_deployment=DeploymentBinding(
                contract_id="deployment/r1",
                version="1.0.0",
                schema_version="assurance/1.0.0",
                document_status="NORMATIVE",
                standard_mapping={"O-RAN": "r1"},
                endpoint_id="r1",
                base_url="https://192.168.50.1:18443/r1",
                transport_security=TransportSecurity.MTLS_AND_OAUTH2,
                secret_refs={"mtlsCa": "file:/run/secrets/ca.crt"},
                trust_anchor_ref="file:/run/secrets/ca.crt",
            ),
        )
        contracts = live_contract_set(deployment, LiveTiming())

        validate_family_set(list(contracts.values()))

    def test_the_home_and_target_cells_cannot_be_the_same(self) -> None:
        from assurance.live.pin_to_cell_driver import LiveCellTopology
        from assurance.contracts.capability import (
            DeploymentBinding,
            TransportSecurity,
        )

        with self.assertRaises(LiveDriverError):
            LivePinToCellDeployment(
                home_nci=TARGET_NCI,
                target_nci=TARGET_NCI,
                ue_scope_id="131",
                topology=LiveCellTopology(
                    plmn={"mcc": "208", "mnc": "95"},
                    nb_id_to_nci={0xB00: TARGET_NCI},
                    expected_epochs=dict(EXPECTED_EPOCHS),
                ),
                r1_deployment=DeploymentBinding(
                    contract_id="deployment/r1",
                    version="1.0.0",
                    schema_version="assurance/1.0.0",
                    document_status="NORMATIVE",
                    standard_mapping={"O-RAN": "r1"},
                    endpoint_id="r1",
                    base_url="https://192.168.50.1:18443/r1",
                    transport_security=TransportSecurity.MTLS,
                    secret_refs={},
                ),
            )



class TheScopeIsFreedWithoutLosingTheRecord(unittest.TestCase):
    """The producer admits one policy per scope, and counts an expired one.

    The reverse-direction attempt at 08:47 was refused ``A1-P returned HTTP 409``
    by the policy the *successful* run had created for the same UE
    (``docs/integration/evidence/GATE3-OTA-20260824T084714Z-run.json``), so
    "never withdraw a verified effect" and "run a second episode on this UE"
    cannot both hold literally.  They are reconciled by archiving the record
    before moving it, and these are the tests of that rule.
    """

    VERIFIED = {
        "policyId": "policy-verified",
        "episodeState": "APPLIED_VERIFIED",
        "episodeTerminal": True,
        "enforceStatus": "NOT_ENFORCED",
        "readback": "VERIFIED",
    }
    STUCK = {
        "policyId": "policy-stuck",
        "episodeState": "QUARANTINED",
        "episodeTerminal": True,
        "enforceStatus": "NOT_ENFORCED",
        "readback": None,
    }

    def test_a_verified_effect_is_kept_unless_the_operator_says_otherwise(self) -> None:
        from tools.g3ota.composition import scope_withdrawal_plan

        plan = scope_withdrawal_plan(
            [self.VERIFIED, self.STUCK], withdraw_verified=False
        )

        self.assertEqual(
            [(entry["policyId"], entry["decision"]) for entry in plan],
            [("policy-verified", "KEEP"), ("policy-stuck", "WITHDRAW")],
        )

    def test_the_operator_can_free_a_scope_a_verified_effect_still_holds(self) -> None:
        from tools.g3ota.composition import scope_withdrawal_plan

        plan = scope_withdrawal_plan(
            [self.VERIFIED, self.STUCK], withdraw_verified=True
        )

        self.assertEqual({entry["decision"] for entry in plan}, {"WITHDRAW"})
        self.assertEqual(
            plan[0]["reason"], "verified effect, archived before withdrawal"
        )

    def test_withdrawing_a_verified_effect_without_an_archive_is_refused(self) -> None:
        """Refused before the transport, so nothing is deleted on the way out."""
        from tools.g3ota.composition import clear_ue_scope

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "producer.sqlite3"
            self._write_producer(database)
            with self.assertRaises(LiveDriverError):
                clear_ue_scope(
                    _NeverReached(),
                    state_database=database,
                    amf_ue_ngap_id=132,
                    policy_type_id=POLICY_TYPE_ID,
                    withdraw_verified=True,
                    archive=None,
                )

    def test_the_archive_holds_the_producer_row_verbatim(self) -> None:
        from tools.g3ota.composition import scope_occupant_records

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "producer.sqlite3"
            policy = self._write_producer(database)
            records = scope_occupant_records(database, amf_ue_ngap_id=132)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["policyObject"], policy)
        self.assertEqual(records[0]["status"]["aicStatus"]["episodeState"],
                         "APPLIED_VERIFIED")
        self.assertEqual(records[0]["digest"], "digest-1")

    def test_a_policy_for_another_ue_is_not_this_scope(self) -> None:
        from tools.g3ota.composition import scope_occupant_records

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "producer.sqlite3"
            self._write_producer(database)
            self.assertEqual(
                scope_occupant_records(database, amf_ue_ngap_id=999), []
            )

    def _write_producer(self, database: Path) -> Dict[str, Any]:
        import sqlite3

        policy = {
            "scope": {
                "ueId": {
                    "guAmfUeNgapId": {
                        "amfUeNgapId": 132,
                        "guAmI": {
                            "plmnId": {"mcc": "208", "mnc": "95"},
                            "amfRegionId": "01",
                            "amfSetId": "040",
                            "amfPointer": "04",
                        },
                    }
                }
            },
            "steeringObjective": {
                "kind": "PIN_TO_CELL",
                "actionEnvelope": {
                    "allowedCells": [
                        {"plmnId": {"mcc": "208", "mnc": "95"},
                         "cId": {"ncI": TARGET_NCI}}
                    ],
                    "forbiddenCells": [],
                },
            },
        }
        status = {
            "enforceStatus": "NOT_ENFORCED",
            "aicStatus": {
                "episodeState": "APPLIED_VERIFIED",
                "episodeTerminal": True,
                "readback": {"result": "VERIFIED"},
            },
        }
        connection = sqlite3.connect(database)
        connection.execute(
            "create table policies (policy_id text, policy_type_id text, "
            "policy_json text, status_json text, digest text, scope_key text, "
            "revision int, idempotency_key text, status_seq int, "
            "producer_epoch text, fenced int, updated_at text)"
        )
        connection.execute(
            "insert into policies values (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("policy-verified", POLICY_TYPE_ID, json.dumps(policy),
             json.dumps(status), "digest-1", "scope-1", 1, "idem-1", 1,
             "epoch-1", 0, "2026-08-24T08:04:00Z"),
        )
        connection.commit()
        connection.close()
        return policy


class _NeverReached:
    """An A1-P binding the refusal must not get far enough to use."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - a guard
        raise AssertionError(
            f"the refusal reached the transport and asked for {name!r}"
        )
if __name__ == "__main__":
    unittest.main()


class TheSteeringReadbackFollowsAUeThatReRegisters(unittest.TestCase):
    """2026-09-16 attempts 130 and 144 both ended EXECUTION_FAILURE here.

    In 144, ue1 re-registered from AMF id 24 to 26 about three minutes into the
    sitting; the KPM stream carried 26 continuously from then on, and the trials
    at +260 s and +269 s still asked for 24 and were answered "the contracted
    readback did not produce an observation".  Two of those end the episode.  The
    role is sealed and the id is resolved at execution (docs/design/
    ue-identity-continuity.md); this readback was still holding the id captured
    when the participant was composed.
    """

    class _Reader:
        """Answers only for the id it is asked about, like the live stream."""

        def __init__(self, live_id: int, nci: str) -> None:
            self.live_id, self.nci, self.asked = live_id, nci, []

        def refresh(self, *, amf_ue_ngap_id=None):
            return ()

        def at_or_before(self, _now, *, lookback_ms, amf_ue_ngap_id):
            self.asked.append(amf_ue_ngap_id)
            if int(amf_ue_ngap_id) != self.live_id:
                return None
            return type("Obs", (), {"serving_nci": self.nci})()

    def _readback(self, reader, amf):
        from assurance.live.pin_to_cell_driver import CorroboratedServingCellReadback

        return CorroboratedServingCellReadback(
            reader=reader, status_poller=object(), amf_ue_ngap_id=amf,
            now=lambda: "2026-09-16T08:17:13.000000Z", monotonic_ms=lambda: 0,
            sleep_ms=lambda _ms: None, cadence_ms=1000, deadline_ms=20000,
            freshness_bound_ms=4000, corroboration_deadline_ms=4000)

    def test_a_frozen_id_stops_reading_when_the_ue_comes_back_as_someone_else(self):
        reader = self._Reader(live_id=26, nci="12345678")
        frozen = self._readback(reader, 24)
        self.assertIsNone(frozen(scope={}, transaction_id="tx", policy_id=None))
        # It now polls out the readback deadline before giving up (a UE keeper
        # revives mid-sitting is away ~10 s), so the count is the poll budget --
        # what matters is that it never asked about anyone but the frozen id.
        self.assertEqual(set(reader.asked), {24})

    def test_a_resolver_follows_the_new_id_and_reads(self):
        reader = self._Reader(live_id=26, nci="12345678")
        current = [24]
        live = self._readback(reader, lambda: current[0])
        self.assertIsNone(live(scope={}, transaction_id="tx", policy_id=None))
        current[0] = 26                      # the role re-registers
        observed = live(scope={}, transaction_id="tx", policy_id=None)
        self.assertEqual(dict(observed), {"servingCell": "12345678"})
        self.assertEqual(reader.asked[-1], 26)
        self.assertEqual(set(reader.asked), {24, 26})

    def test_a_resolver_that_cannot_answer_keeps_the_last_id_rather_than_nothing(self):
        reader = self._Reader(live_id=26, nci="12345678")
        current = [26]
        live = self._readback(reader, lambda: current[0])
        self.assertIsNotNone(live(scope={}, transaction_id="tx", policy_id=None))
        current[0] = None                    # briefly unaddressable, not re-registered
        self.assertIsNotNone(live(scope={}, transaction_id="tx", policy_id=None))
        self.assertEqual(reader.asked, [26, 26])

    def test_the_baseline_read_waits_out_a_keeper_revival_instead_of_failing(self):
        """2026-09-18: keeper revives a dead UE *during* the sitting.

        `UE_ZOMBIE_RESTART_DURING_EPISODE` stops the softmodem and starts it
        again; measured over 84 revivals the stream carries nothing for a
        median of 10.0 s (p99 21.0 s).  Board 032251 asked once inside that
        gap, was told "the contracted readback did not produce an observation"
        and lost two trials while the radio was fine.  The corroboration path
        below already polled; this one looked once.
        """
        reader = self._Reader(live_id=26, nci="12345678")
        reader.live_id = None                # the UE is away: nobody answers
        slept = []
        live = self._readback(reader, lambda: 26)
        live._sleep_ms = lambda ms: (slept.append(ms),
                                     setattr(reader, "live_id", 26)
                                     if len(slept) == 3 else None)
        observed = live(scope={}, transaction_id="tx", policy_id=None)
        self.assertEqual(dict(observed), {"servingCell": "12345678"},
                         "the read must survive a gap the UE comes back from")
        self.assertEqual(len(slept), 3, "it waited exactly until the UE returned")

    def test_the_baseline_read_still_gives_up_on_a_ue_that_never_returns(self):
        """Waiting is bounded: a UE that stays away must not hang the sitting."""
        reader = self._Reader(live_id=None, nci="12345678")
        slept = []
        live = self._readback(reader, lambda: 26)
        live._sleep_ms = slept.append
        self.assertIsNone(live(scope={}, transaction_id="tx", policy_id=None))
        self.assertEqual(len(slept), 20, "20 s of polling at the 1 s cadence")

    def test_the_composition_hands_the_resolver_in(self):
        import inspect
        from tools.liveconsole import agent as agent_mod

        source = inspect.getsource(agent_mod._compose_agent_sitting)
        self.assertIn('key = f"r1-steer@{ue}"', source)
        steering = source.split('key = f"r1-steer@{ue}"', 1)[1][:700]
        self.assertIn("amf_of=", steering,
                      "the steering participant must carry the role resolver")


class AStaleResolverMustNotEraseAUsableUeId(unittest.TestCase):
    """Resolving per read was half the fix; the other half is the fallback.

    ``CorroboratedServingCellReadback`` starts with no id when it is handed a
    resolver, so a role whose resolver entry was already older than
    ``ROLE_IDENTITY_MAX_AGE_S`` on the very first read asked the KPM stream for
    nothing and the trial was locked down with "the contracted readback did not
    produce an observation" -- attempt 158 of 2026-09-16 lost r1-steer@ue2 that
    way, with the per-read resolution already deployed.  The composition path
    now wraps the resolver so the composition-time id is the fallback.
    """

    def participant(self, amf_of, pinned=61):
        from types import SimpleNamespace
        return SimpleNamespace(amf_of=amf_of,
                               identity=SimpleNamespace(amf_ue_ngap_id=pinned))

    def test_without_a_resolver_the_plain_integer_is_passed_through(self):
        from assurance.live.joint_runtime import _resolved_or_pinned
        self.assertEqual(61, _resolved_or_pinned(self.participant(None)))

    def test_with_a_resolver_the_readback_follows_a_re_registration(self):
        from assurance.live.joint_runtime import _resolved_or_pinned
        current = {"id": 61}
        source = _resolved_or_pinned(self.participant(lambda: current["id"]))
        self.assertEqual(61, source())
        current["id"] = 68
        self.assertEqual(68, source())

    def test_a_resolver_that_cannot_answer_yet_falls_back_on_the_first_read(self):
        from assurance.live.joint_runtime import _resolved_or_pinned
        source = _resolved_or_pinned(self.participant(lambda: None))
        self.assertEqual(61, source(), "a stale entry must not read nothing at all")

    def test_a_gap_holds_the_last_known_id_not_the_composition_time_one(self):
        """공백은 쓸 수 있는 번호를 지우면 안 된다 (2026-09-22).

        예전엔 공백에 조립 시점 id(61)로 떨어졌다.  그러면 쓰기 쪽
        (`_role_following_builder.build_for`, 공백에 마지막 id 유지)은 68 을,
        되읽기는 61 을 가리켜 **서로 다른 UE** 를 상대한다.  게다가 61 은 재등록으로
        죽은 번호라 관측이 아예 안 나온다.  마지막으로 확인된 번호를 쥐고 있어야
        쓰기와 일치하고 관측도 나온다.
        """
        from assurance.live.joint_runtime import _resolved_or_pinned
        answers = [68, None]
        source = _resolved_or_pinned(self.participant(lambda: answers.pop(0)))
        self.assertEqual(68, source())
        self.assertEqual(68, source(), "공백이 살아 있는 번호를 지워선 안 된다")

class TheServingCellCollectorFollowsAUeThatReRegisters(unittest.TestCase):
    """되읽기만 고치고 **수집기는 얼린 채**였다 (2026-09-17).

    `joint_runtime` 은 되읽기에 `_resolved_or_pinned(...)`(매 읽기 재해석)를 주면서
    `ServingCellCollector` 에는 `int(participant.identity.amf_ue_ngap_id)` 를 주고
    있었다.  UE 가 판 도중 재등록하면 수집기는 옛 번호를 계속 묻고, 그 구간의 칸이
    비어 `servingCell` coverage 가 1.0 미만이 되며, 그러면 그 KPI 는 통째로
    게시되지 않는다 -- 2026-09-17 실측으로 시행의 **44%** 가 그랬다.

    한 경로를 고쳤으면 같은 원리를 쓰는 다른 경로를 전부 찾아야 한다.
    """

    class _Reader:
        def __init__(self) -> None:
            self.asked = []

        def refresh(self, *, amf_ue_ngap_id=None):
            self.asked.append(amf_ue_ngap_id)
            return ()

        def at_or_before(self, _now, *, lookback_ms, amf_ue_ngap_id):
            return None

    def _collector(self, reader, amf):
        from assurance.live.pin_to_cell_driver import ServingCellCollector

        return ServingCellCollector(
            reader=reader, deployment=None, timing=None,
            anchor=lambda: None,           # 앵커가 없으면 poll 은 일찍 돌아온다
            amf_ue_ngap_id=amf,
            clock_health=lambda observation, instant: None,
            source_id="t", counter_id="c", scope_snapshot={"ueId": "ue1"},
            cadence_ms=1000)

    def test_a_frozen_id_keeps_asking_for_the_old_one(self):
        reader = self._Reader()
        frozen = self._collector(reader, 24)
        frozen.poll(now="2026-09-17T08:00:00.000000Z")
        frozen.poll(now="2026-09-17T08:00:01.000000Z")
        self.assertEqual(reader.asked, [24, 24])

    def test_a_resolver_follows_the_new_id(self):
        reader = self._Reader()
        current = [24]
        live = self._collector(reader, lambda: current[0])
        live.poll(now="2026-09-17T08:00:00.000000Z")
        current[0] = 26                     # 역할이 재등록한다
        live.poll(now="2026-09-17T08:00:01.000000Z")
        self.assertEqual(reader.asked, [24, 26])

    def test_a_momentary_gap_does_not_erase_the_usable_id(self):
        """해석기가 잠깐 답하지 못하는 것은 재등록이 아니다 -- 읽던 번호를 지우지 않는다."""
        reader = self._Reader()
        answer = [26]
        live = self._collector(reader, lambda: answer[0])
        live.poll(now="2026-09-17T08:00:00.000000Z")
        answer[0] = None                    # 순간적인 공백
        live.poll(now="2026-09-17T08:00:01.000000Z")
        self.assertEqual(reader.asked, [26, 26])



class TheCellBaselineComesFromTheLiveStream(unittest.TestCase):
    """2026-09-18: 선언 상수만 쓰면 배포가 그 값에 있지 않을 때 허가증이 라이브와
    어긋나 `REJECTED_CONFIG_MISMATCH` 로 모든 시행이 쓰기 전에 거절된다 — gnb2 는
    배포 시점부터 10 dB 인데 선언은 `0.0` 이라 완주 판 5 개 중 3 개가 그렇게 죽었다.
    """

    def test_a_cell_baseline_is_what_that_cell_is_actually_at(self) -> None:
        from assurance.objectives.joint import TxAttenuationAxisSpec

        ladder = ("13.0", "16.0", "19.0", "22.0")
        declared = TxAttenuationAxisSpec(87654321, ladder)
        live = TxAttenuationAxisSpec(87654321, ladder, observed_baseline="10.0")

        # 못 읽었으면 종전대로 선언 상수 -- 조립을 멈추지 않는다.
        self.assertEqual("0.0", declared.baseline)
        self.assertEqual("10.0", live.baseline)
        # 기준값은 rung 이 아니라 사다리 **앞**에 선다.
        self.assertEqual(("10.0",) + ladder, live.values)
        self.assertEqual(ladder, live.rungs)

    def test_the_reader_names_cells_through_the_topology(self) -> None:
        """NCI 는 KPM 스코프에 없다 -- `e2_node` 를 `nb_id_to_nci` 로 옮겨야 한다."""
        from tools.liveconsole.agent import _live_cell_attenuations

        class _Sample:
            counter_id = "RAN.Cell.TxAttenuationDb"

            def __init__(self, node, value):
                self.scope_snapshot = {"e2_node": node}
                self.value = type("V", (), {"value": value})()

        class _Parsed:
            def __init__(self, samples):
                self.samples = samples

        node_2816 = "ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:00000000000000000000"
        node_3584 = "ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000"

        class _Adapter:
            def parse_lines(self, lines):
                return _Parsed([_Sample(node_2816, 10.0), _Sample(node_3584, 0.0)])

        class _Topology:
            nb_id_to_nci = {2816: 87654321, 3584: 12345678}

        found = _live_cell_attenuations(
            lambda: ["{}"], _Adapter(), [87654321, 12345678], _Topology)
        self.assertEqual({87654321: "10.0", 12345678: "0.0"}, found)

    def test_an_unreadable_stream_leaves_the_declared_constant_alone(self) -> None:
        """못 읽었다고 조립을 멈추면 축 하나가 판 전체를 죽인다."""
        from tools.liveconsole.agent import _live_cell_attenuations

        class _Boom:
            def parse_lines(self, lines):
                raise ValueError("스트림이 깨졌다")

        class _Topology:
            nb_id_to_nci = {2816: 87654321}

        def _raises():
            raise OSError("읽을 수 없다")

        self.assertEqual({}, _live_cell_attenuations(_raises, _Boom(), [87654321], _Topology))
        self.assertEqual({}, _live_cell_attenuations(lambda: ["{}"], _Boom(), [87654321], _Topology))
        self.assertEqual({}, _live_cell_attenuations(lambda: [], _Boom(), [87654321], _Topology))


class TheBaselineReaderReadsThroughARealFanOut(unittest.TestCase):
    """2026-09-18: 이 검사를 쓰다가 **내 수정이 틀렸다는 것**을 알았다.

    나는 "새 소비자는 `drain()` 전까지 비어 있다" 고 보고 호출부에 `fan_out.drain()`
    을 넣었는데, `_FanOutConsumer.__call__` 이 스스로 `drain()` 을 부른다
    (`build.py:551`).  검사가 빨개져서 되돌렸다.

    그리고 픽스처도 두 번 틀렸다 -- 처음엔 `lambda: ["{}"]` 로 줄을 직접 줬고(꼬리를
    안 거침), 그다음엔 내가 손으로 지은 JSON 이 어댑터 스키마를 못 맞췄다(실제 줄에는
    `event` · `recv_unix_us` · `connection_epoch` · `inventory_sha256` 이 있다).
    그래서 **라이브 게이트에서 뜬 줄을 그대로** `tests/fixtures/kpm-atten-line.json`
    에 두고 쓴다.
    """

    def setUp(self) -> None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "fixtures", "kpm-atten-line.json")) as handle:
            self.line = handle.read().strip()
        record = json.loads(self.line)
        self.nb_id = record["nb_id"]
        self.epochs = {record["e2_node"]: record["connection_epoch"]}
        self.nci = 87654321

    def _tail(self):
        from tools.liveconsole.build import _FanOutTail

        served = [[self.line]]   # 꼬리는 한 번만 내어 준다: 라이브와 같다

        def read():
            return served.pop() if served else []

        return _FanOutTail(read)

    def _topology(self):
        nb_id, nci = self.nb_id, self.nci

        class _Topology:
            nb_id_to_nci = {nb_id: nci}

        return _Topology

    def test_calling_a_consumer_drains_the_tail_for_every_consumer(self) -> None:
        tail = self._tail()
        first, second = tail.consumer(), tail.consumer()
        self.assertEqual(1, len(first()))           # 호출이 곧 drain 이다
        self.assertEqual(1, len(second()))          # 같은 줄이 둘 모두에게 갔다
        self.assertEqual([], list(tail.consumer()()))   # 꼬리는 이제 비었다

    def test_a_baseline_is_read_through_the_real_tail(self) -> None:
        from tools.liveconsole.agent import _live_cell_attenuations
        from assurance.collector import KpmJsonlAdapter

        found = _live_cell_attenuations(
            self._tail().consumer(), KpmJsonlAdapter(expected_epochs=self.epochs),
            [self.nci], self._topology())
        self.assertEqual({self.nci: "10.0"}, found)

    def test_an_exhausted_tail_leaves_the_declared_constant(self) -> None:
        """꼬리에 그 셀의 값이 없으면 빈 dict -- 조립은 선언 상수로 가고 멈추지 않는다."""
        from tools.liveconsole.agent import _live_cell_attenuations
        from assurance.collector import KpmJsonlAdapter

        tail = self._tail()
        tail.consumer()()                      # 다른 소비자가 꼬리를 비운다
        found = _live_cell_attenuations(
            tail.consumer(), KpmJsonlAdapter(expected_epochs=self.epochs),
            [self.nci], self._topology())
        self.assertEqual({}, found)
