# Explicit per-flow DL application goodput

`flow_goodput.py` measures I1/I2/I3 payload consumed by a UE TCP application,
not an iperf control-channel report or the UE's aggregate tun traffic. Deploy
**both `flow_goodput.py` and `tagged_echo.py` together**: the source reuses the
latter's private append-only logging, tun identity and same-boot clock helpers.
Neither imports nor the observer launch any traffic.

## Measurement and lifecycle

- The receiver binds the explicit current `oaitun_ue1` IPv4 and device. The one
  accepted TCP connection must originate from the configured single ext-DN IP
  (default `192.168.70.135`) and present the correct session/flow handshake.
- Only bytes consumed **after** that bounded handshake count as application
  payload. TCP/IP overhead, handshake bytes and the independent I4 echo flow
  never enter the application counter. TCP backpressure can reduce delivery;
  the fixed offered pacing is not a claimed delivered rate.
- Every 0.5 s, the receiver records cumulative payload bytes and tun `rx_bytes`
  with UP/IP/ifindex/boot identity and the UE's monotonic timestamp. The tun
  counter remains the operational cross-check and may include other traffic.
- Runtime is at most 3600 s; sender pacing at most 30 Mbps. Delayed sends do not
  catch up in bursts. Receiver lifetime includes its initial connection wait;
  sender lifetime includes bounded connect/handshake time. Declare enough
  workload lifetime to cover preparation, service horizon and archival margin.
- No silent reconnect, source replacement or tun identity repair occurs.
  Waiting-before-handshake, terminal, stale, changed or reset sources produce
  UNKNOWN/missing KPI, never fabricated zero. Zero payload delta from a
  continuously running valid source is a real measured zero.
- The LIVE observer differences counters using **source UE time**, not SSH or
  controller timing. A missing/invalid sample clears its rate baseline. The
  source clock/interface/connection remains pinned even across missing reads.
  `describe().sourceSamples` preserves snapshot counters and boundaries with
  episode evidence; archive the actual raw JSONL separately as well.
- Existing observation-rule aggregation and measurement windows still apply.
  Freeze cadence/statistic/coverage with the workload; raw heartbeat timestamps
  expose the actual counter intervals, not imaginary exact controller bins.

## Explicit bounded example

Example identities/rates are not calibrated targets. Verify the current UE
IPv4 and network ID first. On each UE, choose a different flow ID and fresh log;
all UEs can use port 5203 because they have distinct IP addresses.

```bash
# On UE1, using its verified current tun IPv4. Run while the sender is active.
sudo timeout 125 python3 /tmp/flow_goodput.py receiver \
  --bind-ip 12.1.1.2 --port 5203 \
  --session-id episode-example --flow-id ue1-data \
  --log /tmp/episode-example-ue1-data.jsonl \
  --allow-source-ip 192.168.70.135 --duration-s 120 --max-rate-mbps 2

# Start while that receiver listens; ALL controller-to-UE traffic is in ext-DN.
docker exec oai-ext-dn timeout 95 python3 /tmp/flow_goodput.py sender \
  --receiver-ip 12.1.1.2 --port 5203 \
  --session-id episode-example --flow-id ue1-data \
  --duration-s 90 --rate-mbps 2

# On the source UE/boot, while the receiver and workload are still running:
python3 /tmp/flow_goodput.py snapshot \
  --log /tmp/episode-example-ue1-data.jsonl \
  --session-id episode-example --flow-id ue1-data --max-age-ms 1500
```

The sender prints a bounded-run summary of actual payload submitted to TCP,
separate from the receiver's delivered-payload counters. Preserve both launch
commands, source hashes (both files), summary/raw logs, actual attribution and
workload settings. A copied/finished raw log is archival evidence, not a live
snapshot source. No packet contents, subscriber credentials or passwords belong
in these artifacts.

## Profile wiring

`131` is an illustrative network ID, not a reusable live UE identity:

```json
{
  "liveConsole": {
    "ueHosts": {"131": "ue1"},
    "flowGoodput": {
      "131": {
        "sourcePath": "/tmp/flow_goodput.py",
        "logPath": "/tmp/episode-example-ue1-data.jsonl",
        "sessionId": "episode-example",
        "flowId": "ue1-data",
        "maxAgeMs": 1500
      }
    }
  }
}
```

`build_profile_observer()` selects `LiveFlowGoodputObserver` for configured
`dlGoodputMbps@<ue>` requirements. Those UEs are excluded from the aggregate tun
observer, so two sources cannot overwrite the same KPI. Unconfigured legacy
profiles still use verified tun aggregate measurements, explicitly labelled
`tun-aggregate-rx-bytes`; they must not be claimed to satisfy the formal scenario's
per-flow application definition. Formal I1/I2/I3 profiles must configure all
three service sources. I4 retains its separate `liveConsole.taggedEcho` source.

A single fresh `LiveFlowGoodputObserver.preflight()` establishes a source and
counter baseline, not a rate. The next strictly advancing valid sample computes
the payload rate. Source configuration does not waive the actual LIVE runner's
readiness, source preflight, Kernel, readback, recovery or evidence requirements.

## Verification status

The source/profile composition and actual LIVE malformed-profile refusal were
included in a focused 102-test hermetic run on 2026-09-09, together with the
existing tagged-echo/deadline/tun/metrics regressions. That is a selected suite,
not a full-suite count or an OTA result.

At 22:04 KST, both source files were deployed without starting traffic to UE1,
UE2, UE3 and `oai-ext-dn` under:
`/tmp/aic-flow-9140cea9b49e-beaa921ad26a/`.
~~All four endpoints matched these hashes and passed standalone `--help`:~~
*(sentence superseded — see the correction below; left in place as the original record)*

- `flow_goodput.py`: `9140cea9b49e78ee9c43cddc323820ff59a2cde869d33c9421bd7a9964a0c6ef`
- `tagged_echo.py`: `7080f0ab879d621753728b997df3142ec1c479a75fcca801c049fda52ec6e283`

> **Correction — 2026-09-14 (annotation; the lines above are left unchanged).**
> The struck sentence is a record of the 2026-09-09 22:04 KST deployment, but the
> `tagged_echo.py` digest listed above it is **not** the value that was deployed that
> day. `tools/liveconsole/tagged_echo.py` was edited on 2026-09-14 (windowed cohort
> support), moving its sha256
> `beaa921ad26a4e42b15f1f8c4a7f6dcce38d3f2d902df1772db4b2a6eb6f4930` →
> `7080f0ab879d621753728b997df3142ec1c479a75fcca801c049fda52ec6e283`, and the new
> value was written into this list in place. So, as of this annotation:
>
> - The **repo file and the source pin** in
>   `experiment_results/ota-20260911/guarded_source_process.py` are at `7080f0ab…c6e283`.
>   This much is verified from the files themselves.
> - The **deployed copies** on UE1, UE2, UE3 and `oai-ext-dn` are **believed to be** at
>   the last known value `beaa921a…f4930` — that is what the 2026-09-09 deployment
>   manifest records. This is **unverified**: the endpoints have been powered down for a
>   thermal rest and nobody has inspected them since, so they may also hold nothing at
>   all if `/tmp` was cleared at boot.
> - `flow_goodput.py` (`9140cea9…a0c6ef`) is unchanged and is not affected.
>
> What is certain regardless of endpoint state is the **mismatch between the pin and
> whatever was deployed on 2026-09-09**. Therefore: do **not** read the struck sentence
> as evidence that redeployment can be skipped. A redeploy of `tagged_echo.py` into the
> existing directory name `/tmp/aic-flow-9140cea9b49e-beaa921ad26a/` (the name is inert
> provenance text and must not be renamed) is required before the next guarded run, which
> would otherwise refuse at preflight with `SOURCE_HASH_MISMATCH:tagged_echo.py`.
> Verify with `sha256sum` on all four endpoints after power-on. Full analysis:
> `experiment_results/ota-20260911/SOURCE-PIN-RESUME-VERDICT-20260914T2000.md`.

> **Redeploy — 2026-09-15 ~01:30 KST (v3.1).** `tagged_echo.py` changed again for the
> amendment's issued-cohort identity block: sha256 `7080f0ab…c6e283` →
> `fc92b3f9f12660423814bd7790ac804c74539b2c6cfbf3e137d8b20256e335f9`. It was copied into the
> existing directory `/tmp/aic-flow-9140cea9b49e-beaa921ad26a/` on UE1, UE2, UE3 and
> `oai-ext-dn`, and `sha256sum` on all four returned `fc92b3f9…335f9` for `tagged_echo.py` and
> the unchanged `9140cea9…a0c6ef` for `flow_goodput.py`. The pin in
> `experiment_results/ota-20260911/guarded_source_process.py` was updated to match. `/tmp` on
> the endpoints does not survive their reboot: after one, redeploy before a guarded run.

Deployment evidence:
`experiment_results/ota-20260909/calibration/flow-deployment-20260909T220425/manifest.json`.
No old source/log was overwritten. Installation and hermetic tests alone do not
establish an OTA measurement.

At 22:21 KST, UE3's existing radio instance carried a bounded 2 Mbps / 30 s
payload-source check through the real SSH `LiveFlowGoodputObserver` (no injected
runner, no radio restart). Evidence:
`experiment_results/ota-20260909/calibration/flow-check-20260909T222141/`.

- Sender submitted and receiver consumed exactly **7,401,164 payload bytes**.
  Sender, receiver and raw archival all exited 0; the receiver ended at TCP EOF.
- One preflight established the baseline without a rate; all 29 subsequent rate
  samples were present. Across the 30 snapshots' **29.5253 s** source interval,
  payload goodput was **1.97700 Mbps** and tun aggregate was **2.05172 Mbps**.
  These are different definitions, not inconsistent readings.
- All 60 running raw heartbeats retained one boot/interface identity, advancing
  timestamps and nondecreasing payload/tun counters. No source failure occurred.
- After EOF, the real observer returned no KPI and reported the terminal source;
  it did not replay the last rate. Raw events, observer snapshots/observations,
  commands, hashes and `raw-reconciliation.json` are archived together.

The first attempt (`flow-check-20260909T221941`) never launched its sender:
its operational wrapper missed `ss`'s `IP%oaitun_ue1:port` listener spelling.
The wrapper was corrected; the deployed sources were unchanged. That failed
attempt is retained, not counted as a radio payload failure or a successful run.
The observer key `ue3` in this source-only check is an SSH host label, not a
fabricated AMF UE ID. This verifies the source implementation on one UE, not
UE1/UE2 service, calibrated targets, a full three-UE common window, or a formal
four-intent Coordinator episode.
