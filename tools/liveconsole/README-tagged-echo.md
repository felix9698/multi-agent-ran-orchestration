# I4: explicit live tagged-echo source

`tagged_echo.py` is a standalone, stdlib-only operational program. It does not
start traffic on import or from an observer. Its client is a separate UE1 flow
from I1's DL bulk stream; RTT covers the UE→ext-DN→UE round trip using only the
UE's monotonic clock. This is **not** a one-way command-latency measurement.

## Deployment prerequisites

- For the three-UE scenario, UE association, fresh UE-to-host attribution, and
  the per-UE goodput workload are established before the echo source is used.
- Python 3 with its standard library on both the UE and `oai-ext-dn`; copy the
  same source file to explicit paths on both and record its SHA-256. On Ubuntu 22.04, `python3-minimal` alone lacks
  `json`; the tested container also needed `libpython3.10-stdlib`.
- UE client binds `oaitun_ue1` and its current IPv4. It requires permission to use
  `SO_BINDTODEVICE` (typically sudo). The server binds the ext-DN address, never
  a controller-host route to `12.1.1.x`.
- Client output must be a **new** file. It is exclusive-create, append-only,
  mode 0600. When launched by sudo, ownership goes to `SUDO_UID`/`SUDO_GID` so
  the ordinary SSH observer can read it; the file is not made world-readable.
- Rate, payload, deadlines, and windows are fixed before comparisons. The
  reported experiments use a 256-byte payload at 5 Hz and a 35-ms deadline; the
  commands below are launch examples.

## Explicit bounded workload

Example after deployment to `/tmp/tagged_echo.py` on both endpoints:

```bash
# Inside the ext-DN container; the timeout is inside docker exec.
docker exec oai-ext-dn timeout 125 python3 /tmp/tagged_echo.py server \
  --bind-ip 192.168.70.135 --port 7778 \
  --session-id episode-example --flow-id ue1-command \
  --allow-subnet 192.168.70.134/32 --duration-s 120

# On UE1, while the server runs. This is not a radio stop/restart command.
sudo python3 /tmp/tagged_echo.py client \
  --server-ip 192.168.70.135 --port 7778 \
  --session-id episode-example --flow-id ue1-command \
  --log /tmp/episode-example-ue1-command.jsonl \
  --duration-s 90 --rate-hz 5 --payload-bytes 256 --reply-drain-s 1
```

In the testbed, the UPF applies SNAT: an ext-DN capture of the experiment UDP
headers showed source `192.168.70.134`, not the UE tun address. The example
therefore explicitly permits that single UPF address. The server's default
`12.1.1.0/24` is for a routed, non-NAT deployment. Confirm the actual source
before changing the allowlist; do not open a wildcard network. Session/flow
validation, packet-size and reply-rate bounds remain unchanged.

The source never catches up delayed sends in bursts. Every issued sequence is
logged before send, including send errors and requests with no reply. The
server reflects only the configured session/flow and allowed UE subnet, with
no amplification and at most 20 replies/s. Runtime including drain is bounded
by 3600 s; payload by 1200 bytes. The offered workload does not change when an
intent concedes reliability or deadline.

The client stops on tun DOWN, lost IP, changed IP/ifindex or source failure.
It does not silently reattach or restart attribution. Those changes require
operational recovery and a new source session.

## Live profile composition

The UE ID here is an **example**, not a reusable live identity. Update the
mapping from fresh KPM/AMF attribution before each execution.

```json
{
  "liveConsole": {
    "ueHosts": {"131": "ue1"},
    "taggedEcho": {
      "131": {
        "sourcePath": "/tmp/tagged_echo.py",
        "logPath": "/tmp/episode-example-ue1-command.jsonl",
        "sessionId": "episode-example",
        "flowId": "ue1-command",
        "maxAgeMs": 1500
      }
    }
  }
}
```

The actual `build_agent_sitting()` LIVE path consumes this configuration.
Without it, I4 is refused before policy discovery/scope writes. With it, the
source must answer a fresh snapshot before model calls. No `kpi_observer=`
injection is used to label emulation as LIVE. Source failures during a sitting
omit the KPI and are recorded in `kpiObserverFailures`.

The configured program is queried on its originating UE:

```bash
python3 /tmp/tagged_echo.py snapshot \
  --log /tmp/episode-example-ue1-command.jsonl \
  --session-id episode-example --flow-id ue1-command \
  --deadlines-ms 50,80 --max-age-ms 1500
```

`50,80` is only an example. The runtime requests original and all formed,
operator-authorized target deadlines automatically; it never changes the
source's offered load. Snapshot eligibility uses the **last complete source
heartbeat**, not the controller's unrelated monotonic clock. Remote current
time only checks freshness. Session, flow, source boot, counter progression
and heartbeat are retained with the sample. Terminal/stale/foreign logs fail
nonzero. A copied or ended log is raw evidence, not a current live source.

## Evaluation and evidence

- Each sample has `byDeadlineMs[D] = {issued, eligible, completed}`. Every
  request whose D has passed is eligible, including unanswered requests.
- Window aggregation differences cumulative counters independently for every
  D; it does not average sample ratios. Zero eligible requests are UNKNOWN.
- Each target reads its own D from the **same window**. A relaxed-deadline PASS
  is not evidence for original-deadline attainment. Legacy scalar ratios can
  describe D0 only.
- Metrics charge `reqId#deadline` independently of the reliability concession.
  Service deficits use original D0 throughout H, not the retained relaxed D.
  Ratio bins expose their actual counter boundaries and use the declared
  cadence/coverage; missing/reset/mixed-source bins remain unknown.
- Preserve the raw JSONL, source hash, both launch commands, workload settings,
  source descriptors, service trace, trial evidence and failure records with
  each episode. Raw JSONL is not automatically copied from the UE by this
  observer. Raw-log archival is an explicit campaign operation.
- LIVE tun goodput uses UP/IP/ifindex checks and same-UE-clock counter intervals;
  unavailable counters rebase rather than bridging a missing interval.
  Tun bytes include protocol overhead and all flows on that tun. For formal
  service-specific goodput, retain workload/flow accounting: tun totals alone
  must not be relabelled exact application goodput.

On the testbed, a 30-second, 5-Hz source-only check with the verified UPF
allowlist issued 150 requests and received 149 replies, with raw logs, source
hash, launch arguments, and live snapshots archived together. An earlier run
with the default allowlist recorded all 150 requests as unanswered; such runs
are kept as recorded outcomes rather than discarded or reclassified.
