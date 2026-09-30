"""The Gate 2 seam contract.

Design authority: ``docs/superpowers/specs/2026-08-20-unified-ota-assurance-system-design.md``
sections 4-8.  Ownership authority: ``docs/architecture/SEAMS-GATE2.md``.

This file pins what all four Gate 2 lanes (KCON, KERN, KGW, KAGT) are entitled
to rely on while they work in parallel:

* every module in ``assurance/**`` imports with no toolkit, no transport, no
  hardware and no model client;
* the complete modules behave -- the transition table admits and refuses the
  right transitions, envelope hashes reproduce, a ``DERIVED`` quantity cannot
  exist without its rule and inputs, and ``ConfirmationRecord`` stays sealed;
* the frozen signatures exist, carry their contract in a docstring, and fail
  loudly rather than silently returning ``None``;
* the human-authority vocabulary the design removed has not come back.

Everything here runs without a display, without a network and without the
testbed.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import unittest
from pathlib import Path
from typing import Any, Dict, List

from assurance.advisors import messages as advisory_messages
from assurance.advisors import roles as advisory_roles
from assurance.advisors import strategy as advisory_strategy
from assurance.collector import collector as collector_protocol
from assurance.collector import samples as collector_samples
from assurance.contracts import capability, catalog, epoch, harm, ledgers
from assurance.contracts import measurement, target, validation
from assurance.core import (
    addressing,
    axes,
    components,
    confirmation,
    envelopes,
    provenance,
    states,
)
from assurance.gateway import token as gateway_token
from assurance.gateway import write_gateway
from assurance.kernel import event_store, kernel, reducer

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "assurance"
SEAMS_DOC = REPO_ROOT / "docs" / "architecture" / "SEAMS-GATE2.md"
#: Gate 4 added ``assurance/objectives/`` and owns that subtree's file table in
#: its own document (``SEAMS-GATE2.md`` section 11).  The invariant below is
#: unchanged -- every module file has an owner somewhere -- only *where* the
#: owner is written is now split per gate.
SEAMS_DOCS = (
    SEAMS_DOC,
    REPO_ROOT / "docs" / "architecture" / "SEAMS-GATE4.md",
)

STAMP = "2026-08-21T09:00:00.000000Z"
LATER = "2026-08-21T10:00:00.000000Z"


def _source_files() -> List[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _module_names() -> List[str]:
    """Every module in the package, derived from the tree rather than imported.

    Not ``pkgutil.walk_packages(assurance.__path__, ...)``: under
    ``discover -s tests`` this test package is imported *as* ``assurance`` and
    extends that ``__path__`` (see ``tests/assurance/__init__.py``), so walking
    it would enumerate the tests as if they were product modules.  Reading the
    directory keeps the list the same under every invocation.
    """
    names = ["assurance"]
    for path in _source_files():
        relative = path.relative_to(PACKAGE_ROOT)
        parts = list(relative.parts)
        if parts[-1] == "__init__.py":
            parts.pop()
            if not parts:
                continue
        else:
            parts[-1] = parts[-1][: -len(".py")]
        names.append("assurance." + ".".join(parts))
    return sorted(names)


def _imported_names(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names.append(node.module or "")
    return names


# --------------------------------------------------------------------------- #
# (a) every module imports, and the boundary holds
# --------------------------------------------------------------------------- #

#: Imports that would break the hardware-free, model-free guarantee or cross
#: the O-RAN control boundary.  Design section 15 requires hardware-free work
#: to report zero live E2/RAN/OTA/USRP calls; the cheapest proof is that the
#: transport is not even importable from here.
FORBIDDEN_IMPORTS = {
    "tkinter", "matplotlib", "numpy", "scipy", "pandas",
    "subprocess", "socket", "telnetlib", "paramiko", "requests",
    "http.client", "urllib.request", "asyncio",
    "coordinator", "executor", "gui", "decision", "collectors",
    "experiments", "calibration", "diagnosis", "runstore",
    "oran.rapp", "oran.nonrt", "oran.o1", "oran.integration", "oran.release",
}

#: The only outward import the package allows: the project's existing RFC 8785
#: canonicaliser.  Reused rather than reimplemented so an assurance digest and
#: a frozen ``oran-aic/1.0.0`` artefact digest are the same value.
ALLOWED_ORAN_IMPORTS = {"oran.contract.jcs"}

#: Gate 6's confirmed batch/export boundary is registered rather than hidden
#: from this package scan.  It is hardware-free (its runner admits REPLAY and
#: EMULATED executors only), but it deliberately consumes the existing
#: experiment statistics and the run-directory store/export surfaces.  Keep the
#: exceptions file-specific: adding a transport, model, or arbitrary outward
#: import anywhere else in ``assurance/`` remains a failure.
#:
#: ``runstore`` is the neutral, stdlib-only run-directory schema the Operator
#: Console and this runner share.  It is listed in ``FORBIDDEN_IMPORTS`` and
#: granted back here for one file only, so the narrow exception stays narrow:
#: sharing a storage format must not become a licence for the rest of the
#: package to reach outward.  No ``gui`` entry appears below, and none may: the
#: Gate 2 boundary in that direction is absolute.
BATCH_ALLOWED_IMPORTS = {
    # The Agent's three LLM calls resolve their operator-chosen model through
    # the same backend registry the Cockpit lists (Claude / LiteLLM / mock).
    # Imported lazily inside the resolver; tests script the answers instead.
    "assurance/coordination/agents.py": {"decision.llm_backend"},
    "assurance/batch/metrics.py": {"experiments.metrics"},
    "assurance/batch/runner.py": {
        "runstore.records",
        "runstore.session_store",
        "matplotlib",
        "matplotlib.pyplot",
    },
}


class ModuleHygiene(unittest.TestCase):
    """Everything imports, and nothing imports what it must not."""

    def test_every_module_imports(self):
        for name in _module_names():
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_module_set_is_the_expected_packages(self):
        packages = {n for n in _module_names() if n.count(".") == 1}
        self.assertEqual(
            packages,
            {
                # Hardware-free, enumerable actuator contracts.  The package
                # contains no transport and exposes advisory proposals only.
                "assurance.actions",
                "assurance.advisors",
                "assurance.collector",
                "assurance.contracts",
                "assurance.core",
                "assurance.gateway",
                "assurance.kernel",
                # Gate 3's live half: the PIN_TO_CELL case as the real testbed
                # runs it -- the contract family, the KPM-fed counter source
                # and the corroborated effect readback.  Registered here rather
                # than exempted: every rule below still applies to it, and it
                # is the transport-free half by construction.  The composition
                # root that builds its ports lives outside this package.
                "assurance.live",
                # Gate 2's single integration runtime boundary (task section
                # 3.1).  A module rather than a package: it wires the six
                # above together and owns nothing of its own.
                "assurance.vertical",
                # Gate 4's objective catalog: the machine-readable registry of
                # the seven families (support state, standard mapping, actual
                # deployment capability) and the frozen per-family seats.
                # Registered rather than exempted: every rule in this file --
                # forbidden imports, hardware-free, vocabulary -- applies to it
                # and passes.  See docs/architecture/SEAMS-GATE4.md.
                "assurance.objectives",
                # Gate 6's hardware-free batch/export boundary.  It receives a
                # deliberately narrow dependency exception below so a future
                # transport/model import cannot hide behind this registration.
                "assurance.batch",
                # The xApp coordination layer: capability manifests, the
                # registry, and the two coordinators that map a Policy onto an
                # ordered set of (xApp, concrete Action) steps.  Registered
                # rather than exempted, exactly as Gate 4 registered
                # ``assurance.objectives``: every rule in this file --
                # forbidden imports, hardware-free, vocabulary -- applies to it
                # and passes, and it takes no entry in
                # ``BATCH_ALLOWED_IMPORTS``.  It writes nothing: a produced
                # plan is a declaration that still needs Kernel admission and a
                # Write Gateway permit.  See
                # docs/architecture/SEAMS-GATE2.md section 13.
                "assurance.xapps",
                # The Agent's coordination layer (owner redefinition of
                # 2026-09-07, orc_task/IMPLEMENTATION_CONTRACT.md): T/C/grid
                # data model and the three single-call LLM agents.  Registered
                # like ``assurance.xapps``; its one outward import is the
                # project's LLM backend registry (``COORDINATION_ALLOWED_IMPORTS``).
                "assurance.coordination",
            },
        )

    def test_no_forbidden_imports(self):
        violations = []
        for path in _source_files():
            relative_path = str(path.relative_to(REPO_ROOT))
            allowed = BATCH_ALLOWED_IMPORTS.get(relative_path, set())
            for name in _imported_names(path):
                if name in allowed:
                    continue
                if name.startswith("oran"):
                    if name not in ALLOWED_ORAN_IMPORTS:
                        violations.append(f"{relative_path}:{name}")
                    continue
                for banned in FORBIDDEN_IMPORTS:
                    if name == banned or name.startswith(banned + "."):
                        violations.append(f"{relative_path}:{name}")
        self.assertEqual(violations, [], f"forbidden imports: {violations}")

    def test_addressing_reuses_the_project_canonicaliser(self):
        """The digest must be the existing one, not a second implementation."""
        from oran.contract.jcs import jcs_sha256

        payload = {"b": [1, 2], "a": "x"}
        self.assertEqual(addressing.content_hash(payload), jcs_sha256(payload))


# --------------------------------------------------------------------------- #
# (b) complete modules: behaviour, not just importability
# --------------------------------------------------------------------------- #

class TrialStateMachine(unittest.TestCase):
    """Design section 7's flow and branches, as a table."""

    def test_canonical_path_is_legal_end_to_end(self):
        S = states.TrialState
        path = [
            S.PROPOSED, S.VALIDATING, S.RESERVED, S.PREPARING, S.READY,
            S.COMMIT_DECIDED, S.APPLYING, S.APPLIED_PENDING_RESULT,
            S.SETTLING, S.OBSERVING, S.DECISION_HOLD, S.FINALIZING_LIVE,
            S.SETTLEMENT, S.SETTLED_SUCCESS,
        ]
        for source, dest in zip(path, path[1:]):
            with self.subTest(transition=f"{source.value}->{dest.value}"):
                self.assertTrue(states.is_legal_transition(source, dest))

    def test_representative_illegal_transitions_are_refused(self):
        S = states.TrialState
        illegal = [
            # No apply before the durable commit decision (task 6.3).
            (S.READY, S.APPLYING),
            # Success cannot skip live finalization (design 7.9).
            (S.DECISION_HOLD, S.SETTLEMENT),
            (S.DECISION_HOLD, S.SETTLED_SUCCESS),
            # A post-commit non-success owes a reverse rollback first (7.10).
            (S.STOPPING, S.SETTLEMENT),
            (S.OBSERVING, S.SETTLED_NON_SUCCESS),
            # Pre-commit abort is not available once commit is durable.
            (S.APPLYING, S.PRE_COMMIT_ABORT),
            (S.COMMIT_DECIDED, S.PRE_COMMIT_ABORT),
            # Terminal is terminal.
            (S.SETTLED_SUCCESS, S.SETTLEMENT),
            (S.INCIDENT_LOCKDOWN, S.RECOVERY_VERIFYING),
        ]
        for source, dest in illegal:
            with self.subTest(transition=f"{source.value}->{dest.value}"):
                self.assertFalse(states.is_legal_transition(source, dest))
                with self.assertRaises(states.IllegalTransitionError):
                    states.assert_transition(source, dest)

    def test_every_state_can_still_reach_a_terminal_state(self):
        """The structural half of "every case terminates finitely" (design 8)."""
        for state in states.TrialState:
            with self.subTest(state=state.value):
                if states.is_terminal(state):
                    continue
                self.assertTrue(
                    states.reachable_from(state) & states.TERMINAL_TRIAL_STATES,
                    f"{state.value} cannot reach a terminal state",
                )

    def test_pre_and_post_commit_partition_every_non_terminal_state(self):
        covered = states.PRE_COMMIT_STATES | states.POST_COMMIT_STATES
        self.assertEqual(states.PRE_COMMIT_STATES & states.POST_COMMIT_STATES, frozenset())
        self.assertEqual(
            set(states.TrialState) - covered,
            set(states.TERMINAL_TRIAL_STATES) | {states.TrialState.SETTLEMENT},
        )

    def test_safety_reasons_outrank_the_semantic_verdict(self):
        """Design section 7's fixed precedence."""
        self.assertEqual(len(states.SAFETY_PRECEDENCE), len(list(states.StopReason)))
        outranking = [r for r in states.StopReason if states.outranks_semantic_verdict(r)]
        self.assertEqual(len(outranking), 8)
        self.assertIs(
            states.strongest_reason(
                [states.StopReason.SEMANTIC_NON_SUCCESS, states.StopReason.HARM_LIMIT_BREACH]
            ),
            states.StopReason.HARM_LIMIT_BREACH,
        )
        with self.assertRaises(ValueError):
            states.strongest_reason([])

    def test_transition_table_is_read_only(self):
        with self.assertRaises(TypeError):
            states.TRIAL_TRANSITIONS[states.TrialState.READY] = frozenset()  # type: ignore[index]


class SeparatedAxes(unittest.TestCase):
    """Design section 8: seven dimensions that must not be collapsed."""

    def test_seven_distinct_enum_types(self):
        self.assertEqual(len(axes.AXIS_ENUMS), 7)
        self.assertEqual(len(set(axes.AXIS_ENUMS)), 7)

    def test_no_member_of_one_axis_equals_a_member_of_another(self):
        for i, left in enumerate(axes.AXIS_ENUMS):
            for right in axes.AXIS_ENUMS[i + 1:]:
                for a in left:
                    for b in right:
                        self.assertNotEqual(a, b)

    def test_the_four_named_labels_stay_distinct(self):
        """"INVALID, INDETERMINATE, FAIL, and EXEC_ERROR remain distinct"."""
        self.assertNotEqual(axes.ExecutionValidity.INVALID, axes.TrialOutcome.INVALID)
        self.assertNotEqual(axes.PredicateVerdict.FAIL, axes.TrialOutcome.FAIL)
        self.assertNotEqual(
            axes.PredicateVerdict.INDETERMINATE, axes.TrialOutcome.INDETERMINATE
        )
        self.assertNotEqual(axes.ExecutionValidity.EXEC_ERROR, axes.TrialOutcome.EXEC_ERROR)

    def test_case_termination_has_exactly_six_endings(self):
        self.assertEqual(
            {t.value for t in axes.CaseTermination},
            {
                "SUCCESS", "VECTORS_EXHAUSTED", "EVIDENCE_INCOMPLETE",
                "OPERATOR_ABORT", "SAFETY_INCIDENT", "RECOVERY_FAILURE",
            },
        )

    def test_insufficient_or_invalid_traces_do_not_fill_a_quota(self):
        self.assertTrue(
            axes.counts_toward_closure(
                axes.ExecutionValidity.VALID,
                axes.MeasurementSufficiency.SUFFICIENT,
                axes.PredicateVerdict.FAIL,
            )
        )
        for validity, sufficiency, verdict in [
            (axes.ExecutionValidity.INVALID, axes.MeasurementSufficiency.SUFFICIENT,
             axes.PredicateVerdict.PASS),
            (axes.ExecutionValidity.VALID, axes.MeasurementSufficiency.MISSING_INTERVAL,
             axes.PredicateVerdict.PASS),
            (axes.ExecutionValidity.VALID, axes.MeasurementSufficiency.SUFFICIENT,
             axes.PredicateVerdict.INDETERMINATE),
            (axes.ExecutionValidity.EXEC_ERROR, axes.MeasurementSufficiency.SUFFICIENT,
             axes.PredicateVerdict.FAIL),
        ]:
            with self.subTest(validity=validity, sufficiency=sufficiency, verdict=verdict):
                self.assertFalse(axes.counts_toward_closure(validity, sufficiency, verdict))

    def test_blocked_obligations_are_evidence_incomplete_not_exhaustion(self):
        """Task section 6.16 / design 6.4."""
        for status in (axes.EvidenceCellStatus.TEMP_BLOCKED,
                       axes.EvidenceCellStatus.BUDGET_LOCKED):
            with self.subTest(status=status.value):
                self.assertIs(
                    axes.aggregate_from_cells(frozenset({status}), any_deployed_success=False),
                    axes.AggregateState.EVIDENCE_INCOMPLETE,
                )
        self.assertIs(
            axes.aggregate_from_cells(
                frozenset({axes.EvidenceCellStatus.CLOSED_PASS,
                           axes.EvidenceCellStatus.CLOSED_FAIL}),
                any_deployed_success=False,
            ),
            axes.AggregateState.EXHAUSTED,
        )
        # None of the four blocking statuses may ever produce an exhaustion
        # claim, however many closed cells sit beside them (task 6.16).
        for status in axes.OPEN_EVIDENCE_CELL_STATUSES:
            with self.subTest(status=status.value):
                self.assertIsNot(
                    axes.aggregate_from_cells(
                        frozenset({status, axes.EvidenceCellStatus.CLOSED_PASS}),
                        any_deployed_success=False,
                    ),
                    axes.AggregateState.EXHAUSTED,
                )

    def test_unsettled_outcome_is_not_a_non_success(self):
        with self.assertRaises(ValueError):
            axes.is_non_success(axes.TrialOutcome.NOT_SETTLED)
        self.assertFalse(axes.is_non_success(axes.TrialOutcome.SUCCESS))
        self.assertTrue(axes.is_non_success(axes.TrialOutcome.SAFETY_STOPPED))


class TypedQuantityRules(unittest.TestCase):
    """Design section 6.1 / task section 5.4-5.5."""

    def test_derived_requires_rule_and_inputs(self):
        with self.assertRaises(ValueError):
            provenance.TypedQuantity(1.0, "Mbps", provenance.Provenance.DERIVED, "r")
        with self.assertRaises(ValueError):
            provenance.TypedQuantity(
                1.0, "Mbps", provenance.Provenance.DERIVED, "r", derivation_rule="mean"
            )
        good = provenance.TypedQuantity(
            1.0, "Mbps", provenance.Provenance.DERIVED, "r",
            derivation_rule="mean_over_window", input_refs=("s-1", "s-2"),
        )
        self.assertEqual(good.input_refs, ("s-1", "s-2"))

    def test_non_derived_must_not_carry_a_derivation(self):
        """The opposite dodge: a computed number labelled MEASURED."""
        with self.assertRaises(ValueError):
            provenance.TypedQuantity(
                1.0, "Mbps", provenance.Provenance.MEASURED, "s", derivation_rule="mean"
            )
        with self.assertRaises(ValueError):
            provenance.TypedQuantity(
                1.0, "Mbps", provenance.Provenance.MEASURED, "s", input_refs=("s-1",)
            )

    def test_illustrative_and_draft_are_not_admissible(self):
        for status in (provenance.DocumentStatus.ILLUSTRATIVE,
                       provenance.DocumentStatus.DRAFT):
            quantity = provenance.TypedQuantity(
                1.0, "Mbps", provenance.Provenance.EXPERIMENT_CONFIG, "slide", status
            )
            with self.subTest(status=status.value):
                self.assertFalse(quantity.admissible_for_runtime)
                with self.assertRaises(provenance.InadmissibleQuantityError):
                    quantity.require_admissible("harm bound")

    def test_five_provenance_values_exactly(self):
        self.assertEqual(
            {p.value for p in provenance.Provenance},
            {"OPERATOR_CONFIRMED", "MANIFEST_BOUND", "MEASURED",
             "EXPERIMENT_CONFIG", "DERIVED"},
        )

    def test_unit_and_finiteness_are_enforced(self):
        with self.assertRaises(ValueError):
            provenance.TypedQuantity(1.0, "", provenance.Provenance.MEASURED, "s")
        with self.assertRaises(ValueError):
            provenance.TypedQuantity(float("nan"), "Mbps", provenance.Provenance.MEASURED, "s")

    def test_round_trips_through_its_canonical_form(self):
        quantity = provenance.TypedQuantity(
            3.5, "Mbps", provenance.Provenance.MEASURED, "sample-1"
        )
        self.assertEqual(
            provenance.TypedQuantity.from_canonical_dict(quantity.to_canonical_dict()),
            quantity,
        )


class EnvelopeRules(unittest.TestCase):
    """Design section 6.1 / task section 6.12."""

    def _event(self, **overrides: Any) -> envelopes.EventEnvelope:
        base: Dict[str, Any] = dict(
            schema_version=envelopes.ASSURANCE_SCHEMA_VERSION,
            object_id="case-1",
            event_id="ev-1",
            timestamp=STAMP,
            sequence=0,
            idempotency_key="idem-1",
            source_component=components.ComponentId.ASSURANCE_KERNEL,
            event_kind="ContractAdmitted",
            payload={"b": 1, "a": [1, 2]},
        )
        base.update(overrides)
        return envelopes.EventEnvelope.seal(**base)

    def _state(self) -> envelopes.EnvelopeAdmissionState:
        return envelopes.EnvelopeAdmissionState(
            supported_schema_versions=frozenset({envelopes.ASSURANCE_SCHEMA_VERSION}),
            allowed_sources=frozenset({components.ComponentId.ASSURANCE_KERNEL}),
        )

    def test_hash_is_reproducible_across_equivalent_payloads(self):
        left = self._event(payload={"b": 1, "a": [1, 2]})
        right = self._event(payload={"a": (1, 2), "b": 1})
        self.assertEqual(left.content_hash, right.content_hash)
        self.assertEqual(left.envelope_hash(), right.envelope_hash())
        self.assertEqual(left, right)
        self.assertTrue(left.verify_content_hash())

    def test_every_rejection_class_is_reachable(self):
        state = self._state()
        first = self._event()
        self.assertIsNone(envelopes.classify_envelope(first, state, now=STAMP))
        state.record(first)

        cases = {
            envelopes.EnvelopeRejection.DUPLICATE_EVENT_ID: first,
            envelopes.EnvelopeRejection.REPLAYED_DUPLICATE: self._event(event_id="ev-2"),
            envelopes.EnvelopeRejection.IDEMPOTENCY_COLLISION: self._event(
                event_id="ev-3", payload={"z": 9}
            ),
            envelopes.EnvelopeRejection.STALE_SEQUENCE: self._event(
                event_id="ev-4", idempotency_key="i4", sequence=0, payload={"z": 1}
            ),
            envelopes.EnvelopeRejection.REORDERED: self._event(
                event_id="ev-5", idempotency_key="i5", sequence=7, payload={"z": 2}
            ),
            envelopes.EnvelopeRejection.EXPIRED: self._event(
                event_id="ev-6", idempotency_key="i6", sequence=1,
                payload={"z": 3}, expiry="2020-01-01T00:00:00.000000Z",
            ),
            envelopes.EnvelopeRejection.SCHEMA_VERSION_UNSUPPORTED: self._event(
                event_id="ev-7", idempotency_key="i7", sequence=1,
                payload={"z": 4}, schema_version="assurance/9.9.9",
            ),
            envelopes.EnvelopeRejection.SOURCE_NOT_PERMITTED: self._event(
                event_id="ev-8", idempotency_key="i8", sequence=1, payload={"z": 5},
                source_component=components.ComponentId.INTENT_AGENT,
            ),
        }
        for expected, envelope in cases.items():
            with self.subTest(rejection=expected.value):
                self.assertIs(
                    envelopes.classify_envelope(envelope, state, now=STAMP), expected
                )

        tampered = dataclasses.replace(
            self._event(event_id="ev-9", idempotency_key="i9", sequence=1),
            payload={"tampered": True},
        )
        self.assertIs(
            envelopes.classify_envelope(tampered, state, now=STAMP),
            envelopes.EnvelopeRejection.CONTENT_HASH_MISMATCH,
        )

    def test_mailbox_epoch_mismatch_is_rejected(self):
        good_epoch = addressing.content_hash({"epoch": 1})
        stale_epoch = addressing.content_hash({"epoch": 0})
        state = envelopes.EnvelopeAdmissionState(
            supported_schema_versions=frozenset({envelopes.ASSURANCE_SCHEMA_VERSION}),
            allowed_sources=frozenset(components.ADVISORY_COMPONENTS),
            expected_epoch_hash=good_epoch,
        )
        stale = envelopes.MailboxEnvelope.seal(
            schema_version=envelopes.ASSURANCE_SCHEMA_VERSION,
            object_id="case-1", event_id="m-1", timestamp=STAMP, sequence=0,
            idempotency_key="m-idem",
            source_component=components.ComponentId.EVIDENCE_COORDINATOR,
            message_kind="NEXT_CANDIDATE_PROPOSAL", correlation_id="corr-1",
            epoch_hash=stale_epoch, payload={"candidateId": "c-1"},
        )
        self.assertIs(
            envelopes.classify_envelope(stale, state, now=STAMP),
            envelopes.EnvelopeRejection.EPOCH_MISMATCH,
        )

    def test_classifier_does_not_mutate_the_cursor(self):
        state = self._state()
        envelope = self._event()
        envelopes.classify_envelope(envelope, state, now=STAMP)
        self.assertEqual(state.last_sequence, -1)
        self.assertEqual(state.seen_event_ids, set())

    def test_malformed_envelope_fields_are_refused_at_construction(self):
        with self.assertRaises(ValueError):
            self._event(timestamp="2026-08-21 09:00:00")
        with self.assertRaises(ValueError):
            self._event(sequence=-1)
        with self.assertRaises(ValueError):
            self._event(event_kind="")


class ConfirmationModel(unittest.TestCase):
    """Design section 5: content hash and timestamp, never a person."""

    def _record(self, digest: str) -> confirmation.ConfirmationRecord:
        return confirmation.ConfirmationRecord(
            confirmed_object_type="TargetVector",
            confirmed_content_hash=digest,
            event_id="ev-confirm",
            timestamp=STAMP,
            action=confirmation.ConfirmationAction.CONFIRM_AND_START,
        )

    def test_fields_are_sealed_against_signer_vocabulary(self):
        names = {f.name for f in dataclasses.fields(confirmation.ConfirmationRecord)}
        self.assertEqual(
            names,
            {
                "confirmed_object_type", "confirmed_content_hash", "event_id",
                "timestamp", "action", "changed_after_confirmation",
            },
        )
        self.assertEqual(names & confirmation.FORBIDDEN_CONFIRMATION_FIELDS, set())

    def test_changed_content_invalidates_the_confirmation(self):
        digest = addressing.content_hash({"vector": ["a", "b"]})
        other = addressing.content_hash({"vector": ["a", "c"]})
        record = self._record(digest)
        self.assertTrue(record.is_valid_for(digest))
        self.assertFalse(record.is_valid_for(other))
        self.assertFalse(record.invalidated().is_valid_for(digest))

    def test_five_operator_controls(self):
        self.assertEqual(
            {a.value for a in confirmation.ConfirmationAction},
            {"REVIEW_AND_CONFIRM", "CONFIRM_AND_START", "CONFIRM_BATCH_PLAN",
             "ABORT", "EMERGENCY_STOP"},
        )

    def test_round_trips_through_its_canonical_form(self):
        record = self._record(addressing.content_hash({"vector": []}))
        self.assertEqual(
            confirmation.ConfirmationRecord.from_canonical_dict(record.to_canonical_dict()),
            record,
        )


class KernelTokenRules(unittest.TestCase):
    """Design section 4.4: a deterministic safety permit, not a signature."""

    def _token(self, **overrides: Any) -> gateway_token.KernelToken:
        base: Dict[str, Any] = dict(
            token_kind=gateway_token.TokenKind.COMMIT,
            transaction_id="tx-1",
            trial_id="trial-1",
            fencing_token=4,
            command_sequence=0,
            lease_expiry=LATER,
            expected_config_hash=addressing.content_hash({"config": 1}),
            idempotency_key="idem-1",
            issued_at=STAMP,
        )
        base.update(overrides)
        return gateway_token.KernelToken(**base)

    def test_fields_carry_no_signature_vocabulary(self):
        names = {f.name for f in dataclasses.fields(gateway_token.KernelToken)}
        self.assertEqual(names & gateway_token.FORBIDDEN_TOKEN_FIELDS, set())
        self.assertEqual(
            names,
            {
                "token_kind", "transaction_id", "trial_id", "fencing_token",
                "command_sequence", "lease_expiry", "expected_config_hash",
                "idempotency_key", "issued_at", "issuer",
            },
        )

    def test_only_the_kernel_issues_tokens(self):
        with self.assertRaises(ValueError):
            self._token(issuer=components.ComponentId.INTENT_AGENT)
        with self.assertRaises(ValueError):
            self._token(issuer=components.ComponentId.WRITE_GATEWAY)

    def test_a_higher_fence_supersedes_a_lower_one(self):
        old = self._token(fencing_token=4)
        new = self._token(fencing_token=5)
        self.assertTrue(new.fences_out(old))
        self.assertFalse(old.fences_out(new))

    def test_tokens_for_different_transactions_never_fence_each_other(self):
        mine = self._token(transaction_id="tx-1", fencing_token=9)
        theirs = self._token(transaction_id="tx-2", fencing_token=1)
        self.assertFalse(mine.fences_out(theirs))

    def test_a_token_authorises_exactly_one_operation(self):
        commit = self._token(token_kind=gateway_token.TokenKind.COMMIT)
        self.assertTrue(commit.authorises(gateway_token.TokenKind.COMMIT))
        self.assertFalse(commit.authorises(gateway_token.TokenKind.PREPARE))

    def test_lease_must_have_life_and_expires(self):
        with self.assertRaises(ValueError):
            self._token(lease_expiry=STAMP)
        self.assertFalse(self._token().is_expired(STAMP))
        self.assertTrue(self._token().is_expired("2026-08-21T11:00:00.000000Z"))

    def test_round_trips_through_its_canonical_form(self):
        original = self._token()
        self.assertEqual(
            gateway_token.KernelToken.from_canonical_dict(original.to_canonical_dict()),
            original,
        )


class RawSampleRules(unittest.TestCase):
    """Design section 4.5: raw measurement, straight to the Kernel."""

    def _sample(self, **overrides: Any) -> collector_samples.RawSample:
        base: Dict[str, Any] = dict(
            sample_id="s-1",
            counter_id="dl_throughput",
            value=provenance.TypedQuantity(
                3.4, "Mbps", provenance.Provenance.MEASURED, "trace-1"
            ),
            scope_snapshot={"cellId": "87654321"},
            observed_at=STAMP,
            cadence_ms=1000,
            clock_health=collector_samples.ClockHealth.SYNCHRONISED,
            trace_hash=addressing.content_hash({"trace": 1}),
            sequence=0,
        )
        base.update(overrides)
        return collector_samples.RawSample(**base)

    def test_an_agent_summary_cannot_wear_a_measurement_envelope(self):
        derived = provenance.TypedQuantity(
            3.4, "Mbps", provenance.Provenance.DERIVED, "agent-1",
            derivation_rule="mean", input_refs=("s-1",),
        )
        with self.assertRaises(ValueError):
            self._sample(value=derived)
        with self.assertRaises(ValueError):
            self._sample(source_component=components.ComponentId.XAPP_AGENT)

    def test_gaps_are_reported_not_filled(self):
        gap = collector_samples.MissingInterval(
            "2026-08-21T09:00:01.000000Z", "2026-08-21T09:00:02.500000Z", "kpm gap"
        )
        sample = self._sample(missing_intervals=(gap,))
        self.assertTrue(sample.has_gaps())
        self.assertEqual(sample.missing_ms(), 1500)

    def test_overlapping_gaps_are_refused(self):
        first = collector_samples.MissingInterval(
            "2026-08-21T09:00:01.000000Z", "2026-08-21T09:00:03.000000Z"
        )
        second = collector_samples.MissingInterval(
            "2026-08-21T09:00:02.000000Z", "2026-08-21T09:00:04.000000Z"
        )
        with self.assertRaises(ValueError):
            self._sample(missing_intervals=(first, second))

    def test_a_future_sample_is_not_fresh(self):
        sample = self._sample()
        self.assertTrue(sample.is_fresh("2026-08-21T09:00:02.000000Z", freshness_bound_ms=5000))
        self.assertFalse(sample.is_fresh("2026-08-21T08:59:00.000000Z", freshness_bound_ms=5000))
        self.assertFalse(sample.is_fresh("2026-08-21T09:01:00.000000Z", freshness_bound_ms=5000))

    def test_cadence_must_be_positive(self):
        with self.assertRaises(ValueError):
            self._sample(cadence_ms=0)

    def test_collector_declares_it_delivers_to_the_kernel_only(self):
        self.assertTrue(collector_protocol.DELIVERS_TO_KERNEL_ONLY)


class AdvisoryMessageRules(unittest.TestCase):
    """Design section 4.2: typed proposals that confer nothing."""

    def _epoch(self) -> str:
        return addressing.content_hash({"epoch": 1})

    def _draft_constraint(self) -> target.TypedConstraint:
        return target.TypedConstraint(
            measurement_ref="m/dl",
            operator=target.ComparisonOperator.GREATER_OR_EQUAL,
            bound=provenance.TypedQuantity(
                3.5, "Mbps", provenance.Provenance.EXPERIMENT_CONFIG, "agent-1",
                provenance.DocumentStatus.DRAFT,
            ),
        )

    def test_a_proposed_number_is_inadmissible_by_construction(self):
        message = advisory_messages.AdvisoryMessage(
            message_id="adv-1",
            kind=advisory_messages.AdvisoryKind.INTENT_DRAFT,
            issued_by=components.ComponentId.INTENT_AGENT,
            correlation_id="corr-1",
            epoch_hash=self._epoch(),
            created_at=STAMP,
            body=advisory_messages.IntentDraft(
                objective_family="QoSTarget",
                scope_selector={"ueId": "130"},
                proposed_constraints=(self._draft_constraint(),),
            ),
        )
        bound = message.body.proposed_constraints[0].bound
        self.assertFalse(bound.admissible_for_runtime)

    def test_an_admissible_number_cannot_be_proposed(self):
        normative = target.TypedConstraint(
            measurement_ref="m/dl",
            operator=target.ComparisonOperator.GREATER_OR_EQUAL,
            bound=provenance.TypedQuantity(
                3.5, "Mbps", provenance.Provenance.MEASURED, "trace-1"
            ),
        )
        with self.assertRaises(ValueError):
            advisory_messages.IntentDraft(
                objective_family="QoSTarget",
                scope_selector={},
                proposed_constraints=(normative,),
            )

    def test_a_role_cannot_issue_another_role_s_advisory(self):
        with self.assertRaises(ValueError):
            advisory_messages.AdvisoryMessage(
                message_id="adv-2",
                kind=advisory_messages.AdvisoryKind.NEXT_CANDIDATE_PROPOSAL,
                issued_by=components.ComponentId.INTENT_AGENT,
                correlation_id="corr-1",
                epoch_hash=self._epoch(),
                created_at=STAMP,
                body=advisory_messages.NextCandidateProposal(candidate_id="c-1"),
            )

    def test_a_non_advisory_component_cannot_advise(self):
        with self.assertRaises(ValueError):
            advisory_messages.AdvisoryMessage(
                message_id="adv-3",
                kind=advisory_messages.AdvisoryKind.INTENT_DRAFT,
                issued_by=components.ComponentId.ASSURANCE_KERNEL,
                correlation_id="corr-1",
                epoch_hash=self._epoch(),
                created_at=STAMP,
                body=advisory_messages.IntentDraft("QoSTarget", {}),
            )

    def test_free_text_is_capped(self):
        with self.assertRaises(ValueError):
            advisory_messages.IntentDraft(
                objective_family="QoSTarget",
                scope_selector={},
                explanation="x" * (advisory_messages.MAX_EXPLANATION_CHARS + 1),
            )

    def test_no_advisory_body_carries_kernel_authority(self):
        bodies = (
            advisory_messages.IntentDraft,
            advisory_messages.CandidateAssessment,
            advisory_messages.NextCandidateProposal,
        )
        for body in bodies:
            with self.subTest(body=body.__name__):
                names = {f.name for f in dataclasses.fields(body)}
                self.assertEqual(
                    names & advisory_messages.FORBIDDEN_ADVISORY_FIELDS, set()
                )

    def test_six_comparable_strategies(self):
        self.assertEqual(
            {s.value for s in advisory_strategy.StrategyKind},
            {"LLM_EVIDENCE_COORDINATOR", "DETERMINISTIC", "RANDOM",
             "OPTIMIZATION", "MONOLITHIC_LLM", "ADVERSARIAL_ADVISOR"},
        )


class ContractFamiliesAreInstantiable(unittest.TestCase):
    """Every lane must be able to build a contract set without waiting for KCON."""

    def test_all_families_are_dataclasses(self):
        self.assertEqual(len(validation.CONTRACT_FAMILIES), 24)
        for family in validation.CONTRACT_FAMILIES:
            with self.subTest(family=family.__name__):
                self.assertTrue(dataclasses.is_dataclass(family))

    def test_a_complete_pin_to_cell_contract_set_constructs(self):
        identity = dict(
            version="1.0.0",
            schema_version=envelopes.ASSURANCE_SCHEMA_VERSION,
            document_status="NORMATIVE",
            standard_mapping={"a1p": "A1-P v03.00"},
        )
        measured = provenance.TypedQuantity(
            1.2, "Mbps", provenance.Provenance.MEASURED, "cal-1"
        )
        margin = provenance.TypedQuantity(
            0.4, "Mbps", provenance.Provenance.EXPERIMENT_CONFIG, "plan-1"
        )
        admissible = provenance.TypedQuantity(
            1.6, "Mbps", provenance.Provenance.DERIVED, "proof-1",
            derivation_rule="measured_plus_margin", input_refs=("cal-1", "plan-1"),
        )

        counter = measurement.CounterBinding(
            counter_id="dl_throughput", deployment_counter_name="DRB.UEThpDl",
            source=measurement.MeasurementSource.E2_KPM,
            scope_keys=("cellId", "ueId"), unit="Mbps",
            native_cadence_ms=1000, deployment_binding_ref="dep/r1",
        )
        contract = measurement.MeasurementContract(
            contract_id="m/dl", counter_id="dl_throughput",
            scope_selector={"cellId": "87654321"}, membership_snapshot=("ue1",),
            cadence_ms=1000, window_width_ms=5000, window_stride_ms=5000,
            overlap=measurement.OverlapPolicy.DISJOINT,
            aggregation=measurement.Aggregation.MEAN,
            estimator=measurement.Estimator.SAMPLE_MEAN,
            minimum_entity_count=1, hold_ms=10000,
            gap_policy=measurement.GapPolicy.CONSERVATIVE_CHARGE,
            missing_interval_charge=margin, freshness_bound_ms=3000,
            clock_requirement=measurement.ClockRequirement.SYNCHRONISED_REQUIRED,
            uncertainty_rule=measurement.UncertaintyRule("normal_ci", measured, 0.95),
            **identity,
        )
        registry = measurement.MeasurementRegistry(
            contract_id="registry/1", counters=(counter,), measurements=(contract,),
            **identity,
        )

        constraint = target.TypedConstraint(
            "m/dl", target.ComparisonOperator.GREATER_OR_EQUAL,
            provenance.TypedQuantity(
                3.5, "Mbps", provenance.Provenance.OPERATOR_CONFIRMED, "ev-confirm"
            ),
        )
        option = target.TargetOption(
            contract_id="opt/pin", capability_ref="cap/ts",
            parameter_space={"targetCellId": ("87654321",)}, **identity,
        )
        pin = target.TargetContract(
            contract_id="target/PIN_TO_CELL", objective_family="PIN_TO_CELL",
            scope_selector={"amfUeNgapId": "130"},
            predicates=(target.TargetPredicate("p1", constraint),),
            options=(option,), hold_ms=10000, **identity,
        )
        vector = target.TargetVector(
            contract_id="vector/1", ordered_target_refs=("target/PIN_TO_CELL",),
            **identity,
        )
        release = target.TargetReleasePolicy(contract_id="release/1", **identity)

        bound = harm.CertifiedHarmBound(
            bound_id="hb/1", measured_bound=measured, conservative_margin=margin,
            admissible_bound=admissible, uncertainty_ref="unc/1",
            operating_scope={"prb": "106"}, enforced_timeout_ms=5000,
            calibration_records=("cal-1",), proof_ref="proof-1",
        )
        watchdog = harm.WatchdogContract(
            contract_id="wd/1", watchdog_id="wd/1",
            trigger=target.TypedConstraint(
                "m/dl", target.ComparisonOperator.LESS_OR_EQUAL, admissible
            ),
            action=harm.WatchdogAction.STOP_AND_ROLLBACK, **identity,
        )
        harm_contract = harm.HarmContract(
            contract_id="harm/1", harm_kind=harm.HarmKind.TRIAL_INDUCED,
            scope_selector={"ueId": "ue2"},
            reserve=provenance.TypedQuantity(
                5.0, "Mbps", provenance.Provenance.EXPERIMENT_CONFIG, "plan-1"
            ),
            bounds=(bound,), watchdogs=(watchdog,), missing_interval_charge=margin,
            **identity,
        )

        deployment = capability.DeploymentBinding(
            contract_id="dep/r1", endpoint_id="r1",
            base_url="https://127.0.0.1:8443",
            transport_security=capability.TransportSecurity.MTLS,
            secret_refs={"clientSecret": "env:R1_CLIENT_SECRET"}, **identity,
        )
        actuator = capability.ActuatorBinding(
            contract_id="act/1", capability_ref="cap/ts",
            path=capability.ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
            policy_type_id="ORAN_TrafficSteeringPreference_1.0.0",
            service_model={"serviceModel": "E2SM-RC", "style": "3"},
            readback_measurement_ref="m/readback", deployment_binding_ref="dep/r1",
            **identity,
        )
        manifest = capability.CapabilityManifest(
            contract_id="cap/ts", capability_id="cap/ts",
            supported_objectives=("PIN_TO_CELL",), constraints=(constraint,),
            actuator_refs=("act/1",), measurement_refs=("m/dl",), **identity,
        )
        composition = capability.CompositionManifest(
            contract_id="comp/1", composition_id="comp/1",
            capability_refs=("cap/ts",), **identity,
        )

        candidate = catalog.Candidate(
            candidate_id="cand-1", target_ref="target/PIN_TO_CELL",
            option_ref="opt/pin", parameters={"targetCellId": "87654321"},
            semantic_hash=addressing.content_hash({"candidate": 1}),
            capability_ref="cap/ts",
        )
        frozen_catalog = catalog.CandidateCatalog(
            contract_id="catalog/1", generator_version="gen/1.0.0",
            cardinality=1, candidates=(candidate,),
            catalog_hash=addressing.content_hash({"catalog": 1}),
            epoch_ref="epoch/1", **identity,
        )
        policy = catalog.CoordinationCasePolicy(
            contract_id="case/1", deadline_ms=600000, max_trials=10,
            max_proposals=20, target_release_policy_ref="release/1",
            harm_contract_refs=("harm/1",), **identity,
        )

        record = epoch.EpochRecord(
            contract_id="epoch/1", epoch_id="epoch/1", frozen_at=STAMP,
            target_contract_hashes={"target/PIN_TO_CELL": addressing.content_hash({"t": 1})},
            harm_contract_hashes={"harm/1": addressing.content_hash({"h": 1})},
            measurement_contract_hashes={"m/dl": addressing.content_hash({"m": 1})},
            capability_manifest_hashes={"cap/ts": addressing.content_hash({"c": 1})},
            composition_manifest_hash=addressing.content_hash({"comp": 1}),
            target_vector_hash=addressing.content_hash({"v": 1}),
            target_vector_order=("target/PIN_TO_CELL",),
            case_policy_hash=addressing.content_hash({"p": 1}),
            deployment_binding_hashes={"dep/r1": addressing.content_hash({"d": 1})},
            counter_binding_hashes={"dl_throughput": addressing.content_hash({"cb": 1})},
            actuator_binding_hashes={"act/1": addressing.content_hash({"ab": 1})},
            candidate_generator_version="gen/1.0.0",
            candidate_universe_cardinality=1,
            candidate_semantic_hashes=(candidate.semantic_hash,),
            catalog_hash=frozen_catalog.catalog_hash,
            evaluator_version="eval/1.0.0", reducer_version="reducer/1.0.0",
            **identity,
        )

        # The set is usable, and the checks that are complete actually work.
        self.assertTrue(frozen_catalog.membership_matches_cardinality())
        self.assertEqual(record.target_vector_order, ("target/PIN_TO_CELL",))
        self.assertEqual(registry.measurements[0].contract_id, "m/dl")
        self.assertEqual(pin.objective_family, "PIN_TO_CELL")
        self.assertTrue(release.require_exhaustion_certificate)
        self.assertTrue(policy.require_recovery_before_next_trial)
        self.assertIs(actuator.path, capability.ActuatorPath.OFFICIAL_ORAN_DYNAMIC)
        self.assertEqual(deployment.secret_refs["clientSecret"], "env:R1_CLIENT_SECRET")
        self.assertEqual(manifest.supported_objectives, ("PIN_TO_CELL",))
        self.assertEqual(composition.capability_refs, ("cap/ts",))
        self.assertEqual(harm_contract.bounds[0].enforced_timeout_ms, 5000)

    def test_defensive_copies_stop_a_frozen_contract_from_drifting(self):
        scope = {"cellId": "87654321"}
        option = target.TargetOption(
            contract_id="opt/1", version="1.0.0",
            schema_version=envelopes.ASSURANCE_SCHEMA_VERSION,
            document_status="NORMATIVE", standard_mapping=scope,
            capability_ref="cap/1", parameter_space={},
        )
        scope["cellId"] = "12345678"
        self.assertEqual(option.standard_mapping["cellId"], "87654321")

    def test_ledger_records_construct(self):
        contribution = ledgers.EvidenceContribution(
            contribution_id="cn-1", trial_ref="trial-1",
            candidate_semantic_hash=addressing.content_hash({"c": 1}),
            execution_validity=axes.ExecutionValidity.VALID,
            measurement_sufficiency=axes.MeasurementSufficiency.SUFFICIENT,
            predicate_verdict=axes.PredicateVerdict.PASS,
            trace_refs=("trace-1",), dependency_group="grp-1",
        )
        cell = ledgers.EvidenceCell(
            cell_id="cell-1", target_ref="target/PIN_TO_CELL",
            candidate_semantic_hash=contribution.candidate_semantic_hash,
            status=axes.EvidenceCellStatus.PARTIAL,
            required_independent_contributions=2, contributions=(contribution,),
        )
        evidence_record = ledgers.EvidenceLedgerRecord(
            record_id="er-1", event_ref="ev-1", epoch_ref="epoch/1",
            case_ref="case-1", cell=cell, added_contribution=contribution,
            previous_status=axes.EvidenceCellStatus.OPEN,
        )
        harm_record = ledgers.HarmLedgerRecord(
            record_id="hr-1", event_ref="ev-2", epoch_ref="epoch/1",
            case_ref="case-1", harm_contract_ref="harm/1",
            harm_kind=harm.HarmKind.TRIAL_INDUCED,
            movement=ledgers.ReserveMovement(
                "mv-1", ledgers.MovementKind.CHARGE,
                provenance.TypedQuantity(
                    0.4, "Mbps", provenance.Provenance.MEASURED, "s-1"
                ),
                "observed degradation",
            ),
            trial_ref="trial-1",
        )
        compatibility = ledgers.CompatibilityRecord(
            record_id="cr-1", source_epoch_ref="epoch/0", target_epoch_ref="epoch/1",
            candidate_semantic_hash=contribution.candidate_semantic_hash,
            results={check.value: True for check in ledgers.CompatibilityCheck},
            admitted=True,
        )
        self.assertEqual(len(compatibility.results), 9)
        self.assertIs(evidence_record.cell.status, axes.EvidenceCellStatus.PARTIAL)
        self.assertIs(harm_record.movement.kind, ledgers.MovementKind.CHARGE)


# --------------------------------------------------------------------------- #
# (c) frozen signatures exist and fail loudly
# --------------------------------------------------------------------------- #

#: Module-level frozen functions: present, documented, and raising rather than
#: returning ``None``.  A frozen signature that silently returned would let a
#: lane build on top of a stub and only discover it at integration.
FROZEN_FUNCTIONS = [
    (reducer, "replay"),
    (reducer, "terminal_state_hash"),
    # (advisory_strategy, "deterministic_fallback") graduated: KAGT has
    # implemented this frozen signature for real (see
    # assurance/advisors/strategy.py and docs/architecture/SEAMS-GATE2.md
    # section 3, "자기 레인의 본문은 자유롭게 구현하되").  Its behaviour is
    # covered by tests/assurance/test_kagt_fallback.py instead of by this
    # generic "still raises NotImplementedError" seam check.
]

#: Protocol classes and their frozen method surface.  Protocol bodies are
#: ``...`` by definition, so what is asserted is the surface and its contract
#: docstring rather than a raising body.
FROZEN_PROTOCOLS = {
    event_store.EventStore: [
        "append", "iterate", "last_position", "last_sequence",
        "has_event_id", "idempotency_hash", "uncertain_transactions",
    ],
    reducer.Reducer: ["initial_state", "apply"],
    write_gateway.WriteGateway: [
        "prepare", "ready", "commit", "stop", "reverse_rollback",
        "reread_configuration", "confirm_recovery", "emergency_safe_state",
        "finalize_live", "query_transaction",
    ],
    write_gateway.WriteGatewayAdapter: ["dispatch"],
    collector_protocol.MeasurementCollector: [
        "bind_sink", "poll", "clock_health", "scope_snapshot", "describe_source",
    ],
    advisory_roles.IntentAgent: ["draft_contract", "explain"],
    advisory_roles.XAppAgent: ["assess_candidate"],
    advisory_roles.EvidenceCoordinator: ["propose_next"],
    advisory_strategy.AdvisoryStrategy: ["propose", "describe"],
}

#: Concrete classes whose methods are frozen with raising bodies.
FROZEN_METHODS = {
    kernel.AssuranceKernel: [
        "__init__", "submit_advisory", "admit_contract", "admit_deployment",
        "freeze_epoch", "current_catalog", "open_trial", "advance_trial",
        "issue_token", "reserve", "charge_harm", "ingest_raw_sample",
        "evaluate_trial", "close_evidence", "exhaustion_certificate",
        "release_target_vector", "terminate_case", "settle_trial", "recover",
        "terminal_state_hash",
    ],
    write_gateway.AdapterRegistry: ["register", "resolve", "registered_paths"],
}


class FrozenSignatures(unittest.TestCase):
    """The seam every lane codes against before any body exists."""

    def test_frozen_functions_raise_not_implemented(self):
        for module, name in FROZEN_FUNCTIONS:
            with self.subTest(function=f"{module.__name__}.{name}"):
                function = getattr(module, name)
                self.assertTrue(callable(function))
                self.assertTrue(
                    (function.__doc__ or "").strip(),
                    f"{name} must carry its contract in a docstring",
                )
                signature = inspect.signature(function)
                arguments = {
                    parameter.name: None
                    for parameter in signature.parameters.values()
                    if parameter.default is inspect.Parameter.empty
                    and parameter.kind
                    in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    )
                }
                with self.assertRaises(NotImplementedError):
                    function(**arguments)

    def test_frozen_protocol_surfaces_exist_and_are_documented(self):
        for protocol, methods in FROZEN_PROTOCOLS.items():
            for name in methods:
                with self.subTest(protocol=protocol.__name__, method=name):
                    member = getattr(protocol, name, None)
                    self.assertIsNotNone(member, f"{protocol.__name__}.{name} missing")
                    self.assertTrue(
                        (member.__doc__ or "").strip(),
                        f"{protocol.__name__}.{name} must document its contract",
                    )

    def test_frozen_methods_raise_not_implemented(self):
        for owner, methods in FROZEN_METHODS.items():
            for name in methods:
                with self.subTest(owner=owner.__name__, method=name):
                    member = getattr(owner, name)
                    self.assertTrue(
                        (member.__doc__ or "").strip(),
                        f"{owner.__name__}.{name} must document its contract",
                    )
                    signature = inspect.signature(member)
                    arguments = {
                        parameter.name: None
                        for parameter in signature.parameters.values()
                        if parameter.default is inspect.Parameter.empty
                        and parameter.kind
                        in (
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.KEYWORD_ONLY,
                        )
                    }
                    arguments.pop("self", None)
                    with self.assertRaises(NotImplementedError):
                        member(object.__new__(owner), **arguments)

    def test_every_frozen_body_points_at_its_owning_lane(self):
        """A lane reading the traceback must learn who owns the file."""
        for module, name in FROZEN_FUNCTIONS:
            with self.subTest(function=f"{module.__name__}.{name}"):
                source = inspect.getsource(getattr(module, name))
                self.assertIn("SEAMS-GATE2.md", source)
                self.assertTrue(
                    any(lane in source for lane in ("KCON", "KERN", "KGW", "KAGT")),
                    f"{name} does not name an owning lane",
                )


# --------------------------------------------------------------------------- #
# (d) ownership doc and vocabulary
# --------------------------------------------------------------------------- #

#: Identifiers the design removed from the system (sections 4-5).  Checked as
#: exact identifier names rather than by substring, so a docstring quoting the
#: design ("not a human signature") stays legal while a field named
#: ``signature`` does not.
FORBIDDEN_IDENTIFIERS = {
    "signer", "signer_id", "signers", "signature", "signatures",
    "signature_bytes", "signed_by", "trusted_signer", "approval", "approver",
    "approved_by", "joint_approval", "governance", "threshold", "thresholds",
    "theta_star", "role", "roles", "private_key", "certificate",
}


#: ``assurance/coordination`` is the Agent's own layer: there "role" names which
#: of the three LLM agents (Target / Control / Trajectory) made a decision --
#: the owner's vocabulary (orc_task/MODEL_INSTRUCTION.md) -- and never a human
#: signer or approver.  The human-authority vocabulary check therefore skips it.
VOCABULARY_EXEMPT_PREFIXES = ("assurance/coordination/",)


def _vocabulary_checked_files() -> List[Path]:
    return [path for path in _source_files()
            if not str(path.relative_to(REPO_ROOT)).startswith(VOCABULARY_EXEMPT_PREFIXES)]


def _vocabulary_checked_modules() -> List[str]:
    return [name for name in _module_names() if not name.startswith("assurance.coordination")]


class OwnershipAndVocabulary(unittest.TestCase):

    def test_seams_doc_lists_every_module_file(self):
        text = "\n".join(doc.read_text(encoding="utf-8") for doc in SEAMS_DOCS)
        missing = [
            str(path.relative_to(REPO_ROOT))
            for path in _source_files()
            if str(path.relative_to(REPO_ROOT)) not in text
        ]
        names = ", ".join(doc.name for doc in SEAMS_DOCS)
        self.assertEqual(
            missing, [], f"files with no ownership entry in {names}: {missing}"
        )

    def test_no_human_authority_identifier_in_the_package(self):
        violations = []
        for path in _vocabulary_checked_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                name = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = node.name
                elif isinstance(node, ast.arg):
                    name = node.arg
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    name = node.id
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    name = node.attr
                if name and name.lower() in FORBIDDEN_IDENTIFIERS:
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{name}")
        self.assertEqual(
            violations, [], f"human-authority vocabulary reintroduced: {violations}"
        )

    def test_no_dataclass_field_uses_forbidden_vocabulary(self):
        checked = 0
        for name in _vocabulary_checked_modules():
            module = importlib.import_module(name)
            for _, obj in vars(module).items():
                if isinstance(obj, type) and dataclasses.is_dataclass(obj):
                    fields = {f.name.lower() for f in dataclasses.fields(obj)}
                    with self.subTest(dataclass=obj.__name__):
                        self.assertEqual(fields & FORBIDDEN_IDENTIFIERS, set())
                    checked += 1
        self.assertGreater(checked, 20)

    def test_advisory_components_are_exactly_the_three_agents(self):
        self.assertEqual(
            {c.value for c in components.ADVISORY_COMPONENTS},
            {"INTENT_AGENT", "XAPP_AGENT", "EVIDENCE_COORDINATOR"},
        )
        self.assertEqual(
            components.AUTHORITATIVE_COMPONENTS,
            frozenset({components.ComponentId.ASSURANCE_KERNEL}),
        )


if __name__ == "__main__":
    unittest.main()
