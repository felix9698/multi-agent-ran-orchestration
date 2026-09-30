"""Drive one Campaign 5 family over the air, through A1, from a shell.

All six xApp actions are already wired ``rApp -> A1 -> A1-P producer -> xApp ->
E2SM-RC`` (:mod:`tools.campaign5.route`, one :class:`R1Adapter` per family
behind the Write Gateway).  What was missing was a way to *run* one: the only
live entry point was the Cockpit's steering objective, so exercising a cap or a
scheduler weight over the air meant firing the xApp directly -- off the official
path, and therefore not evidence of it.

This is that entry point.  One family, one value, one hold, one reverse::

    Kernel-shaped permit -> ActuationPlan(adapter="r1-<family>")
      -> TokenBoundWriteGateway prepare/ready/commit
      -> R1Adapter -> policy builder -> live R1 port -> A1-P producer
      -> xApp -> E2SM-RC -> gNB
      -> corroborated readback (producer status AND the KPM counter)
      -> hold -> reverse (DELETE, baseline read back) -> finalize

**What issues the permits.**  The Assurance Kernel issues a token only for a
trial of an admitted case, and a case is an *objective* -- a target, a frozen
candidate catalog, predicates, a harm contract.  This driver runs no objective;
it runs one named action at one named value because an operator asked for it.
So the permits are the driver's, issued by :class:`PermitIssuer` under the same
discipline the Kernel applies (monotonic fence, bounded lease, expected
configuration digest, derived idempotency key) and recorded as such.  Every run
document says ``permitIssuer: "campaign5-live-run"`` and carries **no** Kernel
decision axes: this path has no evaluator, and printing ``VALID`` from a driver
that never ran one would be the fabrication the whole system is built to refuse.
What it does report is what it observed -- the gateway's answers, the
corroborated readback, the write counts and whether the baseline came back.

**What can actually verify today.**  The write half is identical for all four
families.  The *readback* half is not:

``cap`` / ``priority``
    one scalar leaf, UE-scoped, and the counter is on the KPM stream.  These
    can reach a corroborated VERIFIED.
``mcs``
    two value leaves (``minDlMcs``/``maxDlMcs``) against one scalar counter
    record, so the JSONL cannot say which leaf it carries.  The reader answers
    ``None`` and the run is ``UNKNOWN`` -- fail-closed, and honest.
``power``
    :class:`~oran.campaign5.readback.PowerReadbackUnavailable`, pinned by
    :mod:`tools.campaign5.route` itself: the three-component counter loses its
    labels in JSONL.

That is a property of the deployed wire, not of this driver, and it is reported
rather than worked around.  A run that cannot corroborate its effect reverses
and says so.

Safety is the Cockpit's, unchanged: a stale attribution refuses before anything
is written, a producer refusal is ``REJECTED`` with zero writes, an
acknowledgement is never an effect, one policy per semantic scope, and the
scope is held until an independent readback shows the baseline back.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.gateway.journal import JsonFileTransactionJournal
from assurance.gateway.plan import config_hash
from assurance.gateway.r1_binding_journal import JsonFileR1BindingJournal
from assurance.gateway.r1_operation_journal import JsonlR1OperationJournal
from assurance.gateway.token import KernelToken, TokenKind
from assurance.gateway.write_gateway import GatewayOutcome

from oran.campaign5.families import CAMPAIGN5_FAMILIES, Campaign5Family
from oran.campaign5.producer import A1Error

from tools.campaign5.route import (
    build_official_adapter, build_official_gateway, official_plan,
)
from tools.g3ota.composition import (
    KpmTail, RecordingPolicyPort, WallClockPorts, build_r1_policy_port,
    live_topology,
)
from tools.liveconsole.build import (
    CapReadbackAttribution, _FanOutTail, observe_selected_ue,
)
from tools.campaign5.value_flags import FAMILY_VALUE_FLAGS
from tools.liveconsole.profile import LiveConsoleError, load_live_deployment

from assurance.collector.live import build_live_collectors
from assurance.core.timebase import format_utc, parse_utc
from assurance.live.pin_to_cell_driver import KpmUeAttributionReader, kpm_node_nb_id

__all__ = [
    "Campaign5LiveError",
    "FAMILY_VALUE_FLAGS",
    "KpmFamilyConfigReader",
    "PermitIssuer",
    "RUN_SCHEMA_VERSION",
    "build_parser",
    "main",
    "run_live_action",
]

#: The evidence document this driver writes.
RUN_SCHEMA_VERSION = "campaign5-a1-run/1.0.0"

#: How long a permit is valid for.  Short on purpose: a permit outliving the
#: operation it authorises is a permit a delayed retry could use.
LEASE_SECONDS = 60

#: How fresh a KPM configuration indication has to be to corroborate a write.
FRESHNESS_BOUND_MS = 8000

#: Re-exported from :mod:`tools.campaign5.value_flags`, which is transport-free
#: so ``main.py`` can build its parser without loading this module.


class Campaign5LiveError(RuntimeError):
    """This run cannot proceed, and the message says what is missing."""


# --------------------------------------------------------------------------- #
# Permits
# --------------------------------------------------------------------------- #


class PermitIssuer:
    """One transaction's permits, under the Kernel's discipline.

    Not the Kernel: see the module docstring.  What it does keep is every rule
    the gateway checks a permit against, because those are the rules that make
    a permit a safety device rather than a label -- a monotonic fence, a lease
    that expires, the configuration digest the gateway must observe before it
    acts, and an idempotency key derived from the operation rather than chosen.

    The fence advances only when the caller says a *new* attempt begins, so a
    prepare/ready/commit trio shares one fence and a retry after a recovery
    fences the earlier one out.
    """

    def __init__(self, *, transaction_id: str, trial_id: str,
                 now: Callable[[], str],
                 lease_seconds: int = LEASE_SECONDS) -> None:
        self.transaction_id = transaction_id
        self.trial_id = trial_id
        self._now = now
        self._lease_seconds = int(lease_seconds)
        self._fence = 0
        self._sequence = 0
        #: Every permit issued, for the evidence record.
        self.issued: List[Dict[str, Any]] = []

    @property
    def fencing_token(self) -> int:
        return self._fence

    def advance_fence(self) -> int:
        """Begin a new attempt.  The previous fence is now stale."""
        self._fence += 1
        return self._fence

    def __call__(self, kind: str, expected_config_hash: str,
                 sequence: Optional[int] = None) -> KernelToken:
        issued_at = self._now()
        if sequence is None:
            sequence = self._sequence
        self._sequence = max(self._sequence, int(sequence)) + 1
        token = KernelToken(
            token_kind=TokenKind[kind],
            transaction_id=self.transaction_id,
            trial_id=self.trial_id,
            fencing_token=self._fence,
            command_sequence=int(sequence),
            lease_expiry=_plus_seconds(issued_at, self._lease_seconds),
            expected_config_hash=expected_config_hash,
            idempotency_key=(f"{self.transaction_id}:{kind}:"
                             f"{self._fence}:{sequence}"),
            issued_at=issued_at,
        )
        self.issued.append({
            "tokenKind": kind, "fencingToken": self._fence,
            "commandSequence": int(sequence), "issuedAt": issued_at,
            "leaseExpiry": token.lease_expiry,
            "expectedConfigHash": expected_config_hash,
        })
        return token


def _plus_seconds(instant: str, seconds: int) -> str:
    """*instant* moved on, in the one canonical form the token validates."""
    return format_utc(parse_utc(instant) + timedelta(seconds=seconds))


# --------------------------------------------------------------------------- #
# The corroborating reader
# --------------------------------------------------------------------------- #


class KpmFamilyConfigReader:
    """The independent half of the corroborated readback, per family.

    :class:`~tools.liveconsole.build.KpmCapConfigReader` generalized off the
    cap: the same refusal discipline, over whichever counter the family
    declares, and answering the family's own value leaves.

    An indication must be **the same identity** the attribution captured before
    the write -- the same UE on the same node in the same E2 connection epoch
    for a UE-scoped family, the same node for a cell-scoped one.  A same-UE
    record from another cell or another association does not weakly verify the
    write; it verifies something else, and is refused.

    A family whose value is more than one leaf is refused outright: the JSONL
    record carries one scalar per counter and cannot say which leaf it is.
    Answering ``None`` there makes the run ``UNKNOWN``, which is the honest
    outcome and the one an operator can act on -- guessing a leaf would make
    every mcs run look verified.
    """

    def __init__(self, tail: Any, adapter: Any, *, family: Campaign5Family,
                 now: Callable[[], str], freshness_bound_ms: int,
                 expected: CapReadbackAttribution) -> None:
        self._tail = tail
        self._adapter = adapter
        self._family = family
        self._now = now
        self._freshness_bound_ms = int(freshness_bound_ms)
        self._expected = expected
        self._latest: Dict[Tuple[str, str, str], Tuple[str, float]] = {}
        self._pinned_epoch: Optional[str] = None
        self._reassociated = False
        #: Every answer, for the evidence record.  Display and audit only.
        self.reads: List[Dict[str, Any]] = []

    @property
    def expected(self) -> CapReadbackAttribution:
        return self._expected

    @property
    def single_leaf(self) -> Optional[str]:
        """The one value leaf this reader can fill, or ``None``."""
        fields = self._family.value_fields
        return fields[0] if len(fields) == 1 else None

    def _cell_observation(self) -> Optional[Tuple[str, float]]:
        """A cell-scoped counter (power, 2026-09-25): the record carries ``e2_node`` and
        ``connection_epoch`` but no UE, and the topology names the node by its nb id.  Match
        the node by nb id, and pin the association the first answer came from -- a gNB
        restart moves the epoch, and a record from the new association describes a
        scheduler this run never wrote to."""
        wanted = str(self._expected.e2_node)
        rows = [(epoch, observed) for (_ue, node, epoch), observed in self._latest.items()
                if str(kpm_node_nb_id(node)) == wanted or node == wanted]
        if not rows:
            return None
        newest = max(rows, key=lambda row: (row[1][0], int(row[0]) if str(row[0]).isdigit() else -1))
        if self._pinned_epoch is None:
            self._pinned_epoch = newest[0]
        if self._reassociated or newest[0] != self._pinned_epoch:
            # Latched: a later record from the old epoch does not make it current again.
            self._reassociated = True
            # The node re-associated: nothing cached from the pinned epoch describes the
            # scheduler now running (Codex review 2026-09-25 #6).
            self._note("EPOCH_CHANGED", pinned=self._pinned_epoch, newest=newest[0])
            return None
        return newest[1]

    def close(self) -> None:
        detach = getattr(self._tail, "detach", None)
        if callable(detach):
            detach()

    def _note(self, outcome: str, **detail: Any) -> Dict[str, Any]:
        record = {"outcome": outcome, "at": self._now(),
                  "counter": self._family.readback_counter,
                  "expected": self._expected.to_record(), **detail}
        self.reads.append(record)
        return record

    def _absorb(self) -> None:
        lines = self._tail()
        if not lines:
            return
        for sample in self._adapter.parse_lines(lines).samples:
            if sample.counter_id != self._family.readback_counter:
                continue
            scope = sample.scope_snapshot
            ue = str(scope.get("amf_ue_ngap_id", ""))
            node = str(scope.get("e2_node", ""))
            epoch = str(scope.get("connection_epoch", ""))
            if not node or not epoch:
                # A record that does not say which node and which association
                # it describes cannot verify anything.
                continue
            key = (ue, node, epoch)
            previous = self._latest.get(key)
            if previous is None or sample.observed_at >= previous[0]:
                self._latest[key] = (sample.observed_at, float(sample.value.value))

    def read(self, counter_name: str, scope: Mapping[str, Any]
             ) -> Optional[Mapping[str, Any]]:
        if counter_name != self._family.readback_counter:
            return None
        leaf = self.single_leaf
        if leaf is None:
            self._note("MULTI_LEAF_COUNTER_UNSUPPORTED",
                       valueFields=list(self._family.value_fields))
            return None
        self._absorb()
        if self._family.scope_kind == "UE":
            named = str(scope.get("ueId") or "")
            # 2026-09-18: 계획이 UE 를 **역할 이름**("ue1")으로 부르면 여기서 숫자 amf id 와
            # 비교되어 항상 어긋난다.  같은 병을 `liveconsole/build.py` 의 UE 리더(793)가
            # 이미 `role_labelled` 로 막고 있었고, 셀 리더(945)는 오늘 같은 방식으로 고쳤다
            # (거기서는 DU 지역 이름 "NRCellDU-1" 과 NCI 를 비교해 되읽기가 21/21 실패했다).
            # 이 경로(`--live-action`)는 지금 판 사슬에 없지만 형태가 같아 함께 맞춘다.
            role_labelled = bool(named) and not named.isdigit()
            if named != str(self._expected.amf_ue_ngap_id) and not role_labelled:
                self._note("SCOPE_MISMATCH", ueId=named)
                return None
        if self._family.scope_kind == "UE":
            observed = self._latest.get(self._expected.key)
        else:
            observed = self._cell_observation()
        if observed is None:
            # Saying *why* matters: "the counter is not in the stream" and
            # "the counter is there, on another node or another association"
            # need different actions and look identical from the outside.
            elsewhere = sorted(
                (node, epoch) for identifier, node, epoch in self._latest
                if identifier == str(self._expected.amf_ue_ngap_id))
            self._note("ATTRIBUTION_MISMATCH" if elsewhere else "COUNTER_ABSENT",
                       observedElsewhere=[{"e2NodeId": node,
                                           "connectionEpoch": epoch}
                                          for node, epoch in elsewhere])
            return None
        age_ms = abs(
            (parse_utc(self._now()) - parse_utc(observed[0])).total_seconds() * 1000)
        if age_ms > self._freshness_bound_ms:
            self._note("STALE", ageMs=int(age_ms), observedAt=observed[0])
            return None
        value = int(observed[1]) if self._family.scope_kind == "UE" else float(observed[1])
        self._note("OBSERVED", value=value, observedAt=observed[0])
        return {leaf: value}


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


@dataclass
class LiveActionRun:
    """Everything one run observed.  Nothing here is decided, only recorded."""

    family: Campaign5Family
    scope: Mapping[str, Any]
    baseline: Mapping[str, Any]
    target: Mapping[str, Any]
    stamp: str
    gateway_operations: List[List[str]] = field(default_factory=list)
    outcome: str = "NOT_RUN"
    detail: str = ""
    policy_ids: List[str] = field(default_factory=list)
    refusal: Optional[str] = None
    applied_verified: bool = False
    restored_verified: bool = False

    def record(self, kind: str, result: Any) -> Any:
        self.gateway_operations.append([kind, result.outcome.value])
        return result


def _stamp(now: Callable[[], str]) -> str:
    return parse_utc(now()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _validity(now: Callable[[], str], hold_s: int) -> Mapping[str, str]:
    """The policy's own validity window: the hold plus the lease, no more.

    A policy that outlives the run that created it is a policy nobody is
    watching.  The producer expires it even if this process dies before the
    DELETE.
    """
    start = now()
    return {"notBefore": start,
            "notAfter": _plus_seconds(start, int(hold_s) + 2 * LEASE_SECONDS)}


def run_live_action(
    *,
    profile_path: str,
    family_key: str,
    values: Mapping[str, int],
    hold_s: int = 20,
    amf_ue_ngap_id: Optional[int] = None,
    cell_nci: Optional[int] = None,
    gnb_id: Optional[str] = None,
    baseline: Optional[Mapping[str, int]] = None,
    ports: Any = None,
    policy_port: Optional[Any] = None,
    read_new_lines: Optional[Callable[[], Sequence[str]]] = None,
    evidence_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Apply one family value over the air, hold it, reverse it, and report.

    ``policy_port`` and ``read_new_lines`` are the two injection seams a
    hermetic test uses.  Supplying either makes the run a *mock* composition and
    the evidence says so, exactly as the Cockpit's live root does: a run whose
    transport was handed to it did not reach any equipment.
    """
    family = CAMPAIGN5_FAMILIES.get(family_key)
    if family is None:
        raise Campaign5LiveError(
            f"{family_key!r} is not a Campaign 5 family; known: "
            + ", ".join(sorted(CAMPAIGN5_FAMILIES)))
    missing = [leaf for _flag, leaf in FAMILY_VALUE_FLAGS[family_key]
               if leaf not in values]
    if missing:
        raise Campaign5LiveError(f"{family_key}: no value given for {missing}")

    clock = ports or WallClockPorts
    injected = [name for name, value in (("policyPort", policy_port),
                                         ("kpmTail", read_new_lines))
                if value is not None]
    mode = "MOCK" if injected else "LIVE"

    deployment = load_live_deployment(profile_path)
    producer = deployment.action_producer
    if producer is None:
        raise Campaign5LiveError(
            f"{deployment.document_path} names no liveConsole.actionProducer; "
            "the Campaign 5 families are served by the in-repo producer, which "
            "is a different endpoint from the released steering one and has to "
            "be declared before anything can address it")
    bound = producer.for_action(family.catalog_action_id)
    if bound is None:
        raise Campaign5LiveError(
            f"the action producer serves no policy type for "
            f"{family.catalog_action_id}; declared: "
            + ", ".join(sorted(entry.action_id
                               for entry in producer.policy_types.values())))
    if bound.policy_type_id != family.policy_type_id:
        raise Campaign5LiveError(
            f"{family.key} is {family.policy_type_id} and the profile binds "
            f"{family.catalog_action_id} to {bound.policy_type_id}")

    stamp = _stamp(clock.now)
    topology = live_topology(deployment.binding, deployment.capability)
    tail_lines = (read_new_lines if read_new_lines is not None
                  else KpmTail(deployment.kpm_jsonl_path).read_new_lines)
    fan_out = _FanOutTail(tail_lines)
    kpm_adapter = build_live_collectors(deployment.binding)[1]
    # Both consumers are registered *before* anything drains.  The tail is a
    # byte offset fanned out to whoever is attached when a line arrives, so a
    # reader created after the attribution read would start on an empty queue
    # and never see the configuration indications that were already there.
    attribution_lines = fan_out.consumer()
    counter_lines = fan_out.consumer()

    scope, expected, observed_ue = _resolve_scope(
        family, deployment=deployment, topology=topology,
        read_new_lines=attribution_lines, clock=clock,
        amf_ue_ngap_id=amf_ue_ngap_id, cell_nci=cell_nci, gnb_id=gnb_id)

    state_dir = deployment.r1_state_dir / f"campaign5-{family.key}-{stamp}"
    state_dir.mkdir(parents=True, exist_ok=True)
    port = RecordingPolicyPort(
        policy_port if policy_port is not None
        else build_r1_policy_port(
            {**dict(deployment.values), "r1.apiRoot": producer.api_root},
            state_path=state_dir / f"{bound.adapter}-state.json"))

    counter_reader = KpmFamilyConfigReader(
        counter_lines, kpm_adapter, family=family, now=clock.now,
        freshness_bound_ms=FRESHNESS_BOUND_MS, expected=expected)
    binding_journal = JsonFileR1BindingJournal(
        state_dir / f"{bound.adapter}-bindings.json")
    operation_journal = JsonlR1OperationJournal(
        state_dir / f"{bound.adapter}-operations.jsonl")
    validity = _validity(clock.now, hold_s)
    adapter = build_official_adapter(
        family, policy_port=port,
        validity_provider=lambda command: dict(validity),
        kpm_reader=counter_reader,
        monotonic_ms=clock.monotonic_ms, sleep_ms=clock.sleep_ms,
        cadence_ms=deployment.binding.r1.cadence_ms,
        deadline_ms=deployment.binding.r1.deadline_ms,
        api_root=producer.api_root,
        near_rt_ric_id=deployment.binding.r1.near_rt_ric_id,
        # The *action producer's* capability, not the deployment's.  The
        # binding's manifest advertises the released steering producer; the
        # Campaign 5 types are served by a different process, and its
        # advertisement is the one the discovery gate must agree with.
        capability_manifest=None,
        binding_journal=binding_journal,
        operation_journal=operation_journal,
        # A producer that answered 400/404/409 has *decided*; the request
        # provably never reached the gNB.  A transport failure is deliberately
        # not in this tuple: a lost message cannot prove the write did not land.
        refusal_errors=(A1Error,),
        retain_binding_until_restore=True,
        clock=clock.now,
        cell_power_reader=True,
    )

    baseline_config = _baseline(family, counter_reader, baseline)
    plan = official_plan(family, scope=scope, baseline=baseline_config,
                         target=dict(values))
    transaction_id = f"campaign5:{family.key}:{stamp}"
    issuer = PermitIssuer(transaction_id=transaction_id,
                          trial_id=f"{transaction_id}:1", now=clock.now)
    gateway = build_official_gateway(
        family, adapter, safe_state=plan["baselineConfig"], clock=clock.now,
        journal=JsonFileTransactionJournal(
            state_dir / f"{bound.adapter}-transactions.json"))

    run = LiveActionRun(family=family, scope=scope, baseline=baseline_config,
                        target=dict(values), stamp=stamp)
    _drive(run, gateway=gateway, plan=plan, issuer=issuer, clock=clock,
           hold_s=hold_s, adapter=adapter)

    document = _run_document(
        run, deployment=deployment, producer=producer, bound=bound,
        mode=mode, injected=injected, scope=scope, expected=expected,
        observed_ue=observed_ue, port=port, adapter=adapter,
        counter_reader=counter_reader, issuer=issuer, validity=validity,
        hold_s=hold_s, transaction_id=transaction_id,
        binding_journal=binding_journal, state_dir=state_dir)
    written = _write_evidence(
        document, adapter,
        directory=Path(evidence_dir) if evidence_dir is not None
        else deployment.evidence_dir,
        family=family, stamp=stamp)
    counter_reader.close()
    document["evidence"] = written
    return document


def _resolve_scope(family, *, deployment, topology, read_new_lines, clock,
                   amf_ue_ngap_id, cell_nci, gnb_id):
    """The identity leaves this policy names, taken from the stream or refused.

    A UE-scoped family takes its UE from a *fresh* KPM attribution and nothing
    else: the AMF hands out a new ``amfUeNgapId`` on every registration, so a
    UE named from a stale record is a UE that may no longer exist, and a cap on
    it is a cap on whoever holds that id now.
    """
    if family.scope_kind == "UE":
        reader = KpmUeAttributionReader(
            read_new_lines=read_new_lines, topology=topology)
        identity = observe_selected_ue(
            reader, now=clock.now, sleep_ms=clock.sleep_ms,
            freshness_ms=FRESHNESS_BOUND_MS,
            amf_ue_ngap_id=(amf_ue_ngap_id
                            if amf_ue_ngap_id is not None
                            else deployment.requested_amf_ue_ngap_id))
        scope = {"cellId": str(identity.serving_nci),
                 "ueId": str(identity.amf_ue_ngap_id)}
        expected = CapReadbackAttribution(
            amf_ue_ngap_id=int(identity.amf_ue_ngap_id),
            serving_nci=int(identity.serving_nci),
            e2_node=identity.e2_node,
            connection_epoch=int(identity.connection_epoch))
        return scope, expected, {
            "amfUeNgapId": identity.amf_ue_ngap_id,
            "servingCell": identity.serving_nci,
            "e2Node": identity.e2_node,
            "connectionEpoch": identity.connection_epoch,
            "observedAt": identity.observed_at,
        }

    # Cell-scoped.  The cell is named or unambiguous, never picked: this
    # deployment's topology is the only thing that may say which cells exist.
    known = sorted(int(nci) for nci in topology.nb_id_to_nci.values())
    if cell_nci is None:
        if len(known) != 1:
            raise Campaign5LiveError(
                f"{family.key} is cell-scoped and this deployment advertises "
                f"{len(known)} cells ({known}); name one with --cell-nci")
        cell_nci = known[0]
    elif int(cell_nci) not in known:
        raise Campaign5LiveError(
            f"cell {cell_nci} is not in this deployment's topology ({known})")
    nb_id = next((node for node, nci in topology.nb_id_to_nci.items()
                  if int(nci) == int(cell_nci)), None)
    if "gnbId" in family.scope_fields and gnb_id is None and nb_id is None:
        raise Campaign5LiveError(
            f"{family.key} names a gnbId and neither --gnb-id nor the topology "
            "supplies one")
    scope = {"cellId": str(cell_nci)}
    if "gnbId" in family.scope_fields:
        scope["gnbId"] = str(gnb_id if gnb_id is not None else nb_id)
    # The attribution a cell-scoped readback would need is the E2 node *as the
    # KPM record spells it*, which the topology gives as a decimal nb id rather
    # than the record's node key.  Both cell-scoped families are unreadable on
    # this wire today anyway -- mcs has two value leaves against one scalar
    # record, power loses its component labels -- so this records what is known
    # and the reader refuses rather than matching on a guess.
    expected = CapReadbackAttribution(
        amf_ue_ngap_id=0, serving_nci=int(cell_nci),
        e2_node=str(nb_id if nb_id is not None else gnb_id or ""),
        connection_epoch=0)
    return scope, expected, None


def _baseline(family, counter_reader, given):
    """The configuration this run must restore, observed or stated.

    Observed first: the value the counter holds now is the only baseline a
    restore can be checked against.  ``--baseline`` is the operator's override
    for a family whose counter this deployment cannot read, and it is recorded
    as *stated* rather than observed so nobody later reads it as a measurement.
    """
    if given is not None:
        return dict(given)
    leaf = counter_reader.single_leaf
    if leaf is not None:
        observed = counter_reader.read(family.readback_counter,
                                       {"ueId": str(
                                           counter_reader.expected.amf_ue_ngap_id)})
        if observed is not None:
            return dict(observed)
    raise Campaign5LiveError(
        f"{family.key}: the configuration counter "
        f"{family.readback_counter} gave no reading, so there is no baseline "
        "to restore to. State it with --baseline, or fix the counter: a write "
        "with no known baseline is a write nobody can undo.")


def _drive(run, *, gateway, plan, issuer, clock, hold_s, adapter):
    """prepare -> ready -> commit -> hold -> reverse -> finalize."""
    baseline_hash = config_hash(plan["baselineConfig"])
    applied_hash = config_hash(
        {run.family.axis: dict(run.target)})

    prepare = run.record(
        "PREPARE", gateway.prepare(token=issuer("PREPARE", baseline_hash, 0),
                                   plan=dict(plan)))
    if prepare.outcome is not GatewayOutcome.ACKED:
        run.outcome = "REFUSED_AT_PREPARE"
        run.refusal = prepare.outcome.value
        run.detail = prepare.detail
        return
    ready = run.record(
        "READY", gateway.ready(token=issuer("READY", baseline_hash, 1)))
    if ready.outcome is not GatewayOutcome.ACKED:
        run.outcome = "REFUSED_AT_READY"
        run.refusal = ready.outcome.value
        run.detail = ready.detail
        return

    commit = run.record(
        "COMMIT", gateway.commit(token=issuer("COMMIT", baseline_hash, 2)))
    run.policy_ids = [str(value) for value in adapter.bindings().values()]
    if commit.outcome is GatewayOutcome.ACKED:
        run.applied_verified = True
    elif commit.outcome is GatewayOutcome.REJECTED:
        # The producer decided; nothing was sent.  There is nothing to unwind.
        run.outcome = "REJECTED"
        run.refusal = commit.outcome.value
        run.detail = commit.detail
        return
    else:
        # UNKNOWN or PARTIAL_APPLY: a write may have landed, so the reverse
        # below is not optional -- it is the obligation this run took on.
        run.detail = commit.detail

    clock.sleep_ms(max(0, int(hold_s)) * 1000)

    # What the gateway last *observed*, not what this run hoped for.  A permit
    # naming a configuration nobody read is refused (and rightly): after an
    # unresolved commit the live configuration is the baseline as far as
    # anything can tell, and the reversal has to say so.
    reverse_expected = commit.observed_config_hash or (
        applied_hash if run.applied_verified else baseline_hash)
    reverse = run.record(
        "REVERSE_ROLLBACK",
        gateway.reverse_rollback(
            token=issuer("REVERSE_ROLLBACK", reverse_expected, 3)))
    if reverse.outcome is GatewayOutcome.REJECTED_CONFIG_MISMATCH:
        # The refusal reports the hash that *is* live.  Reissuing at a new
        # fence is the reread-and-reissue the gateway's own comment describes,
        # and it is a new attempt rather than a retry of a stale permit.
        live = reverse.observed_config_hash
        if live:
            issuer.advance_fence()
            reverse = run.record(
                "REVERSE_ROLLBACK",
                gateway.reverse_rollback(
                    token=issuer("REVERSE_ROLLBACK", live, 4)))
    run.restored_verified = reverse.outcome is GatewayOutcome.ACKED
    if not run.restored_verified:
        # The reversal could not complete, and a policy this run created may
        # still be live.  A halt does not read first -- that is the point of it
        # -- so it is what remains when the configuration cannot be observed.
        # The binding stays held until an independent readback shows the
        # baseline back; the scope is not released on a DELETE response.
        issuer.advance_fence()
        # The digest is carried because the permit contract requires one; a
        # halt deliberately does not check it, which is what makes a halt work
        # when the configuration is exactly what cannot be read.
        halt = run.record(
            "STOP", gateway.stop(token=issuer("STOP", reverse_expected, 5)))
        run.detail = (run.detail or reverse.detail
                      or halt.detail)

    if run.applied_verified and run.restored_verified:
        run.outcome = "APPLIED_VERIFIED_AND_RESTORED"
    elif run.applied_verified:
        run.outcome = "APPLIED_VERIFIED_RESTORE_UNVERIFIED"
    elif run.restored_verified:
        run.outcome = "APPLY_UNVERIFIED_BASELINE_RESTORED"
    else:
        run.outcome = "UNRESOLVED_WITHDRAWAL_OUTSTANDING"


def _run_document(run, *, deployment, producer, bound, mode, injected, scope,
                  expected, observed_ue, port, adapter, counter_reader, issuer,
                  validity, hold_s, transaction_id, binding_journal, state_dir):
    binding = binding_journal.binding_for(transaction_id)
    return {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "sessionMode": mode,
        "injectedPorts": list(injected),
        # Said in the document, not only in a docstring: this run had no Kernel
        # case, so it has no Kernel decision axes and claims none.
        "permitIssuer": "campaign5-live-run",
        "permitIssuerNote":
            "the Assurance Kernel issues a token only for a trial of an "
            "admitted objective case; this driver runs one named action at one "
            "named value, so the permits are its own -- monotonic fence, "
            "bounded lease, expected configuration digest, derived idempotency "
            "key -- and no Kernel decision axis is reported",
        "family": run.family.key,
        "actionId": run.family.catalog_action_id,
        "policyTypeId": run.family.policy_type_id,
        "adapter": run.family.adapter_name,
        "axis": run.family.axis,
        "rc": {"style": run.family.rc_style,
               "actionId": run.family.rc_action_id,
               "ranParameterIds": list(run.family.rc_param_ids)},
        "scope": dict(scope),
        "baselineConfig": dict(run.baseline),
        "targetConfig": dict(run.target),
        "holdSeconds": int(hold_s),
        "validity": dict(validity),
        "deployment": {**deployment.summary(),
                       "actionProducerApiRoot": producer.api_root,
                       "r1StateDir": str(state_dir)},
        "observedUe": observed_ue,
        "expectedAttribution": expected.to_record(),
        "settlement": {
            "outcome": run.outcome,
            "refusal": run.refusal,
            "detail": run.detail,
            "appliedVerified": run.applied_verified,
            "restoredVerified": run.restored_verified,
            "gatewayOperations": [list(item) for item in run.gateway_operations],
        },
        "axes": None,
        "axesNote":
            "the Kernel's seven decision axes are the verdict of an evaluator "
            "over an admitted case, and this run had neither; what it observed "
            "is in settlement and readbackLog",
        "policyIds": list(run.policy_ids),
        "binding": None if binding is None else {
            "state": binding.state.value,
            "policyId": binding.policy_id,
            "policyRevision": binding.policy_revision,
            "scopeKey": binding.scope_key,
            "detail": binding.detail,
            "holdsScope": binding.holds_scope,
        },
        "writeCounts": adapter.write_counts(transaction_id),
        "unresolvedOperations": [
            entry.to_canonical_dict()
            for entry in adapter.unresolved_operations()],
        "transportCalls": [dict(call) for call in port.calls],
        "readbackLog": [dict(entry) for entry in counter_reader.reads],
        "permits": list(issuer.issued),
    }


def _write_evidence(document, adapter, *, directory, family, stamp):
    """The run document and the driver's own append-only operation record.

    The second file is **not** a Kernel event stream and does not pretend to
    be: it is the R1 adapter's operation journal, one JSON object per line, in
    the order the calls were issued.  That is the append-only record this path
    actually has, and it is the one the write counts above are derived from.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / f"CAMPAIGN5-A1-{family.key}-{stamp}"
    run_path = Path(f"{prefix}-run.json")
    events_path = Path(f"{prefix}-events.jsonl")
    document["eventsKind"] = "r1-operation-journal"
    run_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    events_path.write_text(
        "".join(json.dumps(entry.to_canonical_dict(), sort_keys=True,
                           separators=(",", ":")) + "\n"
                for entry in adapter.operations()),
        encoding="utf-8")
    return {"run": str(run_path), "events": str(events_path)}


# --------------------------------------------------------------------------- #
# Headless surface
# --------------------------------------------------------------------------- #


def print_run(document: Mapping[str, Any]) -> None:
    """The block ``main.py --live --no-gui`` prints, for this path."""
    settlement = document["settlement"]
    print(f"--- campaign5 A1 [{document['sessionMode']}] "
          f"{document['family']} ---")
    print(f"  policy type   : {document['policyTypeId']} "
          f"via {document['adapter']}")
    print(f"  E2SM-RC       : style {document['rc']['style']} action "
          f"{document['rc']['actionId']} params "
          f"{document['rc']['ranParameterIds']}")
    print(f"  producer      : {document['deployment']['actionProducerApiRoot']}")
    print(f"  scope         : {document['scope']}")
    print(f"  baseline      : {document['baselineConfig']}")
    print(f"  target        : {document['targetConfig']} "
          f"(hold {document['holdSeconds']} s)")
    if document.get("observedUe"):
        observed = document["observedUe"]
        print(f"  observed UE   : {observed['amfUeNgapId']} on cell "
              f"{observed['servingCell']} / {observed['e2Node']} epoch "
              f"{observed['connectionEpoch']}")
    operations = " -> ".join(f"{kind} {outcome}" for kind, outcome
                             in settlement["gatewayOperations"])
    print(f"  gateway       : {operations or 'none'}")
    print(f"  outcome       : {settlement['outcome']}")
    if settlement["detail"]:
        print(f"  detail        : {settlement['detail']}")
    print(f"  applied       : {settlement['appliedVerified']}  "
          f"restored: {settlement['restoredVerified']}")
    print(f"  policy ids    : {', '.join(document['policyIds']) or 'none'}")
    counts = document["writeCounts"]
    print(f"  writes        : {counts['applies']} apply, "
          f"{counts['withdrawals']} withdraw, {counts['refused']} refused, "
          f"{counts['unknown']} unresolved")
    if document.get("binding"):
        binding = document["binding"]
        print(f"  binding       : {binding['state']} "
              f"(revision {binding['policyRevision']}, "
              f"holds scope {binding['holdsScope']})")
    print(f"  readbacks     : "
          + (", ".join(entry["outcome"] for entry in document["readbackLog"])
             or "none"))
    print(f"  axes          : not evaluated -- {document['axesNote']}")
    for name, path in (document.get("evidence") or {}).items():
        print(f"  {name:<13} : {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.campaign5.live_run",
        description="Run one Campaign 5 action over the air through A1")
    parser.add_argument("--profile", required=True,
                        help="Live profile document (deployment/liveconsole-profile.json)")
    parser.add_argument("--family", required=True,
                        choices=sorted(CAMPAIGN5_FAMILIES),
                        help="Campaign 5 family to actuate")
    for family_key, flags in sorted(FAMILY_VALUE_FLAGS.items()):
        for flag, leaf in flags:
            parser.add_argument(flag, type=int, default=None,
                                help=f"{family_key}: {leaf}")
    parser.add_argument("--hold-s", type=int, default=20,
                        help="Seconds to hold the value before reversing")
    parser.add_argument("--amf-ue-ngap-id", type=int, default=None,
                        help="UE-scoped families: the UE this run addresses. "
                             "It must be fresh on the KPM stream or the run is "
                             "refused; a UE is never picked by recency.")
    parser.add_argument("--cell-nci", type=int, default=None,
                        help="Cell-scoped families: the cell this run addresses")
    parser.add_argument("--gnb-id", type=str, default=None,
                        help="Cell-scoped families that name a gnbId")
    parser.add_argument("--baseline", type=int, default=None,
                        help="State the baseline value when the counter cannot "
                             "be read. Recorded as stated, never as observed.")
    parser.add_argument("--evidence-dir", type=str, default=None,
                        help="Where to write the run document (defaults to the "
                             "profile's evidence directory)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    flags = FAMILY_VALUE_FLAGS[args.family]
    values: Dict[str, int] = {}
    for flag, leaf in flags:
        given = getattr(args, flag.lstrip("-").replace("-", "_"))
        if given is None:
            print(f"refused: --family {args.family} needs {flag}")
            return 2
        values[leaf] = int(given)
    baseline = (None if args.baseline is None
                else {leaf: int(args.baseline) for _flag, leaf in flags[:1]})
    if baseline is not None and len(flags) > 1:
        print(f"refused: --family {args.family} moves {len(flags)} leaves; "
              "--baseline states one and cannot describe them")
        return 2
    try:
        document = run_live_action(
            profile_path=args.profile, family_key=args.family, values=values,
            hold_s=args.hold_s, amf_ue_ngap_id=args.amf_ue_ngap_id,
            cell_nci=args.cell_nci, gnb_id=args.gnb_id, baseline=baseline,
            evidence_dir=args.evidence_dir)
    except (Campaign5LiveError, LiveConsoleError) as exc:
        print(f"refused before anything was submitted:\n  {exc}")
        return 3
    print_run(document)
    return 0 if document["settlement"]["appliedVerified"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
