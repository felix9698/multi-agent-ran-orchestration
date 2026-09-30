"""Run one Gate 3 PIN_TO_CELL OTA episode through the Cockpit's Kernel path.

The order is the whole point, and every step is either an observation or a
Kernel decision:

1. read the deployment's committed identity (binding, integration values,
   capability manifest) and refuse if a digest moved;
2. observe the UE -- identity, AMF, and the cell it is on right now -- from the
   live KPM indication stream;
3. withdraw any *non-successful* A1-P policy still holding that UE's scope, so
   the producer's one-policy-per-scope rule does not fence this attempt;
4. wire the live vertical path and hand it to
   ``gui.operator.sources.kernel_live.KernelSubmissionSession`` in ``LIVE``
   mode -- the same submission path the finished console uses;
5. type the sentence, take the confirmation over the frozen contract instance's
   content hash, and drive the Kernel's trial loop to a terminal state;
6. write the evidence, including the traces that say the effect was physical.

Nothing here decides a verdict, a closure or a terminal.  Steps 1-3 are Lab
Setup reads and one A1 withdrawal of this run's own predecessors; everything
from step 4 is the Kernel's.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from assurance.advisors.grammar import IntentParseError, parse_utterance
from assurance.contracts.live_binding import load_assurance_live_binding
from assurance.live.pin_to_cell_driver import (
    PIN_TO_CELL_GRAMMAR,
    LiveDriverError,
    LivePinToCellDeployment,
    LivePinToCellRuntime,
    LiveTiming,
    build_live_pin_to_cell_runtime,
)
from assurance.live import KpmUeAttributionReader
from assurance.live.objective_runtime import build_live_objective_runtime

from gui.operator.sources.kernel_live import MODE_LIVE, KernelSubmissionSession

from oran.rapp.headless import load_integration_values

from tools.g5ota.objective_live import (
    bundle_contracts,
    bundle_direction,
    family_grammar,
    family_utterance,
    live_scope,
    objective_dry_run_plan,
)

from tools.g3ota.composition import (
    A1P_OBJECTIVE_KIND,
    KpmTail,
    LivePolicyBuilder,
    RecordingPolicyPort,
    WallClockPorts,
    build_policy_type_discovery,
    build_r1_policy_port,
    clear_ue_scope,
    kpm_slot_occupancy,
    live_topology,
    observe_live_ue,
    producer_episode,
    scope_occupants,
)

DEFAULT_BINDING = "deployment/assurance-live-binding.1.0.0.json"
DEFAULT_VALUES = (
    "/opt/ran-lab/controller/oran-deploy/session-20260819/deployment/integration-values.json"
)
DEFAULT_CAPABILITY = (
    "/opt/ran-lab/controller/oran-deploy/session-20260819/deployment/capability.json"
)
DEFAULT_PRODUCER_DB = (
    "/opt/ran-lab/controller/oran-deploy/session-20260819/state/a1p/a1p-producer.sqlite3"
)
DEFAULT_EVIDENCE = "docs/integration/evidence"

def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _pinned_capability(path: str | Path, expected_sha256: Optional[str]) -> Dict[str, Any]:
    import hashlib

    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise LiveDriverError(
            f"capability manifest digest moved: {digest} != {expected_sha256}"
        )
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


def _sample_record(sample: Any) -> Dict[str, Any]:
    return {
        "observedAt": sample.observed_at,
        "counterId": sample.counter_id,
        "value": sample.value.value,
        "unit": sample.value.unit,
        "cadenceMs": sample.cadence_ms,
        "clockHealth": sample.clock_health.value,
        "scope": dict(sample.scope_snapshot),
        "sequence": sample.sequence,
        "traceHash": sample.trace_hash,
        "missingIntervals": [
            interval.to_canonical_dict() for interval in sample.missing_intervals
        ],
    }


def _view_record(view: Any) -> Dict[str, Any]:
    return {
        "stage": view.stage,
        "mode": view.mode,
        "trialId": view.trial_id,
        "pollCount": view.poll_count,
        "maxPolls": view.max_polls,
        "axes": {
            "executionValidity": view.axes.execution_validity,
            "measurementSufficiency": view.axes.measurement_sufficiency,
            "predicateVerdicts": dict(view.axes.predicate_verdicts),
            "trialOutcome": view.axes.trial_outcome,
            "holdComplete": view.axes.hold_complete,
        },
        "settlement": None
        if view.settlement is None
        else {
            "trialState": view.settlement.trial_state,
            "outcome": view.settlement.outcome,
            "stopReason": view.settlement.stop_reason,
            "evidenceStatus": view.settlement.evidence_status,
            "caseTermination": view.settlement.case_termination,
            "harmCharges": list(view.settlement.harm_charges),
            "detail": view.settlement.detail,
            "gatewayOperations": [list(item) for item in view.settlement.gateway_operations],
        },
        "refusal": view.refusal,
        "refusalDetail": view.refusal_detail,
    }


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--binding", default=DEFAULT_BINDING)
    parser.add_argument("--integration-values", default=DEFAULT_VALUES)
    parser.add_argument("--capability", default=DEFAULT_CAPABILITY)
    parser.add_argument("--producer-db", default=DEFAULT_PRODUCER_DB)
    parser.add_argument("--evidence-dir", default=DEFAULT_EVIDENCE)
    parser.add_argument("--target-nci", type=int, default=None,
                        help="cell to pin to; defaults to the one the UE is not on")
    parser.add_argument("--state-dir", default=None,
                        help="where the R1 consumer keeps its durable state")
    parser.add_argument("--dry-preflight", action="store_true",
                        help="observe and report readiness, submit nothing")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "build and validate the named objective's registry-to-contract drive "
            "plan without observing the lab or constructing a transport; requires "
            "--objective"
        ),
    )
    parser.add_argument("--objective", default=None,
                        help=("objective family to submit (Gate 5).  Omitted, this runs "
                              "the Gate 3 UeCellSteeringPinToCell regression case.  Named, "
                              "the family must be submittable in the Gate 4 registry AND "
                              "declare a policy type; a family the registry blocks is "
                              "refused here with the recorded reasons rather than "
                              "translated into some other objective kind"))
    parser.add_argument("--objective-readiness", action="store_true",
                        help="report every objective family's submission readiness and exit")
    parser.add_argument("--withdraw-verified-scope", action="store_true",
                        help=("also withdraw a policy whose episode already reached "
                              "APPLIED_VERIFIED but which still occupies this UE's "
                              "A1-P scope; the producer admits one policy per scope "
                              "and counts an expired one, so a second episode on the "
                              "same UE is otherwise refused HTTP 409. The complete "
                              "producer rows are archived to the evidence directory "
                              "before anything is withdrawn"))
    parser.add_argument("--utterance",
                        help=("the sentence the Operator actually typed. It is read by the\n"
                              "same deterministic grammar as the sentence this runner would\n"
                              "have composed, and is accepted only when the two readings are\n"
                              "identical -- so an Operator can put their own words in the\n"
                              "evidence without being able to name one cell in the words and\n"
                              "another in the contract"))
    args = parser.parse_args(argv)

    if args.objective_readiness:
        from tools.g3ota.objectives import submission_readiness
        from assurance.objectives import FAMILY_MODULES

        json.dump(
            {"at": WallClockPorts.now(),
             "families": [submission_readiness(f) for f in sorted(FAMILY_MODULES)]},
            sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    stamp = _stamp()
    binding = load_assurance_live_binding(args.binding)
    if args.objective is not None:
        # Refuse before anything is observed, addressed or submitted.  The
        # registry's blocking reasons are a recorded judgement; a runner that
        # could argue past them would make the record decorative.
        from tools.g3ota.objectives import ObjectiveRefused, resolve_submittable_family

        try:
            resolve_submittable_family(args.objective)
        except ObjectiveRefused as exc:
            json.dump({"objective": args.objective, "refused": str(exc)},
                      sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return 4
    if args.dry_run:
        if args.objective is None:
            parser.error("--dry-run requires --objective")
        from assurance.objectives import FAMILY_MODULES

        cells = tuple(int(cell) for cell in binding.cells)
        if len(cells) != 2:
            raise LiveDriverError(
                "objective dry-run requires the binding to name exactly two cells"
            )
        target_nci = int(args.target_nci) if args.target_nci is not None else cells[1]
        home_cells = tuple(cell for cell in cells if cell != target_nci)
        if target_nci not in cells or len(home_cells) != 1:
            raise LiveDriverError(
                "objective dry-run target must be one of the binding's two cells"
            )
        family_module = FAMILY_MODULES[args.objective]()
        bundle = family_module.contract_bundle(
            scope=live_scope(
                amf_ue_ngap_id=0,
                home_nci=home_cells[0],
                target_nci=target_nci,
            ),
            deployment_binding=binding.r1.deployment,
        )
        direction = bundle_direction(bundle)
        if direction.baseline != home_cells[0]:
            raise LiveDriverError(
                f"{args.objective} as frozen moves from {direction.baseline}, "
                f"not from the requested home cell {home_cells[0]}"
            )
        if direction.target != target_nci:
            raise LiveDriverError(
                f"{args.objective} as frozen moves to {direction.target}, not "
                f"to the requested {target_nci}"
            )
        json.dump(
            objective_dry_run_plan(family_module, bundle),
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        return 0
    values = load_integration_values(args.integration_values)
    capability = _pinned_capability(
        args.capability, values.get("backend.capabilityManifestSha256")
    )
    topology = live_topology(binding, capability)
    timing = LiveTiming()

    tail = KpmTail(binding.kpm_jsonl_path)
    reader = KpmUeAttributionReader(
        read_new_lines=tail.read_new_lines, topology=topology
    )
    identity = observe_live_ue(
        reader,
        now=WallClockPorts.now,
        sleep_ms=WallClockPorts.sleep_ms,
        freshness_ms=timing.freshness_bound_ms,
    )
    cells = sorted(set(topology.nb_id_to_nci.values()))
    target_nci = args.target_nci
    if target_nci is None:
        others = [cell for cell in cells if cell != identity.serving_nci]
        if len(others) != 1:
            raise LiveDriverError(
                "the deployment does not name exactly one cell to steer to; "
                "state it with --target-nci"
            )
        target_nci = others[0]

    preflight = {
        "at": WallClockPorts.now(),
        "bindingId": binding.binding_id,
        "sourceDigests": dict(binding.source_digests),
        "observedUe": {
            "amfUeNgapId": identity.amf_ue_ngap_id,
            "guAmI": dict(identity.gu_ami),
            "servingCell": identity.serving_nci,
            "e2Node": identity.e2_node,
            "connectionEpoch": identity.connection_epoch,
            "observedAt": identity.observed_at,
        },
        "targetCell": target_nci,
        "kpmSlots": kpm_slot_occupancy(binding.kpm_jsonl_path),
        "scopeOccupants": scope_occupants(
            args.producer_db, amf_ue_ngap_id=identity.amf_ue_ngap_id
        ),
    }
    if args.dry_preflight:
        json.dump(preflight, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    directory = Path(args.evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = ("GATE3-OTA" if args.objective is None
              else f"GATE5-OTA-{args.objective}")
    archive_path = directory / f"{prefix}-{stamp}-scope-archive.json"

    def archive(records: Sequence[Mapping[str, Any]]) -> None:
        archive_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "gate3-ota-scope-archive/1.0.0",
                    "at": WallClockPorts.now(),
                    "amfUeNgapId": identity.amf_ue_ngap_id,
                    "producerDatabase": str(args.producer_db),
                    "note": (
                        "The A1-P producer's own rows for every policy occupying this "
                        "UE's scope, complete and verbatim, written before any of them "
                        "was withdrawn. A withdrawal that freed the scope moved this "
                        "record; it did not destroy it."
                    ),
                    "records": [dict(record) for record in records],
                },
                indent=2, sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    cleared = clear_ue_scope(
        binding,
        state_database=args.producer_db,
        amf_ue_ngap_id=identity.amf_ue_ngap_id,
        policy_type_id=binding.r1.policy_type_id,
        withdraw_verified=args.withdraw_verified_scope,
        archive=archive,
    )

    deployment = LivePinToCellDeployment(
        home_nci=identity.serving_nci,
        target_nci=int(target_nci),
        ue_scope_id=str(identity.amf_ue_ngap_id),
        topology=topology,
        r1_deployment=binding.r1.deployment,
    )
    state_dir = Path(args.state_dir or f"/tmp/g3ota-{stamp}")
    state_dir.mkdir(parents=True, exist_ok=True)
    policy_port = RecordingPolicyPort(
        build_r1_policy_port(values, state_path=state_dir / "r1-state.json")
    )
    discovery = build_policy_type_discovery(
        policy_port,
        policy_type_id=binding.r1.policy_type_id,
        capability_manifest=capability,
    )
    case_id = f"case/live-pin-to-cell:{stamp}"
    builders: List[LivePolicyBuilder] = []

    def policy_builder_factory(kernel: Any, contracts: Mapping[str, Any],
                               *, objective_kind: Optional[str] = None) -> Any:
        # The wire kind travels with the family, not with the runner: the
        # frozen mapping (ASSURANCE_FAMILY_TO_WIRE_KIND v1.2.0) already says
        # which A1-P objective kind each family is expressed as, and passing it
        # here keeps the default -- the Gate 3 regression case -- untouched.
        extra = {} if objective_kind is None else {"objective_kind": objective_kind}
        builder = LivePolicyBuilder(
            kernel=kernel,
            contracts=contracts,
            deployment=deployment,
            identity=identity,
            case_id=case_id,
            policy_type_discovery=discovery,
            capability_manifest=capability,
            **extra,
        )
        builders.append(builder)
        return builder

    if args.objective is None:
        runtime: Any = build_live_pin_to_cell_runtime(
            deployment=deployment,
            timing=timing,
            binding=binding,
            policy_port=policy_port,
            policy_builder_factory=policy_builder_factory,
            reader=reader,
            identity=identity,
            now=WallClockPorts.now,
            monotonic_ms=WallClockPorts.monotonic_ms,
            sleep_ms=WallClockPorts.sleep_ms,
            case_id=case_id,
        )
        grammar: Mapping[str, Any] = PIN_TO_CELL_GRAMMAR
        utterance = runtime.utterance()
        settle_ms = timing.cadence_ms
    else:
        # The family's own frozen bundle drives the same VerticalPath.  Nothing
        # about the objective is decided here: the registry already refused or
        # allowed it above, the bundle states the geometry and the candidate,
        # and the sentence below is one an Operator could have typed.
        from assurance.objectives import FAMILY_MODULES

        family_module = FAMILY_MODULES[args.objective]()
        scope = live_scope(
            amf_ue_ngap_id=identity.amf_ue_ngap_id,
            home_nci=identity.serving_nci,
            target_nci=int(target_nci),
        )
        runtime = build_live_objective_runtime(
            family_module=family_module,
            scope=scope,
            binding=binding,
            policy_port=policy_port,
            policy_builder_factory=lambda kernel, bundle: policy_builder_factory(
                kernel, bundle_contracts(bundle),
                objective_kind=A1P_OBJECTIVE_KIND[args.objective]),
            reader=reader,
            identity=identity,
            now=WallClockPorts.now,
            monotonic_ms=WallClockPorts.monotonic_ms,
            sleep_ms=WallClockPorts.sleep_ms,
            case_id=case_id,
        )
        # What the bundle actually expresses, which is not always what was
        # asked for: TrafficSteeringPreference fixes its cell pair in module
        # constants and ignores the scope's homeServingCell/targetServingCell.
        # The sentence is written from the bundle so the Operator's words and
        # the frozen contract can never name different cells, and a run whose
        # observed baseline is not the one the bundle names is refused here --
        # before the equipment is addressed -- rather than being discovered as
        # a REJECTED_CONFIG_MISMATCH after a permit has already been issued.
        direction = bundle_direction(runtime.bundle)
        if direction.baseline != int(identity.serving_nci):
            raise LiveDriverError(
                f"{args.objective} as frozen moves {direction.baseline} -> "
                f"{direction.target}, but the UE is observed on "
                f"{identity.serving_nci}. Submitting would name one cell in the "
                "sentence and another in the contract; making the family "
                "directional is a family-contract change, not a runner's."
            )
        if direction.target != int(target_nci):
            raise LiveDriverError(
                f"{args.objective} as frozen moves to {direction.target}, not "
                f"to the requested {int(target_nci)}"
            )
        grammar = family_grammar(args.objective, runtime.bundle)
        utterance = family_utterance(
            args.objective, direction.target, str(identity.amf_ue_ngap_id))
        settle_ms = runtime.geometry.cadence_ms

    if args.utterance:
        utterance = operator_utterance(
            args.utterance, utterance, grammar=grammar, case_id=case_id)

    published: List[Dict[str, Any]] = []
    session = KernelSubmissionSession(
        path=runtime.path,
        cell_id=runtime.cell_id,
        objective_registry=grammar,
        mode=MODE_LIVE,
        publish=lambda _topic, view: published.append(_view_record(view)),
        settle_ms=settle_ms,
    )

    preview = session.draft(utterance)
    instance = session.confirm(preview)
    view = session.start(instance)

    evidence = _evidence(
        stamp=stamp,
        args=args,
        binding=binding,
        runtime=runtime,
        session=session,
        view=view,
        preview=preview,
        instance=instance,
        utterance=utterance,
        preflight=preflight,
        cleared=cleared,
        policy_port=policy_port,
        archive_path=str(archive_path) if archive_path.is_file() else None,
        builders=builders,
        published=published,
    )
    run_path = directory / f"{prefix}-{stamp}-run.json"
    run_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    events_path = directory / f"{prefix}-{stamp}-events.jsonl"
    events_path.write_text(
        "".join(
            json.dumps(envelope.to_canonical_dict(), sort_keys=True,
                       separators=(",", ":")) + "\n"
            for envelope in runtime.event_store.iterate()
        ),
        encoding="utf-8",
    )
    print(json.dumps(
        {
            "terminalState": evidence["settlement"]["trialState"],
            "outcome": evidence["settlement"]["outcome"],
            "evidenceStatus": evidence["settlement"]["evidenceStatus"],
            "observedServingCell": evidence["afterEvidence"]["kpmSlots"]["ueAttribution"],
            "run": str(run_path),
            "events": str(events_path),
        },
        indent=2, sort_keys=True,
    ))
    return 0 if evidence["settlement"]["outcome"] == "SUCCESS" else 2



def operator_utterance(typed: str, generated: str, *,
                       grammar: Mapping[str, Any], case_id: str) -> str:
    """The Operator's own sentence, accepted only when it reads identically.

    ``--utterance`` exists so the sentence in the evidence is one a person
    typed rather than one this runner composed.  It must not become a way to
    say one thing and submit another, so the typed sentence is parsed by the
    *same* deterministic grammar and is accepted only when its whole reading --
    objective family, scope selector, every drafted bound, and every part the
    grammar could not serve -- equals the reading of the sentence the runner
    would otherwise have used.  Anything else is refused here, before the
    equipment is addressed.
    """
    source = f"operator-utterance:{case_id}"
    try:
        typed_reading = parse_utterance(
            typed, objective_registry=grammar, source_record=source)
    except IntentParseError as exc:
        raise LiveDriverError(
            f"the typed sentence is not recognised by the frozen grammar: {exc}"
        ) from None
    generated_reading = parse_utterance(
        generated, objective_registry=grammar, source_record=source)
    if typed_reading != generated_reading:
        raise LiveDriverError(
            "the typed sentence does not read as the contract this run would "
            f"submit.\n  typed:     {typed!r}\n    -> {typed_reading}\n"
            f"  this run:  {generated!r}\n    -> {generated_reading}")
    return typed


def _run_timing(runtime: Any) -> Dict[str, Any]:
    """The window geometry, wherever this runtime keeps it.

    The Gate 3 case carries a ``LiveTiming`` it was handed; a family runtime
    derives a ``BundleGeometry`` from its own measurement contracts and has no
    ``enforced_timeout_ms`` to report, because the bundle's watchdog states it
    rather than the runner.  Reporting the absent field as ``None`` says which
    of the two this run was.
    """
    source = getattr(runtime, "timing", None) or runtime.geometry
    # A multi-counter family geometry has no single counter_id; the per-counter
    # identities travel with each sample.  Report the id only when the run reads
    # exactly one counter, and None otherwise, so evidence assembly never trips
    # over the compatibility property that guards the one-counter surface.
    counters = getattr(source, "counters", None)
    if counters is not None:
        counter_id = counters[0].counter_id if len(counters) == 1 else None
    else:
        counter_id = getattr(source, "counter_id", None)
    return {
        "cadenceMs": source.cadence_ms,
        "windowWidthMs": source.window_width_ms,
        "holdMs": source.hold_ms,
        "freshnessBoundMs": source.freshness_bound_ms,
        "enforcedTimeoutMs": getattr(source, "enforced_timeout_ms", None),
        "counterId": counter_id,
    }


def _evidence(**kwargs: Any) -> Dict[str, Any]:
    runtime: Any = kwargs["runtime"]
    view = kwargs["view"]
    binding = kwargs["binding"]
    args = kwargs["args"]
    bodies = [body for builder in kwargs["builders"] for body in builder.bodies]
    policy_ids = sorted(set(runtime.adapter.bindings().values()))
    settlement = kwargs["published"][-1]["settlement"] if kwargs["published"] else None
    if view.settlement is not None:
        settlement = _view_record(view)["settlement"]
    return {
        "schemaVersion": "gate3-ota-evidence/1.0.0",
        "at": kwargs["stamp"],
        "objectiveFamily": getattr(runtime, "family", None),
        "objectiveWireKind": (
            None if getattr(runtime, "family", None) is None
            else A1P_OBJECTIVE_KIND.get(runtime.family)),
        "bindingId": binding.binding_id,
        "caseId": runtime.case_id,
        "utterance": kwargs["utterance"],
        "preflight": kwargs["preflight"],
        "scopeCleared": kwargs["cleared"],
        "scopeArchive": kwargs.get("archive_path"),
        "contract": {
            "epochHash": runtime.path.epoch_hash(),
            "candidateId": runtime.candidate_id(),
            "contentHash": kwargs["preview"].content_hash(),
            # A property on the confirmed instance, a method on the preview:
            # the instance is a settled fact and the preview is still being
            # read, and the two spellings say so.
            "confirmedContentHash": kwargs["instance"].content_hash,
            "confirmationAction": kwargs["instance"].confirmation.action.value,
            "confirmedAt": kwargs["instance"].confirmation.timestamp,
            "parameters": dict(kwargs["preview"].parameters),
            "timing": _run_timing(runtime),
        },
        "axes": _view_record(view)["axes"],
        "settlement": settlement,
        "terminalStateHash": runtime.path.terminal_state_hash(),
        "readbacks": [dict(entry) for entry in (
            runtime.readback.reads if runtime.readback is not None else ())],
        "policyBodies": bodies,
        "policyIds": policy_ids,
        "samples": [_sample_record(sample) for sample in runtime.collector.emitted()],
        "gatewayLog": [[kind, outcome.value] for kind, outcome in runtime.path.gateway_log],
        "views": kwargs["published"],
        "transportCalls": getattr(kwargs.get("policy_port"), "calls", []),
        "afterEvidence": {
            "kpmSlots": kpm_slot_occupancy(binding.kpm_jsonl_path),
            "producerEpisode": producer_episode(args.producer_db, policy_ids),
            "readerRejectedRecords": runtime.reader.rejected_records,
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(argv)
    except Exception as exc:  # noqa: BLE001 - the operator needs the reason
        import traceback

        json.dump(
            {
                "error": type(exc).__name__,
                "detail": str(exc),
                # The whole trace, not a one-line summary: a live episode that
                # already reached the equipment cannot be re-run for free, so
                # the one report the operator gets has to be diagnosable.
                "traceback": traceback.format_exc().splitlines(),
            },
            sys.stdout, indent=2, sort_keys=True,
        )
        sys.stdout.write("\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
