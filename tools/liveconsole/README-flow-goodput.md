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

## Verification

`tests/test_live_flow_goodput.py` covers source and profile composition, the
refusal of malformed live profiles, counter handling, and terminal-source
behavior without hardware.

On the testbed, a bounded 2 Mbps / 30 s source check through the live SSH
`LiveFlowGoodputObserver` (no injected runner and no radio restart) gave the
following results:

- The sender submitted and the receiver consumed exactly **7,401,164 payload
  bytes**. Sender, receiver, and raw archival all exited 0, and the receiver
  ended at TCP EOF.
- One preflight established the baseline without a rate, and all 29 subsequent
  rate samples were present. Over the **29.5253 s** source interval, payload
  goodput was **1.97700 Mbps** and the tun aggregate was **2.05172 Mbps**; the
  two follow their different definitions.
- All 60 raw heartbeats retained one boot and interface identity, with
  advancing timestamps and nondecreasing payload and tun counters.
- After EOF, the observer returned no KPI and reported the terminal source
  instead of repeating the last rate.

## Deployment

Deploy `flow_goodput.py` and `tagged_echo.py` to every UE and to
`oai-ext-dn`, and confirm with `sha256sum` that each copy matches the source
pin in
[`experiment_results/ota-20260911/guarded_source_process.py`](../../experiment_results/ota-20260911/guarded_source_process.py);
a guarded run refuses at preflight with `SOURCE_HASH_MISMATCH` otherwise. The
endpoints' `/tmp` does not survive a reboot, so redeploy after one. Keep the
deployment manifest with the campaign records. Installation and hermetic tests
alone are not OTA measurements.
