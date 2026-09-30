"""The evidence epoch record.

Owner lane: **KCON** (record type is complete; the freeze entry points below
are frozen signatures the Kernel calls).

Design section 6.3 lists exactly what an epoch freezes:

    - active Target, Harm, and Measurement contracts;
    - capability and composition manifests;
    - target-vector order and case limits;
    - deployment and counter/actuator bindings;
    - candidate generator version, universe cardinality, membership, semantic
      hashes, and catalog hash;
    - evaluator and reducer versions.

Every one of them is a hash field on :class:`EpochRecord`.  Storing hashes
rather than the objects is deliberate: the epoch record is what a trial, a
ledger entry and an advisory message all reference, so it has to be small,
comparable and stable, and the objects themselves are already content-
addressed elsewhere in the stream.

Two consequences follow and are worth stating because they are the point:

* "Agents cannot add, remove, or mutate candidates during an epoch.  New
  intent, manifest, or contract versions wait for a new epoch."  A change to
  any frozen item changes the epoch hash, so it cannot be applied silently --
  it produces a different epoch or it does not happen.
* "Safety-relevant changes require a drained epoch and resolved live
  deployment before activation" (section 6.3, task section 6.14).  Draining is
  the Kernel's job; :attr:`EpochRecord.supersedes_epoch_ref` is where the
  chain of epochs is recorded so a replay can see the transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from assurance.contracts.common import ContractIdentity, frozen_mapping, frozen_tuple
from assurance.core.timebase import is_utc_timestamp

__all__ = ["EpochRecord", "epoch_hash", "freeze_epoch"]


@dataclass(frozen=True)
class EpochRecord(ContractIdentity):
    """The frozen state everything in an epoch is judged against.

    Attributes
    ----------
    epoch_id:
        Identifier referenced by trials, ledger entries and advisory messages.
    frozen_at:
        Canonical UTC instant of the freeze.
    target_contract_hashes / harm_contract_hashes / measurement_contract_hashes:
        Contract id -> content hash for every active contract of that family.
    capability_manifest_hashes / composition_manifest_hash:
        The deployed capability set and the composition that constrains it.
    target_vector_hash / target_vector_order:
        The confirmed vector and its normative order (design section 6.4).
        The order is stored as well as hashed, so a reader does not need the
        original object to know what comes next.
    case_policy_hash:
        The frozen deadline, trial cap, proposal cap and reserve limits.
    deployment_binding_hashes / counter_binding_hashes / actuator_binding_hashes:
        How the contracts reach this deployment.
    candidate_generator_version / candidate_universe_cardinality /
    candidate_semantic_hashes / catalog_hash:
        The finite candidate universe.  Cardinality and membership hashes are
        stored beside the catalog hash so a mismatch is localisable -- "the
        catalog changed" is much less useful than "candidate 7's meaning
        changed".
    evaluator_version / reducer_version:
        Design section 4.3: the same event stream *and reducer version*
        reproduce the same ledger and terminal state hash.  A reducer change
        therefore belongs to the epoch, not to the deployment.
    confirmation_ref:
        The Operator confirmation event this epoch was frozen under.
    supersedes_epoch_ref:
        The epoch this one replaces, if any.
    """

    epoch_id: str
    frozen_at: str
    target_contract_hashes: Mapping[str, str]
    harm_contract_hashes: Mapping[str, str]
    measurement_contract_hashes: Mapping[str, str]
    capability_manifest_hashes: Mapping[str, str]
    composition_manifest_hash: str
    target_vector_hash: str
    target_vector_order: Tuple[str, ...]
    case_policy_hash: str
    deployment_binding_hashes: Mapping[str, str]
    counter_binding_hashes: Mapping[str, str]
    actuator_binding_hashes: Mapping[str, str]
    candidate_generator_version: str
    candidate_universe_cardinality: int
    candidate_semantic_hashes: Tuple[str, ...]
    catalog_hash: str
    evaluator_version: str
    reducer_version: str
    confirmation_ref: Optional[str] = None
    supersedes_epoch_ref: Optional[str] = None
    #: 2026-09-19: the catalog is a frozen domain; ``candidate_semantic_hashes``
    #: then holds the one digest of that domain, not one hash per point.
    candidate_domain: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in (
            "target_contract_hashes",
            "harm_contract_hashes",
            "measurement_contract_hashes",
            "capability_manifest_hashes",
            "deployment_binding_hashes",
            "counter_binding_hashes",
            "actuator_binding_hashes",
        ):
            object.__setattr__(self, name, frozen_mapping(getattr(self, name)))
        object.__setattr__(
            self, "target_vector_order", frozen_tuple(self.target_vector_order)
        )
        object.__setattr__(
            self, "candidate_semantic_hashes", frozen_tuple(self.candidate_semantic_hashes)
        )


# --------------------------------------------------------------------------- #
# Frozen entry points - bodies owned by KCON
# --------------------------------------------------------------------------- #

def freeze_epoch(
    *,
    identity: Mapping[str, Any],
    epoch_id: str,
    frozen_at: str,
    targets: Sequence[Any],
    harms: Sequence[Any],
    measurements: Sequence[Any],
    capabilities: Sequence[Any],
    composition: Any,
    target_vector: Any,
    case_policy: Any,
    deployment_bindings: Sequence[Any],
    counter_bindings: Sequence[Any],
    actuator_bindings: Sequence[Any],
    catalog: Any,
    evaluator_version: str,
    reducer_version: str,
    confirmation: Any = None,
    supersedes_epoch_ref: Optional[str] = None,
) -> EpochRecord:
    """Hash every frozen item and assemble the :class:`EpochRecord`.

    Signature frozen by this design step; body owned by lane **KCON**.

    The body must:

    * hash each object with
      :func:`assurance.contracts.validation.contract_content_hash`, so an
      epoch's digests and a contract's own digest are the same value;
    * take the candidate generator version, cardinality, semantic hashes and
      catalog hash from *catalog* rather than recomputing them, so an epoch
      cannot disagree with the catalog it froze;
    * refuse to freeze when *confirmation* does not cover the target vector's
      current content hash -- design section 5: if confirmed content changes,
      the previous confirmation is invalid and a new click is required, so
      freezing under a stale confirmation would defeat the one control the
      Operator actually has;
    * refuse any contract carrying a non-admissible
      :class:`~assurance.core.provenance.TypedQuantity` (task section 5.5).

    Returns
    -------
    EpochRecord
        Complete; every hash field populated.  There is no partially frozen
        epoch: a missing binding or an unhashed manifest is a refusal, because
        a trial admitted against an incomplete freeze cannot be replayed.
    """
    from assurance.contracts.catalog import CandidateCatalog, CoordinationCasePolicy, catalog_hash, membership_digests
    from assurance.contracts.validation import ContractAdmissionError, contract_content_hash, validate_contract

    if not is_utc_timestamp(frozen_at):
        raise ContractAdmissionError("epoch frozen_at must be canonical UTC")
    if not isinstance(catalog, CandidateCatalog):
        raise ContractAdmissionError("epoch requires CandidateCatalog")
    if not isinstance(case_policy, CoordinationCasePolicy):
        raise ContractAdmissionError("epoch requires CoordinationCasePolicy")
    all_items = (*targets, *harms, *measurements, *capabilities, composition,
                 target_vector, case_policy, *deployment_bindings, *counter_bindings,
                 *actuator_bindings, catalog)
    for item in all_items:
        validate_contract(item)
    if catalog.epoch_ref != epoch_id:
        raise ContractAdmissionError("catalog epoch ref does not match epoch id")
    if catalog.catalog_hash != catalog_hash(
        generator_version=catalog.generator_version, cardinality=catalog.cardinality,
        candidates=catalog.candidates,
    ):
        raise ContractAdmissionError("catalog hash does not match frozen membership")
    vector_hash = contract_content_hash(target_vector)
    if confirmation is None or not hasattr(confirmation, "is_valid_for"):
        raise ContractAdmissionError("epoch freeze requires current target vector confirmation")
    if not confirmation.is_valid_for(vector_hash):
        raise ContractAdmissionError("target vector confirmation is stale or does not match")

    def map_hashes(items: Sequence[Any], key: str = "contract_id") -> Mapping[str, str]:
        values = {getattr(item, key): contract_content_hash(item) for item in items}
        if len(values) != len(items):
            raise ContractAdmissionError(f"duplicate frozen {key}")
        return values

    return EpochRecord(
        **dict(identity), epoch_id=epoch_id, frozen_at=frozen_at,
        target_contract_hashes=map_hashes(targets), harm_contract_hashes=map_hashes(harms),
        measurement_contract_hashes=map_hashes(measurements),
        capability_manifest_hashes=map_hashes(capabilities),
        composition_manifest_hash=contract_content_hash(composition),
        target_vector_hash=vector_hash,
        target_vector_order=tuple(target_vector.ordered_target_refs),
        case_policy_hash=contract_content_hash(case_policy),
        deployment_binding_hashes=map_hashes(deployment_bindings),
        counter_binding_hashes=map_hashes(counter_bindings, "counter_id"),
        actuator_binding_hashes=map_hashes(actuator_bindings),
        candidate_generator_version=catalog.generator_version,
        candidate_universe_cardinality=catalog.cardinality,
        candidate_semantic_hashes=membership_digests(catalog),
        candidate_domain=catalog.is_domain,
        catalog_hash=catalog.catalog_hash,
        evaluator_version=evaluator_version, reducer_version=reducer_version,
        confirmation_ref=getattr(confirmation, "event_id", None),
        supersedes_epoch_ref=supersedes_epoch_ref,
    )


def epoch_hash(record: EpochRecord) -> str:
    """The content hash of an epoch record.

    Signature frozen by this design step; body owned by lane **KCON**.

    This is the value an advisory message carries in
    :attr:`assurance.core.envelopes.MailboxEnvelope.epoch_hash`, and the value
    :func:`assurance.core.envelopes.classify_envelope` compares against to
    reject a proposal formed against a superseded epoch.

    Must cover every field of *record* including
    :attr:`EpochRecord.reducer_version` and
    :attr:`EpochRecord.evaluator_version`.  Excluding either would let a
    reducer change slip past replay verification while every recorded hash
    still matched -- and design section 4.3 makes the reducer version part of
    what "the same result" means.
    """
    from assurance.contracts.validation import contract_content_hash, validate_contract

    validate_contract(record)
    return contract_content_hash(record)
