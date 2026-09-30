"""Live console composition root — verification, with the radio switched off.

``tools/liveconsole`` is the only place in this repository that composes the
Cockpit over the *real* O-RAN control path, so it is also the only place that
could accidentally badge a mock as Live, address a deployment whose identity
moved, or drag a transport into the default entry point.  This file proves it
does none of those.

Hermetic by construction, exactly as the brief requires: every live port is
injected — the KPM indication stream is a scripted list, the R1 policy port is
an in-process fake, the A1-P producer database is a sqlite file in ``tmp``, the
actuation adapter is
:class:`~assurance.gateway.mock_adapter.MockActuationAdapter` handed in through
``adapter_override``, and the clock is virtual.  **No socket, no process, no
radio.**

The *deployment identity* is synthesised in ``tmp`` too, and that is deliberate
rather than convenient.  A binding pins the sha256 of each document it was cut
from, so a test that loaded the committed binding would go red every time the
laboratory outside this repository is re-pinned — reporting a fact about the
lab as a defect in this code.  :class:`LiveConsoleFixture` therefore copies a
deployment's documents into ``tmp`` and writes a binding whose source digests
are computed over those copies, so the identity under test is self-consistent
and immovable.  What the *live* refusal does when a digest really has moved is
proved directly, on documents this file moves on purpose.

The one thing injection cannot be allowed to buy is a Live badge, so
:func:`tools.liveconsole.build.session_mode` derives the mode from whether this
root built every port itself, and the tests below check both directions.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.core.axes import TrialOutcome
from assurance.core.states import TrialState
from assurance.gateway.mock_adapter import MockActuationAdapter
from assurance.live.pin_to_cell_driver import PIN_TO_CELL_GRAMMAR, LiveUeObservation
from assurance.objectives import RegistryError, record_for
from oran.rapp.contract_support import load_schema

from gui.operator.sources.kernel_live import MODE_LIVE, MODE_MOCK

from tools.liveconsole import (
    GATE3_REGRESSION_CASE,
    LiveConsoleError,
    build_live_session,
    live_capable_families,
    load_live_deployment,
    session_mode,
    write_run_evidence,
)
from tools.liveconsole.build import (
    family_for_utterance, observe_selected_ue, requested_amf_ue_ngap_id)
from tools.liveconsole.profile import LIVE_CONSOLE_KEY, REQUIRED_LIVE_KEYS

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The committed authority a live run is addressed through.  Reading it here is
#: the point: a test that invented its own paths would prove nothing about the
#: document an operator actually passes to ``--profile``.
COMMITTED_PROFILE = REPO_ROOT / "deployment" / "liveconsole-profile.json"

HOME_NCI, TARGET_NCI = 12345678, 87654321
HOME_NB_ID, TARGET_NB_ID = 0xE00, 0xB00
NCI_TO_NB_ID = {HOME_NCI: HOME_NB_ID, TARGET_NCI: TARGET_NB_ID}
HOME_E2_NODE = "ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000"
TARGET_E2_NODE = "ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:00000000000000000000"
#: The node string that serves each cell.  A record naming one node and a
#: different nb id is one no gNB can send, and the reader refuses the pair.
NODE_OF = {HOME_NCI: HOME_E2_NODE, TARGET_NCI: TARGET_E2_NODE}
AMF_UE_NGAP_ID = 131
SECOND_AMF_UE_NGAP_ID = 132
POLICY_TYPE_ID = "AIC_UECellSteering_1.0.0"
BASE_INSTANT = datetime(2026, 8, 25, tzinfo=timezone.utc)

#: The one constant the strict integration-values loader pins by value.
BUNDLE_MANIFEST_DIGEST = (
    "6f9908ca9cee29ca5fa7b629f4daa0f502b5b8ce244519a76b9c88c6c1710ce3")


def _schema_shaped_value(key: str) -> Any:
    """A value the integration-values schema accepts for *key*.

    The same shape rules ``tests/test_integration_values.py`` uses to build a
    valid document without a deployment; only the handful of keys this
    composition root reads are then overwritten with the generated fixture.
    """
    if key.endswith("Sha256"):
        return "a" * 64
    if key.endswith("Path"):
        return "artifacts/value.json"
    if key.endswith("Ref"):
        return "env://integration-value"
    if key == "o1.netconf.endpoint":
        return "ssh://netconf.example.test:830"
    if key == "o1.sftp.allowedAuthorities":
        return ["sftp.example.test:22"]
    if key.endswith("Dn"):
        return "ManagedElement=example"
    if key == "o1.fileDataReporting.mnsVersion":
        return "v1"
    if key.endswith("ClientId") or key == "r1.rAppId":
        return "client-1"
    if key.endswith("TokenEndpoint") or key.endswith("managedObjectUri"):
        return "https://auth.example.test/token"
    if (key.endswith("apiRoot") or key.endswith("BaseUri")
            or key.endswith("Destination") or key.endswith("mnsRoot")
            or key.endswith("consumerReference")):
        return "https://service.example.test/api"
    return "integration-value"


class _Clock:
    """A virtual clock.  Nothing in this file waits on anything."""

    def __init__(self) -> None:
        self.ms = 0

    def now(self) -> str:
        moment = BASE_INSTANT + timedelta(milliseconds=self.ms)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def monotonic_ms(self) -> int:
        return self.ms

    def sleep_ms(self, ms: int) -> None:
        self.ms += max(0, int(ms))


def kpm_line(*, epoch: int, serving_nb_id: int = HOME_NB_ID,
             offset_ms: int = -100, node: str = HOME_E2_NODE,
             ues: Sequence[int] = (AMF_UE_NGAP_ID,), at_ms: int = 0) -> str:
    """One KPM format-3 UE-attribution indication, in the shape the reader parses.

    ``epoch`` is required and is read off the binding under test rather than
    written here: the reader discards every indication whose connection epoch
    is not the one the deployment was admitted at, so a hard-coded number would
    turn each of the lab's E2 re-associations into a red test.
    """
    received = BASE_INSTANT + timedelta(milliseconds=int(at_ms) + offset_ms)
    return json.dumps({
        "event": "kpm_indication",
        "kpm_msg_format": 3,
        "e2_node": node,
        "nb_id": int(serving_nb_id),
        "connection_epoch": int(epoch),
        "recv_unix_us": int(received.timestamp() * 1_000_000),
        "ues": [
            {"amf_ue_ngap_id": int(identifier),
             "guami": {"mcc": 208, "mnc": 95, "mnc_digit_len": 2,
                       "amf_region_id": 1, "amf_set_id": 64, "amf_pointer": 4}}
            for identifier in ues
        ],
    })


class _Stream:
    """The KPM tail, replaced by a list that is drained as it is appended to.

    Backed by the caller's list rather than a copy, so a test can publish a
    later indication mid-sitting exactly as the laboratory would -- the reader
    sees it on its next drain and not before.
    """

    def __init__(self, lines: List[str]) -> None:
        self._lines = lines

    def __call__(self) -> Sequence[str]:
        lines = tuple(self._lines)
        del self._lines[:]
        return lines


class _LiveStream:
    """A KPM tail that keeps publishing, as a running deployment does.

    A sitting is not one observation: each case re-reads the identity, and by
    then the virtual clock has advanced past the first indication's freshness
    window.  A fixed list would therefore make every second case look like a
    UE that had gone away, which is a property of the fixture rather than of
    the code.  So this mints one indication per drain, stamped at the clock's
    current instant, and the test turns it off (``stop``) or changes who is on
    it (``ues``) to model the laboratory events it means to.
    """

    def __init__(self, clock: Any, *, epoch: int,
                 serving_nci: Callable[[], int],
                 epoch_of: Optional[Callable[[int], int]] = None,
                 ues: Sequence[int] = (AMF_UE_NGAP_ID,)) -> None:
        self._clock = clock
        self._serving_nci = serving_nci
        # Node, nb id AND epoch all belong to the same cell.  The stream used to
        # carry the home epoch whatever cell it named, so after a steer the
        # record was (target node, target nb, HOME epoch) -- a triple the reader
        # refuses, and the sitting then reads as "no UE is observable".
        self._epoch_of = epoch_of or (lambda _nci: epoch)
        self.epoch = int(epoch)
        self.ues: Tuple[int, ...] = tuple(ues)
        self.publishing = True

    def stop(self) -> None:
        """The UE detached, or the stream died: nothing more is published."""
        self.publishing = False

    def epoch_of(self, nci: int) -> int:
        return int(self._epoch_of(int(nci)))

    def __call__(self) -> Sequence[str]:
        if not self.publishing or not self.ues:
            return ()
        # Reported from the *node that is actually serving the UE*, which after
        # a successful case is the one the trial steered it to.  A stream that
        # kept naming the home node would hide the thing a second case turns
        # on: the next contract's baseline is where the UE is now.
        # The node string moves WITH the nb id.  Giving only serving_nb_id left
        # e2_node at its HOME default, so after a steer the record claimed nb
        # 2816 while naming the 3584 node -- an indication no real gNB can send.
        # The reader refuses that pair (pin_to_cell_driver's membership test,
        # kpm_node_nb_id(node) != nb_id) and the whole sitting then reads as
        # "no UE is observable", which is how one fixture slip put nine tests
        # red across three files.
        serving = int(self._serving_nci())
        return (kpm_line(epoch=self.epoch_of(serving), ues=self.ues,
                         serving_nb_id=NCI_TO_NB_ID[serving],
                         node=NODE_OF[serving],
                         at_ms=self._clock.ms),)


class _PolicyPort:
    """An in-process stand-in for the R1 consumer.  Opens nothing.

    It answers policy-type discovery with this repository's own pinned schema,
    so :func:`tools.g3ota.composition.build_policy_type_discovery` takes its
    inline-schema branch and never has to resolve a digest.
    """

    def __init__(self, policy_type_id: str) -> None:
        self.policy_type_id = policy_type_id
        self.calls: List[str] = []

    def bootstrap_info(self) -> Mapping[str, Any]:
        self.calls.append("bootstrap_info")
        return {"apiRoot": "in-process"}

    def discover_services(self, **_kwargs: Any) -> Sequence[Any]:
        self.calls.append("discover_services")
        return ()

    def discover_policy_types(self) -> Sequence[str]:
        self.calls.append("discover_policy_types")
        return [self.policy_type_id]

    def get_policy_type(self, policy_type_id: str) -> Mapping[str, Any]:
        self.calls.append(f"get_policy_type:{policy_type_id}")
        return {"policyTypeId": policy_type_id,
                "policySchema": load_schema(f"{policy_type_id}.policy"),
                "statusSchema": load_schema(f"{policy_type_id}.status")}


class _Series:
    """A scripted stand-in for the KPM reader, one observation per grid slot."""

    def __init__(self, observed: Sequence[int]) -> None:
        self._observed = list(observed)
        self._calls = 0

    def refresh(self, **_kwargs: Any) -> tuple:
        return ()

    def at_or_before(self, instant: str, **_kwargs: Any) -> Any:
        if not self._observed:
            return None
        value = self._observed[min(self._calls, len(self._observed) - 1)]
        self._calls += 1
        return LiveUeObservation(
            amf_ue_ngap_id=AMF_UE_NGAP_ID,
            gu_ami={"plmnId": {"mcc": "208", "mnc": "95"}, "amfRegionId": "01",
                    "amfSetId": "040", "amfPointer": "04"},
            serving_nci=int(value), e2_node="node", connection_epoch=173,
            observed_at=instant, trace_hash="1" * 64,
        )


def producer_database(path: Path, rows: Sequence[Mapping[str, Any]] = ()) -> Path:
    """An A1-P producer sqlite in ``tmp``, with the columns the readers select."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "create table if not exists policies ("
            " policy_id text primary key, policy_type_id text,"
            " policy_json text, status_json text, digest text, scope_key text,"
            " revision integer, idempotency_key text, status_seq integer,"
            " producer_epoch integer, fenced integer, updated_at text,"
            " not_before_ns integer, expires_at_ns integer)")
        # Rewritten rather than appended: a fixture is called more than once per
        # test and the occupants it states are the occupants there are.
        connection.execute("delete from policies")
        for row in rows:
            connection.execute(
                "insert into policies values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["policy_id"], row.get("policy_type_id", POLICY_TYPE_ID),
                 json.dumps(row["policy"]), json.dumps(row.get("status")),
                 "0" * 64, row.get("scope_key", "scope"), 1, "idem", 1, 1, 0,
                 "2026-08-25T00:00:00Z", 0, 0))
        connection.commit()
    finally:
        connection.close()
    return path


def occupant(policy_id: str, *, episode_state: str,
             policy_type_id: str = POLICY_TYPE_ID,
             amf_ue_ngap_id: int = AMF_UE_NGAP_ID) -> Dict[str, Any]:
    return {
        "policy_id": policy_id,
        "policy_type_id": policy_type_id,
        "policy": {"scope": {"ueId": {"guAmfUeNgapId": {
            "amfUeNgapId": int(amf_ue_ngap_id)}}}},
        "status": {"enforceStatus": "ENFORCED",
                   "aicStatus": {"episodeState": episode_state,
                                 "episodeTerminal": True,
                                 "readback": {"result": "VERIFIED"}}},
    }


class LiveConsoleFixture(unittest.TestCase):
    """A tmp profile that resolves the committed deployment identity."""

    #: The whole deployment identity, generated here.  No deployment directory
    #: is read at all, so this file passes on a machine that has never seen the
    #: laboratory (Sol finding 4); :class:`TheSuiteReadsOnlyItsOwnFiles` proves
    #: it by audit rather than by naming a path.
    HOME_EPOCH, TARGET_EPOCH = 272, 273
    R1_ROOT = "https://r1.deployment.test:18443/r1"
    A1P_ROOT = "https://a1p.deployment.test:9444/A1-P/v2"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="liveconsole-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Read for its schema id and metric list only; nothing in it is
        # resolved, so this fixture needs no laboratory.
        self.committed = json.loads(COMMITTED_PROFILE.read_text(encoding="utf-8"))
        self.clock = _Clock()
        self.capability_path = self.write_capability()
        self.values_path = self.write_values()
        self.binding_path = self.write_binding()
        self.home_epoch = self.HOME_EPOCH

    # -- the generated deployment identity --------------------------------- #

    def write_capability(self, **overrides: Any) -> Path:
        """A capability manifest: the topology ``live_topology`` reads, and no more."""
        document: Dict[str, Any] = {
            "schemaDigests": {"policy": "a" * 64, "status": "b" * 64},
            "topology": {"cells": [
                {"cellId": {"cId": {"ncI": HOME_NCI},
                            "plmnId": {"mcc": "208", "mnc": "95"}},
                 "globalE2NodeId": {"nodeId": {"bitLength": 32, "hex": "0x00000e00"},
                                    "nodeType": "GNB"}},
                {"cellId": {"cId": {"ncI": TARGET_NCI},
                            "plmnId": {"mcc": "208", "mnc": "95"}},
                 "globalE2NodeId": {"nodeId": {"bitLength": 32, "hex": "0x00000b00"},
                                    "nodeType": "GNB"}},
            ]},
        }
        document.update(overrides)
        path = self.tmp / "capability.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True),
                        encoding="utf-8")
        return path

    def write_values(self, **overrides: Any) -> Path:
        """An integration-values document the strict loader accepts.

        Built from the contract schema's own required key list rather than from
        a copy of a deployment's file, so it is exactly the 80 keys the loader
        demands and carries no laboratory in it.  The handful of keys this
        composition root actually reads are set to the generated fixture; the
        rest are schema-shaped filler.
        """
        from oran.contract.integration_values import SCHEMA_NAME
        from oran.contract.validator import ContractValidator

        required = ContractValidator().schema(SCHEMA_NAME)["properties"]["values"]["required"]
        values = {key: _schema_shaped_value(key) for key in required}
        values["r1.apiRoot"] = self.R1_ROOT
        values["a1.apiRoot"] = self.A1P_ROOT
        values["backend.capabilityManifestPath"] = self.capability_path.name
        values["backend.capabilityManifestSha256"] = hashlib.sha256(
            self.capability_path.read_bytes()).hexdigest()
        values.update(overrides)
        path = self.tmp / "integration-values.json"
        path.write_text(json.dumps({
            "schemaVersion": "oran-aic-integration-values/1.0.0",
            "contractProfile": "oran-aic/1.0.0",
            "deploymentMode": "MERGED",
            "bundleManifestJcsSha256": BUNDLE_MANIFEST_DIGEST,
            "values": values,
        }, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def write_binding(self, *, name: str = "binding.json", **overrides: Any) -> Path:
        """A live binding cut from the generated values, digests computed now.

        A binding pins the sha256 of every document it was cut from, so a test
        that loaded the committed one would go red each time the dispatcher
        re-pins the laboratory — reporting a fact about the lab as a defect
        here.  It did, twice, while WP-B was being written.
        """
        secret = f"file://{self.tmp}/secret"
        refs = {"mtlsCa": secret, "mtlsClientCertificate": secret,
                "mtlsClientPrivateKey": secret, "oauthToken": secret}
        document: Dict[str, Any] = {
            "schemaVersion": "assurance-live-binding/1.0.0",
            "bindingId": "liveconsole-test-binding",
            "sources": [{"path": str(self.values_path),
                         "sha256": hashlib.sha256(
                             self.values_path.read_bytes()).hexdigest()}],
            "r1": {"apiRoot": self.R1_ROOT,
                   "nearRtRicId": "near-rt-ric-test-001",
                   "policyTypeId": POLICY_TYPE_ID,
                   "secretRefs": dict(refs),
                   "polling": {"cadenceMs": 1000, "deadlineMs": 20000}},
            "a1p": {"apiRoot": self.A1P_ROOT, "secretRefs": dict(refs)},
            "o1": {"httpsRoot": "https://o1.deployment.test:8443/o1",
                   "netconf": "ssh://o1.deployment.test:830",
                   "sftp": "sftp://o1.deployment.test:2022/pm",
                   "pmDirectory": str(self.tmp / "pm"),
                   "secretRefs": dict(refs)},
            "kpm": {"jsonlPath": str(self.tmp / "a1-live-kpm.jsonl"),
                    "expectedEpochs": {HOME_E2_NODE: self.HOME_EPOCH,
                                       TARGET_E2_NODE: self.TARGET_EPOCH}},
            "e2Nodes": ["0x00000e00", "0x00000b00"],
            "cells": [HOME_NCI, TARGET_NCI],
            "plmn": {"mcc": "208", "mnc": "95"},
        }
        document.update(overrides)
        path = self.tmp / name
        path.write_text(json.dumps(document, indent=2, sort_keys=True),
                        encoding="utf-8")
        return path

    def kpm_line(self, **kwargs: Any) -> str:
        """One indication this binding's reader will accept."""
        kwargs.setdefault("epoch", self.home_epoch)
        return kpm_line(**kwargs)

    def write_profile(self, *, occupants: Sequence[Mapping[str, Any]] = (),
                      **overrides: Any) -> Path:
        block = {
            "assuranceBindingPath": str(self.binding_path),
            "producerDatabasePath": str(producer_database(
                self.tmp / "a1p-producer.sqlite3", occupants)),
            "r1StateDir": str(self.tmp / "r1"),
            "evidenceDir": str(self.tmp / "evidence"),
        }
        block.update(overrides.pop("live_console", {}))
        document: Dict[str, Any] = {
            "schema": self.committed["schema"],
            "profileId": "liveconsole-test",
            "label": "generated test deployment",
            "capabilityManifestPath": str(self.capability_path),
            "integrationValuesPath": str(self.values_path),
            "runsRoot": str(self.tmp / "runs"),
            "metricSelection": list(self.committed["metricSelection"]),
            LIVE_CONSOLE_KEY: block,
        }
        document.update(overrides)
        path = self.tmp / "profile.json"
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return path

    def record_clearer(self) -> Any:
        """A stand-in for ``clear_ue_scope`` that records every call it got."""
        seen: Dict[str, Any] = {}
        calls: List[bool] = []

        def clearer(binding: Any, **kwargs: Any) -> Sequence[Mapping[str, Any]]:
            seen.update(kwargs)
            withdrawing = bool(kwargs["withdraw_verified"])
            calls.append(withdrawing)
            kwargs["archive"]([occupant("p-1", episode_state="APPLIED_VERIFIED")])
            if withdrawing:
                return [{"policyId": "p-1", "decision": "WITHDRAW",
                         "action": "WITHDRAWN", "status": 204,
                         "reason": "verified effect, archived before withdrawal"}]
            return [{"policyId": "p-1", "decision": "KEEP", "action": "KEPT",
                     "reason": "verified effect"}]

        clearer.seen = seen        # type: ignore[attr-defined]
        clearer.calls = calls      # type: ignore[attr-defined]
        return clearer

    def compose(self, *, objective: str = "UELevelTarget",
                lines: Optional[Sequence[str]] = None, profile: Any = None,
                adapter: Any = None,
                occupants: Sequence[Mapping[str, Any]] = (),
                stream: Any = None,
                **kwargs: Any) -> Any:
        self.adapter = adapter if adapter is not None else MockActuationAdapter(
            config={"servingCell": str(HOME_NCI)})
        self.adapter.hosts_watchdogs = False
        self.port = _PolicyPort(POLICY_TYPE_ID)
        # A running stream by default, because a sitting re-observes the UE for
        # every case; an explicit list when a test is about what the reader
        # does with particular records (or with none).
        # ``stream`` lets a caller model a deployment this fixture does not --
        # two E2 nodes, a configuration counter -- without a second copy of the
        # composition.  Otherwise: a running stream by default, because a
        # sitting re-observes the UE for every case; an explicit list when a
        # test is about what the reader does with particular records (or none).
        self.stream: Any = stream if stream is not None else (
            _LiveStream(self.clock, epoch=self.home_epoch,
                        serving_nci=lambda: int(
                            self.adapter.snapshot()["servingCell"]),
                        epoch_of=lambda nci: (self.TARGET_EPOCH
                                              if nci == TARGET_NCI
                                              else self.home_epoch))
            if lines is None else _Stream(list(lines)))
        return build_live_session(
            profile if profile is not None
            else self.write_profile(occupants=occupants),
            objective=objective,
            read_new_lines=self.stream,
            policy_port=self.port,
            adapter_override=self.adapter,
            ports=self.clock,
            stamp="20260825T000000Z",
            **kwargs)


# --------------------------------------------------------------------------- #
# one authority
# --------------------------------------------------------------------------- #


class OneProfileResolvesEveryLiveInput(LiveConsoleFixture):
    """Sol F-5: not four absolute CLI defaults, one named document."""

    def test_every_live_input_comes_out_of_the_one_document(self) -> None:
        deployment = load_live_deployment(self.write_profile())
        self.assertEqual(self.binding_path.resolve(),
                         deployment.binding_path.resolve())
        self.assertEqual(self.values_path.resolve(),
                         deployment.integration_values_path.resolve())
        self.assertEqual(self.capability_path.resolve(),
                         deployment.capability_path.resolve())
        self.assertEqual((self.tmp / "a1p-producer.sqlite3").resolve(),
                         deployment.producer_database.resolve())
        self.assertTrue(deployment.values)
        self.assertTrue(deployment.capability["topology"]["cells"])

    def test_the_kpm_stream_is_named_by_the_binding_not_the_profile(self) -> None:
        deployment = load_live_deployment(self.write_profile())
        self.assertEqual(Path(deployment.binding.kpm_jsonl_path),
                         deployment.kpm_jsonl_path)
        self.assertNotIn("kpmJsonlPath", self.committed[LIVE_CONSOLE_KEY])

    def test_relative_paths_resolve_against_the_document(self) -> None:
        (self.tmp / "nested").mkdir()
        binding = self.tmp / "nested" / "binding.json"
        binding.write_bytes(self.binding_path.read_bytes())
        deployment = load_live_deployment(self.write_profile(
            live_console={"assuranceBindingPath": "nested/binding.json",
                          "evidenceDir": "../elsewhere/evidence"}))
        self.assertEqual(binding.resolve(), deployment.binding_path.resolve())
        self.assertEqual((self.tmp.parent / "elsewhere" / "evidence").resolve(),
                         deployment.evidence_dir.resolve())

    def test_state_and_evidence_directories_default_from_the_runs_root(self) -> None:
        path = self.write_profile(live_console={"r1StateDir": None,
                                                "evidenceDir": None})
        deployment = load_live_deployment(path)
        self.assertEqual((self.tmp / "runs" / "liveconsole" / "r1-state").resolve(),
                         deployment.r1_state_dir.resolve())
        self.assertEqual((self.tmp / "runs" / "liveconsole" / "evidence").resolve(),
                         deployment.evidence_dir.resolve())

    def test_the_summary_carries_no_secret(self) -> None:
        text = json.dumps(load_live_deployment(self.write_profile()).summary())
        for word in ("secret", "privateKey", "BEGIN ", "token"):
            self.assertNotIn(word.lower(), text.lower())


class TheCommittedProfileIsAWellFormedAuthority(unittest.TestCase):
    """The operational document, checked as a document.

    Deliberately *not* resolved against the laboratory: whether
    ``oran-deploy``'s current digests match the committed binding is a fact
    about the lab's re-pin cycle, and asserting it here would report a
    dispatcher's routine re-pin as a defect in this repository.  What the
    repository can hold itself to is that its own file names everything the
    composition root requires.
    """

    def setUp(self) -> None:
        self.document = json.loads(
            COMMITTED_PROFILE.read_text(encoding="utf-8"))

    def test_it_names_every_input_that_has_no_honest_default(self) -> None:
        for key in REQUIRED_LIVE_KEYS:
            self.assertTrue(self.document[LIVE_CONSOLE_KEY].get(key), key)
        for key in ("integrationValuesPath", "capabilityManifestPath",
                    "runsRoot", "profileId"):
            self.assertTrue(self.document.get(key), key)

    def test_it_loads_as_the_gui_profile_it_also_is(self) -> None:
        from gui.operator.session.profile import ExperimentProfile

        profile = ExperimentProfile.load(COMMITTED_PROFILE)
        self.assertEqual(self.document["profileId"], profile.profile_id)

    def test_its_relative_paths_point_at_files_this_repository_holds(self) -> None:
        for value in (self.document[LIVE_CONSOLE_KEY]["assuranceBindingPath"],):
            candidate = Path(value)
            if not candidate.is_absolute():
                self.assertTrue((COMMITTED_PROFILE.parent / candidate).is_file(),
                                value)


class AMovedDigestIsRefusedByName(LiveConsoleFixture):
    """Refuse rather than run: identity that moved is a different deployment."""

    def test_a_moved_capability_manifest_names_itself(self) -> None:
        # Move the *values* document alongside it too, so both names still
        # resolve to the same file and the digest is the only thing left to
        # refuse on.
        moved = self.tmp / "moved" / "capability.json"
        moved.parent.mkdir()
        moved.write_text(self.capability_path.read_text(encoding="utf-8") + "\n",
                         encoding="utf-8")
        values = moved.parent / "integration-values.json"
        values.write_bytes(self.values_path.read_bytes())
        path = self.write_profile(capabilityManifestPath=str(moved),
                                  integrationValuesPath=str(values))
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(path)
        message = str(caught.exception)
        self.assertIn(str(moved), message)
        self.assertIn("digest moved", message)

    def test_a_moved_binding_source_names_the_source(self) -> None:
        document = json.loads(self.binding_path.read_text(encoding="utf-8"))
        moved_source = document["sources"][0]["path"]
        document["sources"][0]["sha256"] = "0" * 64
        copy = self.tmp / "stale-binding.json"
        copy.write_text(json.dumps(document), encoding="utf-8")
        path = self.write_profile(live_console={"assuranceBindingPath": str(copy)})
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(path)
        self.assertIn(moved_source, str(caught.exception))
        self.assertIn(str(copy), str(caught.exception))

    def test_two_names_for_the_capability_manifest_are_refused(self) -> None:
        elsewhere = self.tmp / "same-bytes-different-place.json"
        elsewhere.write_bytes(self.capability_path.read_bytes())
        path = self.write_profile(capabilityManifestPath=str(elsewhere))
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(path)
        self.assertIn("One deployment, one manifest", str(caught.exception))

    def test_the_probe_would_notice_an_unmoved_digest(self) -> None:
        """Otherwise the three refusals above would prove nothing."""
        self.assertTrue(load_live_deployment(self.write_profile()).capability)


class AProfileThatNamesNothingIsRefusedByKey(LiveConsoleFixture):

    def test_a_profile_with_no_live_console_block_is_refused(self) -> None:
        document = dict(self.committed)
        document.pop(LIVE_CONSOLE_KEY)
        path = self.tmp / "replay-profile.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(path)
        self.assertIn(LIVE_CONSOLE_KEY, str(caught.exception))

    def test_each_required_key_is_refused_by_its_own_name(self) -> None:
        for key in REQUIRED_LIVE_KEYS:
            with self.subTest(key=key):
                path = self.write_profile(live_console={key: None})
                with self.assertRaises(LiveConsoleError) as caught:
                    load_live_deployment(path)
                self.assertIn(key, str(caught.exception))

    def test_a_profile_with_no_integration_values_cannot_back_live(self) -> None:
        path = self.write_profile(integrationValuesPath=None)
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(path)
        self.assertIn("integrationValuesPath", str(caught.exception))


# --------------------------------------------------------------------------- #
# the mode is derived, never declared
# --------------------------------------------------------------------------- #


class TheSessionIsLiveOnlyWithTheRealAdapter(LiveConsoleFixture):

    def test_the_mode_rule_itself(self) -> None:
        self.assertEqual(MODE_LIVE, session_mode(()))
        for seam in ("read_new_lines", "policy_port", "adapter_override",
                     "ports", "scope_clearer"):
            with self.subTest(seam=seam):
                self.assertEqual(MODE_MOCK, session_mode((seam,)))

    def test_an_injected_composition_is_mock_and_names_what_was_injected(self) -> None:
        live = self.compose()
        self.assertEqual(MODE_MOCK, live.session.mode)
        self.assertFalse(live.is_live)
        self.assertEqual(
            ("read_new_lines", "policy_port", "adapter_override", "ports"),
            live.injected)
        self.assertEqual(live.session.mode, session_mode(live.injected))

    def test_the_view_the_cockpit_reads_is_never_live(self) -> None:
        live = self.compose()
        view = live.session.draft(live.utterance) and live.session._view()  # noqa: SLF001
        self.assertFalse(view.is_live)

    def test_the_preflight_records_the_mode_and_the_seams(self) -> None:
        live = self.compose()
        self.assertEqual(MODE_MOCK, live.preflight["sessionMode"])
        self.assertEqual(list(live.injected), live.preflight["injectedPorts"])

    def test_the_gate3_runtime_refuses_an_adapter_override_it_would_ignore(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(objective=None)
        self.assertIn("adapter_override", str(caught.exception))


# --------------------------------------------------------------------------- #
# what was composed
# --------------------------------------------------------------------------- #


class TheCompositionIsTheLiveOne(LiveConsoleFixture):

    def test_the_ue_is_observed_from_the_stream_not_configured(self) -> None:
        live = self.compose()
        self.assertEqual(AMF_UE_NGAP_ID, live.identity.amf_ue_ngap_id)
        self.assertEqual(HOME_NCI, live.identity.serving_nci)
        self.assertEqual(HOME_E2_NODE, live.identity.e2_node)

    def test_the_target_is_the_one_advertised_cell_the_ue_is_not_on(self) -> None:
        self.assertEqual(TARGET_NCI, self.compose().target_nci)

    def test_a_cell_the_deployment_does_not_advertise_is_refused(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(target_nci=999)
        self.assertIn("999", str(caught.exception))

    def test_an_unobservable_ue_is_refused_rather_than_addressed_from_memory(
            self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(lines=[self.kpm_line(epoch=self.home_epoch + 1000)])
        self.assertIn("no UE is observable", str(caught.exception))

    def test_the_grammar_is_the_one_family_that_was_named(self) -> None:
        live = self.compose(objective="UELevelTarget")
        self.assertEqual(["UELevelTarget"], list(live.grammar))

    def test_the_operator_sentence_reads_as_the_frozen_contract(self) -> None:
        live = self.compose()
        self.assertIn(str(TARGET_NCI), live.utterance)
        self.assertIn(str(AMF_UE_NGAP_ID), live.utterance)

    def test_a_typed_sentence_that_reads_differently_is_refused(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(utterance=f"Hold the UE-level serving cell at "
                                   f"{HOME_NCI} nci for ueId={AMF_UE_NGAP_ID}")
        self.assertIn("does not read as the contract", str(caught.exception))

    def test_a_typed_sentence_that_reads_identically_is_kept_verbatim(self) -> None:
        live = self.compose()
        typed = live.utterance.replace("Hold the", "hold the")
        again = self.compose(utterance=typed)
        self.assertEqual(typed, again.utterance)

    def test_the_gate3_regression_case_is_the_default_objective(self) -> None:
        live = build_live_session(
            self.write_profile(), objective=None,
            read_new_lines=_Stream([self.kpm_line()]),
            policy_port=_PolicyPort("AIC_UECellSteering_1.0.0"),
            ports=self.clock, stamp="20260825T000000Z")
        self.assertEqual(GATE3_REGRESSION_CASE, live.objective)
        self.assertEqual(list(PIN_TO_CELL_GRAMMAR), list(live.grammar))
        self.assertEqual(MODE_MOCK, live.session.mode)

    def test_the_policy_type_was_discovered_not_assumed(self) -> None:
        live = self.compose()
        self.assertIn("AIC_UECellSteering_1.0.0", live.preflight["policyTypeIds"])
        self.assertIn("get_policy_type:AIC_UECellSteering_1.0.0", self.port.calls)


class LiveCapableIsNarrowerThanSubmittable(LiveConsoleFixture):
    """Sol F-7: the console must not offer an objective it cannot start."""

    def test_every_live_capable_family_is_also_submittable(self) -> None:
        for family in live_capable_families():
            with self.subTest(family=family):
                self.assertTrue(
                    record_for(family).deployment_capability.submittable)

    def test_a_submittable_family_with_no_operator_sentence_is_excluded(self) -> None:
        from assurance.objectives import FAMILY_MODULES
        from tools.g5ota.objective_live import FAMILY_UTTERANCES

        excluded = []
        for family in FAMILY_MODULES:
            try:
                submittable = record_for(family).deployment_capability.submittable
            except RegistryError:
                continue
            if submittable and family not in FAMILY_UTTERANCES:
                excluded.append(family)
                self.assertNotIn(family, live_capable_families())
        self.assertTrue(
            excluded,
            "this deployment has no submittable-but-not-live-capable family, so "
            "the distinction F-7 draws would be untested here")

    def test_a_family_outside_the_live_set_is_refused_by_name(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            build_live_session(self.write_profile(), objective="SliceSLATarget")
        self.assertIn("SliceSLATarget", str(caught.exception))


# --------------------------------------------------------------------------- #
# which UE
# --------------------------------------------------------------------------- #


class TheUeIsNamedOrUnambiguousNeverTheNewest(LiveConsoleFixture):
    """Sol finding 1: a case must not pick its UE by recency.

    The whole failure mode is silent.  This root generates the operator's
    sentence *from* the identity it observed, so an arbitrary pick is
    internally consistent all the way to the policy body: the evidence would
    name the UE that was actuated, and nothing would record that it was not
    the UE anybody meant.
    """

    def two_ue_stream(self) -> Sequence[str]:
        return [self.kpm_line(ues=(AMF_UE_NGAP_ID, SECOND_AMF_UE_NGAP_ID))]

    def test_two_fresh_ues_are_refused_not_resolved_by_recency(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(lines=self.two_ue_stream())
        message = str(caught.exception)
        self.assertIn("more than one UE is observable", message)
        for identifier in (AMF_UE_NGAP_ID, SECOND_AMF_UE_NGAP_ID):
            self.assertIn(str(identifier), message)
        self.assertIn("--amf-ue-ngap-id", message)

    def test_the_named_ue_is_the_one_addressed(self) -> None:
        for identifier in (AMF_UE_NGAP_ID, SECOND_AMF_UE_NGAP_ID):
            with self.subTest(amfUeNgapId=identifier):
                live = self.compose(lines=self.two_ue_stream(),
                                    amf_ue_ngap_id=identifier)
                self.assertEqual(identifier, live.identity.amf_ue_ngap_id)
                self.assertEqual(identifier, live.preflight["requestedUe"])
                self.assertIn(f"ueId={identifier}", live.utterance)

    def test_the_profile_may_name_the_ue(self) -> None:
        live = self.compose(
            lines=self.two_ue_stream(),
            profile=self.write_profile(
                live_console={"amfUeNgapId": SECOND_AMF_UE_NGAP_ID}))
        self.assertEqual(SECOND_AMF_UE_NGAP_ID, live.identity.amf_ue_ngap_id)

    def test_the_typed_sentence_s_scope_names_the_ue(self) -> None:
        """The UE observed is the UE the sentence scopes to, by construction."""
        typed = (f"Hold the UE-level serving cell at {TARGET_NCI} nci "
                 f"for ueId={SECOND_AMF_UE_NGAP_ID}")
        live = self.compose(lines=self.two_ue_stream(), utterance=typed)
        self.assertEqual(SECOND_AMF_UE_NGAP_ID, live.identity.amf_ue_ngap_id)
        self.assertEqual(typed, live.utterance)

    def test_two_sources_naming_two_ues_are_refused(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(
                lines=self.two_ue_stream(),
                amf_ue_ngap_id=AMF_UE_NGAP_ID,
                utterance=(f"Hold the UE-level serving cell at {TARGET_NCI} nci "
                           f"for ueId={SECOND_AMF_UE_NGAP_ID}"))
        self.assertIn("more than one UE", str(caught.exception))

    def test_a_named_ue_that_is_not_on_the_stream_is_refused_by_name(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(amf_ue_ngap_id=SECOND_AMF_UE_NGAP_ID)
        message = str(caught.exception)
        self.assertIn(f"amfUeNgapId {SECOND_AMF_UE_NGAP_ID} is not observable",
                      message)
        self.assertIn(str(AMF_UE_NGAP_ID), message)

    def test_one_fresh_ue_needs_no_selector(self) -> None:
        live = self.compose()
        self.assertEqual(AMF_UE_NGAP_ID, live.identity.amf_ue_ngap_id)
        self.assertIsNone(live.preflight["requestedUe"])

    def test_the_selector_rule_itself(self) -> None:
        self.assertIsNone(requested_amf_ue_ngap_id())
        self.assertEqual(7, requested_amf_ue_ngap_id(explicit=7))
        self.assertEqual(7, requested_amf_ue_ngap_id(profile_value="7"))
        self.assertEqual(7, requested_amf_ue_ngap_id(utterance="pin ueId=7"))
        # Agreement across sources is fine; disagreement is not.
        self.assertEqual(7, requested_amf_ue_ngap_id(
            explicit=7, profile_value=7, utterance="pin ueId=7"))
        with self.assertRaises(LiveConsoleError):
            requested_amf_ue_ngap_id(explicit=7, utterance="pin ueId=8")
        with self.assertRaises(LiveConsoleError):
            requested_amf_ue_ngap_id(utterance="pin ueId=ue-1")


class TheRefusalSaysWhatTheStreamActuallyShowed(LiveConsoleFixture):
    """"No UE attached" and "the stream is stale" need different actions.

    The dispatcher's first live headless run refused with the old one-line
    message and could not tell the two apart from the text.
    """

    def refusal(self, lines: Sequence[str]) -> str:
        with self.assertRaises(LiveConsoleError) as caught:
            self.compose(lines=lines)
        return str(caught.exception)

    def test_it_states_the_freshness_window(self) -> None:
        self.assertIn("freshness window 4000 ms", self.refusal([]))

    def test_a_silent_stream_is_told_apart_from_a_stale_one(self) -> None:
        silent = self.refusal([])
        self.assertIn("0 stream line(s) drained", silent)
        self.assertIn("0 UE-level record(s) parsed", silent)
        self.assertIn("0 record(s) discarded", silent)

        stale = self.refusal([self.kpm_line(epoch=self.home_epoch + 1000)])
        self.assertIn("1 stream line(s) drained", stale)
        self.assertIn("0 UE-level record(s) parsed", stale)
        self.assertIn("1 record(s) discarded", stale)

    def test_an_old_indication_is_a_gap_not_an_identity(self) -> None:
        """Parsed, in-epoch, but older than the window: still refused."""
        message = self.refusal([self.kpm_line(offset_ms=-60_000)])
        self.assertIn("1 UE-level record(s) parsed", message)
        self.assertIn("0 record(s) discarded", message)


# --------------------------------------------------------------------------- #
# the two identity documents describe one deployment
# --------------------------------------------------------------------------- #


class TheValuesAndTheBindingAreCrossBound(LiveConsoleFixture):
    """Sol finding 2: the client's endpoint and the recorded one are one thing.

    The R1 client is built from the integration values and the frozen
    ``DeploymentBinding`` the Kernel records comes from the binding.  Both are
    individually valid documents, so without a cross-bind a second well-formed
    values file could send every policy to a different Near-RT RIC while the
    epoch, the evidence and the terminal state hash recorded the first.
    """

    def test_values_the_binding_was_not_cut_from_are_refused(self) -> None:
        # Beside the original, so both documents still name one capability
        # manifest and the cross-bind is the only thing left to refuse on.
        elsewhere = self.tmp / "second-integration-values.json"
        elsewhere.write_bytes(self.values_path.read_bytes())
        path = self.write_profile(integrationValuesPath=str(elsewhere))
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(path)
        message = str(caught.exception)
        self.assertIn("was not cut from", message)
        self.assertIn(str(elsewhere), message)

    def test_values_edited_after_the_binding_was_cut_are_refused(self) -> None:
        """The binding's own identity check catches this one, and names it."""
        self.values_path.write_text(
            self.values_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(self.write_profile())
        message = str(caught.exception)
        self.assertIn("identity source digest mismatch", message)
        self.assertIn(str(self.values_path), message)

    def test_a_second_r1_endpoint_is_refused_by_name(self) -> None:
        self.values_path = self.write_values(
            **{"r1.apiRoot": "https://other-ric.deployment.test:18443/r1"})
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(self.write_profile(
                live_console={"assuranceBindingPath": str(self.write_binding(
                    name="rebound.json",
                    sources=[{"path": str(self.values_path),
                              "sha256": hashlib.sha256(
                                  self.values_path.read_bytes()).hexdigest()}]))}))
        message = str(caught.exception)
        self.assertIn("r1.apiRoot", message)
        self.assertIn("other-ric.deployment.test", message)

    def test_a_second_a1_endpoint_is_refused_by_name(self) -> None:
        self.values_path = self.write_values(
            **{"a1.apiRoot": "https://other-a1p.deployment.test:9444"})
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(self.write_profile(
                live_console={"assuranceBindingPath": str(self.write_binding(
                    name="rebound.json",
                    sources=[{"path": str(self.values_path),
                              "sha256": hashlib.sha256(
                                  self.values_path.read_bytes()).hexdigest()}]))}))
        self.assertIn("a1p.apiRoot", str(caught.exception))

    def test_a_capability_naming_other_cells_is_refused(self) -> None:
        self.capability_path = self.write_capability(topology={"cells": [
            {"cellId": {"cId": {"ncI": 11111111}},
             "globalE2NodeId": {"nodeId": {"hex": "0x00000e00"}}},
            {"cellId": {"cId": {"ncI": 22222222}},
             "globalE2NodeId": {"nodeId": {"hex": "0x00000b00"}}}]})
        self.values_path = self.write_values()
        self.binding_path = self.write_binding()
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(self.write_profile())
        message = str(caught.exception)
        self.assertIn("11111111", message)
        self.assertIn(str(HOME_NCI), message)

    def test_a_capability_naming_other_e2_nodes_is_refused(self) -> None:
        self.capability_path = self.write_capability(topology={"cells": [
            {"cellId": {"cId": {"ncI": HOME_NCI}},
             "globalE2NodeId": {"nodeId": {"hex": "0x00000f00"}}},
            {"cellId": {"cId": {"ncI": TARGET_NCI}},
             "globalE2NodeId": {"nodeId": {"hex": "0x00000b00"}}}]})
        self.values_path = self.write_values()
        self.binding_path = self.write_binding()
        with self.assertRaises(LiveConsoleError) as caught:
            load_live_deployment(self.write_profile())
        self.assertIn("E2 nodes", str(caught.exception))

    def test_the_agreeing_pair_resolves(self) -> None:
        """Otherwise the six refusals above would prove nothing."""
        deployment = load_live_deployment(self.write_profile())
        self.assertEqual(self.R1_ROOT, deployment.binding.r1.api_root)


# --------------------------------------------------------------------------- #
# the A1 scope lifecycle
# --------------------------------------------------------------------------- #


class TheScopeLifecycleStaysHonest(LiveConsoleFixture):
    """One policy per scope, and a withdrawal is a move rather than a delete."""

    def test_a_verified_effect_is_kept_by_default(self) -> None:
        live = self.compose(
            occupants=(occupant("p-1", episode_state="APPLIED_VERIFIED"),))
        self.assertEqual(
            [{"policyId": "p-1", "policyTypeId": POLICY_TYPE_ID,
              "episodeState": "APPLIED_VERIFIED",
              "episodeTerminal": True, "enforceStatus": "ENFORCED",
              "readback": "VERIFIED", "decision": "KEEP",
              "reason": "verified effect", "action": "KEPT"}],
            [dict(entry) for entry in live.scope_cleared])

    def test_the_occupants_are_archived_verbatim_before_anything_is_withdrawn(
            self) -> None:
        live = self.compose(
            occupants=(occupant("p-1", episode_state="APPLIED_VERIFIED"),))
        archive = Path(live.preflight["scopeArchive"])
        document = json.loads(archive.read_text(encoding="utf-8"))
        self.assertEqual("p-1", document["records"][0]["policyId"])
        self.assertEqual(AMF_UE_NGAP_ID, document["amfUeNgapId"])

    def test_opening_a_case_never_withdraws_a_verified_effect(self) -> None:
        """Opening a case clears stuck attempts and nothing else.

        A verified policy is a decision, not a leftover, so it is left for the
        preview to name (see :class:`AVerifiedOccupantIsANamedPrecondition`)
        rather than withdrawn as a side effect of composing.
        """
        clearer = self.record_clearer()
        self.compose(scope_clearer=clearer)
        self.assertIs(False, clearer.seen["withdraw_verified"])
        self.assertEqual(AMF_UE_NGAP_ID, clearer.seen["amf_ue_ngap_id"])
        self.assertEqual(POLICY_TYPE_ID, clearer.seen["policy_type_id"])

    def test_the_scope_occupants_are_read_before_anything_is_cleared(self) -> None:
        live = self.compose(
            occupants=(occupant("p-9", episode_state="APPLY_PENDING"),),
            scope_clearer=self.record_clearer())
        self.assertEqual([{"policyId": "p-9", "policyTypeId": POLICY_TYPE_ID,
                           "episodeState": "APPLY_PENDING",
                           "episodeTerminal": True, "enforceStatus": "ENFORCED",
                           "readback": "VERIFIED"}],
                         live.preflight["scopeOccupants"])


class AWithdrawalIsOnlyReportedWhenItHappened(unittest.TestCase):
    """Sol finding 3, on :func:`clear_ue_scope` itself.

    The old body recorded every DELETE as ``WITHDRAWN`` whatever came back, so
    a 401 from an expired credential or a 500 from the producer read as "scope
    cleared" — and the run then walked into the HTTP 409 the clearing existed
    to prevent, with its own evidence saying the scope was free.

    No socket: the connection class is replaced, so what is under test is the
    status handling and the vacancy re-read, not TLS.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="scope-clear-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def binding(self) -> Any:
        secret = f"file://{self.tmp}/secret"
        return SimpleNamespace(
            a1p=SimpleNamespace(base_url="https://a1p.test:9444/A1-P/v2",
                                secret_refs={"mtlsCa": secret,
                                             "mtlsClientCertificate": secret,
                                             "mtlsClientPrivateKey": secret,
                                             "oauthToken": secret}))

    def clear(self, rows: Sequence[Mapping[str, Any]], *, status: int = 204,
              survives: Sequence[str] = (), body: bytes = b"{}",
              **kwargs: Any) -> Any:
        from unittest import mock

        import tools.g3ota.composition as composition

        database = producer_database(self.tmp / "producer.sqlite3", rows)

        def delete(*_args: Any, **_kwargs: Any) -> Any:
            """One DELETE: answer with *status*, then drop the row unless it survives."""
            def close() -> None:
                remaining = [row for row in rows
                             if row["policy_id"] in set(survives)]
                producer_database(database, remaining)
            return SimpleNamespace(
                request=lambda *a, **k: None,
                getresponse=lambda: SimpleNamespace(
                    status=status, read=lambda: body),
                close=close)

        with mock.patch.object(composition.http.client, "HTTPSConnection", delete), \
                mock.patch.object(composition, "_a1p_context",
                                  lambda secrets: None), \
                mock.patch.object(composition, "_a1p_authorization",
                                  lambda secrets, **k: "Bearer test"):
            return composition.clear_ue_scope(
                self.binding(), state_database=database,
                amf_ue_ngap_id=AMF_UE_NGAP_ID, policy_type_id=POLICY_TYPE_ID,
                sleep=lambda _seconds: None, verify_attempts=2, **kwargs)

    def test_an_accepted_delete_is_reported_withdrawn(self) -> None:
        actions = self.clear([occupant("p-1", episode_state="APPLY_PENDING")])
        self.assertEqual(["WITHDRAWN"], [entry["action"] for entry in actions])

    def test_a_refused_delete_raises_instead_of_reporting_success(self) -> None:
        from assurance.live.pin_to_cell_driver import LiveDriverError

        for status in (401, 403, 409, 500):
            with self.subTest(status=status):
                with self.assertRaises(LiveDriverError) as caught:
                    self.clear([occupant("p-1", episode_state="APPLY_PENDING")],
                               status=status)
                self.assertIn(str(status), str(caught.exception))
                self.assertIn("p-1", str(caught.exception))

    def test_a_policy_that_survives_the_delete_is_refused(self) -> None:
        from assurance.live.pin_to_cell_driver import LiveDriverError

        with self.assertRaises(LiveDriverError) as caught:
            self.clear([occupant("p-1", episode_state="APPLY_PENDING")],
                       survives=("p-1",))
        message = str(caught.exception)
        self.assertIn("still records p-1", message)
        self.assertIn("409", message)

    def test_a_404_is_accepted_only_when_the_scope_is_really_vacant(self) -> None:
        from assurance.live.pin_to_cell_driver import LiveDriverError

        actions = self.clear([occupant("p-1", episode_state="APPLY_PENDING")],
                             status=404)
        self.assertEqual(["NOT_FOUND"], [entry["action"] for entry in actions])
        with self.assertRaises(LiveDriverError):
            self.clear([occupant("p-1", episode_state="APPLY_PENDING")],
                       status=404, survives=("p-1",))

    def test_a_verified_effect_is_kept_and_never_deleted(self) -> None:
        actions = self.clear([occupant("p-1", episode_state="APPLIED_VERIFIED")],
                             archive=lambda records: None)
        self.assertEqual(["KEPT"], [entry["action"] for entry in actions])

    def test_a_foreign_policy_type_is_refused_not_deleted(self) -> None:
        from assurance.live.pin_to_cell_driver import LiveDriverError

        with self.assertRaises(LiveDriverError) as caught:
            self.clear([occupant("p-x", episode_state="APPLY_PENDING",
                                 policy_type_id="SomeOtherType_1.0.0")])
        message = str(caught.exception)
        self.assertIn("another type", message)
        self.assertIn("SomeOtherType_1.0.0", message)
        self.assertIn(POLICY_TYPE_ID, message)


class TheWithdrawalPlanIsPureAndReadable(unittest.TestCase):
    """The rule on its own, with no transport and no database."""

    def plan(self, occupants: Sequence[Mapping[str, Any]], **kwargs: Any) -> Any:
        from tools.g3ota.composition import scope_withdrawal_plan

        kwargs.setdefault("withdraw_verified", False)
        kwargs.setdefault("policy_type_id", POLICY_TYPE_ID)
        return scope_withdrawal_plan(occupants, **kwargs)

    def test_a_foreign_type_outranks_every_other_decision(self) -> None:
        for state in ("APPLIED_VERIFIED", "APPLY_PENDING"):
            for withdraw_verified in (False, True):
                with self.subTest(state=state, withdraw=withdraw_verified):
                    plan = self.plan(
                        [{"policyId": "p", "policyTypeId": "Other_1.0.0",
                          "episodeState": state}],
                        withdraw_verified=withdraw_verified)
                    self.assertEqual("FOREIGN", plan[0]["decision"])

    def test_the_default_still_keeps_a_verified_effect(self) -> None:
        plan = self.plan([{"policyId": "p", "policyTypeId": POLICY_TYPE_ID,
                           "episodeState": "APPLIED_VERIFIED"}])
        self.assertEqual("KEEP", plan[0]["decision"])

    def test_withdraw_verified_moves_it_only_when_asked(self) -> None:
        plan = self.plan([{"policyId": "p", "policyTypeId": POLICY_TYPE_ID,
                           "episodeState": "APPLIED_VERIFIED"}],
                         withdraw_verified=True)
        self.assertEqual("WITHDRAW", plan[0]["decision"])
        self.assertIn("archived", plan[0]["reason"])

    def test_a_row_with_no_recorded_type_is_judged_as_before(self) -> None:
        """Older producer rows predate the column; they are not foreign."""
        plan = self.plan([{"policyId": "p", "episodeState": "APPLY_PENDING"}])
        self.assertEqual("WITHDRAW", plan[0]["decision"])


class AForeignScopeOccupantStopsTheComposition(LiveConsoleFixture):
    """The same refusal, reached the way an operator reaches it."""

    def test_the_session_is_not_composed(self) -> None:
        with self.assertRaises(Exception) as caught:
            self.compose(occupants=(occupant("p-x", episode_state="APPLY_PENDING",
                                             policy_type_id="Other_1.0.0"),))
        self.assertIn("another type", str(caught.exception))


# --------------------------------------------------------------------------- #
# one sitting, many intents
# --------------------------------------------------------------------------- #


class SittingFixture(LiveConsoleFixture):
    """A sitting whose every case can be driven to a terminal deterministically."""

    def sitting(self, **kwargs: Any) -> Any:
        live = self.compose(**kwargs)
        self._script(live)
        return live

    def sentence(self, *, family: str = "UELevelTarget",
                 ue: int = AMF_UE_NGAP_ID) -> str:
        """What an operator would type next: put the UE on the other cell.

        Written from where the UE actually is, because that is what the next
        case's frozen bundle steers *from*.  Re-typing the previous sentence
        after a successful case names the cell the UE is already on, and
        :func:`operator_utterance` refuses it -- correctly, and the test below
        proves that too.
        """
        from tools.g5ota.objective_live import family_utterance

        here = int(self.adapter.snapshot()["servingCell"])
        return family_utterance(
            family, TARGET_NCI if here == HOME_NCI else HOME_NCI, str(ue))

    def _script(self, live: Any, observed: Optional[Sequence[int]] = None) -> None:
        """Point the current case's collector at a scripted serving-cell series.

        The default is *this case's own* target, not a fixed cell: after a
        successful case the UE is on the other cell, so the next case's frozen
        bundle steers it back and observing the first case's target would be
        observing the baseline.
        """
        series = _Series(
            observed if observed is not None else [live.target_nci] * 24)
        collector = live.runtime.collector
        # A single-counter family has one collector; a multi-counter one wraps
        # the serving-cell collector as its first member.  The scripted reader
        # belongs to the serving-cell collector in both shapes.
        target = (collector.collectors[0]
                  if hasattr(collector, "collectors") else collector)
        target._reader = series  # noqa: SLF001

    def run_case(self, live: Any, sentence: str,
                 observed: Optional[Sequence[int]] = None) -> Any:
        """Draft, confirm and start one sentence in *live*, scripting its case.

        Not ``run``: :class:`unittest.TestCase` owns that name.
        """
        preview = live.session.draft(sentence)
        self._script(live, observed)
        instance = live.session.confirm(preview)
        return live.session.start(instance)


class OneSittingRunsSeveralIntents(SittingFixture):
    """WP-L item 1: the console drafts the next contract in the same session.

    Each case gets its own epoch and its own fresh observation, so neither
    ``OBJECTIVE_NOT_IN_EPOCH`` nor ``TRIAL_ALREADY_RUN`` -- both correct for a
    case, both wrong for a sitting -- can fire across cases.
    """

    def test_two_consecutive_cases_both_settle(self) -> None:
        """Steer, then steer back -- two intents, one sitting, one process."""
        live = self.sitting()
        first = self.run_case(live, self.sentence())
        self.assertEqual(TrialState.SETTLED_SUCCESS.value,
                         first.settlement.trial_state)
        self.assertEqual({"servingCell": str(TARGET_NCI)},
                         self.adapter.snapshot())
        second = self.run_case(live, self.sentence())
        self.assertEqual(TrialState.SETTLED_SUCCESS.value,
                         second.settlement.trial_state)
        self.assertEqual({"servingCell": str(HOME_NCI)}, self.adapter.snapshot())
        self.assertEqual(2, len(live.cases))

    def test_re_typing_the_previous_sentence_is_refused_after_it_ran(self) -> None:
        """The UE has moved, so the old sentence names the cell it is on."""
        live = self.sitting()
        typed = self.sentence()
        self.run_case(live, typed)
        with self.assertRaises(LiveConsoleError) as caught:
            live.session.draft(typed)
        self.assertIn("does not read as the contract", str(caught.exception))

    def test_three_consecutive_cases_each_get_their_own_epoch(self) -> None:
        live = self.sitting()
        for _ in range(3):
            self.run_case(live, self.sentence())
        self.assertEqual(3, len(live.cases))
        case_ids = [case.case_id for case in live.cases]
        self.assertEqual(len(set(case_ids)), 3, case_ids)
        trials = [case.session.trial_id for case in live.cases]
        self.assertEqual(len(set(trials)), 3, trials)
        epochs = [case.runtime.path.epoch_hash() for case in live.cases]
        self.assertEqual(len(set(epochs)), 3, epochs)

    def test_the_sentence_chooses_the_family_per_case(self) -> None:
        """A steering sentence after a UE-level one opens a steering case."""
        live = self.sitting()
        live.session.draft(self.sentence())
        self.assertEqual("UELevelTarget", live.objective)
        live.session.draft(self.sentence(family="TrafficSteeringPreference"))
        self.assertEqual(["UELevelTarget", "TrafficSteeringPreference"],
                         [case.objective for case in live.cases])
        self.assertEqual(["TrafficSteeringPreference"], list(live.grammar))
        # A new epoch, so the sentence is drafted rather than refused
        # OBJECTIVE_NOT_IN_EPOCH by the first case's frozen catalog.
        self.assertNotEqual(live.cases[0].runtime.path.epoch_hash(),
                            live.cases[1].runtime.path.epoch_hash())

    def test_a_family_whose_frozen_direction_is_wrong_is_refused(self) -> None:
        """``TrafficSteeringPreference`` fixes its cell pair in module constants.

        So once the UE is on the target cell that family has nothing to say,
        and the refusal names both cells rather than submitting a contract that
        moves from a cell the UE is not on.
        """
        live = self.sitting()
        self.run_case(live, self.sentence())
        with self.assertRaises(LiveConsoleError) as caught:
            live.session.draft(self.sentence(family="TrafficSteeringPreference"))
        self.assertIn("as frozen moves", str(caught.exception))

    def test_a_second_case_re_observes_the_ue_never_from_memory(self) -> None:
        """The AMF re-issues the id, so the second case reads it again."""
        live = self.sitting()
        self.run_case(live, self.sentence())
        # The UE re-registered and the AMF issued a new id.  The next case must
        # address the one on the stream now, not the one the first remembered.
        self.stream.ues = (SECOND_AMF_UE_NGAP_ID,)
        live.session.draft(self.sentence(ue=SECOND_AMF_UE_NGAP_ID))
        self.assertEqual(SECOND_AMF_UE_NGAP_ID,
                         live.cases[-1].identity.amf_ue_ngap_id)

    def test_a_running_case_blocks_the_next_draft(self) -> None:
        live = self.sitting()
        preview = live.session.draft(self.sentence())
        live.session.confirm(preview)
        live.cases[-1].session._trial_id = "trial/pretend-running"  # noqa: SLF001
        with self.assertRaises(LiveConsoleError) as caught:
            live.session.draft(self.sentence())
        self.assertIn("still running", str(caught.exception))

    def test_an_unstarted_case_is_redrafted_not_replaced(self) -> None:
        """Re-reading the same sentence is design section 5's invalidation."""
        live = self.sitting()
        typed = self.sentence()
        live.session.draft(typed)
        first_case = live.case
        live.session.draft(typed)
        self.assertIs(first_case, live.case)
        self.assertEqual(1, len(live.cases))

    def test_a_sentence_no_live_family_recognises_is_refused(self) -> None:
        live = self.sitting()
        with self.assertRaises(LiveConsoleError) as caught:
            live.session.draft("please make the network faster")
        self.assertIn("no live-capable objective recognises",
                      str(caught.exception))

    def test_a_sentence_naming_two_families_is_refused_not_ordered(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            family_for_utterance("joint qos steering for ueId=131")
        message = str(caught.exception)
        self.assertIn("QoSandTSP", message)
        self.assertIn("TrafficSteeringPreference", message)

    def test_the_second_case_is_refused_when_the_ue_is_gone(self) -> None:
        live = self.sitting()
        self.run_case(live, self.sentence())
        self.stream.stop()              # the UE detached between cases
        with self.assertRaises(LiveConsoleError) as caught:
            live.session.draft(self.sentence())
        self.assertIn("no UE is observable", str(caught.exception))
        self.assertEqual(1, len(live.cases))


class AVerifiedOccupantIsANamedPrecondition(SittingFixture):
    """WP-L item 2: withdrawal is an operator step, not a flag or a side effect."""

    def test_the_preview_names_it_and_the_hash_covers_it(self) -> None:
        live = self.sitting()
        without = live.session.draft(self.sentence())
        self.assertEqual((), without.preconditions)
        producer_database(self.tmp / "a1p-producer.sqlite3",
                          [occupant("p-1", episode_state="APPLIED_VERIFIED")])
        with_occupant = live.session.draft(self.sentence())
        self.assertEqual(
            (f"withdraw policy p-1 (APPLIED_VERIFIED) on UE {AMF_UE_NGAP_ID}",),
            with_occupant.preconditions)
        self.assertNotEqual(without.content_hash(),
                            with_occupant.content_hash())

    def test_a_changed_occupant_voids_the_confirmation(self) -> None:
        live = self.sitting()
        producer_database(self.tmp / "a1p-producer.sqlite3",
                          [occupant("p-1", episode_state="APPLIED_VERIFIED")])
        preview = live.session.draft(self.sentence())
        instance = live.session.confirm(preview)
        # Someone else's policy takes the scope between confirm and start.
        producer_database(self.tmp / "a1p-producer.sqlite3",
                          [occupant("p-2", episode_state="APPLIED_VERIFIED")])
        with self.assertRaises(LiveConsoleError) as caught:
            live.session.start(instance)
        message = str(caught.exception)
        self.assertIn("changed after the contract was confirmed", message)
        self.assertIn("p-1", message)
        self.assertIn("p-2", message)

    def test_the_confirmed_precondition_is_withdrawn_at_start(self) -> None:
        clearer = self.record_clearer()
        live = self.sitting(scope_clearer=clearer)
        producer_database(self.tmp / "a1p-producer.sqlite3",
                          [occupant("p-1", episode_state="APPLIED_VERIFIED")])
        preview = live.session.draft(self.sentence())
        self.assertTrue(preview.preconditions)
        self._script(live)
        live.session.start(live.session.confirm(preview))
        # Opening the case did not withdraw it; starting the confirmed contract did.
        self.assertEqual([False, True], clearer.calls)
        met = live.preflight["preconditionsMet"]
        self.assertEqual([preview.preconditions[0]], met[0]["named"])

    def test_nothing_is_withdrawn_when_nothing_was_named(self) -> None:
        clearer = self.record_clearer()
        live = self.sitting(scope_clearer=clearer)
        self.run_case(live, self.sentence())
        self.assertEqual([False], clearer.calls)

    def test_the_confirmation_dialog_shows_it(self) -> None:
        """Hashing an act the operator was never shown is not covering it."""
        import main

        console = main.build_console()
        live = self.sitting()
        producer_database(self.tmp / "a1p-producer.sqlite3",
                          [occupant("p-1", episode_state="APPLIED_VERIFIED")])
        preview = live.session.draft(self.sentence())
        console.attach_kernel_session(live.session)
        shown: List[Any] = []
        console._confirmed = lambda spec: shown.append(spec) or False  # noqa: SLF001
        console._warn = lambda *a, **k: None                            # noqa: SLF001
        console._action_kernel_confirm("")                              # noqa: SLF001
        console._action_kernel_start("")                                # noqa: SLF001
        self.assertEqual(2, len(shown), shown)
        for spec in shown:
            self.assertIn(f"precondition: {preview.preconditions[0]}",
                          list(spec.effects), spec)

    def test_a_foreign_policy_type_is_never_a_precondition(self) -> None:
        from tools.liveconsole.build import scope_preconditions

        database = producer_database(
            self.tmp / "foreign.sqlite3",
            [occupant("p-x", episode_state="APPLIED_VERIFIED",
                      policy_type_id="Other_1.0.0")])
        self.assertEqual((), scope_preconditions(
            database, amf_ue_ngap_id=AMF_UE_NGAP_ID,
            policy_type_id=POLICY_TYPE_ID))


# --------------------------------------------------------------------------- #
# the round trip, over the real Kernel
# --------------------------------------------------------------------------- #


class TheRoundTripReachesATerminal(LiveConsoleFixture):
    """Draft -> confirm -> start, on the same session object the Cockpit drives."""

    def drive(self, observed: Sequence[int]) -> Any:
        live = self.compose()
        live.runtime.collector._reader = _Series(observed)  # noqa: SLF001
        preview = live.session.draft(live.utterance)
        instance = live.session.confirm(preview)
        self.live = live
        return live.session.start(instance)

    def test_a_held_target_settles_success_over_the_mock_adapter(self) -> None:
        view = self.drive([TARGET_NCI] * 24)
        self.assertEqual(TrialState.SETTLED_SUCCESS.value,
                         view.settlement.trial_state)
        self.assertEqual(TrialOutcome.SUCCESS.value, view.settlement.outcome)
        self.assertEqual({"servingCell": str(TARGET_NCI)}, self.adapter.snapshot())
        self.assertFalse(view.is_live)

    def test_a_target_that_does_not_hold_rolls_back_to_the_baseline(self) -> None:
        view = self.drive([TARGET_NCI, TARGET_NCI, HOME_NCI, TARGET_NCI] * 6)
        self.assertEqual(TrialState.SETTLED_NON_SUCCESS.value,
                         view.settlement.trial_state)
        self.assertEqual({"servingCell": str(HOME_NCI)}, self.adapter.snapshot())

    def test_the_run_evidence_holds_the_preflight_and_the_kernel_s_own_events(
            self) -> None:
        view = self.drive([TARGET_NCI] * 24)
        written = write_run_evidence(self.live, view)
        document = json.loads(Path(written["run"]).read_text(encoding="utf-8"))
        self.assertEqual(MODE_MOCK, document["sessionMode"])
        self.assertEqual(self.live.case_id, document["caseId"])
        self.assertEqual(AMF_UE_NGAP_ID,
                         document["preflight"]["observedUe"]["amfUeNgapId"])
        self.assertEqual(TrialOutcome.SUCCESS.value,
                         document["settlement"]["outcome"])
        self.assertEqual([], document["policyBodies"],
                         "a mock adapter builds no A1 policy body")
        events = Path(written["events"]).read_text(encoding="utf-8").splitlines()
        self.assertTrue(events)
        self.assertTrue(all(json.loads(line) for line in events))


class TheConsoleBadgesLiveOnlyOnTheKernelSDeclaration(unittest.TestCase):
    """The other half of the contract: what ``gui/operator/app.py`` does with it.

    ``attach_live_session`` attaches the session and selects LIVE, and the
    console is required to answer with the basis ``LIVE_KERNEL_WRITE_GATEWAY``
    — the write left through the Write Gateway, which only whoever wired the
    path can say.  A ``MOCK`` session must not be able to buy that badge.
    """

    def console(self) -> Any:
        import main

        return main.build_console()

    def kernel_session(self, mode: str) -> Any:
        from tests.gui.kernel_submission_support import (
            PIN_TO_CELL_GRAMMAR as GRAMMAR, KernelSubmissionFixture)

        fixture = KernelSubmissionFixture()
        session = fixture.session(mode=mode)
        self.assertEqual(list(GRAMMAR), list(session.objective_registry))
        return session

    def test_a_live_declaring_session_backs_the_write_gateway_basis(self) -> None:
        console = self.console()
        console.attach_kernel_session(self.kernel_session(MODE_LIVE))
        console.select_mode(MODE_LIVE)
        mode, evidence = console.session_mode_evidence()
        self.assertEqual(MODE_LIVE, mode)
        self.assertEqual("LIVE_KERNEL_WRITE_GATEWAY", evidence["basis"])
        self.assertEqual(MODE_LIVE, evidence["kernelSessionMode"])

    def test_a_mock_session_cannot_select_live_at_all(self) -> None:
        console = self.console()
        console.attach_kernel_session(self.kernel_session(MODE_MOCK))
        with self.assertRaises(Exception) as caught:
            console.select_mode(MODE_LIVE)
        self.assertIn("Kernel", str(caught.exception))

    def test_attach_live_session_attaches_then_selects_live(self) -> None:
        """What the root does to the console, on a console that only records."""
        from tools.liveconsole import attach_live_session
        import tools.liveconsole.build as build_module

        for mode, expected in ((MODE_LIVE, [MODE_LIVE]), (MODE_MOCK, [])):
            with self.subTest(mode=mode):
                recorder = _RecordingConsole()
                composed = _Composed(self.kernel_session(mode))
                original = build_module.build_live_session
                build_module.build_live_session = lambda *a, **k: composed
                try:
                    attach_live_session(recorder, "unused")
                finally:
                    build_module.build_live_session = original
                self.assertIs(composed.session, recorder.attached)
                self.assertEqual(expected, recorder.selected)


class _RecordingConsole:
    """A console that only records what a composition root did to it."""

    def __init__(self) -> None:
        self.attached: Any = None
        self.selected: List[str] = []

    def attach_kernel_session(self, session: Any) -> Any:
        self.attached = session
        return session

    def select_mode(self, mode: str) -> str:
        self.selected.append(mode)
        return mode


class _Composed:
    """The minimum of a ``LiveSession`` ``attach_live_session`` touches."""

    def __init__(self, session: Any) -> None:
        self.session = session


class TheHeadlessEntryPointPrintsTheLiveHalf(SittingFixture):
    """``main.py --live --no-gui``'s body, driven over the injected composition.

    The dispatcher runs this against the radio, where a typo costs a scarce
    OTA slot, so the function is exercised here instead — with
    ``build_live_session`` replaced by one that returns a session composed
    entirely from injected ports.  Everything after that call is the real
    thing: the same draft/confirm/start, the same printing, the same evidence.
    """

    def run_headless(self, *, sentences: int = 1, withdraw_verified: bool = False,
                     stop_after_first: bool = False,
                     occupants: Sequence[Mapping[str, Any]] = ()) -> Any:
        """Drive ``main.run_headless_live`` over an injected sitting.

        The dispatcher runs this against the radio, where a typo costs a scarce
        OTA slot, so the function is exercised here instead — with
        ``build_live_session`` replaced by one that returns a sitting composed
        entirely from injected ports.  Everything after that call is the real
        thing: the same draft/confirm/start per case, the same printing, the
        same evidence.
        """
        import io
        from contextlib import redirect_stdout

        import main
        import tools.liveconsole as liveconsole

        from tools.g5ota.objective_live import family_utterance

        composed = self.compose(occupants=occupants,
                                scope_clearer=self.record_clearer())
        self._script(composed)
        # The operator's plan, written before anything runs: put the UE on the
        # other cell, then put it back.  The UE starts on home, so the cells
        # alternate from the target.
        alternating = (TARGET_NCI, HOME_NCI)
        typed = [family_utterance("UELevelTarget", alternating[index % 2],
                                  str(AMF_UE_NGAP_ID))
                 for index in range(sentences)]
        if stop_after_first:
            typed[-1] = "please make the network faster"

        original = liveconsole.build_live_session
        liveconsole.build_live_session = lambda *a, **k: composed
        self.addCleanup(setattr, liveconsole, "build_live_session", original)

        # The sitting drives its own cases, so the collector has to be scripted
        # as each one opens.  Wrapping ``draft`` is how a test stands in for
        # the radio without the runner knowing.
        real_draft = composed.session.draft

        def draft(text: str) -> Any:
            preview = real_draft(text)
            self._script(composed)
            return preview

        composed.session.draft = draft   # type: ignore[assignment]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main.run_headless_live(
                profile_path="unused", objective="UELevelTarget",
                utterances=typed, withdraw_verified=withdraw_verified)
        self.composed = composed
        return code, buffer.getvalue()

    def test_it_prints_the_hardware_free_fields_and_the_a1_half(self) -> None:
        code, output = self.run_headless()
        self.assertEqual(0, code, output)
        for field in ("objective", "mode", "case", "trial", "stage",
                      "execution validity", "measurement suff.",
                      "predicate verdict", "trial state", "trial outcome",
                      "observed UE", "a1 scope cleared", "a1 policy ids",
                      "a1 transport", "wrote run", "wrote events"):
            with self.subTest(field=field):
                self.assertIn(field, output)

    def test_it_never_prints_a_live_badge_for_an_injected_composition(self) -> None:
        _code, output = self.run_headless()
        self.assertIn("mode               : MOCK (is_live=False)", output)

    def test_several_sentences_run_as_several_cases_in_one_sitting(self) -> None:
        """WP-L item 3: ``--cmd`` repeated is one sitting, one case each."""
        code, output = self.run_headless(sentences=2)
        self.assertEqual(0, code, output)
        self.assertIn("--- case 1 of 2 ---", output)
        self.assertIn("--- case 2 of 2 ---", output)
        self.assertIn("--- sitting summary ---", output)
        self.assertIn("settled success    : 2 of 2", output)
        self.assertEqual(2, len(self.composed.cases))

    def test_the_exit_code_is_non_zero_unless_every_case_settled(self) -> None:
        """A sitting is not a success because its first case worked."""
        code, output = self.run_headless(sentences=2, stop_after_first=True)
        self.assertEqual(2, code, output)
        self.assertIn("settled success    : 1 of 2", output)
        self.assertIn("REFUSED_AT_DRAFT", output)

    def test_a_precondition_needs_the_flag_when_no_operator_can_click(self) -> None:
        code, output = self.run_headless(
            occupants=(occupant("p-1", episode_state="APPLIED_VERIFIED"),))
        self.assertEqual(2, code, output)
        self.assertIn("precondition       : withdraw policy p-1", output)
        self.assertIn("--withdraw-verified-scope", output)
        self.assertIn("REFUSED_PRECONDITION_NOT_AUTHORISED", output)

    def test_the_flag_authorises_the_withdrawal_headless(self) -> None:
        code, output = self.run_headless(
            occupants=(occupant("p-1", episode_state="APPLIED_VERIFIED"),),
            withdraw_verified=True)
        self.assertEqual(0, code, output)
        self.assertIn("precondition met   : p-1", output)

    def test_it_leaves_the_evidence_it_names(self) -> None:
        _code, output = self.run_headless()
        written = [line for line in output.splitlines()
                   if line.startswith("wrote ")]
        self.assertTrue(written, output)
        for line in written:
            self.assertTrue(Path(line.split(":", 1)[1].strip()).is_file(), line)


# --------------------------------------------------------------------------- #
# the default entry point is untouched
# --------------------------------------------------------------------------- #

#: What must not arrive when ``python3 main.py`` composes its console: the
#: pre-Kernel runtime ``tests/gui/test_default_entry_reachability.py`` guards,
#: plus this work package's own composition root and the transports it reaches.
FORBIDDEN_PREFIXES = (
    "coordinator", "executor", "collectors", "gui.dashboard",
    "oran.rapp.gui_entry", "oran.rapp.coordinator_adapter", "oran.rapp.headless",
    "tools.legacy",
    # WP-B's own additions: the live composition root, the R1 transport it is
    # the only place allowed to construct, and the TLS/HTTP machinery behind it.
    # (``http.client``/``ssl`` are not listed: the standard library arrives
    # through ordinary dependencies and proves nothing about composition.)
    "tools.liveconsole", "tools.g3ota", "oran.rapp.r1_client",
    "oran.rapp.r1_security", "oran.rapp.policy_translator",
)

PROBE = """
import json, sys
sys.path.insert(0, {root!r})
import main
console = main.build_console()
print("@@" + json.dumps({{
    "modules": sorted(sys.modules),
    "hasKernelSession": getattr(console, "kernel_session", None) is not None,
    "hasLiveComposition": console.live is not None,
    "mode": console.controller.state().mode,
}}))
"""


def _offenders(modules: Sequence[str]) -> List[str]:
    return sorted(name for name in modules
                  if any(name == item or name.startswith(item + ".")
                         for item in FORBIDDEN_PREFIXES))


class TheLazyImportKeepsTheDefaultEntryClean(unittest.TestCase):
    """``--live`` adds a transport to the repository, not to ``main.py``.

    The same clean-process probe ``tests/gui/test_default_entry_reachability.py``
    uses, with this work package's own composition root added to the forbidden
    set.  A static scan of ``main.py`` would not catch an import three calls
    down inside ``build_console``; this does.
    """

    @classmethod
    def setUpClass(cls) -> None:
        result = subprocess.run(
            [sys.executable, "-c", PROBE.format(root=str(REPO_ROOT))],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise AssertionError(
                f"probe failed ({result.returncode})\n{result.stdout}\n{result.stderr}")
        payload = next((line for line in result.stdout.splitlines()
                        if line.startswith("@@")), None)
        if payload is None:
            raise AssertionError(f"probe printed no payload\n{result.stdout}")
        cls.payload = json.loads(payload[2:])

    def test_the_default_composition_loads_no_transport(self) -> None:
        self.assertEqual([], _offenders(self.payload["modules"]))

    def test_it_still_opens_disconnected_with_nothing_attached(self) -> None:
        self.assertEqual("DISCONNECTED", self.payload["mode"])
        self.assertFalse(self.payload["hasKernelSession"])
        self.assertFalse(self.payload["hasLiveComposition"])

    def test_the_offender_scan_would_notice_this_package(self) -> None:
        """A guard that cannot fail is not a guard."""
        self.assertEqual(["tools.liveconsole.build"],
                         _offenders(["json", "tools.liveconsole.build", "assurance"]))

    def test_build_console_attaches_nothing_in_process_either(self) -> None:
        import main

        self.assertIsNone(main.build_console().kernel_session)


AUDIT_PROBE = """
import io, json, os, site, sys, sysconfig, tempfile, unittest
sys.path.insert(0, {root!r})


def _roots():
    # Every directory this suite is *entitled* to read, by role.  The writable
    # one is a directory made for this probe and handed to it as TMPDIR --
    # never the system temp root, which holds other people's files and would
    # entitle a read of a laboratory artefact somebody happened to stage there.
    named = [
        {root!r},                       # the repository: read-only fixtures
        {sandbox!r},                    # everything this probe writes, and only that
        sys.prefix, sys.base_prefix,    # the interpreter and its standard library
    ]
    for key in ("stdlib", "platstdlib", "purelib", "platlib", "data"):
        try:
            named.append(sysconfig.get_path(key))
        except (KeyError, ValueError):
            pass
    try:
        named.extend(site.getsitepackages())
    except AttributeError:
        pass
    named.append(site.getusersitepackages())
    return tuple(sorted({{os.path.realpath(path) for path in named if path}}))


ROOTS = _roots()
outside = []


def hook(event, args):
    if event not in ("open", "os.listdir", "os.scandir") or not args:
        return
    target = args[0]
    if isinstance(target, bytes):
        target = target.decode("utf-8", "replace")
    if not isinstance(target, str) or not target:
        return
    resolved = os.path.realpath(target)
    if any(resolved == root or resolved.startswith(root + os.sep)
           for root in ROOTS):
        return
    outside.append(f"{{event}}: {{resolved}}")


sys.addaudithook(hook)
import tests.test_liveconsole as module
suite = unittest.TestLoader().loadTestsFromNames({names!r}, module)
result = unittest.TextTestRunner(verbosity=0, stream=io.StringIO()).run(suite)
print("@@" + json.dumps({{
    "outside": sorted(set(outside)),
    "roots": list(ROOTS),
    "childTmp": tempfile.gettempdir(),
    "run": result.testsRun,
    "ok": result.wasSuccessful(),
}}))
"""


class TheSuiteReadsOnlyItsOwnFiles(unittest.TestCase):
    """This file must pass on a machine that has never seen the deployment.

    Not argued from the source, measured -- and measured by *entitlement*
    rather than by naming a host path, because naming one would put a
    developer's checkout layout into a test (``tests.test_portability``
    forbids exactly that, and it is right to).

    The fixture-heavy classes are run again inside a clean interpreter under an
    audit hook, and every file the process opens must lie under a directory
    this suite is entitled to read: the repository (read-only fixtures), the
    process's own temporary root (everything it writes), or the interpreter and
    its libraries.  A deployment directory is none of those, so the previous
    coupling -- which reached the laboratory through *absolute paths inside a
    profile document*, not through any literal in this file -- would show up
    here as a path outside all three.
    """

    NAMES = (
        "OneProfileResolvesEveryLiveInput",
        "TheValuesAndTheBindingAreCrossBound",
        "TheUeIsNamedOrUnambiguousNeverTheNewest",
        "TheCompositionIsTheLiveOne",
        "TheScopeLifecycleStaysHonest",
        "TheCommittedProfileIsAWellFormedAuthority",
    )

    @classmethod
    def setUpClass(cls) -> None:
        # A directory of this probe's own, handed over as TMPDIR so everything
        # the child writes lands inside the one writable root it is entitled
        # to.  Entitling ``tempfile.gettempdir()`` instead would have let the
        # audit pass while reading any file anyone else had left in /tmp --
        # including a staged laboratory artefact, which is the exact thing this
        # audit exists to catch.
        cls._sandbox = tempfile.mkdtemp(prefix="liveconsole-audit-")
        source = AUDIT_PROBE.format(
            root=str(REPO_ROOT), sandbox=cls._sandbox, names=list(cls.NAMES))
        environment = dict(os.environ)
        environment.update({"TMPDIR": cls._sandbox, "TEMP": cls._sandbox,
                            "TMP": cls._sandbox})
        result = subprocess.run(
            [sys.executable, "-c", source], cwd=str(REPO_ROOT), env=environment,
            capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise AssertionError(
                f"audit probe failed ({result.returncode})\n"
                f"{result.stdout}\n{result.stderr}")
        payload = next((line for line in result.stdout.splitlines()
                        if line.startswith("@@")), None)
        if payload is None:
            raise AssertionError(f"audit probe printed no payload\n{result.stdout}")
        cls.payload = json.loads(payload[2:])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._sandbox, ignore_errors=True)

    def test_nothing_outside_the_entitled_roots_is_opened(self) -> None:
        self.assertEqual([], self.payload["outside"])

    def test_the_probe_actually_ran_the_fixtures(self) -> None:
        """An audit of a suite that ran nothing would prove nothing."""
        self.assertGreater(self.payload["run"], 20, self.payload)
        self.assertTrue(self.payload["ok"], self.payload)

    def test_the_entitlement_is_three_roles_not_a_path_list(self) -> None:
        """The repository and *this probe's own* temporary root, separately."""
        roots = self.payload["roots"]
        self.assertIn(os.path.realpath(str(REPO_ROOT)), roots)
        self.assertIn(os.path.realpath(self._sandbox), roots)

    def test_the_system_temp_root_is_not_entitled(self) -> None:
        """Sol E5-1: the audit must not be able to read anyone else's /tmp.

        The child runs with its own ``TMPDIR``, so the directory it writes into
        is a subtree of the sandbox and the system temp root is entitled
        nowhere.  Were it entitled, a laboratory file staged in ``/tmp`` would
        be readable with the audit still green.
        """
        system = os.path.realpath(tempfile.gettempdir())
        sandbox = os.path.realpath(self._sandbox)
        self.assertNotIn(system, self.payload["roots"])
        self.assertTrue(sandbox.startswith(system + os.sep),
                        "the sandbox should live under the system root it "
                        "replaces, or this test proves nothing")
        # And the child really used it: what it wrote is inside the sandbox.
        self.assertEqual(sandbox, os.path.realpath(self.payload["childTmp"]))

    def test_the_hook_would_notice_a_read_outside_them(self) -> None:
        """The hook itself, on a process that does read outside its roots.

        The file is a *sibling* of the sandbox -- in the system temp root the
        probe is deliberately not entitled to -- so it is outside every
        entitled directory without this test naming anybody's checkout, and it
        is exactly the case E5-1 was about.
        """
        outsider = Path(tempfile.gettempdir()) / f"liveconsole-outsider-{os.getpid()}"
        source = (
            "import os, sys\n"
            f"roots = (os.path.realpath({self._sandbox!r}),)\n"
            "seen = []\n"
            "def hook(event, args):\n"
            "    if event == 'open' and args:\n"
            "        p = os.path.realpath(str(args[0]))\n"
            "        if not any(p.startswith(r + os.sep) for r in roots):\n"
            "            seen.append(p)\n"
            "sys.addaudithook(hook)\n"
            f"try:\n    open({str(outsider)!r}).close()\n"
            "except OSError:\n    pass\n"
            "print(len(seen))\n")
        result = subprocess.run([sys.executable, "-c", source],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual("1", result.stdout.strip(), result.stderr)


class TheEntryPointRefusesAnUnaddressedLiveRun(unittest.TestCase):
    """``--live`` without the one authority is refused before anything runs."""

    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "main.py", *argv], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=300)

    def test_live_without_a_profile_is_refused_by_name(self) -> None:
        result = self._run("--live", "--no-gui")
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("--profile", result.stderr)

    def test_live_and_hardware_free_together_are_refused(self) -> None:
        result = self._run("--live", "--hardware-free", "--profile", "x.json")
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("pick one", result.stderr)

    def test_withdraw_verified_scope_needs_live(self) -> None:
        result = self._run("--hardware-free", "--no-gui",
                           "--withdraw-verified-scope")
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("--live", result.stderr)


if __name__ == "__main__":
    unittest.main()


class TheWithdrawalErrorNamesItsCause(AWithdrawalIsOnlyReportedWhenItHappened):
    """거절 본문은 **자르기 전에** 풀어야 한다.

    A1-P scope 철회가 실패하면 그 사유가 판을 시작조차 못 하게 만드는데,
    ``composition.py`` 는 본문을 200자로 **먼저 자르고** 넣고 있었다.  RFC7807 은
    원인을 ``detail`` 에 싣고 ``type``/``title``/``status`` 가 앞을 채우므로,
    먼저 자르면 남는 것은 코드 이름뿐이다 -- 2026-09-17 에 조종 503 네 건이
    정확히 그렇게 ``'status': 503, 'deta`` 에서 끊겨 하루 종일 원인을 몰랐다.
    """

    PROBLEM = {"type": "urn:oran-aic:problem:AIC_SCOPE_OCCUPIED" + "x" * 90,
               "title": "AIC_SCOPE_OCCUPIED",
               "status": 409,
               "detail": "policy p-1 is finalized and cannot be withdrawn",
               "instance": "urn:uuid:9f1d8c22-0b47-4a1e-9d2e-6c8f0a51b3ee"}

    def test_an_rfc7807_refusal_names_its_detail(self) -> None:
        from assurance.live.pin_to_cell_driver import LiveDriverError

        with self.assertRaises(LiveDriverError) as caught:
            self.clear([occupant("p-1", episode_state="APPLY_PENDING")],
                       status=409,
                       body=json.dumps(self.PROBLEM).encode("utf-8"))
        message = str(caught.exception)
        self.assertIn(self.PROBLEM["detail"], message,
                      "사유가 잘려 나갔다:\n" + message)
        self.assertNotIn("urn:uuid:", message)

    def test_the_other_producer_shape_is_named_too(self) -> None:
        from assurance.live.pin_to_cell_driver import LiveDriverError

        with self.assertRaises(LiveDriverError) as caught:
            self.clear([occupant("p-1", episode_state="APPLY_PENDING")],
                       status=409,
                       body=b'{"error": "scope holds a finalized policy"}')
        self.assertIn("scope holds a finalized policy", str(caught.exception))

    def test_a_body_that_is_not_json_is_kept_verbatim(self) -> None:
        from assurance.live.pin_to_cell_driver import LiveDriverError

        with self.assertRaises(LiveDriverError) as caught:
            self.clear([occupant("p-1", episode_state="APPLY_PENDING")],
                       status=502, body=b"<html>502 Bad Gateway</html>")
        self.assertIn("502 Bad Gateway", str(caught.exception))
