# labctl — testbed preparation utility

`labctl` is an Ubuntu operations tool, separate from the Cockpit, for checking,
starting in order, and stopping the 5G core, near-RT RIC, gNBs, and UEs of the
testbed. Neither the Cockpit nor the rApp calls this tool.

## Inventory

The hosts and components are defined by a JSON inventory, so the testbed can be
described without changing Python code. The repository ships an example
profile, [`profiles/pc1-dual-cell-prb24.json`](profiles/pc1-dual-cell-prb24.json):

| Host | Transport | Role |
|---|---|---|
| `pc1` | local | 5G core, near-RT RIC, gNB1 |
| `gnb2` | SSH | gNB2 |
| `ue1`, `ue2`, `ue3` | SSH | UE1-UE3 |

Copy the profile and set the SSH targets, start and stop commands, radio
profile, and binary and configuration pins of your own testbed before running
it. The reported experiments use 38-PRB, 15-MHz cells; the 38-PRB campaign
bring-up commands are in [`scripts/hardware/`](../../scripts/hardware/README.md).

## One-time setup

- Public-key SSH must work from PC1 to every remote host in the inventory.
- A remote `sudo -n` helper must be installed and approved in advance.
- Do not put SSH passwords, tokens, or private keys in this repository or in a
  profile.
- Run every command from the repository root or through `bin/labctl`.

## Preparation sequence

1. Check power, cables, antennas, and attenuators on all equipment.
2. With the gNB USRPs still powered off, run the read-only checks:

   ```bash
   bin/labctl status
   bin/labctl preflight
   ```

3. Review the planned commands. This does not execute any start or stop
   command:

   ```bash
   bin/labctl prepare
   ```

4. Power on the gNB USRPs and confirm that the RF connections are safe.
5. Stage and validate the static configuration from the repository, then run
   the preparation with an explicit confirmation:

   ```bash
   bin/labctl apply-config --execute --yes
   bin/labctl prepare --execute --yes
   ```

6. Confirm that the JSON result is `COMPLETED`, then proceed to the Cockpit's
   Live binding and preflight. `labctl` does not configure the Cockpit's live
   profile or intents.

## Shutdown

By default, shutdown stops only the components that `labctl` started in the
most recent successful run, in reverse order, and keeps the 5G core running:

```bash
bin/labctl stop
bin/labctl stop --execute --yes
```

Stopping the 5G core is a separate, explicit choice:

```bash
bin/labctl stop --execute --yes --include-core
```

`SIGKILL` is never used automatically. If a graceful stop does not complete
within its time limit, the tool stops and records that manual action is needed.

## Run records

Each run writes mode-0600 logs and an atomically written `run.json` under the
profile's `stateRoot/<run-id>/`. Show the latest location with:

```bash
bin/labctl logs
```

Processes that were already running are recorded as `PREEXISTING` and are not
stopped by default shutdown or failure rollback. If a run fails midway, only the
non-core components started by that run are stopped, in reverse order.

## Readiness receipt

`labctl receipt --output <path>` writes a secret-free, unsigned readiness
receipt. Tests inject probes; a real status probe requires the explicit
`--live-probes` option. The receipt describes equipment readiness only; it is
not trial evidence, and the Cockpit only displays it.

USRP and network observations can become `READY` only through live probes.
Without them, the hardware-free default reports `UNKNOWN` rather than inferring
readiness from environment variables.

`cleanup` and `recover` both return only the components owned by the latest run
to their stopped state. Recovery does not restart RF equipment; the operator
reviews the report and starts a new preparation.

## Additional components

Further endpoints, such as A1-P or O1 services, can be added to an inventory
overlay as probe-only components once their endpoints and `secretRef` values
are defined for the deployment. Each additional UE needs an SSH host entry, a
UE component, and its dependency and helper paths. No password or key material
belongs in any overlay.
