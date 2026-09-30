# scripts/hardware — the hardware bring-up lane (separate from the GUI)

This is the **hardware lane**: bringing the near-RT RIC, the gNBs, the UEs and
the xApp control path up so intents can run *over the air*.  It is deliberately
separate from the experiment lane — the GUI and the hardware-free experiments in
[`docs/user/09-hardware-free-experiments.md`](../../docs/user/09-hardware-free-experiments.md)
never call anything here, and nothing here is needed to run a paper experiment
hardware-free.

Two tools do hardware preparation, for two purposes:

| Tool | Purpose |
|---|---|
| `bin/labctl` (see [`tools/labctl/README.md`](../../tools/labctl/README.md)) | the **supported** Lab Setup Utility: inventory-driven status / preflight / prepare / apply-config / stop / receipt for the PIN_TO_CELL 24-PRB dual-cell profile. Use this first. |
| `scripts/hardware/*.sh` (this directory) | the **campaign** bring-up used for the live xApp / E2SM-RC over-the-air runs (38-PRB dual-cell, `our_rc_xapp` Style-2 controls, FlexRIC handover). These encode the procedure that was verified over the air; they are thin, parameterised wrappers, not a substitute for labctl's receipt. |

> **Nothing here belongs to the GUI.**  The Cockpit imports no transport,
> actuator or lab tool; the boundary is enforced by
> `tests/gui/test_cockpit_acceptance.py`.  Run these on the lab hosts, by the
> operator, never from the console.

## Environment

These scripts read every machine-specific path from environment variables so no
absolute path or secret is baked into the repository.  Copy and edit:

```bash
cp scripts/hardware/env.sh.example scripts/hardware/env.sh
$EDITOR scripts/hardware/env.sh        # set RIC/OAI/xapp paths for your lab
source scripts/hardware/env.sh
```

No SIM key (Ki/OPc), IMSI, subscriber value or SSH password ever goes in these
files or in `env.sh`.  `env.sh` is git-ignored.

## Safety and privilege

- **PC1 (gNB1) needs local `sudo`** for the softmodem's real-time threads; the
  operator runs `restart_gnb.sh` there.  The near-RT RIC comes up **non-root**.
- USRP power is the operator's manual step.  A gNB restart waits ~15 s for the
  X310 to release before re-acquiring it.
- DL load in a run must originate **inside** the `oai-ext-dn` container; a
  host-originated ping to a UE IP leaks to the internet.

## Runbook (working live order)

```bash
source scripts/hardware/env.sh

# 1) Bring the CN up with the approved local Lab Setup procedure and confirm
#    its user plane before attaching any radio node.  Do not treat this as
#    Kernel evidence.
bin/labctl status

# 2) near-RT RIC (non-root), with the connection witness enabled
bash scripts/hardware/bringup_ric.sh

# 3) gNBs: gNB1 is an operator action on PC1; gNB2 is an operator sudo action
#    on enb2.  Complete *all* gNB/gate changes before the single re-pin below.
sudo -E bash scripts/hardware/restart_gnb.sh gnb1
ssh -t enb2 'sudo -E bash scripts/hardware/restart_gnb.sh gnb2'

# 4) The UEs immediately before the run.  The wrapper selects the 24-PRB B206mini
#    launcher, stops an old UE, shows its tun IP, and reports the UL prime.
bash scripts/hardware/attach_ue.sh ue1 24
#    Or, with the start flags the radio actually needs (see "UE start flags"):
#    ssh ue<n> 'sudo env UE_RTPRIO=99 UE_FRAMES=2048 bash ~/ue-start-rt.sh gnb1'
#    Then prove the downlink before trusting the address:
#    docker exec oai-ext-dn ping -c3 <tun-ip>

# 5) Read-only readiness receipt.
bash scripts/hardware/readiness.sh --output /tmp/oran-live-readiness.json

# 6) One staged re-pin after every gNB/gate change.  It has five discovery and
#    validation steps, then rApp capability/inventory propagation and KPM JSONL
#    rotation; inspect the no-write plan first.
bash scripts/hardware/repin_a1p.sh --dry-run
bash scripts/hardware/repin_a1p.sh

# 7) Start the Campaign-5 supplementary-action producer in a separate,
#    supervised terminal. Its preflight exits 4 if the KPM gate is not
#    appending, and it stays in the foreground after a successful preflight.
#    Configure the HW_CAMPAIGN5_* file references in env.sh first; never put
#    their secret contents in env.sh.
bash scripts/hardware/run_campaign5_producer.sh

# 8) A fresh receipt must say READY before starting the Cockpit.
bash scripts/hardware/readiness.sh --output /tmp/oran-live-readiness.json
python3 main.py --live --profile deployment/liveconsole-profile.json
# Or headless:
python3 main.py --live --profile deployment/liveconsole-profile.json --no-gui
```

`HW_LIVE_ARTIFACT_ROOT` is the operator-facing root for the live capability,
inventory, KPM JSONL, and release artifacts. Its default remains
`$HOME/oran-deploy/session-20260819/lower-live`; set the current name in new
`env.sh` files.

`readiness.sh` is read-only and reports why an observation is missing.  The
re-pin writes staged files with backups and publishes last; it is Lab Setup,
never Kernel evidence.  The Cockpit profile is the 24-PRB dual-cell profile.
`run_campaign5_producer.sh` is deliberately fail-closed: exit 4 means its KPM
JSONL did not append during the probe window, so it did not start a writer.

## The three UE machines

| host alias | machine | USRP (B206mini) | slice |
|---|---|---|---|
| `ue1` | ran-ue1 | 35DA62D | sst 1 / sd FFFFFF |
| `ue2` | ran-ue2 | 35D5F42 | sst 1 / sd FFFFFF |
| `ue3` | ran-ue3 (192.168.0.54, added 2026-09-08) | 352F0C1 | sst 222 / sd 00007B |

UE3 was provisioned by copying UE1's software: UHD 4.9.0.0 in `/opt`, the OAI UE binary and its
libraries, the runtime profile, the seven `ai-ran-*` launchers (paths and serial rewritten for this
host), Ettus' udev rule, and an `ld.so.conf.d` entry so `libuhd` resolves without `LD_LIBRARY_PATH`.
Its subscriber reuses an identity the core already carries with both credentials and session data, so
no core database change was needed; **the IMSI and its keys live in the lab's own configuration and in
`~/ai-ran-stage/runtime/phase-b/nr-ue.conf` on that machine, never in this repository.**

UE3's slice is deliberately different from UE1's and UE2's: with two groups, the
`slicePrbQuota@<sst>` axis is a real group control (sst 1 moves UE1 and UE2 together) rather than a
duplicate of the per-UE cap.  `HW_UE3_HOST` is registered in `env.sh`; the live KPI observer maps UEs
to hosts positionally, so three UEs resolve without a code change.  `HW_REQUIRED_UES` still names
`ue1,ue2` -- add `ue3` once it has actually attached on the radio, which needs the USRPs back.

## UE start flags (paid for on 2026-09-08)

The softmodem needs real-time priority and nothing else added:

| flag | verdict |
|---|---|
| `chrt -f 99` | **required** — without it the UE dies of USB overflow within minutes |
| `taskset -c 4-11` | **never** — eight cores starve the transmit thread, the log fills with `L` (late), the PRACH leaves its occasion and the gNB never sees it. The symptom is a perfect downlink with random access that never succeeds, which looks exactly like a dead antenna |
| `--agc` | **never** — the stock launchers do not pass it; receive gain climbs and the frequency correction oscillates |
| `UE_FRAMES=2048` | recommended — 512 receive/transmit frames is not enough to survive an RRC drop |

After an `ERROR_CODE_OVERFLOW` crash the B2xx needs one `uhd_usrp_probe` before the next start; a USB
unbind/rebind does not restore it. `~/ue-start-rt.sh` now does this automatically and takes
`UE_RTPRIO`, `UE_CPUS`, `UE_AGC`, `UE_NOFO`, `UE_FRAMES`, `UE_CARRIER`, `UE_SSB` and an optional
second argument naming the profile.

## Known traps

| Situation | Discriminating symptom and operator rule |
|---|---|
| KPM gate removal | `docker rm -f` can SIGKILL the gate and leave the RIC at exit 139.  After a RIC restart, neither agent re-sends E2 SETUP; restart both gNBs. |
| gNB2 restart | A non-sudo `pkill` cannot stop enb2's root softmodem.  A new PID that immediately reports a busy USRP or `socket closed` is not a restart; use sudo, verify the PID changed, and wait at least 15 seconds. |
| SCTP association churn | Shutting down one association can drop the other (`SCTP_SHUTDOWN_EVENT` / SETUP timeout).  Make every gNB and gate change first, then re-pin once. |
| UE1 at 24 PRB | The generic launcher returns `UNSUPPORTED_PRB=24`; use `ai-ran-nrue-start-gnb1-prb24`.  `ERROR_CODE_OVERFLOW` / `readBlockSize == tmp` means USB overflow: reattach immediately before the run.  Prove DL only with `docker exec oai-ext-dn ping <tun-ip>`. |
| Campaign capability | Campaign-5 gNB capability changes require re-pin step 4 refresh.  Never edit release inputs inside a bundle; restore pinned content before `finalize`. |
| Docker timestamps | `docker logs --since` with a bare time is local time.  Pass a RFC3339 timestamp ending in `Z`. |
| Header refresh | Never run `refresh_headers.sh` without `--from-live` while the gate container is up: a second KPM gate xApp is churn that has crashed the RIC (exit 139) and dropped both E2 associations. Add `--confirm-hosts` to join each header to its physical host through the AMF's UE table. |
| iperf3 hangs on 5201 | A leftover TCP sink (`/tmp/srv.py`) from an earlier session holds 5201 on the UE hosts. It accepts the connection and speaks nothing, so an iperf3 client hangs until its timeout. Use port 5202. |
| Sitting refuses every trial | `prepare: REJECTED_CONFIG_MISMATCH` means a manual probe left an axis off its baseline. Restore every axis before starting a sitting. |
| Sitting measures nothing | A live sitting started without `source scripts/hardware/env.sh` has no `HW_UE<n>_HOST`, so the tun-rate observer resolves no hosts and every KPI reads UNKNOWN while the run still completes. |
| A1-P recovery wedge | Cockpit shows `SAFETY_STOPPED`, R1 returns 503, and the producer log says `xApp worker stopped`.  Restart with `docker restart oran-aic-a1p-producer`, then re-run readiness.  The root cause is in the carried-over stage bundle, not this repository. |

## Field gotchas (paid for the hard way)

- **UE liveness is `ip -4 -o addr show up dev oaitun_ue1`, not `pgrep`** — a
  released UE keeps its IPv4 address while its DL is dead.
- **After UE attach, prime the uplink once** (`ping -I oaitun_ue1 <ext-dn>`) or
  DL reads 0.
- **Measure throughput from the UE tun `rx_bytes` delta**, not iperf3's receiver
  report (it depends on the control channel and often reads 0).
- **Never `pkill -f iperf3` inside an ssh command** — it matches and kills the
  ssh shell itself; use `pkill -x iperf3`.  Start iperf3 servers with `setsid`.
- **Freshly power-cycled USRPs need to warm up** before SSB/SIB1 decode is
  reliable (PBCH decode fails and the timing offset is large until the TCXO
  settles); do **not** ice them.
- Config in `configs/oai/` is not auto-deployed; the restart scripts stage it.
