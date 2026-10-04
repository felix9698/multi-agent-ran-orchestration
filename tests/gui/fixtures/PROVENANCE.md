# Operator Cockpit replay fixtures

The fixtures in this directory are committed so that the GUI tests are
**hermetic**: they never read lab staging paths, never contact hardware, a
model API or the network, and give the same answer on a fresh checkout. They
are consumed by the ten-step replay scenario, the error-state matrix, the
topology enumeration, and the decision projection tests.

---

## `lo1-capture-min/` — live O1 harness capture

**Source.** A capture recorded by the live O1 harness on the testbed (run
`sc084-pc1-upper-109-20260813T141759Z`, scenario `SC-084`, `schemaVersion
oran-aic-upper-live-o1-harness-capture/2.0.0`, 28 top-level keys).

The document is otherwise verbatim. It validates against
`docs/upper-live-o1-harness/capture-schema.2.0.0.json` with zero errors, and
`tests/gui/test_replay_lo1_capture.py` re-checks this on every run so the
fixture remains faithful to the recorded capture.

### Redactions applied

| What | Change | Why |
|---|---|---|
| Lab endpoint address | `192.168.0.50` → `192.0.2.50` | RFC 5737 TEST-NET-1; a committed fixture should not carry a lab address |
| Staging path prefix | removed | leaves `netconf.readiness.states[].evidence.durableStatePath` as `state/o1-subscription.json`; that state directory was removed by the capture's own cleanup actions, so the reference is intentionally unresolved |
| Runtime and teardown owner | local account → `lab-operator:lower-integration` | local account name |

Three raw artifacts embed the lab address, so redacting them changed their bytes
and digests:

* `raw/netconf/rpc-0010-get-perfmetricjob-reply.xml`
* `raw/netconf/rpc-0012-get-perfmetricjob-reply.xml`
* `raw/o1/notification-*.json`

The capture was re-sealed for exactly those three: the recorded `sha256` and
`byteCount` were recomputed, and because the notification's filename embeds its
digest, the file was renamed to match. Content-addressed resolution depends on
these digests, and the test asserts them.

No secret material was redacted because none was captured: the capture asserts
`redaction.secretValuesCaptured = false` and
`redaction.credentialMaterialCaptured = false`, and carries only opaque
references (`secret://sftp` and similar). Reference names are retained because
the Settings provenance panel displays them.

The adapter is rooted at this directory and never walks to its parent; the
traversal refusal is tested.

### Raw artifacts intentionally omitted

Three large artifacts are referenced by the capture but not committed:

| Path | Bytes | Reason |
|---|---|---|
| `raw/netconf/session-client-to-server.bin` | 7 055 | binary NETCONF wire dump |
| `raw/netconf/session-server-to-client.bin` | 11 520 | binary NETCONF wire dump |
| `raw/release/RELEASE-MANIFEST.json` | 59 611 | release manifest, larger than the whole capture |

Their references are kept on purpose. The adapter must record a
`DANGLING_REFERENCE` data issue and render the drill-down as unavailable rather
than failing the load, and this fixture exercises that path. `state/` is
committed empty (`.gitkeep`) for the same reason.

Every other referenced artifact resolves: 30 references, 27 resolvable, 0 digest
mismatches, 0 orphan files.

---

## `lo1-capture-negative/` — schema-negative cases

Mutation descriptors rather than copies. Each file names a pointer, the mutation
and the expected outcome; the test applies it to the capture above in a
temporary directory.

| Case | Verifies |
|---|---|
| `unknown-schema-version` | a `schemaVersion` this build does not vendor is refused, and the message names both the observed and the supported versions |
| `absent-schema-version` | without a version to bind to, the load fails closed rather than guessing from the document shape |
| `missing-required-key` | a missing required section is a schema mismatch |
| `wrong-typed-field` | `run.sequenceHighWaterMark` as a string is refused; the timeline merge order depends on it |
| `undeclared-property` | the schema is `additionalProperties: false`; a hand-added annotation is not a capture |
| `aborted-disposition` | `run.disposition = ABORTED_GUARD` is a valid capture that must never be summarized as COMPLETED |
| `normalization-record-without-value` | a PM record with no value becomes a `null` sample with quality `MISSING`, never a zero or an interpolated value |

---

## `experiment-run-min/` — offline runner output (synthetic)

Generated with

```
python3 -m experiments.runner --mode synthetic --trials 2 \
  --methods llm_no_history --no-figures --seed 7 --output <dir>
```

40 `StepRecord`s and 4 `EpisodeRecord`s from the synthetic offline runner.
Three non-semantic edits were made:

* the session ID was set to the stable `20260101_000000` (the runner uses a
  wall-clock ID);
* `_meta.git_hash` was replaced with `"fixture"`;
* `*_steps.csv` is not committed because `.gitignore` excludes `*.csv`. The
  adapter reads `steps.json`, which contains the same records.

Everything else is exactly what the runner wrote, including
`_meta.paper_ready = false`, which the GUI displays.

The synthetic runner writes the radio keys `rsrp` and `sinr` without a link
direction. Because the metric registry does not merge uplink and downlink
series, the adapter does not map them onto `ue_dl_ss_rsrp_dbm` or
`gnb_ul_avg_rsrp_dbm`; it records those metrics as `UNAVAILABLE` with the
reason, in contrast to `experiment-run-live-min/`.

---

## `experiment-run-live-min/` — live-format runner output

A compact, hand-built record in the format written by the live measurement
path, so the adapter's live-record handling can be tested without shipping
campaign data. It carries `gnb_ul_avg_rsrp_dbm`, `gnb_ul_snr_db`,
`ue_dl_ss_rsrp_dbm`, `ue_dl_sinr_db` and `radio_source`, three load phases,
one `ue2` throughput sample that is `null` (an unknown value, so the chart must
draw a gap), and one `EpisodeRecord` with the full raw/calibrated/threshold
schema and the latency decomposition.

`_meta.mode` is `live`, so `manifest.mode` becomes `LIVE`, while the telemetry
source class remains `EXPERIMENT_RECORD`: "live" describes where the radio was,
not which boundary the numbers came through.

Its UE2 goodput target is `3.5` Mbps (`config.LIVE_I2_TARGET_MBPS`), compared
with `8.0` in the synthetic fixture (`IntentConfig.throughput_target_mbps`),
which is why the goal line is read from each run's own configuration snapshot
rather than a module constant.

The values are illustrative test inputs, not measurements, and are not
presented as results anywhere in the repository.

---

## `capability-manifest-min.json`

A minimal RAN capability manifest (`aic.ran-capability.1.0.0`). It declares
`RRU.PrbDl` and `DRB.UEThpDl` as assurance KPIs, `RRU.PrbDl` as a decision KPI,
two E2 nodes and the policy type, and declares nothing about RSRP, SINR, MCS or
IQ, so the topology and metric-availability panels are enumerated from a
manifest and unsupported metrics render as `UNSUPPORTED` with the reason.

## `profile-min.json`

A minimal experiment profile for the ten-step replay scenario: source adapter
and path, intent targets, metric selection, graph, refresh and recording
settings, model selection, and the contract authority version.
`llm.credentialRefs` is a list of reference names; no credential value appears
in any fixture here.
