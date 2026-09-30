# Phase B Operator Console — replay fixture provenance

Every fixture here is committed so the GUI tests are **hermetic**: they never read
`/opt/ran-lab/controller/a1p-stage`, never contact hardware, an LLM API or the network,
and give the same answer on a fresh checkout.

Owner: track **T3** (`docs/phase-b-gui/file-ownership.1.0.0.json`, `tests/gui/fixtures/**`).
Consumed by all four tracks — T4's ten-step replay scenario and error-state
matrix, T1's topology enumeration, T2's decision projection.

---

## `lo1-capture-min/` — golden live-O1 harness capture

**Source.** `/opt/ran-lab/controller/a1p-stage/sc084-upper-1.0.9-live-run-012/capture-attempt-1/`
(read-only; run `sc084-pc1-upper-109-20260813T141759Z`, scenario `SC-084`,
`schemaVersion oran-aic-upper-live-o1-harness-capture/2.0.0`, 28 top-level keys).

**The document is otherwise verbatim.** It still validates against
`docs/upper-live-o1-harness/capture-schema.2.0.0.json` with zero errors, and
`tests/gui/test_replay_lo1_capture.py` re-checks that on every run — so the
fixture cannot drift into being convenient but no longer faithful.

### Redactions applied

| what | from | to | why |
|---|---|---|---|
| lab endpoint address | `192.168.0.50` | `192.0.2.50` | RFC 5737 TEST-NET-1; a committed fixture should not carry a real lab address |
| staging path prefix | `…/sc084-upper-1.0.9-live-run-012/capture-attempt-1/` | *(removed)* | leaves `netconf.readiness.states[].evidence.durableStatePath` as `state/o1-subscription.json`; the reference stays **dangling** either way, because `cleanup.actions[]` removed `state/` at teardown (GAP-14) |
| runtime/teardown owner | `ran-node1:codex-lower-integration` | `lab-operator:lower-integration` | local account name |

Three raw artifacts embed the lab address, so redacting them changed their bytes
and therefore their digests:

* `raw/netconf/rpc-0010-get-perfmetricjob-reply.xml`
* `raw/netconf/rpc-0012-get-perfmetricjob-reply.xml`
* `raw/o1/notification-*.json`

The capture was **re-sealed** for exactly those three: the recorded `sha256` and
`byteCount` were recomputed, and because the notification's *filename* embeds its
digest the file was renamed to match. A fixture whose recorded digest and bytes
disagree would be unusable — content-addressed resolution through a packaged
evidence bundle depends on that digest, and the test asserts it.

No secret material was redacted because none exists: the capture asserts
`redaction.secretValuesCaptured = false` and
`redaction.credentialMaterialCaptured = false`, and carries only opaque
references (`secret://sftp` and similar). Reference *names* are retained
deliberately — the Settings provenance panel displays them, and nothing else.

The adapter is rooted at this directory and never walks to its parent. The real
capture's parent holds `pki/` and `secrets/` material that is out of scope and is
not copied here; the traversal refusal is tested.

### Raw artifacts deliberately omitted

Three large artifacts are referenced by the capture but not committed:

| path | bytes | reason |
|---|---|---|
| `raw/netconf/session-client-to-server.bin` | 7 055 | binary NETCONF wire dump |
| `raw/netconf/session-server-to-client.bin` | 11 520 | binary NETCONF wire dump |
| `raw/release/RELEASE-MANIFEST.json` | 59 611 | release manifest, larger than the whole capture |

Their references are **left in place on purpose**. The adapter must record a
`DANGLING_REFERENCE` data issue and render the drill-down as unavailable rather
than failing the load, and this fixture is what proves it does. `state/` is
committed empty (`.gitkeep`) for the same reason.

Every other referenced artifact resolves: 30 references, 27 resolvable, 0 digest
mismatches, 0 orphan files.

---

## `lo1-capture-negative/` — schema-negative cases

Mutation **descriptors**, not copies. Each file names a pointer, the mutation and
the expected outcome; the test applies it to the golden capture in a temp
directory. Seven full copies of a 70 KB document would add half a megabyte of
duplicated bytes for no extra coverage, and a one-line mutation is far easier to
review than a diff of two large files.

| case | proves |
|---|---|
| `unknown-schema-version` | a `schemaVersion` this build does not vendor is refused, and the message names both the observed and the supported versions — no pattern-matching onto a known shape |
| `absent-schema-version` | with nothing to bind to, the load fails closed rather than guessing from the document shape |
| `missing-required-key` | a missing required section is a schema mismatch |
| `wrong-typed-field` | `run.sequenceHighWaterMark` as a string is refused; the timeline merge order depends on it |
| `undeclared-property` | the schema is `additionalProperties: false`; a hand-added annotation is not a capture |
| `aborted-disposition` | `run.disposition = ABORTED_GUARD` is a **valid** capture that must still never be summarized as COMPLETED — the distinction is made downstream, not by refusing the document |
| `normalization-record-without-value` | a PM record with no value becomes a `null` sample with quality `MISSING` — never a zero, never interpolated |

---

## `experiment-run-min/` — offline runner output (synthetic)

Generated on this machine with

```
python3 -m experiments.runner --mode synthetic --trials 2 \
  --methods llm_no_history --no-figures --seed 7 --output <dir>
```

40 `StepRecord`s and 4 `EpisodeRecord`s across the five legacy phases.
Three edits, all of them non-semantic:

* the session id was rewritten to the stable `20260101_000000` (the runner uses a
  wall-clock id, which would make the fixture unreviewable);
* `_meta.git_hash` was replaced with `"fixture"` — it records the worktree the
  fixture was generated in, which is not a property of the fixture and would
  churn on every rebuild;
* `*_steps.csv` is **not committed**: the repository's `.gitignore` excludes
  `*.csv`. The adapter reads `steps.json`, which is the richer of the two, so
  nothing is lost — but a reader comparing this directory against a live
  `experiment_results/` should expect the CSV to be absent here.

Everything else is exactly what the runner wrote, including
`_meta.paper_ready = false`, which the GUI displays rather than hides.

**Note on the radio keys.** The synthetic harness writes the *legacy ambiguous*
`ue_kpis` keys `rsrp` and `sinr`. They do not say whether the value is uplink or
downlink, and the metric registry forbids merging UL and DL into one series. The
adapter therefore does **not** map them onto `ue_dl_ss_rsrp_dbm` or
`gnb_ul_avg_rsrp_dbm`; it records those metrics as `UNAVAILABLE` with the reason,
which is what `experiment-run-live-min/` exists to contrast with.

---

## `experiment-run-live-min/` — offline runner output, honest radio keys

Hand-built, because `experiment_results/` is empty on every machine in this
project and no live run output exists on disk to excerpt. It carries what the
live measurement path writes: `gnb_ul_avg_rsrp_dbm`, `gnb_ul_snr_db`,
`ue_dl_ss_rsrp_dbm`, `ue_dl_sinr_db` and `radio_source`, three `sec14-load`
phases, one `ue2` throughput sample that is `null` (honest unknown, so the chart
must draw a gap), and one `EpisodeRecord` with the full raw/calibrated/threshold
schema and the latency decomposition.

`_meta.mode` is `live`, which is the point: `manifest.mode` becomes `LIVE`, and
the telemetry source class nevertheless stays `EXPERIMENT_RECORD`. "Live"
describes where the radio was, not which boundary the numbers came through.

Its I2 target is `3.5` Mbps (`config.LIVE_I2_TARGET_MBPS`) against the synthetic
fixture's `8.0` (`IntentConfig.throughput_target_mbps`) — which is exactly why
the goal line is read from each run's own config snapshot and never from a module
constant.

The values are illustrative, not measured, and nothing in the repository presents
them as a result: they exist only to exercise the adapter.

---

## `capability-manifest-min.json`

Copied verbatim from `docs/phase-a/raw/mock-local/capability.json`
(`aic.ran-capability.1.0.0`). It declares `RRU.PrbDl` and `DRB.UEThpDl` as
assurance KPIs, `RRU.PrbDl` as a decision KPI, two E2 nodes and the policy type,
and declares nothing about RSRP, SINR, MCS or IQ — so the topology and the metric
availability panel are enumerated from a real manifest, and the metrics it is
silent about render `UNSUPPORTED` with the reason rather than disappearing.

## `profile-min.json`

A minimal experiment profile for the ten-step replay scenario: source adapter and
path, intent targets, metric selection, graph/refresh/recording settings, LLM
selection and the contract authority version. `llm.credentialRefs` is a list of
reference **names**; no credential value appears in any fixture here.
