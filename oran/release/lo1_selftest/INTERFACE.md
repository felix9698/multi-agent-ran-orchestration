# O1 Provider emulator interface

This document describes the contract-faithful O1 Provider emulator used by the
live O1 harness self-test, and the interface that O1 consumer implementations
are tested against. This file is the human-readable form; the machine-readable
form is

```
python3 -m oran.release.lo1_selftest interface
```

which prints the registered fixture digests, the required capability list and
the golden sample values the emulator is forbidden to put on the wire — all
read from the frozen bundle, none written down here.

In an extracted release, invoke the same interface through `bin/lo1-selftest`.
The launcher resolves the release root from its own location, loads Python only
from the packaged `lib/`, and binds `contracts/`, `spec/`, and
`ARTIFACT-PROVENANCE.json` inside that release. It clears ambient `PYTHONPATH`
and does not require a repository or Git. Source-tree development keeps the
module form above and derives provenance from the checkout.

**Nothing in this package is SC-084 acceptance.** The only state label it emits
is `UPPER_LIVE_O1_HARNESS_SELF_TEST`, and disposition for the scenario belongs
to the lower conformance runner under the live-O1 integration authority.

---

## 1. What the emulator is

A contract-faithful Provider built **only** from the eight inputs
`emulator-boundary.1.0.0.json#/emulatorConstruction/permittedInputs` names. It
binds three loopback listeners for the duration of a self-test run and nothing
else:

| Surface | Endpoint member | Transport |
|---|---|---|
| NETCONF | `EmulatorEndpoints.netconf` = `ssh://127.0.0.1:<ephemeral>` | real SSH, subsystem `netconf`, RFC 6242 framing: `]]>]]>` for the hello exchange, chunked for everything after it |
| FileDataReportingMnS | `EmulatorEndpoints.mns_root` = `https://127.0.0.1:<ephemeral>` | TLS 1.2+, routes derived from `oran-aic-o1-pa-file.1.0.0.json#/delivery/subscription` |
| SFTP | `EmulatorEndpoints.sftp_authority` = `127.0.0.1:<ephemeral>` | real SSH, subsystem `sftp`, read-only |

`EmulatorEndpoints.host_key_sha256` is the SHA-256 of the host key the emulator
generated **this run**. It is a runtime pin, never a stored one. Both SSH
listeners present the same host key; they are separate authorities so that no
two role-table origins collide.

## 2. The frozen signatures

```python
EMULATOR_KIND: str = "CONTRACT_FAITHFUL_EMULATOR"
SELF_TEST_STATE_LABEL: str = "UPPER_LIVE_O1_HARNESS_SELF_TEST"

@dataclass(frozen=True)
class EmulatorEndpoints:
    netconf: str; mns_root: str; sftp_authority: str; host_key_sha256: str

@dataclass(frozen=True)
class EmulatorResult:
    scenario_id: str
    http_sequence: tuple[int, ...]
    observations: Mapping[str, Any]
    unknown_routes: int
    emitted_pm_sha256: tuple[str, ...]

class ProviderEmulator:
    def __init__(self, *, bundle_path: Path, vector: Mapping[str, Any],
                 work_dir: Path, seed: int, consumer_notification_uri: str) -> None
    def start(self) -> EmulatorEndpoints
    def secret_map(self) -> dict[str, str]
    def emit_pm_file(self, *, window_start: str, window_end: str) -> tuple[str, bytes]
    def send_file_ready(self) -> int
    def run_scenario(self, scenario_id: str) -> EmulatorResult
    def stop(self) -> None
    @property
    def unknown_route_count(self) -> int
    @property
    def golden_value_collisions(self) -> tuple[str, ...]
```

### Additive helpers (the signatures above are unchanged)

| Method | Why it exists |
|---|---|
| `adopt_vector(vector)` | A vector cannot carry the emulator's ephemeral ports before the emulator has bound them. Call after `start()` with the final vector. |
| `adopt_consumer_uri(uri)` | Same bootstrap loop for the consumer's own listener. |
| `trust_consumer(ssl_context)` | The notification hop verifies the consumer's TLS leaf. Without it the emulator **refuses** to POST rather than skipping verification. |
| `client_tls_context()` | Client context trusting the emulator's own MnS leaf. |
| `client_private_key()` | The ephemeral client key for SSH publickey auth in tests. |
| `inject(fault)` | Arms one declared Provider-side fault. Unknown names raise. |
| `advertised_capabilities()` | The capability list read from the frozen NETCONF profile. |
| `emitted_files()` / `netconf_observations` | Provider-side observations for reporting. |

## 3. The contract consumers are tested against

1. **NETCONF is byte-exact.** A request is answered only if the message the
   framing layer reassembles is byte-equal to one of the eight registered
   `golden/o1/netconf/*.xml` fixtures. Anything else is an `rpc-error` with
   `operation-not-supported` and increments `unknown_route_count`.
   `unknown_route_count` must be `0` on a clean run (`G-EMU-1`).

   **NETCONF framing is RFC 6242, implemented independently here.**
   The frozen profile declares `NETCONF_1_1_OVER_SSH` and requires
   `urn:ietf:params:netconf:base:1.1`, so the `<hello>` exchange is framed with
   `]]>]]>` and **every** message after it is chunked
   (`LF '#' size LF` per chunk, `LF '#' '#' LF` to end the message). Replies are
   deliberately emitted in several 512-octet chunks, so a consumer's reassembly
   path is exercised on every exchange rather than only on a large payload.
   This emulator implements the framing from the RFC on its own — it does not
   import the consumer's codec, and the consumer does not import this one — so
   that a shared non-conformant framing cannot make both sides pass together. A post-hello `]]>]]>`, a malformed or leading-zero
   chunk size, a truncated stream and a missing end-of-chunks are refused with
   `LO1-PFRAME-001..006`; the session then ends **unanswered**.
2. **PM values diverge from golden, by requirement.**
   `RULE-O1-LIVE-VALUE-INVARIANTS` forbids equality to a golden sample value
   for live input, so `RRU.PrbDl` is generated inside the range the frozen PM
   file profile declares and is never equal to any `<r>` value present in
   `valid-prb.xml`, `null-prb.xml` or `suspect-prb.xml`. Two consecutive runs
   produce **different** PM digests (`G-EMU-2`). Do not pin a PM digest.
3. **The measurement window comes from the run's observed clock.** A window
   that collides with a golden timestamp is refused at generation time.
4. **Secrets are references.** `secret_map()` returns
   `secretRef -> run-scoped path`, keyed by the `secretRef` values in the
   vector the emulator was given. The frozen vector schema admits only
   `secret://`, `vault://` and `k8s-secret://`. Key bytes never leave the
   run-scoped temporary directory, which lives outside the release tree, the
   capture root and the repository, and is removed at `stop()`.
5. **The emulator can never be production.** `providerKind` is always
   `CONTRACT_FAITHFUL_EMULATOR`; the capture schema's `profile` branch then
   forces `emulatorInUse: true`, `counterpartKind:
   UPPER_ARTIFACT_SELF_TEST_HARNESS` and `selfTestLabel:
   UPPER_LIVE_O1_HARNESS_SELF_TEST`. Starting under the live profile with the
   emulator reachable is refused (`LO1-ST-N10`).
6. **`run_scenario` refuses a scenario the frozen assignment does not put on
   the `live-O1` profile**, and refuses one whose step vector declares no
   `O1_NOTIFY` / `O1_RETRIEVE` pair.

## 4. Faults consumers may arm in their own tests

Provider-side, via `inject(...)`; none is reachable on a clean run:

`PM_VALUE_OUT_OF_RANGE`, `PM_VALUE_EQUALS_GOLDEN`, `PM_DN_COLLISION`,
`PM_SUSPECT_SAMPLE`, `PM_NULL_VALUE`, `NOTIFY_EVENT_TIME_MISMATCH`,
`NOTIFY_EXPIRY_BEFORE_READY`, `NOTIFY_SUBSCRIPTION_MISMATCH`,
`SUPPRESS_NOTIFICATION`, `WITHHOLD_PM_FILE`.

## 5. The independent verifier

`verifier.py` imports **nothing** from this repository — standard library and
`jsonschema` only — and re-derives every digest, linkage, value and counter
from the raw bytes under the capture root. It reads the oracle from the frozen
catalog at adjudication time and produces findings, never a verdict.

```python
IndependentVerifier(capture_path=..., bundle_path=..., gates_path=...,
                    capture_schema_path=...).readjudicate() -> dict
verify_from_raw_evidence(capture_root, *, bundle_path) -> dict
```

Findings are stable `CODE:detail` strings so a caller can assert the specific
defect rather than "something failed".

## 6. Command line

| Command | Exit codes |
|---|---|
| `interface` | 0 |
| `selftest-run --work DIR [...]` | 0 OK · 65 findings · 69 runtime absent · 78 admission refused |
| `adjudicate --capture DIR` | 0 clean · 65 findings |
| `probe --name {emulator-rpc,emulator-capability,emulator-pm,egress,oracle-literals}` | 0 · 65 |
| `determinism --a DIR --b DIR [--negative-control]` | 0 · 65 |
| `falsify [--id ID] [--report FILE]` | 0 all falsified · 65 otherwise |

## 7. Core-runtime capture sections

In a self-test run, the core-runtime capture sections (`exchanges`,
`coordinator`, `deterministicStubs`, `state`) are built by the self-test
itself and labelled `SYNTHETIC_FALSIFIER_SUBSTRATE` in the bundle sidecar, so
they can never be mistaken for runtime output. `RUNTIME_UNDER_TEST` is reserved
for sections produced by the runtime in `oran/release/lo1/`. Under
`--require-runtime`, the driver reports `RUNTIME_ABSENT` (exit 69) when that
runtime is unavailable. The O1 sub-graph — notifications, retrievals,
normalization, raw artefacts, the external-target ledger, cleanup, and the
redaction scan — is produced from real sockets in every run.
