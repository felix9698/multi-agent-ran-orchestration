# O-RAN interface contracts

This directory holds the machine-readable interface contracts between the agent
coordinator (Non-RT RIC / rApp side) and the near-RT RIC, xApp, E2, and OAI
side of the testbed.

| Directory | Contents |
|---|---|
| [`1.0.1/`](1.0.1/) | Current contract authority: A1 policy and status schemas, R1/A1/O1/E2 profiles, NETCONF/YANG fixtures, golden vectors, the scenario catalog, and runner semantics |
| [`1.0.0/`](1.0.0/) | Previous authority, retained because `1.0.1` is defined relative to it |
| [`campaign5/`](campaign5/) | A1 policy and status schemas for cell transmit power, DL MCS bounds, scheduling priority, and UE DL PRB cap |
| [`gate8-slice-actuator-hf/`](gate8-slice-actuator-hf/) | A1 policy and status schemas for slice SLA targets (slice PRB quota) |

## Integrity pinning

Each versioned authority is distributed as a sealed package. Its
`handoff-manifest.<version>.json` pins the byte count and SHA-256 of every
document in the package, and `shared-contract-bundle/bundle-manifest.<version>.json`
pins every bundle member. [`oran/contract/digests.py`](../../oran/contract/digests.py)
verifies these digests before any contract member is used, and the manifest
digests are also recorded in test fixtures and capture schemas.

The Markdown documents inside `1.0.0/` and `1.0.1/` are therefore kept byte for
byte as issued. They were written as working documents for the parallel
development of the two sides of the interface, in Korean, and include the
original task description and change notes. The normative content used by the
implementation is the schemas, profiles, fixtures, and catalog in
`shared-contract-bundle/`.
