"""Falsifiers: the mutations that must turn a passing run into a failing one.

The self-test is upper-authored on both sides, so its only defence against a
circular argument is that a *deliberately broken* run really is rejected.  Each
mutation below is applied to real bytes on a real socket, never to the oracle
that judges them:

============  ===============  ===========================================
id            seam             what actually changes on the wire
============  ===============  ===========================================
UBM-ST-M01    ingress proxy    one byte of the policy body the upper PUTs to A1
UBM-ST-M02    runner driver    the second ``R1_DME_PUBLISH`` step is not issued
UBM-ST-M03    runner driver    ``policyId`` is predicted instead of captured
UBM-ST-M04    egress proxy     ``201`` on R1 policy create becomes ``200``
============  ===============  ===========================================

M01 and M04 are transport mutations, so they falsify *any* upper implementation,
not only the stand-in used while the runtime executor works in parallel.
"""

from __future__ import annotations

from dataclasses import dataclass

M01_FLIP_A1_POLICY_BYTE = "UBM-ST-M01"
M02_DROP_ONE_DME_PUSH = "UBM-ST-M02"
M03_PREDICT_POLICY_ID = "UBM-ST-M03"
M04_R1_CREATE_200 = "UBM-ST-M04"


@dataclass(frozen=True)
class Mutation:
    identifier: str
    seam: str
    description: str
    scenarios: tuple[str, ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        identifier=M01_FLIP_A1_POLICY_BYTE,
        seam="INGRESS_PROXY_BEFORE_LOWER_A1",
        description="flip one byte of the policy body the upper forwards to A1",
        scenarios=("SC-083", "SC-091", "SC-092"),
    ),
    Mutation(
        identifier=M02_DROP_ONE_DME_PUSH,
        seam="RUNNER_DRIVER",
        description="drop one of the two DME pushes",
        scenarios=("SC-083",),
    ),
    Mutation(
        identifier=M03_PREDICT_POLICY_ID,
        seam="RUNNER_DRIVER",
        description="predict policyId instead of using the Location capture",
        scenarios=("SC-083", "SC-091", "SC-092"),
    ),
    Mutation(
        identifier=M04_R1_CREATE_200,
        seam="EGRESS_PROXY_BEFORE_UPPER_R1",
        description="return 200 instead of 201 on R1 policy create",
        scenarios=("SC-083", "SC-091", "SC-092"),
    ),
)

MUTATION_IDS = tuple(item.identifier for item in MUTATIONS)
BY_ID = {item.identifier: item for item in MUTATIONS}
DRIVER_MUTATIONS = frozenset({M02_DROP_ONE_DME_PUSH, M03_PREDICT_POLICY_ID})
PROXY_MUTATIONS = frozenset({M01_FLIP_A1_POLICY_BYTE, M04_R1_CREATE_200})
