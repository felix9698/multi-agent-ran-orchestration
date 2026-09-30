"""The rApp status projection is read-only, and it stays read-only.

Union of two independently written suites.  Tracks T1 and T3 both implemented
this module's body; integration kept T1's implementation (the owner of record in
``docs/phase-b-gui/file-ownership.1.0.0.json``) and kept **both** test suites,
because the assertions are properties rather than implementation details and the
union is strictly stronger than either half.  T3's section is marked below; two
of its assertions were rebased onto the surviving implementation's spellings and
say so where they are.

``oran/rapp/status_projection.py`` is the console's only status read path.  It
exists because the three richest status payloads in this repository are declared
development control planes rather than R1, and consuming them from an operator
console would defeat the whole boundary.

The read-only property is therefore proven by AST rather than promised in a
docstring: this module walks the projection's syntax tree and asserts that no
mutating client method is called, that no development control-plane name is
built, and that neither a transport nor the A1 client is imported.  The
behavioural tests then show the projection degrades into stated unknowns instead
of raising, because a status read must never be able to abort an experiment.
"""

import ast
import unittest
from pathlib import Path

from oran.rapp import status_projection as sp

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "oran" / "rapp" / "status_projection.py"
SOURCE = MODULE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE, filename=str(MODULE_PATH))

#: Everything on R1Client that changes state somewhere else.
MUTATORS = {
    "create_policy", "update_policy", "delete_policy",
    "create_status_subscription", "update_status_subscription",
    "delete_status_subscription", "create_continuous_job", "update_data_job",
    "delete_data_job", "accept_evidence", "recover_one_time_pull",
    "mutate", "reset",
    # From T3's suite: a notification handler mutates the ledger too.
    "handle_status_notification",
}

FORBIDDEN_IMPORTS = {
    "oran.nonrt.a1_client", "subprocess", "socket", "http.client",
    "urllib.request", "requests", "telnetlib", "paramiko",
    "executor.system_controller", "gui.legacy_tools",
    # From T3's suite: this module is projection logic, not a view, so a
    # toolkit or plotting import here would mean the layers had merged.
    "tkinter", "matplotlib",
}


def _status_document(policy_id="p-1", *, enforce="ENFORCED",
                     policy_state="ACTIVE", episode_state="APPLIED_VERIFIED",
                     seq=3):
    document = {
        "enforceStatus": enforce,
        "aicStatus": {
            "policyId": policy_id, "policyRevision": 2,
            "producerEpoch": "3a0d5f4e-0000-4000-8000-000000000001",
            "statusSeq": seq, "policyState": policy_state,
            "policyTerminal": False,
            "episodeId": "9c1d5f4e-0000-4000-8000-000000000002",
            "episodeState": episode_state, "episodeTerminal": True,
            "occurredAt": "2026-08-14T10:15:00Z",
            "control": {"result": "ACK"},
            "readback": {"result": "VERIFIED"},
            "rollback": {"state": "NOT_REQUESTED"},
            "trace": {"correlationId": "c-1"},
        },
    }
    if enforce == "NOT_ENFORCED":
        document["enforceReason"] = "OTHER_REASON"
    return document


def _evidence_record(observation="1f0d5f4e-0000-4000-8000-00000000000a",
                     *, quality="OK", policy_id="p-1"):
    return {
        "dmeTypeId": "aic:policy-evidence:1.0.0",
        "observationId": observation,
        "observedAt": "2026-08-14T10:16:00Z",
        "window": {"start": "2026-08-14T10:15:00Z",
                   "end": "2026-08-14T10:16:00Z"},
        "measurementScope": {
            "managedObjectClass": "NRCellDU",
            "managedObjectDn": "SubNetwork=oran-lab,NRCellDU=2",
            "cellId": {"plmnId": {"mcc": "208", "mnc": "95"},
                       "cId": {"ncI": 12345678}}},
        "phase": "AFTER", "quality": quality,
        "samples": [{"name": "RRU.PrbDl", "value": 30.0, "unit": "percent",
                     "quality": quality}],
        "correlation": {"policyTypeId": "AIC_UECellSteering_1.0.0",
                        "policyId": policy_id, "policyRevision": 2},
    }


class FakeStore:
    def __init__(self, state, *, valid_reads=None):
        self._state = state
        self._valid_reads = valid_reads
        self.snapshot_calls = 0

    def snapshot(self):
        """Return the state, or an empty document once ``valid_reads`` is spent.

        A store whose second read is empty is not a contrivance: the durable
        ledger is rewritten atomically under a lock, and a projection that reads
        it three times can legitimately catch it mid-swap.  It is also the
        sharpest way to prove the single-read contract.
        """
        self.snapshot_calls += 1
        if (self._valid_reads is not None
                and self.snapshot_calls > self._valid_reads):
            return {"status": {}, "evidence": {}, "bindings": {}, "audit": []}
        return dict(self._state)


class FakeClient:
    """Only the read surface the projection is allowed to touch."""

    def __init__(self, *, state=None, status=None, fail=False,
                 valid_reads=None):
        self.state = FakeStore(state or {}, valid_reads=valid_reads)
        self._status = status or {}
        self._fail = fail
        self.calls = []

    @property
    def snapshot_calls(self):
        return self.state.snapshot_calls

    def get_policy_status(self, policy_id):
        self.calls.append(("get_policy_status", policy_id))
        if self._fail:
            raise RuntimeError("R1 unreachable")
        return self._status[policy_id]


class FakeAdapter:
    def __init__(self, transitions=(), fail=False):
        self._transitions = list(transitions)
        self._fail = fail

    def transition_snapshot(self):
        if self._fail:
            raise RuntimeError("adapter locked")
        return list(self._transitions)


# --------------------------------------------------------------------------- #
# The mechanical proof
# --------------------------------------------------------------------------- #


class ReadOnlyByConstruction(unittest.TestCase):

    def test_the_module_declares_itself_read_only(self):
        self.assertTrue(sp.READ_ONLY)

    def test_no_mutating_client_method_is_called(self):
        offenders = [f"{node.func.attr}@{node.lineno}"
                     for node in ast.walk(TREE)
                     if isinstance(node, ast.Call)
                     and getattr(node.func, "attr", None) in MUTATORS]
        self.assertEqual(offenders, [],
                         "a mutating call in a read-only projection")

    def test_no_transport_and_no_a1_client_is_imported(self):
        imported = []
        for node in ast.walk(TREE):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imported.append(node.module or "")
        for name in imported:
            for banned in FORBIDDEN_IMPORTS:
                self.assertFalse(name == banned or name.startswith(banned + "."),
                                 f"{name} is forbidden in the projection")

    def test_no_development_control_plane_route_is_named(self):
        """The development state routes are not consumable by this console.

        Prose explaining why is expected in the docstrings, so only executable
        code is scanned: string constants outside docstrings, and attribute
        names.  A request would have to be built out of one of those.
        """
        docstrings = set()
        for node in ast.walk(TREE):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)
        offenders = [
            f"line {node.lineno}" for node in ast.walk(TREE)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value not in docstrings and "harness" in node.value.lower()]
        offenders += [f"line {node.lineno}: .{node.attr}"
                      for node in ast.walk(TREE)
                      if isinstance(node, ast.Attribute)
                      and node.attr.startswith("harness")]
        self.assertEqual(offenders, [])

    def test_the_projection_only_reads_the_sanctioned_accessors(self):
        """Whitelist, not blacklist: every attribute call is accounted for."""
        allowed = {
            "get_policy_status", "snapshot", "transition_snapshot",
            # local helpers and stdlib
            "get", "items", "append", "keys", "values", "strip", "partition",
            "now", "strftime", "astimezone", "debug", "getLogger",
        }
        unexpected = set()
        for node in ast.walk(TREE):
            if isinstance(node, ast.Call):
                attr = getattr(node.func, "attr", None)
                if attr and attr not in allowed:
                    unexpected.add(attr)
        self.assertEqual(unexpected, set(),
                         "unreviewed attribute call in the projection")


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #


class PolicyStatusProjection(unittest.TestCase):

    def test_a_status_document_projects_every_axis_field(self):
        client = FakeClient(status={"p-1": _status_document()})
        view = sp.project_policy_status(client, "p-1")
        self.assertEqual(view.policy_id, "p-1")
        self.assertEqual(view.enforce_status, "ENFORCED")
        self.assertEqual(view.policy_state, "ACTIVE")
        self.assertEqual(view.episode_state, "APPLIED_VERIFIED")
        self.assertEqual(view.control_result, "ACK")
        self.assertEqual(view.readback_result, "VERIFIED")
        self.assertEqual(view.rollback_state, "NOT_REQUESTED")
        self.assertEqual(view.status_seq, 3)
        self.assertEqual(view.occurred_at, "2026-08-14T10:15:00Z")

    def test_a_failed_read_yields_none_rather_than_raising(self):
        client = FakeClient(status={}, fail=True)
        self.assertIsNone(sp.project_policy_status(client, "p-1"))

    def test_a_client_without_the_reader_yields_none(self):
        self.assertIsNone(sp.project_policy_status(object(), "p-1"))

    def test_a_partial_document_projects_with_honest_nulls(self):
        client = FakeClient(status={"p-1": {"enforceStatus": "ENFORCED"}})
        view = sp.project_policy_status(client, "p-1")
        self.assertEqual(view.enforce_status, "ENFORCED")
        self.assertIsNone(view.policy_state)
        self.assertIsNone(view.episode_state)
        self.assertIsNone(view.status_seq)


class DurableStateProjection(unittest.TestCase):

    def _client(self):
        return FakeClient(state={
            "status": {
                "p-1": {"producerEpoch": "e-1", "statusSeq": 3,
                        "snapshot": _status_document("p-1")},
                "p-2": {"producerEpoch": "e-2", "statusSeq": 1,
                        "snapshot": _status_document(
                            "p-2", enforce="NOT_ENFORCED",
                            policy_state="ERROR",
                            episode_state="APPLY_FAILED", seq=1)},
            },
            "evidence": {
                "job-1:obs-1": {"digest": "d", "bindingId": "b-1",
                                "dataJobId": "job-1",
                                "record": _evidence_record()},
            },
            "bindings": {"b-1": {"dataJobId": "job-1", "active": True}},
            "audit": [{"kind": "LATE_OR_DUPLICATE_STATUS", "policyId": "p-1"}],
        })

    def test_policies_project_from_the_durable_ledger_without_extra_reads(self):
        client = self._client()
        views = sp.project_policies(client)
        self.assertEqual([v.policy_id for v in views], ["p-1", "p-2"])
        self.assertEqual(views[1].policy_state, "ERROR")
        self.assertEqual(client.calls, [],
                         "the ledger read must not trigger an R1 round trip")

    def test_evidence_projects_with_scope_quality_and_measurements(self):
        views = sp.project_evidence(self._client())
        self.assertEqual(len(views), 1)
        view = views[0]
        self.assertEqual(view.quality, "OK")
        self.assertEqual(view.phase, "AFTER")
        self.assertEqual(view.policy_id, "p-1")
        self.assertEqual(view.data_job_id, "job-1")
        self.assertEqual(view.measurement_names, ("RRU.PrbDl",))
        self.assertEqual(view.sample_count, 1)
        self.assertEqual(view.cell_id, "208-95/12345678")
        self.assertEqual(view.window_end, "2026-08-14T10:16:00Z")

    def test_the_whole_state_projects_in_one_read(self):
        state = sp.project_state_store(self._client())
        self.assertEqual(len(state.policies), 2)
        self.assertEqual(len(state.evidence), 1)
        self.assertEqual(state.binding_count, 1)
        self.assertIn("LATE_OR_DUPLICATE_STATUS", state.audit_flags)
        self.assertIn("b-1:job-1", state.ledger_references)
        self.assertTrue(state.captured_at.endswith("Z"))
        self.assertIsNone(state.error)

    def test_a_broken_store_yields_an_empty_view_with_a_reason(self):
        class Broken:
            @property
            def state(self):
                raise RuntimeError("ledger unreadable")

        state = sp.project_state_store(Broken())
        self.assertEqual(state.policies, ())
        self.assertIn("ledger unreadable", state.error)

    def test_a_missing_store_is_empty_not_an_exception(self):
        state = sp.project_state_store(object())
        self.assertEqual(state.policies, ())
        self.assertEqual(state.evidence, ())

    def test_the_whole_state_comes_from_exactly_one_snapshot(self):
        """The read contract, asserted directly: one call, not three.

        Regression for the defect where ``project_state_store`` took its own
        snapshot and then called ``project_policies`` and ``project_evidence``,
        each of which took another.  Three reads mean the three axes the console
        renders side by side can come from three different moments.
        """
        client = self._client()
        sp.project_state_store(client)
        self.assertEqual(client.snapshot_calls, 1,
                         "project_state_store must read the durable store once")

    def test_a_store_valid_only_on_its_first_read_still_projects_fully(self):
        """The reviewer's reproduction: first read valid, later reads empty.

        Under the three-read implementation this yielded policies=0 and
        evidence=0 - a console showing an empty deployment while the store held
        two policies and an accepted evidence record.
        """
        client = FakeClient(state=self._client().state._state, valid_reads=1)
        state = sp.project_state_store(client)
        self.assertEqual(client.snapshot_calls, 1)
        self.assertEqual([view.policy_id for view in state.policies],
                         ["p-1", "p-2"])
        self.assertEqual(len(state.evidence), 1)
        self.assertEqual(state.evidence[0].measurement_names, ("RRU.PrbDl",))
        self.assertEqual(state.binding_count, 1)
        self.assertIn("LATE_OR_DUPLICATE_STATUS", state.audit_flags)

    def test_the_three_axes_come_from_one_moment(self):
        """Policies, evidence and bindings must describe the same instant.

        The store below mutates between reads.  Whatever the projection
        reports, it must be internally consistent - never policies from one
        moment and evidence from the next.
        """
        base = self._client().state._state
        second = {"status": {}, "evidence": {}, "bindings": {}, "audit": []}
        sequence = [base, second, second]

        class Mutating:
            def __init__(self):
                self.snapshot_calls = 0

            def snapshot(self):
                index = min(self.snapshot_calls, len(sequence) - 1)
                self.snapshot_calls += 1
                return dict(sequence[index])

        class Client:
            def __init__(self):
                self.state = Mutating()

        client = Client()
        state = sp.project_state_store(client)
        self.assertEqual(client.state.snapshot_calls, 1)
        self.assertTrue(state.policies and state.evidence,
                        "the first snapshot's contents must all survive")

    def test_the_public_helpers_each_read_once(self):
        """``project_policies`` and ``project_evidence`` are single-read too."""
        for projector in (sp.project_policies, sp.project_evidence):
            client = self._client()
            self.assertTrue(projector(client))
            self.assertEqual(client.snapshot_calls, 1,
                             f"{projector.__name__} read the store more than once")

    def test_garbage_shapes_never_raise(self):
        for payload in ({"status": "not a dict"}, {"evidence": [1, 2]},
                        {"bindings": None}, {"audit": "x"}, {}):
            client = FakeClient(state=payload)
            sp.project_policies(client)
            sp.project_evidence(client)
            sp.project_state_store(client)


class TransitionProjection(unittest.TestCase):

    def test_transitions_pass_through_with_their_origin(self):
        adapter = FakeAdapter([{"from": "S0", "to": "S1", "origin": "REAL"},
                               {"from": "S1", "to": "S2",
                                "origin": "SYNTHETIC"}])
        views = sp.project_transitions(adapter)
        self.assertEqual(len(views), 2)
        self.assertEqual(views[1]["origin"], "SYNTHETIC")

    def test_a_failing_adapter_yields_an_empty_tuple(self):
        self.assertEqual(sp.project_transitions(FakeAdapter(fail=True)), ())
        self.assertEqual(sp.project_transitions(object()), ())

    def test_non_mapping_entries_are_dropped_not_coerced(self):
        self.assertEqual(sp.project_transitions(FakeAdapter(["S0", None])), ())


class CapabilityProjection(unittest.TestCase):

    MANIFEST = {
        "manifestId": "m-1", "nearRtRicId": "ric-1",
        "effectiveAt": "2026-08-04T00:00:00Z",
        "topology": {"cells": [{"managedObjectDn": "NRCellDU=1"}]},
        "decisionKpis": [{"name": "RRU.PrbDl"}],
        "assuranceKpis": [{"name": "RRU.PrbDl"}, {"name": "DRB.UEThpDl"}],
        "controlAxes": ["serving_cell"],
        "policyTypes": ["AIC_UECellSteering_1.0.0"],
        "e2Deployment": {"nodes": [{"role": "SOURCE"}]},
        "limits": {"maxActivePolicies": 64},
    }

    def test_kpi_names_come_out_of_the_declaration(self):
        view = sp.project_capability(self.MANIFEST)
        self.assertEqual(view.decision_kpis, ("RRU.PrbDl",))
        self.assertEqual(view.assurance_kpis, ("RRU.PrbDl", "DRB.UEThpDl"))
        self.assertEqual(view.control_axes, ("serving_cell",))
        self.assertEqual(view.limits["maxActivePolicies"], 64)

    def test_an_absent_manifest_is_an_error_not_a_default_inventory(self):
        view = sp.project_capability({})
        self.assertEqual(view.decision_kpis, ())
        self.assertEqual(view.topology, {})
        self.assertTrue(view.error)

    def test_a_manifest_that_declares_nothing_yields_nothing(self):
        view = sp.project_capability({"manifestId": "m"})
        self.assertEqual(view.assurance_kpis, ())
        self.assertEqual(view.policy_types, ())
        self.assertIsNone(view.error)

    def test_the_repository_fixture_manifest_projects(self):
        import json

        path = REPO_ROOT / "docs" / "phase-a" / "raw" / "mock-local" / \
            "capability.json"
        view = sp.project_capability(
            json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(view.near_rt_ric_id, "near-rt-ric-fixture-001")
        self.assertIn("RRU.PrbDl", view.assurance_kpis)
        self.assertEqual(len(view.topology["cells"]), 2)


# --------------------------------------------------------------------------- #
# Track T3's suite, carried across at integration.
#
# T3 wrote these against its own body.  The properties survive the change of
# implementation unaltered; only two spellings were rebased, and each says so
# where it sits.  Nothing was weakened to make it pass.
# --------------------------------------------------------------------------- #


T3_STATUS_DOCUMENT = {
    "enforceStatus": "NOT_ENFORCED",
    "enforceReason": "OTHER_REASON",
    "aicStatus": {
        "policyId": "p-1", "policyRevision": 3, "producerEpoch": "epoch-1",
        "statusSeq": 7, "policyState": "RECOVERY_PENDING",
        "policyTerminal": False,
        "episodeId": "e-1", "episodeState": "ROLLBACK_FAILED",
        "episodeTerminal": True,
        "occurredAt": "2026-08-13T14:20:00Z",
        "control": {"result": "NACK"},
        "readback": {"result": "MISMATCH"},
        "rollback": {"state": "FAILED"},
        "error": {"code": "AIC_ROLLBACK_FAILED", "stage": "ROLLBACK"},
        "trace": {},
    },
}

T3_EVIDENCE_RECORD = {
    "dmeTypeId": "aic:policy-evidence:1.0.0",
    "observationId": "obs-1",
    "observedAt": "2026-08-13T14:20:00Z",
    "window": {"start": "2026-08-13T14:19:00Z", "end": "2026-08-13T14:20:00Z"},
    "policyScope": {"ueId": "ue2"},
    "measurementScope": {
        "managedObjectClass": "NRCellDU",
        "managedObjectDn": "SubNetwork=oran-lab,NRCellDU=1",
        "cellId": {"cId": {"ncI": 87654321},
                   "plmnId": {"mcc": "208", "mnc": "95"}},
    },
    "source": {"interface": "O1"},
    "phase": "AFTER", "quality": "SUSPECT", "ambiguityReason": None,
    "samples": [{"name": "RRU.PrbDl", "value": 30, "quality": "OK"},
                {"name": "DRB.UEThpDl", "value": None, "quality": "MISSING"}],
    "correlation": {"policyTypeId": "AIC_UECellSteering_1.0.0",
                    "policyId": "p-1", "policyRevision": 3},
}


class _T3FakeStore:
    def __init__(self, state):
        self._state = state

    def snapshot(self):
        return self._state


class _T3FakeClient:
    """Only the read surface exists.  A mutating call would raise AttributeError."""

    def __init__(self, state=None, status=None, fail=False):
        self.state = _T3FakeStore(state or {})
        self._status = status
        self._fail = fail
        self.reads = []

    def get_policy_status(self, policy_id):
        self.reads.append(policy_id)
        if self._fail:
            raise RuntimeError("R1 unreachable")
        if self._status is None:
            raise KeyError(policy_id)
        return self._status


class _T3FakeAdapter:
    def __init__(self, transitions):
        self._transitions = transitions

    def transition_snapshot(self):
        return list(self._transitions)


class ProjectionLivesOutsideTheGui(unittest.TestCase):

    def test_it_lives_outside_the_gui_package_on_purpose(self):
        """So the import-graph proof shows the GUI reaches no transport itself."""
        self.assertEqual(MODULE_PATH.parts[-3:],
                         ("oran", "rapp", "status_projection.py"))


class FailingPolicyStatusProjection(unittest.TestCase):
    """The unhappy status object: every axis of a failure is carried, separately."""

    def test_every_axis_of_the_status_object_is_carried_separately(self):
        view = sp.project_policy_status(
            _T3FakeClient(status=T3_STATUS_DOCUMENT), "p-1")
        self.assertEqual(view.enforce_status, "NOT_ENFORCED")
        self.assertEqual(view.enforce_reason, "OTHER_REASON")
        self.assertEqual(view.policy_state, "RECOVERY_PENDING")
        self.assertEqual(view.episode_state, "ROLLBACK_FAILED")
        self.assertEqual(view.control_result, "NACK")
        self.assertEqual(view.readback_result, "MISMATCH")
        self.assertEqual(view.rollback_state, "FAILED")
        self.assertEqual(view.error_code, "AIC_ROLLBACK_FAILED")
        self.assertEqual(view.error_stage, "ROLLBACK")
        self.assertEqual(view.policy_revision, "3")
        self.assertEqual(view.status_seq, 7)

    def test_an_unknown_policy_is_none_not_a_blank_healthy_view(self):
        """A ``KeyError`` from the reader is an unknown policy, not a healthy one."""
        self.assertIsNone(sp.project_policy_status(_T3FakeClient(), "p-1"))

    def test_a_failing_read_does_not_raise_into_the_console(self):
        self.assertIsNone(
            sp.project_policy_status(_T3FakeClient(fail=True), "p-1"))

    def test_absent_fields_stay_none_rather_than_defaulting_to_healthy(self):
        view = sp.project_policy_status(
            _T3FakeClient(status={"enforceStatus": "ENFORCED", "aicStatus": {}}),
            "p-9")
        self.assertEqual(view.policy_id, "p-9")
        self.assertIsNone(view.policy_state)
        self.assertIsNone(view.control_result)
        self.assertIsNone(view.readback_result)


class DurableStateProjectionWithRetiredBinding(unittest.TestCase):

    def setUp(self):
        self.client = _T3FakeClient(state={
            "status": {"p-1": {"producerEpoch": "epoch-1", "statusSeq": 7,
                               "snapshot": T3_STATUS_DOCUMENT}},
            "evidence": {"job-1:obs-1": {"digest": "d", "bindingId": "b-1",
                                         "dataJobId": "job-1",
                                         "record": T3_EVIDENCE_RECORD}},
            "bindings": {"b-1": {"active": True}, "b-2": {"active": False}},
            "audit": [{"kind": "LATE_OR_DUPLICATE_STATUS"}],
        })

    def test_policies_come_from_the_snapshot_without_a_round_trip(self):
        views = sp.project_policies(self.client)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].policy_id, "p-1")
        self.assertEqual(self.client.reads, [], "no extra R1 read per policy")

    def test_evidence_carries_quality_window_and_measurement_names(self):
        views = sp.project_evidence(self.client)
        self.assertEqual(len(views), 1)
        view = views[0]
        self.assertEqual(view.quality, "SUSPECT")
        self.assertEqual(view.phase, "AFTER")
        self.assertEqual(view.window_start, "2026-08-13T14:19:00Z")
        self.assertEqual(view.sample_count, 2)
        self.assertEqual(view.measurement_names, ("RRU.PrbDl", "DRB.UEThpDl"))
        self.assertEqual(view.binding_id, "b-1")
        # Rebased spelling: the surviving implementation renders a cell as
        # ``mcc-mnc/ncI``, which carries the PLMN the other spelling dropped.
        # The property under test is that the structured cell id reaches the
        # console as a legible identity, not which of the two it is.
        self.assertEqual(view.cell_id, "208-95/87654321")

    def test_a_retired_binding_is_not_counted_as_an_active_one(self):
        """``b-2`` is retired.  Counting it would promise evidence that is not coming."""
        state = sp.project_state_store(self.client)
        self.assertEqual(state.binding_count, 1)
        self.assertEqual(len(state.ledger_references), 2,
                         "the ledger still lists the retired binding")

    def test_one_snapshot_backs_the_whole_view(self):
        state = sp.project_state_store(self.client)
        self.assertEqual(len(state.policies), 1)
        self.assertEqual(len(state.evidence), 1)
        self.assertEqual(state.audit_flags, ("LATE_OR_DUPLICATE_STATUS",))
        self.assertTrue(state.captured_at.endswith("Z"))
        self.assertIsNone(state.error)

    def test_an_unreadable_state_store_yields_a_view_with_a_reason(self):
        class Broken:
            @property
            def state(self):
                raise RuntimeError("durable store is gone")

        state = sp.project_state_store(Broken())
        self.assertEqual(state.policies, ())
        self.assertTrue(state.error)

    def test_a_malformed_snapshot_is_skipped_not_guessed_at(self):
        client = _T3FakeClient(state={"status": {"p-1": "not-a-mapping"},
                                      "evidence": {"x": {"record": 5}}})
        self.assertEqual(sp.project_policies(client), ())
        self.assertEqual(sp.project_evidence(client), ())


class TransitionOriginProjection(unittest.TestCase):

    def test_origin_is_preserved_so_synthetic_is_never_read_as_measured(self):
        adapter = _T3FakeAdapter([
            {"from": "S0", "to": "S1", "outcome": "FSM_EVENT",
             "evidenceRef": None, "origin": "REAL"},
            {"from": "S1", "to": "S6", "outcome": "commit_original",
             "evidenceRef": "evid-1", "origin": "SYNTHETIC"},
        ])
        projected = sp.project_transitions(adapter)
        self.assertEqual([t["origin"] for t in projected], ["REAL", "SYNTHETIC"])
        self.assertEqual(projected[1]["evidenceRef"], "evid-1")

    def test_an_entry_without_an_origin_is_unknown_not_real(self):
        projected = sp.project_transitions(
            _T3FakeAdapter([{"from": "S0", "to": "S1"}]))
        self.assertEqual(projected[0]["origin"], "UNKNOWN")

    def test_a_failing_adapter_yields_an_empty_tuple(self):
        class BrokenAdapter:
            def transition_snapshot(self):
                raise RuntimeError("no adapter")

        self.assertEqual(sp.project_transitions(BrokenAdapter()), ())


class CapabilityProjectionFromTheFixtureManifest(unittest.TestCase):

    def setUp(self):
        import json

        self.manifest = json.loads(
            (REPO_ROOT / "tests" / "gui" / "fixtures"
             / "capability-manifest-min.json").read_text(encoding="utf-8"))

    def test_the_inventory_comes_from_the_manifest(self):
        view = sp.project_capability(self.manifest)
        self.assertEqual(view.assurance_kpis, ("RRU.PrbDl", "DRB.UEThpDl"))
        self.assertEqual(view.decision_kpis, ("RRU.PrbDl",))
        self.assertEqual(view.policy_types, ("AIC_UECellSteering_1.0.0",))
        # Rebased spelling: the surviving implementation passes the manifest's
        # ``e2Deployment`` through whole, so the E2 node inventory is read from
        # there rather than from a synthesised ``topology["e2Nodes"]``.  That is
        # also where ``live_ops._inventory_elements`` reads it, so this asserts
        # the shape the console actually consumes.
        self.assertEqual(len(view.e2_deployment["nodes"]), 2)
        self.assertEqual(view.limits["maxActivePolicies"], 64)
        self.assertIsNone(view.error)

    def test_no_manifest_means_no_inventory_rather_than_a_fixed_testbed(self):
        for empty in ({}, None):
            with self.subTest(manifest=empty):
                view = sp.project_capability(empty)
                self.assertEqual(view.decision_kpis, ())
                self.assertEqual(view.topology, {})
                self.assertEqual(view.e2_deployment, {})
                self.assertTrue(view.error)

    def test_element_names_and_counts_are_never_hard_coded(self):
        """Section 6: a deployment's inventory is whatever it declares."""
        smaller = dict(self.manifest)
        smaller["e2Deployment"] = {"nodes": [], "e2apVersion": "2.03"}
        smaller["assuranceKpis"] = []
        smaller["topology"] = {}
        view = sp.project_capability(smaller)
        self.assertEqual(view.e2_deployment["nodes"], [])
        self.assertEqual(view.topology, {})
        self.assertEqual(view.assurance_kpis, ())


if __name__ == "__main__":
    unittest.main()
