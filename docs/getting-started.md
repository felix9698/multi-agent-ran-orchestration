# Getting started

## 1. Install

Use Ubuntu 22.04 LTS with Python 3.12. Install the distribution packages providing
Tk and venv for that interpreter if you need the graphical console. From the
repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py --help
```

The core installation is enough for offline experiments. Model SDKs and radio
stacks are separate; [Models](models.md) describes the former.

## 2. Exercise the implementation without hardware

```bash
python scripts/test_public_artifact.py
OUT="$(mktemp -d /tmp/ran-quickcheck.XXXXXX)"
python tools/campaign5/run_agent_experiments.py \
  --scenario three-ue --condition contention-boundary \
  --methods three-agent --model mock:agent \
  --repetitions 1 --budget 2 --out "$OUT"
```

Inspect `manifest.json`, `metrics.json`, and the generated episode records.
These runs use a mock model and an emulated RAN with a small trial budget; they
demonstrate the execution and evaluation path. The OTA settings of the paper are
listed in [Experiments](experiments.md).

## 3. Open the Cockpit

```bash
python main.py
```

The console opens disconnected. The principal workspaces are:

| Workspace | Use |
|---|---|
| Main | Model choices, natural-language intent entry, active intents and actions |
| Live Operations | Deployment binding, preflight and session connection |
| Contract Studio | Requirements, permitted adjustments and experimental contracts |
| Trial & Safety | Current trial, assurance status, stop and recovery controls |
| Evidence Ledger | Recorded observations and their assessment |
| Batch Experiments | Matched experiment configuration and execution |
| Objective Registry | Available objective/control profiles and capabilities |
| Intent & Decision | Agent proposals and target/control exploration |
| Analysis & Results | Stored runs, charts and export |
| Demo View | Presentation-oriented view of the selected session |
| Settings & Integration | Profiles, model integration and display settings |

Loading a profile is not permission to start radio equipment. Missing live data is
not filled with synthetic values. A replay must be selected explicitly and keeps
its replay provenance.

For a compatible stored recording:

```bash
python main.py --replay /path/to/recording --no-gui
```

Use `python main.py --help` for supported export and replay options. An arbitrary
CSV is not interchangeable with a recorded session; the adapter validates its
input layout.

## 4. Connect your own prepared testbed

New OTA experiments require OAI gNB/NR-UE/CN5G, the matching FlexRIC/E2 integration,
the required xApps and policy services, and readback/measurement paths. The offline
quickstart starts none of them. Lab images, credentials, and radio-specific
calibration are deployment-specific and are not part of the repository.

Provide a live profile with the schema consumed by
[`tools/liveconsole/profile.py`](../tools/liveconsole/profile.py):

- GUI settings and a run-output directory;
- integration values and capability manifest;
- `liveConsole.assuranceBindingPath` and `producerDatabasePath`;
- runtime UE-to-host identity mapping and measurement sources;
- action producer endpoints and credential **references**, not secret values.

Paths in a profile resolve relative to that profile. Use your actual endpoint,
capability and binding documents with matching digests. Do not turn a placeholder
or modified example into a live binding by disabling verification.

In the GUI, load the profile, bind the deployment, run preflight, select Live,
then start the session. The equivalent composition entry is:

```bash
python main.py --live --profile /path/to/your/live-profile.json
```

The separate lab utility is available through `python bin/labctl --help`.
Its shipped inventory is an example profile; set the hosts, commands, radio
profile (38 PRBs in the reported experiments), and binary and configuration pins
of your testbed before executing it. The campaign bring-up scripts in
[`scripts/hardware/`](../scripts/hardware/README.md) likewise read
deployment-specific settings.

Operate radios only in an authorized laboratory setup. Review the target hosts
and every start/stop action before using the preparation utility.
