# Reproducibility

This repository accompanies **Automating Multi-Intent RAN Orchestration through
Multi-Agent Live Resolution**. It provides the agent coordinator, the
deterministic assurance and execution layers, the O-RAN control integration,
the experiment and analysis tooling, and the [manuscript figures](../assets/figures/README.md).

## Hardware-free example

The example below exercises the shared agent sitting, Assurance Kernel, Write
Gateway interfaces, evaluator, and result writer with an emulated RAN and a mock
model. It needs no radio equipment or model API credentials. Its measurements,
model responses, latency, and token counts are simulated, so it demonstrates
the implementation rather than reproducing the OTA measurements in the paper.

See [Experiment methods and artifacts](experiments.md) for the mapping between
paper methods and code identifiers.

### Environment

Use Python 3.12 and the repository's [requirements](../requirements.txt).
Run these commands from the repository root:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Dependency installation may need network access; the mock example itself does
not. The schema libraries validate the vendored O-RAN contracts locally, and
Matplotlib renders the demonstration figures with a noninteractive backend.
Optional model SDKs are not needed.

### Quickcheck

Start in a fresh shell without inherited `AIC_*` campaign overrides, and use a
new, short output path so records are not mixed and generated credential
references stay within the profile validator's 128-character limit:

```sh
QUICKCHECK_OUT="$(mktemp -d /tmp/ran-quickcheck.XXXXXX)"
PYTHONDONTWRITEBYTECODE=1 MPLBACKEND=Agg python tools/campaign5/run_agent_experiments.py \
  --scenario three-ue --condition contention-boundary \
  --methods three-agent --model mock:agent \
  --repetitions 1 --budget 2 --seed 0 --out "$QUICKCHECK_OUT"
```

The command uses `mock:agent` and omits `--live`. Real model names contact a
model service even when the radio is emulated. The two-trial budget keeps the
check short; the paper's setting is six additional trials within 480 s. An
unresolved episode is a valid recorded outcome.

Inspect the generated `manifest.json`, `episodes/`, `runtime/`, `metrics.json`,
and `figures/`. The manifest records the scenarios, settings, model choices, and
episode IDs of the invocation. Recompute the metrics from the saved episodes:

```sh
python -m experiments.agent_metrics "$QUICKCHECK_OUT/episodes" \
  --out "$QUICKCHECK_OUT/recomputed-metrics.json"
```

Matching the seed does not make timestamps or episode identifiers byte-identical.

### Focused offline tests

The following selection covers the rule-based baseline, prompt views, the
weighted preference evaluator, event replay, and saved-episode metrics without
calling a real model:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest \
  tests.assurance.test_rule_greedy \
  tests.assurance.test_v52_view \
  tests.assurance.test_v53_view \
  tests.assurance.test_v53_fair_prompts \
  tests.assurance.test_v51_weighted_evaluator.Weighted \
  tests.assurance.test_kern_replay \
  tests.test_agent_metrics
```

`python scripts/test_public_artifact.py` runs the self-contained offline suite.
A few broader tests, such as `tests/test_v5_design.py`, read pilot corpus files
from the original campaign environment and are not part of that suite.

## OTA experiments

The reported results were obtained on the two-gNB, three-UE 5G SA testbed
described in Section III-C of the paper (band n78, 30-kHz SCS, 38 PRBs,
15-MHz channels, OAI gNB/NR-UE and CN5G, FlexRIC). The campaign comprises
17 blocks and 56 recorded episodes: 17 for 3A, 20 for RM, and 19 for MA. Each
episode starts from a measured reference (trial 0) and allows at most six
additional live trials within 480 s, with a 15-s KPI window per trial.

The procedure is retained under
[`experiment_results/ota-20260911/`](../experiment_results/ota-20260911/) and
described in [experiments.md](experiments.md). Running it requires a prepared
deployment of the same kind: OAI gNB/UE and core, the RIC and xApps,
measurement sources, validated live profiles, model serving for model-based
methods, and authorized access to the radio equipment.

## Data availability and limits

The repository contains the experiment and analysis code. The raw campaign
records are not included; the analysis tools read the following inputs:

- the campaign plan (`ops/overnight/<campaign>.plan.json`), progress records,
  and per-attempt logs;
- per-block reference measurements and the campaign environment settings;
- the input corpus (`pilot38-v4.5-L10/`) and generated block corpora with hashes;
- `formal38guarded-*/live-sitting.stdout` and `evidence/AGENT-*-episode.json`;
- the corresponding event streams, stored prompts, and raw goodput and echo
  measurements.

Test fixtures in this repository are synthetic or redacted inputs for the test
suite and are not part of the campaign dataset. The figure PDFs in
[`assets/figures/`](../assets/figures/) are the manuscript figures.

Do not run live campaign, reference-measurement, maintenance, or preparation
scripts as part of the quickcheck. OTA execution requires a separately prepared
deployment, authorized equipment access, live profiles, and operator
supervision.
