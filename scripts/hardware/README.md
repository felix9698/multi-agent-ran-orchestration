# scripts/hardware — testbed bring-up

These scripts bring up the near-RT RIC, the gNBs, the UEs, and the xApp control
path for over-the-air experiments on the testbed described in the paper (two
gNBs and three UEs, band n78, 38 PRBs, OAI gNB/NR-UE and CN5G, FlexRIC). They
are separate from the Cockpit and from the hardware-free experiments in
[`docs/reproducibility.md`](../../docs/reproducibility.md); neither calls
anything here, and nothing here is needed to run the offline examples.

Two tools prepare the hardware:

| Tool | Purpose |
|---|---|
| `bin/labctl` (see [`tools/labctl/README.md`](../../tools/labctl/README.md)) | Inventory-driven status, preflight, prepare, apply-config, stop, and readiness receipt for the core, RIC, and gNB hosts. |
| `scripts/hardware/*.sh` (this directory) | Campaign bring-up for the live xApp and E2SM-RC experiments: 38-PRB dual-cell gNB restarts, UE attachment, A1-P re-pin, and the action producer. Thin, parameterized wrappers around the procedure used for the OTA campaign. |

> **These scripts do not belong to the Cockpit.** The Cockpit imports no
> transport, actuator, or lab tool; the boundary is enforced by
> `tests/gui/test_cockpit_acceptance.py`. Run these on the lab hosts as the
> operator, never from the console.

## Environment

Every machine-specific path is read from environment variables, so no absolute
path or secret is stored in the repository. Copy and edit:

```bash
cp scripts/hardware/env.sh.example scripts/hardware/env.sh
$EDITOR scripts/hardware/env.sh        # set RIC/OAI/xApp paths for your lab
source scripts/hardware/env.sh
```

No SIM key (Ki/OPc), IMSI, subscriber value, or SSH password belongs in these
files or in `env.sh`. `env.sh` is git-ignored. Set `HW_REQUIRED_UES` to every UE
that must be attached for a run (`ue1,ue2,ue3` for the three-UE scenario).

## Safety and privilege

- **PC1 (gNB1) needs local `sudo`** for the softmodem's real-time threads; the
  operator runs `restart_gnb.sh` there. The near-RT RIC runs as a non-root user.
- Powering the USRPs is a manual operator step. A gNB restart waits about 15 s
  for the X310-class radio to be released before re-acquiring it.
- Downlink load must originate **inside** the `oai-ext-dn` container; a
  host-originated ping to a UE IP leaves through the host's default route
  instead of the 5G core.

## Bring-up order

```bash
source scripts/hardware/env.sh

# 1) Bring the core up with the lab procedure and confirm its user plane
#    before attaching any radio node.
bin/labctl status

# 2) Near-RT RIC (non-root), with the connection witness enabled.
bash scripts/hardware/bringup_ric.sh

# 3) gNBs: gNB1 on PC1, gNB2 on its own host. Complete all gNB and gate
#    changes before the single re-pin in step 6.
sudo -E bash scripts/hardware/restart_gnb.sh gnb1
ssh -t enb2 'sudo -E bash scripts/hardware/restart_gnb.sh gnb2'

# 4) Attach the UEs immediately before the run (38 PRB by default). The wrapper
#    stops an old UE instance, shows its tun IP, and reports the uplink prime.
bash scripts/hardware/attach_ue.sh ue1
bash scripts/hardware/attach_ue.sh ue2
bash scripts/hardware/attach_ue.sh ue3
#    Verify the downlink before relying on the address:
#    docker exec oai-ext-dn ping -c3 <tun-ip>

# 5) Read-only readiness receipt.
bash scripts/hardware/readiness.sh --output /tmp/oran-live-readiness.json

# 6) One staged re-pin after all gNB and gate changes. It runs five discovery
#    and validation steps, then rApp capability/inventory propagation and KPM
#    JSONL rotation; inspect the no-write plan first.
bash scripts/hardware/repin_a1p.sh --dry-run
bash scripts/hardware/repin_a1p.sh

# 7) Start the supplementary-action producer in a separate, supervised
#    terminal. Its preflight exits 4 if the KPM gate is not appending, and it
#    stays in the foreground after a successful preflight. Configure the
#    HW_CAMPAIGN5_* file references in env.sh first.
bash scripts/hardware/run_campaign5_producer.sh

# 8) Review a fresh readiness receipt, then start the Cockpit.
bash scripts/hardware/readiness.sh --output /tmp/oran-live-readiness.json
python3 main.py --live --profile deployment/liveconsole-profile.json
# Or headless:
python3 main.py --live --profile deployment/liveconsole-profile.json --no-gui
```

`HW_LIVE_ARTIFACT_ROOT` is the root for the live capability, inventory, KPM
JSONL, and release artifacts; set it in `env.sh`.

`readiness.sh` is read-only and reports why an observation is missing. The
re-pin writes staged files with backups and publishes them last; it is a
preparation step, not trial evidence. `run_campaign5_producer.sh` fails closed:
exit 4 means its KPM JSONL did not append during the probe window, so it did not
start a writer.

## UE hosts

| Host alias | Radio | Slice |
|---|---|---|
| `ue1` | NI USRP-B206mini-i | SST 1 / SD FFFFFF |
| `ue2` | NI USRP-B206mini-i | SST 1 / SD FFFFFF |
| `ue3` | NI USRP-B206mini-i | SST 222 / SD 00007B |

Each UE host runs UHD 4.9.0, the OAI NR-UE binary and its runtime profile, the
UE launchers, the Ettus udev rule, and an `ld.so.conf.d` entry so `libuhd`
resolves without `LD_LIBRARY_PATH`. Subscriber identities and keys are kept in
the lab's own core and UE configuration, never in this repository.

UE3 uses a different slice from UE1 and UE2, so the `slicePrbQuota@<sst>` axis
acts as a group control (SST 1 moves UE1 and UE2 together) rather than
duplicating the per-UE cap. The live KPI observer maps UEs to hosts through
`HW_UE<n>_HOST`.

## UE start flags

The NR-UE softmodem needs real-time priority and nothing else added:

| Flag | Recommendation |
|---|---|
| `chrt -f 99` | **Required**; without it the UE can stop on USB overflow within minutes. |
| `taskset -c 4-11` | **Avoid**; restricting cores starves the transmit thread, so PRACH misses its occasion and random access fails while the downlink looks healthy. |
| `--agc` | **Avoid**; receive gain climbs and the frequency correction oscillates. |
| `UE_FRAMES=2048` | Recommended; 512 frames is not enough to survive an RRC drop. |

After an `ERROR_CODE_OVERFLOW` exit, the B2xx radio needs one `uhd_usrp_probe`
before the next start; a USB unbind/rebind does not restore it. The UE start
script runs this probe automatically and accepts `UE_RTPRIO`, `UE_CPUS`,
`UE_AGC`, `UE_NOFO`, `UE_FRAMES`, `UE_CARRIER`, `UE_SSB`, and an optional
second argument naming the profile.

## Troubleshooting

| Situation | Symptom and remedy |
|---|---|
| KPM gate removal | `docker rm -f` can stop the gate abruptly and leave the RIC at exit 139. After a RIC restart, neither agent re-sends E2 SETUP; restart both gNBs. |
| gNB2 restart | A non-sudo `pkill` cannot stop a root softmodem. A new PID that immediately reports a busy USRP or `socket closed` is not a restart; use sudo, verify the PID changed, and wait at least 15 s. |
| SCTP association churn | Shutting down one association can drop the other (`SCTP_SHUTDOWN_EVENT` or SETUP timeout). Make every gNB and gate change first, then re-pin once. |
| USB overflow on a UE | `ERROR_CODE_OVERFLOW` or `readBlockSize == tmp` indicates USB overflow; reattach immediately before the run and verify DL with `docker exec oai-ext-dn ping <tun-ip>`. |
| Capability changes | gNB capability changes require the capability refresh in re-pin step 4. Do not edit release inputs inside a bundle; restore pinned content before `finalize`. |
| Docker timestamps | `docker logs --since` interprets a bare time as local time; pass an RFC 3339 timestamp ending in `Z`. |
| Header refresh | Run `refresh_headers.sh` with `--from-live` while the gate container is up; a second KPM gate xApp can crash the RIC (exit 139) and drop both E2 associations. Add `--confirm-hosts` to join each header to its physical host through the AMF's UE table. |
| iperf3 hangs on 5201 | A leftover TCP sink can hold port 5201 on the UE hosts and accept connections without responding. Use port 5202. |
| Sitting refuses every trial | `prepare: REJECTED_CONFIG_MISMATCH` means a manual probe left an axis off its baseline. Restore every axis before starting a sitting. |
| Sitting measures nothing | A sitting started without `source scripts/hardware/env.sh` has no `HW_UE<n>_HOST`, so the observer resolves no hosts and every KPI reads UNKNOWN. |
| A1-P producer stopped | The Cockpit shows `SAFETY_STOPPED`, R1 returns 503, and the producer log reports `xApp worker stopped`. Restart with `docker restart oran-aic-a1p-producer`, then re-run readiness. |

## Measurement practice

- **Check UE liveness with `ip -4 -o addr show up dev oaitun_ue1`**, not
  `pgrep`; a released UE keeps its IPv4 address while its downlink is down.
- **Prime the uplink once after attachment** (`ping -I oaitun_ue1 <ext-dn>`);
  otherwise the downlink can read 0.
- **Measure goodput at the UE** from the application payload counters (see
  [`tools/liveconsole/README-flow-goodput.md`](../../tools/liveconsole/README-flow-goodput.md))
  rather than from iperf3's receiver report.
- **Use `pkill -x iperf3`**, not `pkill -f iperf3`, inside an ssh command, and
  start iperf3 servers with `setsid`.
- **Let freshly powered USRPs warm up** before relying on SSB/SIB1 decoding;
  PBCH decoding and timing offset stabilize once the oscillator settles.
- Configuration in `configs/oai/` is not deployed automatically; the restart
  scripts stage it.
