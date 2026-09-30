"""Finite Candidate Catalog and Coordination Case Policy.

Owner lane: **KCON** (the data types); catalog *generation* is called by the
Kernel lane through the frozen entry point below.

Design section 6.3 freezes, per epoch, the "candidate generator version,
universe cardinality, membership, semantic hashes, and catalog hash", and then
states the rule the whole structure exists to enforce: "Agents cannot add,
remove, or mutate candidates during an epoch."

That is the direct cutover for GAP-03 in ``docs/architecture/GATE1-MAP.md``,
where the legacy path parsed a fresh model-produced ``proposed_config`` every
cycle and had no object fixing what the candidate space even was.  Here the
space is enumerated, counted and hashed before any trial runs; an advisory
message can only ever *name* a candidate that is already in it.

``semantic_hash`` on each candidate is separate from the catalog hash on
purpose.  Two catalogs can contain the same candidate; cross-epoch evidence
reuse (design section 8) needs to know that the *meaning* of candidate X is
unchanged even though the epoch around it changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence as _SequenceABC
from typing import Any, Mapping, Optional, Sequence, Tuple

from assurance.contracts.common import ContractIdentity, frozen_mapping, frozen_tuple
from assurance.core.addressing import content_hash

__all__ = [
    "Candidate",
    "CandidateCatalog",
    "CoordinationCasePolicy",
    "DomainMembership",
    "catalog_hash",
    "domain_candidate",
    "domain_lookup",
    "generate_catalog",
    "membership_digests",
]


@dataclass(frozen=True)
class Candidate:
    """One admissible, fully specified thing the Kernel may trial.

    Attributes
    ----------
    candidate_id:
        Stable within the epoch.
    target_ref / option_ref:
        The target contract and target option this instantiates.
    parameters:
        One concrete point in the option's bounded parameter space.  Values
        are strings so the candidate has exactly one canonical form; typed
        interpretation belongs to the actuator binding.
    semantic_hash:
        Digest over what this candidate *means* -- target, option, parameters
        and the referenced contract versions -- excluding epoch-local
        identifiers.  Cross-epoch reuse compares this, not
        :attr:`candidate_id`.
    capability_ref:
        The capability that would act, resolved at generation time so an
        agent cannot propose a candidate whose capability is not deployed.
    """

    candidate_id: str
    target_ref: str
    option_ref: str
    parameters: Mapping[str, str]
    semantic_hash: str
    capability_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", frozen_mapping(self.parameters))


@dataclass(frozen=True)
class CandidateCatalog(ContractIdentity):
    """The complete, finite candidate universe for one epoch.

    Attributes
    ----------
    generator_version:
        Which generator produced this membership.  Frozen into the epoch
        (design section 6.3) so a regenerated catalog that differs is a
        detectable change rather than a surprise.
    cardinality:
        Stored explicitly, not derived from ``len(candidates)``.  A mismatch
        between the recorded cardinality and the actual membership is exactly
        the corruption the epoch freeze is meant to catch, and a derived
        property could never report it.
    candidates:
        The membership, in generated order.
    catalog_hash:
        Digest over generator version, cardinality and ordered membership.
    epoch_ref:
        The epoch this catalog belongs to.
    """

    generator_version: str
    cardinality: int
    candidates: Tuple[Candidate, ...]
    catalog_hash: str
    epoch_ref: str

    def __post_init__(self) -> None:
        super().__post_init__()
        # A frozen *domain* is its own immutable sequence: materialising it
        # here is exactly the cost the domain exists to avoid.
        if not isinstance(self.candidates, DomainMembership):
            object.__setattr__(self, "candidates", frozen_tuple(self.candidates))

    @property
    def is_domain(self) -> bool:
        return isinstance(self.candidates, DomainMembership)

    def membership_matches_cardinality(self) -> bool:
        """True when the recorded cardinality matches the membership.

        A plain, complete check -- the Kernel calls it before admitting any
        trial against this catalog.
        """
        return self.cardinality == len(self.candidates)


@dataclass(frozen=True)
class CoordinationCasePolicy(ContractIdentity):
    """The finite limits one coordination case runs under.

    Design section 8: "No agent can keep a case alive past its deadline, trial
    cap, proposal cap, or usable harm reserve."  All four are here, as numbers
    frozen into the epoch, so termination is arithmetic rather than a
    judgement call.

    Attributes
    ----------
    deadline_ms:
        Wall-clock budget for the whole case.
    max_trials / max_proposals:
        Hard caps.  A rejected proposal still consumes the proposal cap;
        otherwise a malformed-output loop would be free.
    max_consecutive_indeterminate:
        Guard against a case burning its whole budget on traces that cannot
        decide anything -- it terminates as ``EVIDENCE_INCOMPLETE`` instead.
    target_release_policy_ref / harm_contract_refs:
        The policies this case runs under.
    require_recovery_before_next_trial:
        Task section 6.13.  ``True`` in every shipped policy; present as data
        so the boundary test can assert it.
    """

    deadline_ms: int
    max_trials: int
    max_proposals: int
    target_release_policy_ref: str
    harm_contract_refs: Tuple[str, ...]
    max_consecutive_indeterminate: int = 3
    require_recovery_before_next_trial: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self, "harm_contract_refs", frozen_tuple(self.harm_contract_refs)
        )


# --------------------------------------------------------------------------- #
# Frozen entry points - bodies owned by KCON
# --------------------------------------------------------------------------- #

def generate_catalog(
    *,
    targets: Sequence[Any],
    capabilities: Sequence[Any],
    composition: Any,
    generator_version: str,
    epoch_ref: str,
    identity: Mapping[str, Any],
) -> CandidateCatalog:
    """Enumerate the complete finite candidate universe for one epoch.

    Signature frozen by this design step; body owned by lane **KCON**.

    Parameters
    ----------
    targets:
        The :class:`~assurance.contracts.target.TargetContract` objects the
        epoch admits, each carrying its
        :class:`~assurance.contracts.target.TargetOption` list.
    capabilities:
        Admitted :class:`~assurance.contracts.capability.CapabilityManifest`
        objects.  An option whose capability is absent yields no candidates:
        design section 10 requires missing actuator, KPI, rollback or evidence
        paths to fail closed rather than be advertised.
    composition:
        The :class:`~assurance.contracts.capability.CompositionManifest`
        whose mutual exclusions and joint constraints prune the cross product.
    generator_version:
        Recorded in the catalog and frozen into the epoch.
    epoch_ref:
        The epoch this catalog is generated for.
    identity:
        Contract identity fields for the produced catalog.

    Returns
    -------
    CandidateCatalog
        With ``cardinality`` set from the enumeration, ``candidates`` in a
        deterministic order, each candidate's ``semantic_hash`` computed, and
        ``catalog_hash`` computed over generator version, cardinality and
        ordered membership.

    The body must be **deterministic**: the same inputs must produce the same
    order and the same hashes on every host and every run.  Design section 15
    verifies "deterministic candidate cardinality and catalog hash", and
    section 4.3 requires replay without an LLM to reach an identical terminal
    state -- neither survives an enumeration that depends on set iteration
    order or on a dict built from an unordered source.

    It must also be **total**: every option in every admitted target is either
    enumerated or excluded for a recorded reason.  A silently dropped option
    makes the catalog look smaller than the space it claims to cover, which
    would let an exhaustion certificate be issued over an incomplete universe.
    """
    from assurance.contracts.capability import CapabilityManifest, CompositionManifest
    from assurance.contracts.target import TargetContract
    from assurance.contracts.validation import ContractAdmissionError, contract_content_hash

    if not generator_version or not epoch_ref:
        raise ContractAdmissionError("catalog generator version and epoch ref are required")
    if not isinstance(composition, CompositionManifest):
        raise ContractAdmissionError("catalog requires a composition manifest")
    capabilities_by_ref = {
        capability.contract_id: capability for capability in capabilities
        if isinstance(capability, CapabilityManifest)
    }
    active_refs = set(composition.capability_refs)
    if not active_refs.issubset(capabilities_by_ref):
        raise ContractAdmissionError("composition names unavailable capability")
    for pair in composition.mutual_exclusions:
        if set(pair).issubset(active_refs):
            raise ContractAdmissionError("composition contains mutually exclusive capabilities")

    # 2026-09-18: 열거와 **나란히** 선언을 모은다.  해시는 이 선언으로 낸다 --
    # 후보를 하나씩 정규화하던 비용(후보당 2.5 ms)이 사라지고, 같은 선언이면 같은
    # 해시라는 보증은 그대로다.  열거는 여전히 필요하다: `candidates` 는 선택·검증이
    # 실제로 쓰는 값이다.  바뀐 것은 **해시가 곱에 비례하지 않는다**는 것뿐이다.
    # 2026-09-19 (오너: "셀이 10개 되면 선택이 불가하냐, 말이 안 된다"): the epoch
    # freezes the **domain** -- each option's allowed values per axis -- and the
    # cardinality as their product.  Nothing is enumerated: a candidate is the
    # point of the domain its parameters name, decoded on demand with the same
    # id, order and semantic hash the old enumeration produced.
    blocks: list = []
    offset = 0
    for target in sorted(targets, key=lambda item: getattr(item, "contract_id", "")):
        if not isinstance(target, TargetContract):
            raise ContractAdmissionError("catalog target is not a TargetContract")
        for option in sorted(target.options, key=lambda item: item.contract_id):
            capability = capabilities_by_ref.get(option.capability_ref)
            if capability is None or option.capability_ref not in active_refs:
                continue
            keys = sorted(option.parameter_space)
            # ``set`` first: a value listed twice on one axis would name one
            # point twice (2026-09-18, the ladder rung equal to the baseline).
            values = [sorted(set(option.parameter_space[key])) for key in keys]
            size = 1
            for axis_values in values:
                size *= len(axis_values)
            if size == 0:
                continue
            blocks.append({
                "targetRef": target.contract_id, "optionRef": option.contract_id,
                "capabilityRef": capability.contract_id,
                "targetHash": contract_content_hash(target),
                "optionHash": contract_content_hash(option),
                "capabilityHash": contract_content_hash(capability),
                "keys": keys, "values": values, "offset": offset, "size": size,
            })
            offset += size
    membership = DomainMembership(blocks)
    digest = catalog_hash(generator_version=generator_version,
                          cardinality=len(membership), candidates=membership)
    return CandidateCatalog(
        **dict(identity), generator_version=generator_version, cardinality=len(membership),
        candidates=membership, catalog_hash=digest, epoch_ref=epoch_ref,
    )


def _point(block: Mapping[str, Any], index: int) -> Candidate:
    """The candidate at ``index`` of one domain block (mixed radix, last axis fastest)."""
    local = index - int(block["offset"])
    chosen = []
    for axis_values in reversed(block["values"]):
        local, position = divmod(local, len(axis_values))
        chosen.append(axis_values[position])
    parameters = dict(zip(block["keys"], reversed(chosen)))
    return Candidate(
        candidate_id=f"candidate/{index:06d}", target_ref=block["targetRef"],
        option_ref=block["optionRef"], parameters=parameters,
        semantic_hash=content_hash({"targetHash": block["targetHash"],
                                    "optionHash": block["optionHash"],
                                    "capabilityHash": block["capabilityHash"],
                                    "parameters": parameters}),
        capability_ref=block["capabilityRef"])


def domain_candidate(blocks: Sequence[Mapping[str, Any]], candidate_id: str) -> Optional[Candidate]:
    """Decode ``candidate/NNNNNN`` against frozen domain blocks, or ``None``."""
    text = str(candidate_id)
    if not text.startswith("candidate/") or not text[len("candidate/"):].isdigit():
        return None
    index = int(text[len("candidate/"):])
    if f"candidate/{index:06d}" != text:
        return None
    for block in blocks:
        if int(block["offset"]) <= index < int(block["offset"]) + int(block["size"]):
            return _point(block, index)
    return None


def domain_lookup(blocks: Sequence[Mapping[str, Any]],
                  parameters: Mapping[str, Any]) -> Optional[Candidate]:
    """The candidate whose parameters are exactly ``parameters`` -- a membership
    test: every key of one block, each value inside that axis's allowed set."""
    wanted = {str(k): str(v) for k, v in dict(parameters).items()}
    for block in blocks:
        if sorted(wanted) != list(block["keys"]):
            continue
        index = 0
        for key, axis_values in zip(block["keys"], block["values"]):
            try:
                position = [str(value) for value in axis_values].index(wanted[key])
            except ValueError:
                index = -1
                break
            index = index * len(axis_values) + position
        if index >= 0:
            return _point(block, int(block["offset"]) + index)
    return None


class DomainMembership(_SequenceABC):
    """An epoch's candidate universe as a frozen domain, indexable like a tuple.

    ``len`` is the product; ``[i]`` and iteration decode points in the same
    order the enumeration used, so ids are unchanged.  Iterating the whole of a
    large domain is still linear in its size -- hot paths use :meth:`get` and
    :meth:`find`, which are constant in it.
    """

    def __init__(self, blocks: Sequence[Mapping[str, Any]]) -> None:
        self._blocks = tuple(
            {**dict(block), "keys": tuple(block["keys"]),
             "values": tuple(tuple(v) for v in block["values"])} for block in blocks)
        self._size = sum(int(block["size"]) for block in self._blocks)

    @property
    def blocks(self) -> Tuple[Mapping[str, Any], ...]:
        return self._blocks

    def declaration(self) -> list:
        """What the hash covers: every block, in order, values included."""
        return [{"targetRef": b["targetRef"], "optionRef": b["optionRef"],
                 "capabilityRef": b["capabilityRef"], "targetHash": b["targetHash"],
                 "optionHash": b["optionHash"], "capabilityHash": b["capabilityHash"],
                 "keys": list(b["keys"]), "values": [list(v) for v in b["values"]],
                 "offset": int(b["offset"]), "size": int(b["size"])}
                for b in self._blocks]

    def __len__(self) -> int:
        return self._size

    def __getitem__(self, index):  # type: ignore[override]
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self._size)))
        if index < 0:
            index += self._size
        if not 0 <= index < self._size:
            raise IndexError(index)
        return domain_candidate(self._blocks, f"candidate/{index:06d}")

    def __iter__(self):
        for index in range(self._size):
            yield self[index]

    def get(self, candidate_id: str) -> Optional[Candidate]:
        return domain_candidate(self._blocks, candidate_id)

    def find(self, parameters: Mapping[str, Any]) -> Optional[Candidate]:
        return domain_lookup(self._blocks, parameters)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DomainMembership) and self.declaration() == other.declaration()

    def __hash__(self) -> int:
        return hash(repr(self.declaration()))


def membership_digests(catalog: "CandidateCatalog") -> Tuple[str, ...]:
    """What an epoch record stores for the membership.

    An enumerated catalog: each candidate's semantic hash.  A domain: one digest
    of the domain declaration -- the points are fixed by it, so it is the whole
    membership, at a cost that does not grow with the product.
    """
    if isinstance(catalog.candidates, DomainMembership):
        return (content_hash({"domain": catalog.candidates.declaration()}),)
    return tuple(candidate.semantic_hash for candidate in catalog.candidates)


def catalog_hash(
    *,
    generator_version: str,
    cardinality: int,
    candidates: Optional[Sequence[Candidate]] = None,
    declaration: Optional[Mapping[str, Any]] = None,
) -> str:
    """Compute the catalog hash from generator version, count and membership.

    Signature frozen by this design step; body owned by lane **KCON**.

    Kept separate from :func:`generate_catalog` so the Kernel can *verify* a
    catalog it did not generate -- on replay, on recovery, and when checking
    that an advisory message's ``epoch_hash`` refers to the catalog actually
    in force.

    The hash must cover the ordered membership, not a set of it: two catalogs
    with the same candidates in a different order are different catalogs,
    because target-vector order is normative (design section 6.4) and the
    enumeration order is what makes generation reproducible.

    Uses :func:`assurance.core.addressing.content_hash` so an assurance digest
    and a frozen ``oran-aic/1.0.0`` artefact digest are the same kind of value.
    """
    from assurance.contracts.validation import ContractAdmissionError, canonical_form

    if candidates is not None and cardinality != len(candidates):
        raise ContractAdmissionError("catalog cardinality does not match candidate membership")
    if candidates is None:
        raise ContractAdmissionError("catalog hash needs the membership")
    if isinstance(candidates, DomainMembership):
        # The domain fixes every point, so hashing it covers the membership.
        return content_hash({"generatorVersion": generator_version,
                             "cardinality": cardinality,
                             "domain": candidates.declaration()})
    # 2026-09-18 (오너 지시: "당연히 고쳐라").  멤버십을 덮는 것은 그대로 두고,
    # **덮는 방법**만 바꾼다.
    #
    # 예전에는 후보마다 `canonical_form(candidate)` 을 돌려 그 전체를 해시했다.
    # 측정값이 후보당 2.5 ms 였고(`joint.py:183`: 4096 이면 10.3 초) 축이 늘면
    # 지수로 늘어나 **실제 RAN 규모에서는 판을 시작할 수 없다.**
    #
    # 그런데 후보는 생성될 때 이미 `semantic_hash` 를 하나씩 갖는다 -- target ·
    # option · capability 의 해시와 파라미터를 덮는 값이다(`generate_catalog`).
    # 같은 것을 두 번 계산하고 있었고, 두 번째가 훨씬 비쌌다.  이미 있는 값을 쓰면
    # **멤버십 전체를 덮는다는 보증은 똑같고** 비용만 사라진다: 후보가 하나라도
    # 다르면 그 `semantic_hash` 가 다르고, 순서가 다르면 수열이 다르다(순서는
    # 규범이다 -- 위 docstring).  `candidate_id` 도 함께 넣어 자리번호까지 고정한다.
    return content_hash({
        "generatorVersion": generator_version,
        "cardinality": cardinality,
        # JCS 는 튜플을 받지 않는다 -- 리스트로 적는다.
        #
        # 2026-09-18 23:5x: `parameters` 를 다시 넣었다.  위 가속이 저장된
        # `semantic_hash` 만 덮으면서, **동결 뒤 제자리에서 바뀐 파라미터**를 못 잡게
        # 됐다 -- `semantic_hash` 는 생성 때 한 번 계산된 값이라 dict 를 고쳐도 그대로다.
        # `test_epoch_freeze_is_stable_and_catalog_membership_is_immutable` 이 그래서
        # 빨개졌다(나는 원장에 "내 변경 전부터 빨갰다" 고 잘못 적었다).  후보에서
        # 제자리 변경이 가능한 필드는 `parameters` 하나뿐이다(나머지는 frozen).
        # 비용: 19,200 후보에서 0.14 s → 0.51 s.  후보마다 `canonical_form` 을 돌리던
        # 예전 방식(후보당 2.5 ms)과는 다르다 -- 한 번의 JCS 직렬화다.
        "candidates": [[candidate.candidate_id, candidate.semantic_hash,
                        dict(candidate.parameters)]
                       for candidate in candidates],
    })
